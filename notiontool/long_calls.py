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
* Hard limit (live 25.10.: nt_job_result answered 'still running' for 20+ min): every job has a
  deadline, its tool's own timeout + JOB_GRACE_S, at least JOB_HARD_S. A job past its deadline, or
  whose thread ended without a result, is given up: nt_job_result raises JobLost with the job's last
  output instead of still_running forever. Nothing is started again (no resend); a late result of a
  given-up job is dropped.
* Finished jobs are kept KEEP_S seconds, at most MAX_JOBS jobs; running jobs are never
  dropped. Results are the tools' own (already capped) results.
* Never hold engine.lock while calling run() or result().
"""
import hashlib
import json
import os
import subprocess
import threading
import time
import uuid

SYNC_LIMIT = 45.0          # default; dig measured no cutoff up to 56 s
MIN_LIMIT, MAX_LIMIT = 10.0, 55.0
SETTING = 'mcp_sync_limit_s'
KEEP_S = 30 * 60
MAX_JOBS = 50
DEFAULT_WAIT, MAX_WAIT = 50.0, 50.0  # one long wait instead of many short polls (each call = Notion steps)
JOB_HARD_S = 300.0         # hard limit of a job whose tool has no timeout of its own
JOB_GRACE_S = 60.0         # added to a tool's own timeout (nt_shell kills at its timeout, max 1800 s)
MAX_TOOL_TIMEOUT = 1800.0
TAIL_CHARS = 2000          # last output kept per job for the JobLost error
KILL_WAIT_S = 10.0         # after a kill: wait at most this long for the process and its pipes
JOB_TOOL = 'nt_job_result'
HINT = ('Still running on the PC. Do not start it again. Call nt_job_result with this job_id '
        'to get the result (waits up to 50 s per call; do not poll with shorter waits or sleep commands).')
JOB_TOOL_SPEC = {
    'name': JOB_TOOL,
    'description': ('Get the result of a NotionTool tool call that answered still_running with a job_id '
                    '(calls longer than about 45 s). Waits up to wait_s seconds (default 50, max 50), then '
                    'returns the final result or still_running again. A job past its hard limit (its timeout '
                    '+ 60 s, at least 300 s) returns an error with its last output. Read-only; only the caller '
                    'who started the job can read it (coworkers pass their assignment_token).'),
    'annotations': {'readOnlyHint': True, 'destructiveHint': False},
    'inputSchema': {'type': 'object', 'properties': {
        'job_id': {'type': 'string'}, 'wait_s': {'type': 'number'},
        'assignment_token': {'type': 'string', 'description': 'Coworker chats only: the assignment token used for the original call.'}},
        'required': ['job_id']},
}

_current = threading.local()  # the job running on this thread (output_sink)


class JobLost(ValueError):
    """A job that passed its hard limit or lost its thread; carries the job's last output."""


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


def job_deadline(args):
    """Seconds a job may run: the tool's own timeout (also inside mcp_call arguments) + JOB_GRACE_S, >= JOB_HARD_S."""
    args = args if isinstance(args, dict) else {}
    inner = args.get('arguments') if isinstance(args.get('arguments'), dict) else {}
    raw = args.get('timeout', inner.get('timeout'))
    try:
        own = 0.0 if raw is None or isinstance(raw, bool) else float(raw)
    except (TypeError, ValueError):
        own = 0.0
    if own != own:
        own = 0.0
    return max(JOB_HARD_S, min(MAX_TOOL_TIMEOUT, max(0.0, own)) + JOB_GRACE_S)


def job_key(caller, tool, args):
    text = json.dumps([str(caller), str(tool), args], sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def output_sink():
    """note(text) that keeps the last TAIL_CHARS of output of the job running on this thread (no-op outside)."""
    job = getattr(_current, 'job', None)
    if job is None:
        return lambda text: None

    def note(text):
        if text:
            job['tail'] = (job.get('tail', '') + str(text))[-TAIL_CHARS:]
    return note


def kill_tree(process, creationflags=0):
    """Stop a process and its children (taskkill /T on Windows). Bounded, never raises."""
    if os.name == 'nt':
        try:
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True,
                           timeout=KILL_WAIT_S, creationflags=creationflags)
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        process.kill()
    except OSError:
        pass


# ---- RAM limit for coworker commands ---------------------------------------
# Live 26.10.: one coworker command left a python process that grew to 17 GB (the PC has 16 GB);
# the whole PC froze for 30 min and even taskkill /T at the timeout did not reach it (its parent
# had already ended). contain() puts the command into a Windows job object: all its processes
# together get a RAM limit, and release() stops every process still left in the job.
JOB_MEM_MB = 4096          # all processes of one command together; env NT_JOB_MEM_MB overrides
_LIMIT_JOB_MEMORY = 0x200  # JOB_OBJECT_LIMIT_JOB_MEMORY
_LIMIT_KILL_ON_CLOSE = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
_EXTENDED_INFO = 9         # JobObjectExtendedLimitInformation
_win = None


def job_mem_mb():
    try:
        value = int(os.environ.get('NT_JOB_MEM_MB') or JOB_MEM_MB)
    except ValueError:
        value = JOB_MEM_MB
    return max(256, value)


def _kernel():
    """(ctypes, kernel32, Extended structure), loaded once."""
    global _win
    if _win is None:
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit', ctypes.c_longlong), ('PerJobUserTimeLimit', ctypes.c_longlong),
                        ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                        ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                        ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD),
                        ('SchedulingClass', wintypes.DWORD)]

        class Io(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                'ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
                'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

        class Extended(ctypes.Structure):
            _fields_ = [('BasicLimitInformation', Basic), ('IoInfo', Io),
                        ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                        ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]

        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                  wintypes.DWORD, ctypes.c_void_p]
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        _win = (ctypes, k32, Extended)
    return _win


def contain(process, limit_mb=None):
    """Job handle holding process (and every process it starts) under a shared RAM limit; None
    outside Windows or when Windows refuses (then only kill_tree applies). Call release() after."""
    if os.name != 'nt':
        return None
    try:
        ctypes, k32, Extended = _kernel()
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = Extended()
        info.BasicLimitInformation.LimitFlags = _LIMIT_JOB_MEMORY | _LIMIT_KILL_ON_CLOSE
        info.JobMemoryLimit = int(limit_mb or job_mem_mb()) * 1024 * 1024
        if not (k32.SetInformationJobObject(job, _EXTENDED_INFO, ctypes.byref(info), ctypes.sizeof(info))
                and k32.AssignProcessToJobObject(job, int(process._handle))):
            k32.CloseHandle(job)
            return None
        return job
    except (OSError, AttributeError, ValueError, TypeError):
        return None


def release(job):
    """Stop every process still in the job and close it; the peak RAM of the job in MB (None if unknown)."""
    if not job:
        return None
    peak = None
    try:
        ctypes, k32, Extended = _kernel()
        info = Extended()
        if k32.QueryInformationJobObject(job, _EXTENDED_INFO, ctypes.byref(info), ctypes.sizeof(info), None):
            peak = int(info.PeakJobMemoryUsed // (1024 * 1024))
        k32.TerminateJobObject(job, 1)
        k32.CloseHandle(job)
    except (OSError, AttributeError, ValueError, TypeError):
        pass
    return peak


def communicate(process, timeout, creationflags=0, note=None):
    """(stdout bytes, stderr bytes, timed_out) of a Popen with PIPEs; never blocks much past timeout.

    Root cause of the hung nt_shell jobs: process.communicate() after the kill waits WITHOUT a limit
    for the pipes, and a grandchild that keeps them open (pytest workers, started apps) blocked the
    tool call and its job forever; subprocess.run(timeout) does the same inside and dropped the output
    ({exit_code: null, timed_out: true}, no output). Here reader threads collect the output as it
    comes, so a timeout still returns everything written so far, and every wait is bounded.
    """
    end = time.monotonic() + max(0.0, float(timeout))
    parts = {'stdout': [], 'stderr': []}

    def reader(name, stream):
        try:
            while True:
                chunk = stream.read1(65536) if hasattr(stream, 'read1') else stream.read(65536)
                if not chunk:
                    break
                parts[name].append(chunk)
                if note:
                    note(chunk.decode('utf-8', 'replace'))
        except (OSError, ValueError):
            pass

    readers = [threading.Thread(target=reader, args=(name, stream), daemon=True, name='nt-pipe-' + name)
               for name, stream in (('stdout', process.stdout), ('stderr', process.stderr)) if stream is not None]
    for thread in readers:
        thread.start()
    for thread in readers:
        thread.join(max(0.0, end - time.monotonic()))
    try:
        process.wait(max(0.0, end - time.monotonic()))
        timed_out = False
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_tree(process, creationflags)
        try:
            process.wait(KILL_WAIT_S)
        except subprocess.TimeoutExpired:
            pass
    for thread in readers:
        thread.join(KILL_WAIT_S if timed_out else 0.5)  # a grandchild may keep a pipe open: give up on it
    return b''.join(parts['stdout']), b''.join(parts['stderr']), timed_out


class Jobs:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._lock = threading.Lock()
        self._jobs = {}   # job_id -> job dict (insertion order = start order)

    # ---- public -----------------------------------------------------------
    def run(self, caller, tool, args, fn, limit=None, deadline=None):
        """Run fn() on a daemon thread; its result if done within limit, else a still_running answer.

        deadline: seconds until the job is given up (JobLost); default JOB_HARD_S."""
        limit = SYNC_LIMIT if limit is None else max(0.0, float(limit))
        key = job_key(caller, tool, args)
        with self._lock:
            self._cleanup()
            job = next((j for j in self._jobs.values() if j['key'] == key and not j['done'].is_set()), None)
            reused = job is not None
            if job is None:
                job = self._new(caller, tool, key, JOB_HARD_S if deadline is None else max(0.0, float(deadline)))
                job['thread'] = threading.Thread(target=self._work, args=(job, fn), name='nt-job-' + job['id'], daemon=True)
        if not reused:
            job['thread'].start()
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
    def _new(self, caller, tool, key, seconds=JOB_HARD_S):
        started = self.clock()
        job = {'id': 'job-' + uuid.uuid4().hex[:16], 'caller': str(caller), 'tool': str(tool), 'key': key,
               'started': started, 'deadline': started + seconds, 'finished': None, 'done': threading.Event(),
               'result': None, 'error': None, 'handed_out': False, 'thread': None, 'tail': ''}
        self._jobs[job['id']] = job
        return job

    def _work(self, job, fn):
        _current.job = job
        result = error = None
        try:
            result = fn()
        except BaseException as exc:  # noqa: BLE001 - kept and raised when the result is fetched
            error = exc
        finally:
            _current.job = None
            with self._lock:
                if not job['done'].is_set():  # a job given up at its deadline keeps its JobLost error
                    job['result'], job['error'] = result, error
                    job['finished'] = self.clock()
                    job['done'].set()

    def _answer(self, job, wait, drop_if_unseen):
        thread = job.get('thread')

        def lost():  # _work sets done before its thread ends, so a dead thread without done lost its result
            return thread is not None and thread.ident is not None and not thread.is_alive() and not job['done'].is_set()
        if lost():
            self._give_up(job, 'ended without a result (its thread was lost)')
        left = job['deadline'] - self.clock()
        if not job['done'].wait(max(0.0, min(wait, left))):
            if self.clock() >= job['deadline']:
                self._give_up(job, 'did not finish within its hard limit of %d s' % round(job['deadline'] - job['started']))
            elif lost():
                self._give_up(job, 'ended without a result (its thread was lost)')
            if not job['done'].is_set():
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

    def _give_up(self, job, why):
        with self._lock:
            self._expire(job, why)

    def _expire(self, job, why):
        """Mark a running job done with JobLost (caller holds self._lock)."""
        if job['done'].is_set():
            return
        tail = (job.get('tail') or '').strip()
        job['error'] = JobLost('%s (%s) %s; NotionTool stopped waiting and did NOT start it again. '
                               'Check its effect before deciding what to do. Last output: %s'
                               % (job['tool'], job['id'], why, tail[-TAIL_CHARS:] or '(none)'))
        job['finished'] = self.clock()
        job['done'].set()

    def _cleanup(self):
        now = self.clock()
        for job in [j for j in self._jobs.values() if not j['done'].is_set() and now >= j['deadline']]:
            self._expire(job, 'did not finish within its hard limit of %d s' % round(job['deadline'] - job['started']))
        for ident in [i for i, j in self._jobs.items()
                      if j['done'].is_set() and j['finished'] is not None and now - j['finished'] > KEEP_S]:
            del self._jobs[ident]
        if len(self._jobs) > MAX_JOBS:
            finished = [i for i, j in self._jobs.items() if j['done'].is_set()]
            for ident in finished[:len(self._jobs) - MAX_JOBS]:
                del self._jobs[ident]


JOBS = Jobs()
