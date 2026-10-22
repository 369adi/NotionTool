"""Long tool calls: answer Notion before its MCP request timeout (-32001).

Notion stops waiting for a tools/call after roughly a minute while the tool keeps
running on the PC, so the caller loses the result and often repeats the command.
Jobs.run() runs the whole tool call on one daemon thread and waits at most the sync
limit. A call that finishes in time returns its normal result unchanged; a slower one
returns a still_running answer with a job_id, and nt_job_result fetches the result.

* The whole call runs on the job thread, so Bridge.call() records tool_finished there,
  when the job really finishes (not when still_running was returned).
* An exception of the job is kept and raised again when the result is fetched, so the
  caller gets the normal tool error.
* Same caller + tool + arguments while a job still runs: that job is reused, never a
  second run (repeated commands, busy shell slots).
* A job can only be read by the caller key that started it.
* Finished jobs are kept KEEP_S seconds, at most MAX_JOBS jobs; running jobs are never
  dropped. Results are the tools' own (already capped) results.
* Never hold engine.lock while calling run() or result().
"""
import hashlib
import json
import threading
import time
import uuid

SYNC_LIMIT = 45.0          # default; dig measured no cutoff up to 56 s
MIN_LIMIT, MAX_LIMIT = 10.0, 55.0
SETTING = 'mcp_sync_limit_s'
KEEP_S = 30 * 60
MAX_JOBS = 50
DEFAULT_WAIT, MAX_WAIT = 30.0, 40.0
JOB_TOOL = 'nt_job_result'
HINT = ('Still running on the PC. Do not start it again. Call nt_job_result with this job_id '
        'to get the result (waits up to 40 s per call).')
JOB_TOOL_SPEC = {
    'name': JOB_TOOL,
    'description': ('Get the result of a NotionTool tool call that answered still_running with a job_id '
                    '(calls longer than about 45 s). Waits up to wait_s seconds (default 30, max 40), then '
                    'returns the final result or still_running again. Read-only; only the caller who started '
                    'the job can read it (coworkers pass their assignment_token).'),
    'annotations': {'readOnlyHint': True, 'destructiveHint': False},
    'inputSchema': {'type': 'object', 'properties': {
        'job_id': {'type': 'string'}, 'wait_s': {'type': 'number'},
        'assignment_token': {'type': 'string', 'description': 'Coworker chats only: the assignment token used for the original call.'}},
        'required': ['job_id']},
}


def sync_limit(settings=None):
    """Seconds to wait before answering still_running: setting mcp_sync_limit_s clamped 10-55, else SYNC_LIMIT."""
    raw = (settings or {}).get(SETTING) if isinstance(settings, dict) else None
    if raw is None or isinstance(raw, bool):
        return SYNC_LIMIT
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return SYNC_LIMIT
    if value != value:  # NaN
        return SYNC_LIMIT
    return max(MIN_LIMIT, min(MAX_LIMIT, value))


def wait_seconds(raw):
    try:
        value = DEFAULT_WAIT if raw is None or isinstance(raw, bool) else float(raw)
    except (TypeError, ValueError):
        value = DEFAULT_WAIT
    if value != value:
        value = DEFAULT_WAIT
    return max(0.0, min(MAX_WAIT, value))


def job_key(caller, tool, args):
    text = json.dumps([str(caller), str(tool), args], sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


class Jobs:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._lock = threading.Lock()
        self._jobs = {}   # job_id -> job dict (insertion order = start order)

    # ---- public -----------------------------------------------------------
    def run(self, caller, tool, args, fn, limit=None):
        """Run fn() on a daemon thread; its result if done within limit, else a still_running answer."""
        limit = SYNC_LIMIT if limit is None else max(0.0, float(limit))
        key = job_key(caller, tool, args)
        with self._lock:
            self._cleanup()
            job = next((j for j in self._jobs.values() if j['key'] == key and not j['done'].is_set()), None)
            reused = job is not None
            if job is None:
                job = self._new(caller, tool, key)
        if not reused:
            thread = threading.Thread(target=self._work, args=(job, fn), name='nt-job-' + job['id'], daemon=True)
            thread.start()
        return self._answer(job, limit, drop_if_unseen=not reused)

    def result(self, caller, job_id, wait_s=None):
        """nt_job_result: the final result (or the job's error), or still_running after wait_s."""
        with self._lock:
            self._cleanup()
            job = self._jobs.get(str(job_id or ''))
        if job is None:
            raise ValueError('Unknown or expired job_id (finished jobs are kept 30 min)')
        if job['caller'] != str(caller):
            raise ValueError('This job_id belongs to another caller')
        return self._answer(job, wait_seconds(wait_s), drop_if_unseen=False)

    def running(self):
        with self._lock:
            return [j['id'] for j in self._jobs.values() if not j['done'].is_set()]

    def __len__(self):
        with self._lock:
            return len(self._jobs)

    def __contains__(self, job_id):
        with self._lock:
            return job_id in self._jobs

    # ---- internals --------------------------------------------------------
    def _new(self, caller, tool, key):
        job = {'id': 'job-' + uuid.uuid4().hex[:16], 'caller': str(caller), 'tool': str(tool), 'key': key,
               'started': self.clock(), 'finished': None, 'done': threading.Event(),
               'result': None, 'error': None, 'handed_out': False}
        self._jobs[job['id']] = job
        return job

    def _work(self, job, fn):
        try:
            job['result'] = fn()
        except BaseException as exc:  # noqa: BLE001 - kept and raised when the result is fetched
            job['error'] = exc
        finally:
            job['finished'] = self.clock()
            job['done'].set()

    def _answer(self, job, wait, drop_if_unseen):
        if not job['done'].wait(wait):
            job['handed_out'] = True
            return {'still_running': True, 'job_id': job['id'], 'tool': job['tool'],
                    'elapsed_s': round(self.clock() - job['started'], 1), 'hint': HINT}
        if drop_if_unseen and not job['handed_out']:
            # Answered directly: nobody holds this job_id, so do not keep it.
            with self._lock:
                self._jobs.pop(job['id'], None)
        if job['error'] is not None:
            raise job['error']
        return job['result']

    def _cleanup(self):
        now = self.clock()
        for ident in [i for i, j in self._jobs.items()
                      if j['done'].is_set() and j['finished'] is not None and now - j['finished'] > KEEP_S]:
            del self._jobs[ident]
        if len(self._jobs) > MAX_JOBS:
            finished = [i for i, j in self._jobs.items() if j['done'].is_set()]
            for ident in finished[:len(self._jobs) - MAX_JOBS]:
                del self._jobs[ident]


JOBS = Jobs()
