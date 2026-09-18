import { DatabaseSync } from 'node:sqlite';
import { closeSync, fchmodSync, mkdirSync, openSync } from 'node:fs';
import { dirname } from 'node:path';
import { canonical, SessionError } from './utils.js';

export class SessionStore {
  constructor(path = ':memory:') {
    if (path !== ':memory:') {
      mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
      const fd = openSync(path, 'a', 0o600);
      fchmodSync(fd, 0o600); closeSync(fd);
    }
    this.db = new DatabaseSync(path);
    // Deliberately shares the Python agent's schema for in-place upgrades.
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, project TEXT NOT NULL, agent TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL, request_id TEXT NOT NULL,
        payload TEXT NOT NULL, source TEXT NOT NULL, text TEXT NOT NULL, created_at REAL NOT NULL,
        status TEXT NOT NULL, result TEXT, UNIQUE(session, request_id)
      );
    `);
  }
  close() { this.db.close(); }
  create(session, project, agent) { this.db.prepare('INSERT INTO sessions VALUES (?, ?, ?)').run(session, project, agent); }
  metadata(session) { return { ...(this.db.prepare('SELECT * FROM sessions WHERE id = ?').get(session) || { id: session }) }; }
  forget(session) {
    this.db.exec('BEGIN');
    try {
      this.db.prepare('DELETE FROM events WHERE session = ?').run(session);
      this.db.prepare('DELETE FROM sessions WHERE id = ?').run(session);
      this.db.exec('COMMIT');
    } catch (error) { this.db.exec('ROLLBACK'); throw error; }
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
