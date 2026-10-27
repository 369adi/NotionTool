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
        # Daily counts per kind of events removed by the 2000-event retention window.
        self.db.execute('CREATE TABLE IF NOT EXISTS event_rollup(day TEXT, kind TEXT, total INTEGER NOT NULL, PRIMARY KEY(day,kind))')
        self.db.execute("CREATE INDEX IF NOT EXISTS event_task ON events(json_extract(doc, '$.task_id'))")
        self.db.commit()
        self.projects = {}
        try:
            self.prune_old_tasks()  # before compact: the freed pages go back to the disk in the same start
        except Exception:
            pass  # never block the start; the hourly upkeep tries again
        self.compact()

    # Free pages above this share are given back to the disk at start (incremental, cheap).
    COMPACT_FREE_SHARE = 0.2

    def compact(self):
        """Switch the db to auto_vacuum=INCREMENTAL once (needs one full VACUUM) and return free pages.

        Runs at start, before any worker thread uses the db. Never raises: a busy or read-only db
        simply stays as it is.
        """
        try:
            with self.lock:
                mode = self.db.execute('PRAGMA auto_vacuum').fetchone()[0]
                if mode != 2:
                    self.db.execute('PRAGMA auto_vacuum=INCREMENTAL')
                    self.db.execute('VACUUM')  # only once: afterwards the mode is stored in the file
                    return 'vacuum'
                pages = self.db.execute('PRAGMA page_count').fetchone()[0]
                free = self.db.execute('PRAGMA freelist_count').fetchone()[0]
                if pages and free / pages > self.COMPACT_FREE_SHARE:
                    # every result row frees one page: without fetchall only the first page is freed
                    self.db.execute('PRAGMA incremental_vacuum').fetchall()
                    self.db.commit()
                    return 'incremental'
        except sqlite3.Error:
            return 'skipped'
        return ''

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
        # Lag fix 2026-10-25: no ORDER BY in SQL. With the (kind) index SQLite copied every doc through a temp
        # B-tree to sort by rowid (35 MB of task docs: 0.25-1 s per call, under the lock every engine, desktop
        # and UI thread needs). The rows are sorted by rowid in Python after the lock is released: same order.
        with self.lock:
            rows = self.db.execute('SELECT rowid, doc FROM objects WHERE kind=?', (kind,)).fetchall()
        rows.sort(key=lambda row: row[0])
        return [json.loads(row[1]) for row in rows]

    # Startup lag fix 2026-10-27: the hot engine/watch loops only care about unfinished tasks, but all('tasks')
    # parsed every task ever (1374 docs, 37 MB) in Python on every call. With ~10 threads doing that at startup
    # the GIL piled up and ticks took 26-182 s. SQLite filters here in C; only the few open docs are parsed.
    # Kept: unfinished states, tasks with an open patch copy (patch_state working/waiting) and remote_unknown.
    _OPEN_TASKS_SQL = ("SELECT rowid, doc FROM objects WHERE kind='tasks' AND ("
                       "COALESCE(json_extract(doc,'$.state'),'') NOT IN ('done','failed','blocked','cancelled') "
                       "OR json_extract(doc,'$.patch_state') IN ('working','waiting') "
                       "OR (json_type(doc,'$.remote_unknown') NOT IN ('false','null') "
                       "AND json_extract(doc,'$.remote_unknown') NOT IN (0,'')))")

    def open_tasks(self):
        """Tasks that are not finished, in rowid order (same order as all('tasks')). Fresh dicts per call.

        Callers must still apply their own state filter: this is only a fast superset of the open tasks."""
        with self.lock:
            rows = self.db.execute(self._OPEN_TASKS_SQL).fetchall()
        rows.sort(key=lambda row: row[0])
        return [json.loads(row[1]) for row in rows]

    # Owner 2026-10-27: only recent tasks are kept, old ones are deleted (no archive). A task goes when it is
    # finished, has no open patch copy, is not remote_unknown, its run is finished (or gone), its newest time
    # (finished/started/created) is older than TASK_KEEP_S and no kept task points at it (deps, review_of, ...).
    # Runs at every start (Store init) and hourly from CoworkerUpkeep.loop.
    TASK_KEEP_S = 2 * 86400
    TASK_FINISHED = ('done', 'failed', 'blocked', 'cancelled')
    RUN_FINISHED = ('done', 'cancelled', 'blocked')
    TASK_REF_KEYS = ('deps', 'review_of', 'review_task', 'salvage_of', 'salvage_from')

    def prune_old_tasks(self, now=None, keep_s=None):
        """Delete old finished tasks (rules above) and any leftover 'tasks_archive' rows. Returns the count."""
        now = time.time() if now is None else now
        keep_s = self.TASK_KEEP_S if keep_s is None else keep_s
        with self.lock:
            rows = self.db.execute("SELECT id, doc FROM objects WHERE kind='tasks'").fetchall()
            runs = {i: json.loads(d) for i, d in self.db.execute("SELECT id, doc FROM objects WHERE kind='runs'")}

        def newest(task):
            best = 0.0
            for key in ('finished', 'started', 'created'):
                value = task.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 1e9:
                    best = max(best, float(value))
            return best

        doomed, wanted = [], set()
        for ident, doc in rows:
            task = json.loads(doc)
            run = runs.get(str(task.get('run_id') or ''))
            age = newest(task)
            if (task.get('state') in self.TASK_FINISHED and task.get('patch_state') not in ('working', 'waiting')
                    and not task.get('remote_unknown') and age and now - age > keep_s
                    and (run is None or run.get('state') in self.RUN_FINISHED)):
                doomed.append(str(ident))
                continue
            for key in self.TASK_REF_KEYS:
                value = task.get(key)
                for item in (value if isinstance(value, list) else [value]):
                    if isinstance(item, (str, int)) and item:
                        wanted.add(str(item))
        doomed = [i for i in doomed if i not in wanted]
        with self.lock, self.db:
            for ident in doomed:
                # re-check the state in SQL: a task that was reopened meanwhile is never deleted
                self.db.execute("DELETE FROM objects WHERE kind='tasks' AND id=? AND json_extract(doc,'$.state') "
                                "IN ('done','failed','blocked','cancelled')", (ident,))
            archived = self.db.execute("DELETE FROM objects WHERE kind='tasks_archive'").rowcount
        return len(doomed) + max(0, archived or 0)

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

    def event(self, kind, run_id='', *, commit=True, **fields):
        # commit=False leaves the row to the next write on this connection: one disk sync instead of two.
        from .telemetry import EVENT_SCHEMA, IDENTIFIERS
        fields = redact(fields)
        for name in IDENTIFIERS:
            fields.setdefault(name, '')
        if run_id and not fields['project_id']:
            fields['project_id'] = self.project_of(run_id)
        fields['v'] = EVENT_SCHEMA
        with self.lock:
            seq = self.db.execute('INSERT INTO events(ts,run_id,kind,doc) VALUES(?,?,?,?)',
                                  (time.time(), run_id, kind, json.dumps(fields, ensure_ascii=False))).lastrowid
            cutoff = seq - 2000
            if self.db.execute('SELECT 1 FROM events WHERE seq < ? LIMIT 1', (cutoff,)).fetchone():
                # Count the oldest events per day and kind before retention removes them.
                self.db.execute("INSERT INTO event_rollup(day,kind,total) SELECT date(ts,'unixepoch','localtime'),kind,COUNT(*) "
                                "FROM events WHERE seq < ? GROUP BY 1,2 "
                                "ON CONFLICT(day,kind) DO UPDATE SET total=total+excluded.total", (cutoff,))
                self.db.execute('DELETE FROM events WHERE seq < ?', (cutoff,))
            if commit:
                self.db.commit()
        return seq

    def project_of(self, run_id):
        # Project of a run for event attribution; the chat folder until projects exist.
        projects = self.__dict__.setdefault('projects', {})
        if run_id not in projects:
            run = self.get('runs', run_id)
            if not run:
                return ''
            projects[run_id] = run.get('project_id') or run.get('folder_id') or ''
        return projects[run_id]

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
        defaults = dict(id='app', max_workers=40, per_account=5, coworker_max_age_hours=22,
                       automatic_setup=False, background_remote=False, bridge_auto=False,
                       remote_mcp_enabled=False, advertise_project_tools=False,
                       max_attempts=3, task_timeout=900, max_fix_rounds=1, tool_parallelism=4,
                       public_mcp_url='', theme='dark', notion_transport='edge',
                       context_token_limit=1200000, context_turn_limit=600, context_handoff_chars=24000,
                       host_api_port=8790, host_api_allow_network=False, host_api_bind='127.0.0.1',
                       coworker_fresh_context='threshold', context_replace_mode='soft',
                       # Coworker switch rule (chat_context.switch_decision; docs/CONTEXT_SWITCH_BENCH.md 'Defaults').
                       coworker_switch_policy='measured', switch_min_tokens=400000, switch_base_tokens=12000,
                       switch_cost_s=76, switch_slope_ms_per_100k=3514, switch_error_permille_per_100k=0,
                       switch_retry_s=60, switch_remaining_tasks=3,
                       ui_mode='classic', dynamic_roles=False, role_definitions=[],
                       coworker_rotate_mode='new_welcome', main_context_policy='hint',
                       coworker_delete_old_chat=True, idle_mode=True)
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
