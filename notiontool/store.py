"""Small transactional store; no credentials are exposed through snapshots."""
from __future__ import annotations
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path


def uid(prefix=''):
    return prefix + uuid.uuid4().hex


SECRET_KEYS = ('token', 'cookie', 'authorization', 'password', 'secret', 'api_key', 'apikey', 'bearer', 'code')
# Token counts are metrics, not credentials; their numeric values stay readable.
COUNT_KEYS = ('tokens', 'token_limit', 'token_count', 'token_budget')


def redact(value, key=''):
    """Logs must never contain cookies, bearer tokens, passwords or login codes."""
    lowered = key.lower()
    if lowered and any(word in lowered for word in SECRET_KEYS) and lowered not in ('status_code', 'error_code'):
        count = isinstance(value, (int, float)) and not isinstance(value, bool)
        return value if count and any(word in lowered for word in COUNT_KEYS) else '[redacted]'
    if isinstance(value, dict):
        return {k: redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        import re
        value = re.sub(r'(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}', r'\1[redacted]', value)
        value = re.sub(r'(?i)(token_v2|notion_user_id|cookie)[=:]\s*[^;\s]+', r'\1=[redacted]', value)
    return value


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.directory / 'notiontool.db', check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS objects(kind TEXT, id TEXT, doc TEXT, PRIMARY KEY(kind,id))')
        self.db.execute('CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, run_id TEXT, kind TEXT, doc TEXT)')
        self.db.execute("CREATE INDEX IF NOT EXISTS message_chat ON objects(kind, json_extract(doc, '$.chat_id'))")
        self.db.execute('CREATE TABLE IF NOT EXISTS counters(name TEXT PRIMARY KEY, value INTEGER NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)')
        self.db.commit()

    SCHEMA_VERSION = 2

    def schema_version(self):
        with self.lock:
            row = self.db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        return int(row[0]) if row else 1

    def set_schema_version(self, version):
        with self.lock, self.db:
            self.db.execute("INSERT INTO meta VALUES('schema_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(version),))

    def next_counter(self, name, records_fn=None):
        """Atomically reserve the next durable number (e.g. Main N).

        records_fn(value) may return records committed in the same transaction,
        so a number is never handed out without its owning record.
        """
        with self.lock, self.db:
            self.db.execute('INSERT OR IGNORE INTO counters VALUES(?,0)', (name,))
            self.db.execute('UPDATE counters SET value=value+1 WHERE name=?', (name,))
            value = self.db.execute('SELECT value FROM counters WHERE name=?', (name,)).fetchone()[0]
            for kind, doc in (records_fn(value) if records_fn else ()):
                self.db.execute('INSERT INTO objects VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET doc=excluded.doc',
                    (kind, str(doc['id']), json.dumps(doc, ensure_ascii=False)))
        return value

    def counter(self, name):
        with self.lock:
            row = self.db.execute('SELECT value FROM counters WHERE name=?', (name,)).fetchone()
        return row[0] if row else 0

    def raise_counter(self, name, minimum):
        with self.lock, self.db:
            self.db.execute('INSERT OR IGNORE INTO counters VALUES(?,0)', (name,))
            self.db.execute('UPDATE counters SET value=MAX(value,?) WHERE name=?', (int(minimum), name))

    def get(self, kind, ident, default=None):
        with self.lock:
            row = self.db.execute('SELECT doc FROM objects WHERE kind=? AND id=?', (kind, str(ident))).fetchone()
        return json.loads(row[0]) if row else default

    def all(self, kind):
        with self.lock:
            rows = self.db.execute('SELECT doc FROM objects WHERE kind=? ORDER BY rowid', (kind,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def put(self, kind, doc):
        with self.lock:
            self.db.execute('INSERT INTO objects VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET doc=excluded.doc',
                            (kind, str(doc['id']), json.dumps(doc, ensure_ascii=False)))
            self.db.commit()
        return doc

    def delete(self, kind, ident):
        with self.lock:
            self.db.execute('DELETE FROM objects WHERE kind=? AND id=?', (kind, str(ident)))
            self.db.commit()

    def batch(self, records, deletes=()):
        """Commit linked generation/ledger changes together."""
        with self.lock, self.db:
            for kind, doc in records:
                self.db.execute('INSERT INTO objects VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET doc=excluded.doc',
                    (kind, str(doc['id']), json.dumps(doc, ensure_ascii=False)))
            for kind, ident in deletes:
                self.db.execute('DELETE FROM objects WHERE kind=? AND id=?', (kind, str(ident)))

    def event(self, kind, run_id='', **fields):
        fields = redact(fields)
        with self.lock:
            self.db.execute('INSERT INTO events(ts,run_id,kind,doc) VALUES(?,?,?,?)',
                            (time.time(), run_id, kind, json.dumps(fields, ensure_ascii=False)))
            self.db.execute('DELETE FROM events WHERE seq < (SELECT COALESCE(MAX(seq),0)-2000 FROM events)')
            self.db.commit()

    def events(self, after=0, limit=150):
        with self.lock:
            rows = self.db.execute('SELECT seq,ts,run_id,kind,doc FROM events WHERE seq>? ORDER BY seq DESC LIMIT ?',
                                   (after, limit)).fetchall()
        return [dict(seq=s, ts=t, run_id=r, kind=k, **json.loads(d)) for s,t,r,k,d in reversed(rows)]

    def delete_events(self, kind, **match):
        """Delete events of one kind whose fields equal all given values."""
        if not all(name.isidentifier() for name in match):
            raise ValueError('Event field names must be identifiers')
        where = ''.join(f" AND json_extract(doc,'$.{name}')=?" for name in match)
        with self.lock:
            self.db.execute('DELETE FROM events WHERE kind=?'+where, (kind, *match.values()))
            self.db.commit()

    def message(self, chat_id, role, text, run_id=''):
        return self.put('messages', dict(id=uid('m'), chat_id=chat_id, role=role, text=text,
                                         run_id=run_id, created=time.time()))

    def message_page(self, chat_id, limit=100):
        limit = max(1, min(10000, int(limit)))
        with self.lock:
            rows = self.db.execute("SELECT doc FROM objects WHERE kind='messages' AND json_extract(doc,'$.chat_id')=? ORDER BY rowid DESC LIMIT ?",
                                   (chat_id, limit+1)).fetchall()
        return dict(messages=[json.loads(row[0]) for row in reversed(rows[:limit])], has_more=len(rows)>limit)

    def message_marker(self, chat_id):
        """Cheap change marker so the UI refetches history only when it changed."""
        with self.lock:
            row = self.db.execute("SELECT COUNT(*), COALESCE(MAX(rowid),0) FROM objects WHERE kind='messages' AND json_extract(doc,'$.chat_id')=?", (chat_id,)).fetchone()
        return f'{row[0]}:{row[1]}'

    def settings(self):
        defaults = dict(id='app', max_workers=40, per_account=8,
                       max_attempts=3, task_timeout=900, max_fix_rounds=2, tool_parallelism=4,
                       public_mcp_url='', theme='dark', notion_transport='edge',
                       context_token_limit=60000, context_turn_limit=80, context_handoff_chars=24000,
                       host_api_port=8790, host_api_allow_network=False, host_api_bind='127.0.0.1',
                       coworker_fresh_context='threshold', context_replace_mode='soft')
        return dict(defaults, **self.get('settings', 'app', {}))

    def remember(self, ident, text, source=''):
        """Retain every finding; the short working index may be compacted separately."""
        with self.lock:
            memory = self.get('memory', ident, dict(id=ident, text=''))
            if memory.get('text') and not self.get('memory_baselines', ident):
                self.put('memory_baselines', dict(id=ident, text=memory['text']))
            entry = dict(id=uid('mem'), memory_id=ident, text=str(text), source=source, created=time.time())
            memory['text'] = (memory.get('text', '') + '\n' + str(text))[-24000:]
            self.batch([('memory_entries', entry), ('memory', memory)])
            return memory

    def close(self):
        with self.lock:
            self.db.close()
