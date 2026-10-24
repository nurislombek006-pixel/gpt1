"""Pure helpers and durable job storage; no Telegram credentials."""
import json
import re
import sqlite3
from urllib.parse import urlsplit


def message_link(text):
    text = text.strip()
    if not text.startswith(('https://', 'http://')):
        text = 'https://' + text
    url = urlsplit(text)
    if url.hostname not in ('t.me', 'telegram.me'):
        raise ValueError('Нужна ссылка вида https://t.me/канал/123')
    parts = [p for p in url.path.split('/') if p]
    if parts and parts[0] == 's':
        parts.pop(0)
    if len(parts) in (3, 4) and parts[0] == 'c':
        if not all(p.isdigit() and int(p) > 0 for p in parts[1:]):
            raise ValueError('Некорректная ссылка на сообщение')
        return int('-100' + parts[1]), int(parts[-1])
    if len(parts) in (2, 3) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{3,31}', parts[0]):
        if not all(p.isdigit() and int(p) > 0 for p in parts[1:]):
            raise ValueError('Некорректный номер сообщения')
        return parts[0], int(parts[-1])
    raise ValueError('Нужна ссылка на отдельное сообщение, а не приглашение в чат')


def filename(name, fallback):
    name = (name or fallback).replace('\\', '/').split('/')[-1]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(' .')
    # Keep extension even when the original name is long.
    head, dot, tail = name.rpartition('.')
    if dot and len(tail) <= 12:
        name = head[:140] + dot + tail
    else:
        name = name[:150]
    return name or fallback


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS dialogs (
            id INTEGER PRIMARY KEY, title TEXT, username TEXT);
          CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT,
            state TEXT DEFAULT 'queued', sent INTEGER DEFAULT 0,
            skipped INTEGER DEFAULT 0, errors INTEGER DEFAULT 0);
          CREATE TABLE IF NOT EXISTS delivered (
            chat INTEGER, message INTEGER, bot_message INTEGER,
            PRIMARY KEY(chat, message));
        ''')
        self.db.execute("UPDATE jobs SET state='queued' WHERE state='running'")
        self.db.commit()

    def dialog(self, chat_id, title, username):
        self.db.execute('INSERT OR REPLACE INTO dialogs VALUES (?,?,?)',
                        (chat_id, title, username or ''))
        self.db.commit()

    def search(self, text):
        needle = text.casefold().lstrip('@')
        return [dict(r) for r in self.db.execute('SELECT * FROM dialogs ORDER BY title')
                if needle in r['title'].casefold() or needle in r['username'].casefold()]

    def enqueue(self, payload):
        count = self.db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0]
        if count >= 20:
            raise ValueError('В очереди уже 20 заданий. Дождитесь завершения или отмените их.')
        cur = self.db.execute('INSERT INTO jobs(payload) VALUES (?)', (json.dumps(payload),))
        self.db.commit()
        return cur.lastrowid

    def next(self):
        row = self.db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY id LIMIT 1").fetchone()
        if not row:
            return None
        self.state(row['id'], 'running')
        return row['id'], json.loads(row['payload'])

    def state(self, job_id, state):
        self.db.execute('UPDATE jobs SET state=? WHERE id=?', (state, job_id))
        self.db.commit()

    def count(self, job_id, kind):
        if kind not in ('sent', 'skipped', 'errors'):
            raise ValueError('Invalid counter')
        self.db.execute(f'UPDATE jobs SET {kind}={kind}+1 WHERE id=?', (job_id,))
        self.db.commit()

    def seen(self, chat, msg):
        return self.db.execute('SELECT 1 FROM delivered WHERE chat=? AND message=?', (chat, msg)).fetchone() is not None

    def delivered(self, chat, msg, bot_msg, job):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO delivered VALUES (?,?,?)', (chat, msg, bot_msg))
            self.db.execute('UPDATE jobs SET sent=sent+1 WHERE id=?', (job,))

    def status(self):
        return [dict(r) for r in self.db.execute('SELECT id,state,sent,skipped,errors FROM jobs ORDER BY id DESC LIMIT 8')]

    def cancel_queued(self):
        self.db.execute("UPDATE jobs SET state='cancelled' WHERE state='queued'")
        self.db.commit()
