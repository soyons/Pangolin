import { DatabaseSync } from 'node:sqlite';
import { closeSync, fchmodSync, mkdirSync, openSync } from 'node:fs';
import { dirname } from 'node:path';
import { canonical, id, SessionError } from './utils.js';

export class SessionStore {
  constructor(path = ':memory:') {
    if (path !== ':memory:') {
      mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
      const fd = openSync(path, 'a', 0o600);
      fchmodSync(fd, 0o600); closeSync(fd);
    }
    this.path = path;
    this.db = new DatabaseSync(path);
    // Preserve the original Python schema; new state and the upload queue are separate tables.
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, project TEXT NOT NULL, agent TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL, request_id TEXT NOT NULL,
        payload TEXT NOT NULL, source TEXT NOT NULL, text TEXT NOT NULL, created_at REAL NOT NULL,
        status TEXT NOT NULL, result TEXT, UNIQUE(session, request_id)
      );
      CREATE TABLE IF NOT EXISTS local_states (session TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'running', snapshot TEXT);
      CREATE TABLE IF NOT EXISTS approval_guards (session TEXT PRIMARY KEY, screen_id TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS sync_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS outbox (seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, session TEXT NOT NULL, payload TEXT NOT NULL);
      CREATE TRIGGER IF NOT EXISTS sync_session_insert AFTER INSERT ON sessions
        WHEN EXISTS(SELECT 1 FROM sync_meta WHERE key='binding') BEGIN
          INSERT INTO outbox(kind,session,payload) VALUES('session',NEW.id,
            json_object('project',NEW.project,'agent',NEW.agent,'status','running'));
        END;
      CREATE TRIGGER IF NOT EXISTS sync_event_insert AFTER INSERT ON events
        WHEN EXISTS(SELECT 1 FROM sync_meta WHERE key='binding') BEGIN
          INSERT INTO outbox(kind,session,payload) VALUES('event',NEW.session,
            json_object('id',NEW.id,'source',NEW.source,'text',NEW.text,'created_at',NEW.created_at,'status',NEW.status));
        END;
      CREATE TRIGGER IF NOT EXISTS sync_event_update AFTER UPDATE OF status ON events
        WHEN EXISTS(SELECT 1 FROM sync_meta WHERE key='binding') BEGIN
          INSERT INTO outbox(kind,session,payload) VALUES('event',NEW.session,
            json_object('id',NEW.id,'source',NEW.source,'text',NEW.text,'created_at',NEW.created_at,'status',NEW.status));
        END;
    `);
    this.sync = false;
  }
  close() { this.db.close(); }
  meta(key) { return this.db.prepare('SELECT value FROM sync_meta WHERE key=?').get(key)?.value; }
  setMeta(key, value) { this.db.prepare('INSERT INTO sync_meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value').run(key, String(value)); }
  transaction(fn) {
    this.db.exec('BEGIN IMMEDIATE');
    try { const result = fn(); this.db.exec('COMMIT'); return result; }
    catch (error) { this.db.exec('ROLLBACK'); throw error; }
  }
  enqueue(kind, session, payload) {
    if (this.sync) this.db.prepare('INSERT INTO outbox(kind,session,payload) VALUES(?,?,?)').run(kind, session, JSON.stringify(payload));
  }
  enableSync(binding, importExisting = false) {
    const owner = canonical(binding), previous = this.meta('binding');
    if (previous && previous !== owner) throw Error('本地历史已绑定其他账号或设备，请使用独立配置目录。');
    const existing = this.db.prepare('SELECT count(*) AS n FROM sessions').get().n;
    if (!previous && existing && !importExisting) throw Error('上传旧会话前需执行 login --sync-existing，或使用新的 --prefix。');
    if (!previous && existing && this.path !== ':memory:') this.db.prepare('VACUUM INTO ?').run(this.path + '.backup-' + Date.now());
    this.sync = true;
    if (!previous) this.transaction(() => {
      this.setMeta('binding', owner); this.setMeta('stream', id()); this.setMeta('ack', '0');
      for (const session of this.all()) {
        this.enqueue('session', session.id, { project: session.project, agent: session.agent, status: session.status });
        for (const event of this.db.prepare('SELECT id,source,text,created_at,status FROM events WHERE session=? ORDER BY id').iterate(session.id)) {
          this.enqueue('event', session.id, event);
        }
        const snapshot = this.snapshot(session.id);
        if (snapshot) this.enqueue('terminal', session.id, { text: snapshot.text, observed_at: snapshot.observed_at });
      }
    });
    // A process could die after pasting input but before recording the result. Never replay it.
    this.db.prepare("UPDATE events SET status='uncertain' WHERE status='pending'").run();
  }
  batch() {
    const entries = []; let bytes = 0;
    for (const row of this.db.prepare('SELECT * FROM outbox ORDER BY seq LIMIT 32').iterate()) {
      const item = { ...row, payload: JSON.parse(row.payload) }, size = Buffer.byteLength(JSON.stringify(item));
      if (entries.length && bytes + size > 100000) break;
      entries.push(item); bytes += size;
    }
    return { type: 'sync', stream: this.meta('stream'), entries };
  }
  ack(cursor, sentThrough) {
    const previous = Number(this.meta('ack') || 0);
    if (!Number.isSafeInteger(cursor) || cursor < previous || cursor > sentThrough) throw Error('Invalid synchronization acknowledgement');
    this.transaction(() => {
      this.db.prepare('DELETE FROM outbox WHERE seq <= ?').run(cursor);
      this.setMeta('ack', cursor);
    });
  }
  create(session, project, agent) { this.db.prepare('INSERT INTO sessions VALUES (?, ?, ?)').run(session, project, agent); }
  metadata(session) {
    const record = this.db.prepare('SELECT * FROM sessions WHERE id = ?').get(session);
    if (!record) return { id: session };
    return this.sync ? { ...record, status: this.db.prepare('SELECT status FROM local_states WHERE session=?').get(session)?.status || 'running' } : { ...record };
  }
  all() {
    return this.db.prepare("SELECT s.*, COALESCE(l.status,'running') AS status FROM sessions s LEFT JOIN local_states l ON l.session=s.id").all().map(row => ({ ...row }));
  }
  setStatus(session, status) {
    if (this.metadata(session).status === status) return;
    this.transaction(() => {
      this.db.prepare('INSERT INTO local_states(session,status) VALUES(?,?) ON CONFLICT(session) DO UPDATE SET status=excluded.status').run(session, status);
      const record = this.metadata(session);
      if (record.project) this.enqueue('session', session, { project: record.project, agent: record.agent, status });
    });
  }
  snapshot(session) { const row = this.db.prepare('SELECT snapshot FROM local_states WHERE session=?').get(session); return row?.snapshot ? JSON.parse(row.snapshot) : null; }
  observeScreen(session, screen) { this.db.prepare('DELETE FROM approval_guards WHERE session=? AND screen_id<>?').run(session, screen); }
  consumeApproval(session, screen) {
    if (this.db.prepare('SELECT 1 FROM approval_guards WHERE session=? AND screen_id=?').get(session, screen)) {
      throw new SessionError('此提示已处理，请等待终端更新后再操作。');
    }
    this.db.prepare('INSERT INTO approval_guards VALUES(?,?) ON CONFLICT(session) DO UPDATE SET screen_id=excluded.screen_id').run(session, screen);
  }
  saveSnapshot(session, value) {
    const previous = this.snapshot(session);
    if (previous?.text === value.text && previous?.screen_id === value.screen_id) return;
    this.transaction(() => {
      this.db.prepare('INSERT INTO local_states(session,snapshot) VALUES(?,?) ON CONFLICT(session) DO UPDATE SET snapshot=excluded.snapshot').run(session, JSON.stringify(value));
      if (previous?.text !== value.text) this.enqueue('terminal', session, { text: value.text, observed_at: value.observed_at });
    });
  }
  saveProjects(projects) {
    const value = JSON.stringify(projects);
    if (this.meta('projects') !== value) this.transaction(() => { this.setMeta('projects', value); this.enqueue('projects', '', { projects }); });
  }
  forget(session) {
    this.transaction(() => {
      // Keep sequence numbers contiguous while removing the contents of not-yet-uploaded records.
      this.db.prepare("UPDATE outbox SET kind='deleted',payload='{}' WHERE session=?").run(session);
      this.db.prepare('DELETE FROM events WHERE session = ?').run(session);
      this.db.prepare('DELETE FROM sessions WHERE id = ?').run(session);
      this.db.prepare('DELETE FROM local_states WHERE session = ?').run(session);
      this.db.prepare('DELETE FROM approval_guards WHERE session = ?').run(session);
      this.enqueue('deleted', session, {});
    });
  }
  previous(session, request, payload) {
    const row = this.db.prepare('SELECT * FROM events WHERE session = ? AND request_id = ?').get(session, request);
    if (!row) return null;
    if (canonical(JSON.parse(row.payload)) !== canonical(JSON.parse(payload))) throw new SessionError('请求 ID 已使用，请刷新后重新操作。');
    if (row.status !== 'sent') throw new SessionError('上次操作的执行状态不确定，请核对终端后再操作，避免重复发送。');
    return JSON.parse(row.result);
  }
  begin(session, request, payload, source, text) {
    this.db.prepare(`INSERT INTO events (session, request_id, payload, source, text, created_at, status)
      VALUES (?, ?, ?, ?, ?, ?, 'pending')`).run(session, request, payload, source, text, Date.now() / 1000);
  }
  finish(session, request, result = null) {
    this.db.prepare('UPDATE events SET status = ?, result = ? WHERE session = ? AND request_id = ?')
      .run(result === null ? 'uncertain' : 'sent', JSON.stringify(result), session, request);
  }
  events(session, after = 0) {
    const rows = this.db.prepare(`SELECT id, source, text, created_at, status FROM events
      WHERE session = ? AND id > ? ORDER BY id LIMIT 100`).iterate(session, after);
    const events = []; let size = 0;
    for (const row of rows) {
      const length = Buffer.byteLength(JSON.stringify(row));
      if (events.length && size + length > 66000) break;
      events.push({ ...row }); size += length;
    }
    return events;
  }
}
