"""Cached desktop state and a non-blocking state pusher for the NotionTool desktop window.

Why: the old loop rebuilt ``desktop_state`` every second from full store reads. ``store.all('tasks')``
parses every task document (prompts, raw responses, results: ~16 MB for ~500 tasks) and was called
twice per build (``notion_shell.build_state`` and ``desktop_state``), each time under the store lock
the engine also needs. One build took ~600 ms on the real store; with busy coworkers the loop thread
waited on the lock and on the synchronous CDP push (10 s timeout), so pushes stalled long enough for
the overlay's 20 s "disconnected" banner.

This module:
- reads tasks without heavy fields (SQL ``json_remove``), re-reading only new/unfinished ones, caps runs (active runs +
  last ``HISTORY_RUNS``) and events (last ``EVENT_LIMIT``);
- rebuilds only when the store revision (sqlite ``total_changes``), the app settings file or the
  time bucket changed;
- sends only on change, plus a tiny ``{type: 'heartbeat', ts}`` every ``HEARTBEAT_S``;
- pushes on its own worker thread; while one push is in flight newer pushes are dropped (never
  queued) and a push older than ``PUSH_TIMEOUT_S`` is reported as a timeout;
- records timings (avg/max/last) for /api/app/status and stores a ``push_slow`` event above ``SLOW_MS``.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

HEAVY_TASK_KEYS = ('submitted_prompt', 'result', 'raw_response', 'partial', 'attempt_log', 'prompt',
                   'acceptance', 'model_reason', 'request_fingerprint', 'model_verification',
                   'verify_messages_read', 'handoff_included', 'reported_model_ids')
RESULT_CHARS = 500       # result preview pushed to the page (desktop_state cuts at 2000; 60 x 2 KB alone was 120 KB)
FINAL_TASK_STATES = ('done', 'cancelled', 'failed')
TASK_FULL_REFRESH_S = 30.0   # finished tasks are cached; a full re-read catches rare edits/deletes
ACTIVE_TASK_STATES = ('running', 'sending', 'uncertain', 'reviewing', 'cancelling', 'queued', 'claimed')
DONE_RUN_STATES = ('done', 'failed', 'cancelled', 'completed', 'archived')
EVENT_LIMIT = 30
HISTORY_RUNS = 20
HEARTBEAT_S = 3.0
PUSH_TIMEOUT_S = 3.0
SLOW_MS = 2000.0
SLOW_EVENT_GAP_S = 60.0
TIME_BUCKET_S = 5.0     # time-based fields (recent tasks, ages) refresh at least this often
MIN_BUILD_S = 1.0       # never rebuild more often than this


def store_revision(store):
    """Cheap change counter: rows changed on the store's sqlite connection (no lock, no query)."""
    value = getattr(getattr(store, 'db', None), 'total_changes', None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _strip(task):
    light = {k: v for k, v in task.items() if k not in HEAVY_TASK_KEYS}
    if 'result' in task:
        result = task.get('result')
        light['result'] = result[:RESULT_CHARS] if isinstance(result, str) else result
    return light


_LIGHT_SQL = ("SELECT rowid, json_remove(doc,%s), json_type(doc,'$.result'), "
              "CASE json_type(doc,'$.result') WHEN 'text' THEN substr(json_extract(doc,'$.result'),1,%d) "
              "ELSE json(json_extract(doc,'$.result')) END FROM objects WHERE kind='tasks'" %
              (','.join("'$.%s'" % key for key in HEAVY_TASK_KEYS), RESULT_CHARS))


def _light_row(row):
    task = json.loads(row[1])
    kind, value = row[2], row[3]
    if kind == 'text':
        task['result'] = value
    elif kind is not None and value is not None:
        task['result'] = json.loads(value)
    return task


class TaskCache:
    """Light task docs kept across builds; each build re-reads only new and unfinished tasks."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.rows = {}
        self.max_rowid = 0
        self.full_at = None

    def tasks(self, store):
        db, lock = getattr(store, 'db', None), getattr(store, 'lock', None)
        if db is None or lock is None:
            return [_strip(task) for task in store.all('tasks')]
        now = self.clock()
        full = self.full_at is None or now - self.full_at >= TASK_FULL_REFRESH_S
        try:
            with lock:
                if full:
                    rows = db.execute(_LIGHT_SQL).fetchall()
                else:
                    pending = [rid for rid, task in self.rows.items() if task.get('state') not in FINAL_TASK_STATES]
                    rows = db.execute(_LIGHT_SQL + ' AND (rowid > ? OR rowid IN (SELECT value FROM json_each(?)))',
                                      (self.max_rowid, json.dumps(pending))).fetchall()
        except Exception:
            return [_strip(task) for task in store.all('tasks')]
        if full:
            self.rows, self.full_at = {}, now
        for row in rows:
            self.rows[row[0]] = _light_row(row)
            self.max_rowid = max(self.max_rowid, row[0])
        return [self.rows[rid] for rid in sorted(self.rows)]


def cap_runs(runs):
    """Active runs plus the last HISTORY_RUNS finished runs, ordered by creation."""
    ordered = sorted(runs, key=lambda r: r.get('created') or 0)
    active = [r for r in ordered if str(r.get('state') or '') not in DONE_RUN_STATES]
    history = [r for r in ordered if str(r.get('state') or '') in DONE_RUN_STATES][-HISTORY_RUNS:]
    keep = {id(r) for r in active + history}
    return [r for r in ordered if id(r) in keep]


class LightStore:
    """Read view of the store for one build: each kind is read once, tasks without heavy fields."""

    def __init__(self, store, task_cache=None):
        self._store = store
        self._tasks = task_cache or TaskCache()
        self._rows = {}

    def __getattr__(self, name):
        return getattr(self._store, name)

    def all(self, kind):
        if kind not in self._rows:
            if kind == 'tasks':
                rows = self._tasks.tasks(self._store)   # all tasks (links/attention need them), light docs
            elif kind == 'runs':
                rows = cap_runs(self._store.all('runs'))
            else:
                rows = self._store.all(kind)
            self._rows[kind] = rows
        return list(self._rows[kind])

    def events(self, after=0, limit=150):
        return self._store.events(after=after, limit=max(1, min(int(limit), EVENT_LIMIT)))


class LightServer:
    def __init__(self, server, store):
        self._server = server
        self.store = store

    def __getattr__(self, name):
        return getattr(self._server, name)


class PushStats:
    """Timings (ms) and counters for /api/app/status."""

    def __init__(self):
        self.lock = threading.Lock()
        self.timings = {}
        self.counters = dict(builds=0, pushes=0, heartbeats=0, dropped=0, timeouts=0, failures=0, slow_events=0)
        self.last_message_at = None
        self.last_error = ''

    def record(self, name, value):
        with self.lock:
            t = self.timings.setdefault(name, dict(count=0, total=0.0, max=0.0, last=0.0))
            t['count'] += 1
            t['total'] += value
            t['max'] = max(t['max'], value)
            t['last'] = value

    def count(self, name, step=1):
        with self.lock:
            self.counters[name] = self.counters.get(name, 0) + step

    def snapshot(self):
        with self.lock:
            timings = {k: dict(avg=round(v['total'] / v['count'], 2) if v['count'] else 0.0,
                               max=round(v['max'], 2), last=round(v['last'], 2), count=v['count'])
                       for k, v in self.timings.items()}
            return dict(timings=timings, counters=dict(self.counters), last_message_at=self.last_message_at,
                        last_error=self.last_error)


def _ms(start):
    return (time.perf_counter() - start) * 1000.0


class DesktopStateCache:
    """desktop_state, rebuilt only when the store revision, app settings or time bucket changed."""

    def __init__(self, server, data_dir=None, *, builder=None, settings_loader=None, settings_path=None,
                 stats=None, clock=time.monotonic, wall=time.time):
        self.server = server
        self.data_dir = data_dir
        self.builder = builder
        self.settings_loader = settings_loader
        self.settings_path = settings_path
        self.stats = stats or PushStats()
        self.clock = clock
        self.wall = wall
        self._rev = None
        self._state = None
        self._built_at = None
        self.task_cache = TaskCache(clock)

    def _settings_mtime(self):
        path = self.settings_path
        if path is None:
            from . import desktop_shell
            path = Path(self.data_dir or desktop_shell.DATA_DIR) / desktop_shell.APP_FILE
        try:
            return os.stat(path).st_mtime_ns
        except OSError:
            return None

    def revision(self):
        return (store_revision(self.server.store), self._settings_mtime(), int(self.wall() // TIME_BUCKET_S))

    def get(self):
        """(state, rebuilt)."""
        rev = self.revision()
        fresh = self._state is not None
        if fresh and rev == self._rev and rev[0] is not None:
            return self._state, False
        if fresh and self._built_at is not None and self.clock() - self._built_at < MIN_BUILD_S:
            return self._state, False
        from . import desktop_shell
        loader = self.settings_loader or desktop_shell.load_app_settings
        builder = self.builder or desktop_shell.desktop_state
        start = time.perf_counter()
        app = loader(self.data_dir)
        self.stats.record('load_app_settings_ms', _ms(start))
        start = time.perf_counter()
        state = builder(LightServer(self.server, LightStore(self.server.store, self.task_cache)), app)
        if isinstance(state, dict) and isinstance(state.get('events'), list):
            state['events'] = state['events'][-EVENT_LIMIT:]
        self.stats.record('desktop_state_ms', _ms(start))
        self.stats.count('builds')
        self._state, self._rev, self._built_at = state, rev, self.clock()
        return state, True


def _state_key(state):
    if isinstance(state, dict):
        state = {k: v for k, v in state.items() if k != 'ts'}
    return json.dumps(state, sort_keys=True, ensure_ascii=True, default=str)


HEARTBEAT_JS = ('(function(b){if(typeof window.__ntHarnessBeat==="function")window.__ntHarnessBeat(b);'
                'else window.__ntDeskLastMessage=Date.now();})(%s)')


class StatePusher:
    """One ``tick()`` per loop turn; never blocks on the page."""

    def __init__(self, shell, cache, *, store=None, stats=None, clock=time.monotonic, wall=time.time,
                 heartbeat_s=HEARTBEAT_S, timeout_s=PUSH_TIMEOUT_S, slow_ms=SLOW_MS, spawn=None):
        self.shell = shell
        self.cache = cache
        self.store = store
        self.stats = stats or getattr(cache, 'stats', None) or PushStats()
        self.clock = clock
        self.wall = wall
        self.heartbeat_s = heartbeat_s
        self.timeout_s = timeout_s
        self.slow_ms = slow_ms
        self.spawn = spawn or self._thread
        self.lock = threading.Lock()
        self._sent_key = None
        self._last_sent = None
        self._inflight = None      # (kind, started) of the running push
        self._timed_out = False
        self._slow_at = None

    @staticmethod
    def _thread(fn):
        thread = threading.Thread(target=fn, daemon=True, name='desktop-push')
        thread.start()
        return thread

    def tick(self):
        start = time.perf_counter()
        state, _rebuilt = self.cache.get()
        build_ms = _ms(start)
        if build_ms > self.slow_ms:
            self._slow('build', build_ms)
        self._check_inflight()
        key = _state_key(state)
        now = self.clock()
        if key != self._sent_key:
            return self._send('state', state, key)
        if self._last_sent is None or now - self._last_sent >= self.heartbeat_s:
            return self._send('heartbeat', {'type': 'heartbeat', 'ts': round(self.wall(), 3)}, None)
        return None

    def _check_inflight(self):
        with self.lock:
            inflight = self._inflight
        if inflight and not self._timed_out and self.clock() - inflight[1] > self.timeout_s:
            self._timed_out = True
            self.stats.count('timeouts')
            self._slow('push_' + inflight[0], (self.clock() - inflight[1]) * 1000.0)

    def _send(self, kind, payload, key):
        with self.lock:
            if self._inflight is not None:
                self.stats.count('dropped')   # stale pushes are dropped; the next tick sends the newest state
                return False
            self._inflight, self._timed_out = (kind, self.clock()), False
        if kind == 'state':
            self.stats.record('payload_kb', len(key) / 1024.0)
        self.spawn(lambda: self._run(kind, payload, key))
        return True

    def _run(self, kind, payload, key):
        start = time.perf_counter()
        ok = False
        try:
            if kind == 'state':
                self.shell.push(payload)
                ok = True
            else:
                evaluate = getattr(self.shell, '_evaluate', None)
                ok = bool(evaluate(HEARTBEAT_JS % json.dumps(payload))) if callable(evaluate) else False
        except Exception as exc:  # noqa: BLE001 - a failed push must never stop the loop
            self.stats.last_error = (str(exc) or type(exc).__name__)[:200]
        elapsed = _ms(start)
        self.stats.record('push_ms' if kind == 'state' else 'heartbeat_ms', elapsed)
        if elapsed > self.slow_ms:
            self._slow('push_' + kind, elapsed)
        with self.lock:
            self._inflight = None
            if ok:
                self._last_sent = self.clock()
                if kind == 'state':
                    self._sent_key = key
        if ok:
            self.stats.count('pushes' if kind == 'state' else 'heartbeats')
            self.stats.last_message_at = round(self.wall(), 3)
        else:
            self.stats.count('failures')

    def _slow(self, stage, ms):
        now = self.clock()
        if self._slow_at is not None and now - self._slow_at < SLOW_EVENT_GAP_S:
            return
        self._slow_at = now
        self.stats.count('slow_events')
        if self.store is not None:
            try:
                self.store.event('push_slow', message='Desktop %s took %d ms' % (stage, int(ms)),
                                 stage=stage, ms=int(ms))
            except Exception:
                pass
