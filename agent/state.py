"""Local session history and retry protection; never stores model credentials."""
import json
import os
from pathlib import Path
import sqlite3
import time


class SessionError(ValueError):
    """An error safe to show to the remote user."""


class SessionStore:
    def __init__(self, path=None):
        if path:
            path = Path(path).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            os.fchmod(fd, 0o600)
            os.close(fd)
        self.db = sqlite3.connect(str(path) if path else ':memory:', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, project TEXT NOT NULL, agent TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL,
                request_id TEXT NOT NULL, payload TEXT NOT NULL,
                source TEXT NOT NULL, text TEXT NOT NULL, created_at REAL NOT NULL,
                status TEXT NOT NULL, result TEXT,
                UNIQUE(session, request_id)
            );
        ''')

    def create(self, session, project, agent):
        with self.db:
            self.db.execute('INSERT INTO sessions VALUES (?, ?, ?)', (session, project, agent))

    def metadata(self, session):
        row = self.db.execute('SELECT * FROM sessions WHERE id = ?', (session,)).fetchone()
        return dict(row) if row else {'id': session}

    def forget(self, session):
        with self.db:
            self.db.execute('DELETE FROM events WHERE session = ?', (session,))
            self.db.execute('DELETE FROM sessions WHERE id = ?', (session,))

    def previous(self, session, request_id, payload):
        row = self.db.execute('SELECT * FROM events WHERE session = ? AND request_id = ?',
                              (session, request_id)).fetchone()
        if row is None:
            return None
        if row['payload'] != payload:
            raise SessionError('请求 ID 已使用，请刷新后重新操作。')
        if row['status'] != 'sent':
            raise SessionError('上次操作的执行状态不确定，请核对终端后再操作，避免重复发送。')
        return json.loads(row['result'])

    def begin(self, session, request_id, payload, source, text):
        with self.db:
            self.db.execute('''INSERT INTO events
                (session, request_id, payload, source, text, created_at, status)
                VALUES (?, ?, ?, ?, ?, ?, 'pending')''',
                (session, request_id, payload, source, text, time.time()))

    def finish(self, session, request_id, result=None):
        with self.db:
            self.db.execute('UPDATE events SET status = ?, result = ? WHERE session = ? AND request_id = ?',
                            ('sent' if result is not None else 'uncertain', json.dumps(result), session, request_id))

    def events(self, session, after=0):
        # Bound each response well below the relay's WebSocket frame limit.
        rows = self.db.execute('''SELECT id, source, text, created_at, status FROM events
            WHERE session = ? AND id > ? ORDER BY id LIMIT 100''', (session, after))
        events, size = [], 0
        for row in rows:
            item = dict(row)
            length = len(json.dumps(item, ensure_ascii=False).encode())
            if events and size + length > 66000:
                break
            events.append(item)
            size += length
        return events
