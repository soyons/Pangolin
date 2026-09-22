"""Account-scoped durable state. Call from the event loop; password work runs separately."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
import uuid

from .models import PAYLOADS


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def public_user(row):
    return {'id': row['id'], 'email': row['email'], 'email_verified': False}


class Store:
    def __init__(self, path):
        path = Path(path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=5)
        self.db.row_factory = sqlite3.Row
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version > 1:
            raise RuntimeError('Database is newer than this server; restore the matching server version')
        if version < 1 and self.db.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]:
            self.backup(str(path) + '.backup-' + str(time.time_ns()))
        self.db.executescript('''
          PRAGMA foreign_keys=ON;
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
            created_at REAL NOT NULL, revision INTEGER NOT NULL DEFAULT 0
          );
          CREATE TABLE IF NOT EXISTS logins (
            token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
            csrf TEXT NOT NULL, expires_at REAL NOT NULL
          );
          CREATE TABLE IF NOT EXISTS devices (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), name TEXT NOT NULL,
            token_hash TEXT NOT NULL, expires_at REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0,
            last_seen REAL, projects TEXT NOT NULL DEFAULT '[]', stream TEXT, cursor INTEGER NOT NULL DEFAULT 0
          );
          CREATE INDEX IF NOT EXISTS devices_owner ON devices(user_id);
          CREATE TABLE IF NOT EXISTS conversations (
            device_id TEXT NOT NULL REFERENCES devices(id), id TEXT NOT NULL,
            project TEXT NOT NULL DEFAULT '', agent TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'running', updated_at REAL NOT NULL,
            terminal TEXT NOT NULL DEFAULT '', observed_at REAL,
            deleted INTEGER NOT NULL DEFAULT 0, delete_acked INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(device_id, id)
          );
          CREATE TABLE IF NOT EXISTS events (
            device_id TEXT NOT NULL, session TEXT NOT NULL, event_id INTEGER NOT NULL,
            source TEXT NOT NULL, text TEXT NOT NULL, created_at REAL NOT NULL,
            status TEXT NOT NULL, revision INTEGER NOT NULL,
            PRIMARY KEY(device_id, session, event_id),
            FOREIGN KEY(device_id, session) REFERENCES conversations(device_id, id)
          );
          CREATE INDEX IF NOT EXISTS event_cursor ON events(device_id, session, revision);
          PRAGMA user_version=1;
        ''')

    def close(self):
        self.db.close()

    def backup(self, target):
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with sqlite3.connect(target) as destination:
            self.db.backup(destination)

    def user(self, email):
        return self.db.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()

    def create_user(self, email, password_hash):
        uid = uuid.uuid4().hex
        with self.db:
            self.db.execute('INSERT INTO users(id,email,password_hash,created_at) VALUES (?,?,?,?)',
                            (uid, email, password_hash, time.time()))
        return self.user(email)

    def login(self, uid):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.db:
            self.db.execute('DELETE FROM logins WHERE expires_at <= ?', (time.time(),))
            self.db.execute('INSERT INTO logins VALUES (?,?,?,?)', (digest(token), uid, csrf, time.time() + 30 * 86400))
        return token, csrf

    def authenticate(self, token):
        return self.db.execute('''SELECT users.*, logins.csrf FROM logins JOIN users ON users.id=logins.user_id
            WHERE token_hash=? AND expires_at>?''', (digest(token), time.time())).fetchone()

    def logout(self, token):
        with self.db:
            self.db.execute('DELETE FROM logins WHERE token_hash=?', (digest(token),))

    def change_password(self, uid, encoded):
        with self.db:
            self.db.execute('UPDATE users SET password_hash=?,revision=revision+1 WHERE id=?', (encoded, uid))
            self.db.execute('DELETE FROM logins WHERE user_id=?', (uid,))
            self.db.execute('UPDATE devices SET revoked=1 WHERE user_id=?', (uid,))

    def touch(self, uid):
        self.db.execute('UPDATE users SET revision=revision+1 WHERE id=?', (uid,))
        return self.db.execute('SELECT revision FROM users WHERE id=?', (uid,)).fetchone()[0]

    def bind(self, uid, name, device_id=None):
        if device_id:
            device = self.device(uid, device_id)
            if not device:
                raise ValueError('设备不属于此账号；新设备请省略 device_id')
        else:
            if self.db.execute('SELECT count(*) FROM devices WHERE user_id=?', (uid,)).fetchone()[0] >= 100:
                raise ValueError('每个账号最多绑定 100 台设备')
            device_id = 'dev-' + uuid.uuid4().hex
        token = secrets.token_urlsafe(32)
        with self.db:
            self.db.execute('''INSERT INTO devices(id,user_id,name,token_hash,expires_at) VALUES (?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,token_hash=excluded.token_hash,
                expires_at=excluded.expires_at,revoked=0''', (device_id, uid, name, digest(token), time.time() + 90 * 86400))
            self.touch(uid)
        return {'device': device_id, 'device_token': token, 'expires_at': time.time() + 90 * 86400}

    def device(self, uid, device):
        return self.db.execute('SELECT * FROM devices WHERE id=? AND user_id=?', (device, uid)).fetchone()

    def device_auth(self, device, token):
        return self.db.execute('''SELECT * FROM devices WHERE id=? AND token_hash=? AND revoked=0 AND expires_at>?''',
                               (device, digest(token), time.time())).fetchone()

    def refresh_device(self, device):
        # Extending the validity of the same scoped credential is retry-safe even if the response is lost.
        with self.db:
            self.db.execute('UPDATE devices SET expires_at=? WHERE id=?', (time.time() + 90 * 86400, device))

    def revoke(self, uid, device):
        with self.db:
            self.db.execute('UPDATE devices SET revoked=1 WHERE id=? AND user_id=?', (device, uid))
            self.touch(uid)

    def presence(self, uid, device):
        with self.db:
            self.db.execute('UPDATE devices SET last_seen=? WHERE id=?', (time.time(), device))
            self.touch(uid)

    def devices(self, uid):
        return [dict(row) for row in self.db.execute('''SELECT id,name,last_seen,revoked,expires_at FROM devices
                                                       WHERE user_id=? ORDER BY name,id''', (uid,))]

    def conversation(self, device, session):
        return self.db.execute('SELECT * FROM conversations WHERE device_id=? AND id=? AND deleted=0', (device, session)).fetchone()

    def sessions(self, device):
        return [dict(row) for row in self.db.execute('''SELECT id,project,agent,status,updated_at FROM conversations
            WHERE device_id=? AND deleted=0 ORDER BY updated_at DESC,id''', (device,))]

    def save_session(self, device, session, data):
        self.db.execute('''INSERT INTO conversations(device_id,id,project,agent,status,updated_at) VALUES(?,?,?,?,?,?)
          ON CONFLICT(device_id,id) DO UPDATE SET project=excluded.project,agent=excluded.agent,
          status=excluded.status,updated_at=excluded.updated_at WHERE conversations.deleted=0''',
                        (device, session, data['project'], data['agent'], data['status'], time.time()))

    def created(self, uid, device, session, data):
        with self.db:
            self.save_session(device, session, {**data, 'status': 'running'})
            self.touch(uid)

    def delete(self, uid, device, session, acknowledged=False):
        with self.db:
            self.db.execute('''UPDATE conversations SET deleted=1,terminal='',observed_at=NULL,
                delete_acked=?,status='stopped' WHERE device_id=? AND id=?''', (int(acknowledged), device, session))
            self.db.execute('DELETE FROM events WHERE device_id=? AND session=?', (device, session))
            self.touch(uid)

    def ingest(self, device, batch):
        # Validate the entire batch BEFORE starting the transaction. A bad batch cannot partly advance the cursor.
        entries = [(entry, PAYLOADS[entry.kind].model_validate(entry.payload).model_dump()) for entry in batch.entries]
        current = self.db.execute('SELECT * FROM devices WHERE id=?', (device,)).fetchone()
        if current['stream'] and current['stream'] != batch.stream:
            raise ValueError('本地同步数据库已改变，请以新设备重新绑定')
        cursor = current['cursor']
        uid = current['user_id']
        with self.db:
            for entry, data in entries:
                if entry.seq <= cursor:
                    continue
                if entry.seq != cursor + 1:
                    raise ValueError('同步序号不连续')
                cursor = entry.seq
                if entry.kind == 'projects':
                    if entry.session:
                        raise ValueError('Invalid project event')
                    self.db.execute('UPDATE devices SET projects=? WHERE id=?', (json.dumps(data['projects']), device))
                    self.touch(uid)
                    continue
                if not entry.session:
                    raise ValueError('Missing session')
                row = self.db.execute('SELECT * FROM conversations WHERE device_id=? AND id=?', (device, entry.session)).fetchone()
                if row and row['deleted']:
                    if entry.kind == 'deleted':
                        self.db.execute('UPDATE conversations SET delete_acked=1 WHERE device_id=? AND id=?', (device, entry.session))
                    continue
                if entry.kind == 'session':
                    if not row and self.db.execute('SELECT count(*) FROM conversations WHERE device_id=? AND deleted=0', (device,)).fetchone()[0] >= 1000:
                        raise ValueError('会话数量达到上限，请删除旧对话')
                    self.save_session(device, entry.session, data)
                    self.touch(uid)
                elif not row and entry.kind == 'deleted':
                    self.db.execute('''INSERT INTO conversations(device_id,id,status,updated_at,deleted,delete_acked)
                                     VALUES(?,?,'stopped',?,1,1)''', (device, entry.session, time.time()))
                elif not row:
                    raise ValueError('同步消息之前必须同步会话')
                elif entry.kind == 'event':
                    revision = self.touch(uid)
                    self.db.execute('''INSERT INTO events VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(device_id,session,event_id)
                      DO UPDATE SET source=excluded.source,text=excluded.text,created_at=excluded.created_at,
                      status=excluded.status,revision=excluded.revision''', (device, entry.session, data['id'], data['source'],
                        data['text'], data['created_at'], data['status'], revision))
                    self.db.execute('UPDATE conversations SET updated_at=? WHERE device_id=? AND id=?', (time.time(), device, entry.session))
                elif entry.kind == 'terminal':
                    self.db.execute('''UPDATE conversations SET terminal=?,observed_at=?,updated_at=? WHERE device_id=? AND id=?''',
                                    (data['text'], data['observed_at'], time.time(), device, entry.session))
                    self.touch(uid)
                elif entry.kind == 'deleted':
                    self.db.execute('UPDATE conversations SET deleted=1,delete_acked=1,terminal=?,status=? WHERE device_id=? AND id=?',
                                    ('', 'stopped', device, entry.session))
                    self.db.execute('DELETE FROM events WHERE device_id=? AND session=?', (device, entry.session))
                    self.touch(uid)
            self.db.execute('UPDATE devices SET stream=?,cursor=?,last_seen=? WHERE id=?', (batch.stream, cursor, time.time(), device))
        deleted = [row[0] for row in self.db.execute('''SELECT id FROM conversations WHERE device_id=? AND deleted=1
                                                     AND delete_acked=0 LIMIT 100''', (device,))]
        return {'type': 'sync.ack', 'stream': batch.stream, 'cursor': cursor, 'deleted': deleted}

    def state(self, device, session, after):
        row = self.conversation(device, session)
        if not row:
            return None
        events, size, cursor = [], 0, after
        for event in self.db.execute('''SELECT event_id AS id,source,text,created_at,status,revision FROM events
            WHERE device_id=? AND session=? AND revision>? ORDER BY revision LIMIT 101''', (device, session, after)):
            value = dict(event)
            length = len(json.dumps(value, ensure_ascii=False).encode())
            if events and (size + length > 66000 or len(events) == 100):
                break
            cursor = value.pop('revision')
            events.append(value)
            size += length
        more = bool(self.db.execute('SELECT 1 FROM events WHERE device_id=? AND session=? AND revision>? LIMIT 1',
                                    (device, session, cursor)).fetchone())
        return {'session': {key: row[key] for key in ('id', 'project', 'agent', 'status')},
                'events': events, 'cursor': cursor, 'has_more': more,
                'terminal': {'source': 'terminal', 'text': row['terminal'], 'observed_at': row['observed_at']},
                'screen_id': None, 'interaction': None, 'live': False}

    def expire_history(self, days):
        rows = self.db.execute('''SELECT c.device_id,c.id,d.user_id FROM conversations c JOIN devices d ON d.id=c.device_id
            WHERE c.deleted=0 AND c.status='stopped' AND c.updated_at<?''', (time.time() - days * 86400,)).fetchall()
        for row in rows:
            self.delete(row['user_id'], row['device_id'], row['id'])
