"""Live progress of busy Coworkers (read only, rule I5: reading a thread is fine, nothing is ever sent).

For each Coworker task in state running/uncertain the feed reads the task's own Notion chat with the
same call the reconcile code uses (NotionClient.transcript -> getThreadTranscript, newest WINDOW
entries, direction backward). It never uses the app window: the client goes through the harness
transport, whose every request takes a NetworkBudget ticket (I11). Reads run at 'background'
priority, so task traffic always goes first.

Only the task's own turn counts (its user step marker, like notion.recover). Extracted: steps so far,
the current step in plain words, its target (file name, command first 60 chars), the last answer
snippet (max 140 chars), the last activity time, a rough progress estimate ('Step x of y', a
checklist, else the phase exploring -> editing -> testing -> answering) and a stalled flag when no
new step arrived for STALL_S seconds.

Scheduling: every busy task is read every POLL_S seconds, new tasks are staggered across the poll
interval, at most one read starts per tick and at most MAX_PER_MINUTE reads start in any 60 s window
(10 busy Coworkers: 36/min instead of 40). A task that ends is dropped at once (no further reads).
API: ProgressFeed(store, adapter).start() / .stop() / .tick(); .payload(task_id) and .public() give
the small dicts for state.progress[task_id] and GET /api/progress.
"""
import json
import ntpath
import re
import threading
import time
from collections import deque

from .turn_watch import stamp, text_of

POLL_S = 15.0
STALL_S = 300.0
MAX_PER_MINUTE = 36
WINDOW = 40
ERROR_BACKOFF_S = 60.0
TICK_S = 1.0
ACTIVE = ('running', 'uncertain')
SNIPPET = 140
TARGET = 60

KIND_WORDS = (
    ('Writing file', ('write', 'edit', 'create', 'patch', 'replace', 'append', 'save', 'insert', 'delete', 'move')),
    ('Running command', ('shell', 'run', 'exec', 'command', 'terminal', 'powershell', 'bash', 'cmd', 'test')),
    ('Searching code', ('search', 'grep', 'find', 'query', 'lookup', 'glob')),
    ('Reading files', ('read', 'view', 'open', 'list', 'get', 'load', 'cat', 'fetch', 'dir', 'tree')),
)
WRAPPERS = ('mcp_call', 'runtool', 'run_tool', 'callfunction', 'call_function', 'tool_call')
TEST_WORDS = re.compile(r'\b(unittest|pytest|node --check|npm test|test_\w+|tests?)\b', re.IGNORECASE)
STEP_RE = re.compile(r'\bstep\s+(\d{1,3})\s*(?:of|/)\s*(\d{1,3})\b', re.IGNORECASE)
DONE_BOX = re.compile(r'(?m)^\s*(?:[-*]\s*)?(?:\[[xX]\]|\u2705|\u2611)')
OPEN_BOX = re.compile(r'(?m)^\s*(?:[-*]\s*)?(?:\[ \]|\u2610|\u2b1c)')
PHASES = {'starting': 5, 'exploring': 15, 'editing': 45, 'testing': 70, 'answering': 90, 'done': 100}


def _kind(entity):
    return str(entity.get('kind') or entity.get('type') or '').lower()


def role(entity):
    """answer, user, thinking, tool, tool_result or '' (metadata)."""
    kind = _kind(entity)
    if kind == 'assistant_message':
        return 'answer'
    if kind == 'user_message':
        return 'user'
    if 'thinking' in kind or 'reasoning' in kind:
        return 'thinking'
    if entity.get('tool_use_id') or any(w in kind for w in ('tool', 'execution', 'computer')):
        return 'tool_result' if any(w in kind for w in ('result', 'output', 'response')) else 'tool'
    return ''


def _args(entity):
    for key in ('input', 'arguments', 'args', 'tool_input', 'toolInput', 'parameters', 'toolArguments'):
        value = entity.get(key)
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                continue
        if isinstance(value, dict):
            return value
    return {}


def tool_call(entity):
    """(name, args) of a tool step; wrapper calls (mcp_call, runTool) are unwrapped to the inner tool."""
    name = ''
    for key in ('tool_name', 'toolName', 'name', 'tool', 'function'):
        value = entity.get(key)
        if isinstance(value, dict):
            value = value.get('name')
        if isinstance(value, str) and value.strip():
            name = value.strip()
            break
    args = _args(entity)
    for _ in range(2):
        if name.lower().split('.')[-1] not in WRAPPERS:
            break
        inner = next((args.get(k) for k in ('tool_name', 'toolName', 'function', 'name') if isinstance(args.get(k), str)), '')
        if not inner:
            break
        nested = next((args.get(k) for k in ('arguments', 'toolArguments', 'args', 'input') if isinstance(args.get(k), dict)), {})
        name, args = inner, nested
    return name, args


def step_words(name):
    low = (name or '').lower().split('.')[-1]
    for words, keys in KIND_WORDS:
        if any(k in low for k in keys):
            return words
    return 'Using a tool'


def _short(text, limit):
    text = ' '.join(str(text or '').split())
    return text if len(text) <= limit else text[:limit - 1] + '\u2026'


def step_target(args):
    for key in ('path', 'file', 'file_path', 'filePath', 'filename', 'target', 'dir', 'directory'):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return ntpath.basename(value.strip().rstrip('/\\')) or value.strip()
    files = args.get('files') or args.get('paths')
    if isinstance(files, list) and files and isinstance(files[0], str):
        first = ntpath.basename(files[0].rstrip('/\\')) or files[0]
        return first + (' +%d' % (len(files) - 1) if len(files) > 1 else '')
    for key in ('command', 'cmd', 'script', 'code', 'query', 'pattern', 'q'):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return _short(value, TARGET)
    return ''


def own_turn(transcript, task):
    """Entities of this task's own turn, oldest first (marker logic as notion.recover; never another turn)."""
    entities = sorted((p.get('entity') or {} for p in (transcript or {}).get('patches', []) if p.get('op') == 'put'),
                      key=lambda e: e.get('sequence', 0))
    before = task.get('before_seq', 0) or 0
    fresh = [e for e in entities if (e.get('sequence', 0) or 0) > before]
    step = task.get('step_id')
    resumes = set(task.get('resume_steps') or [])
    markers = [e for e in fresh if role(e) == 'user' and step and step in (e.get('id'), e.get('client_message_id'))]
    if markers:
        after = markers[0].get('sequence', 0)
    else:
        users = [e for e in fresh if role(e) == 'user' and not ({e.get('id'), e.get('client_message_id')} & resumes)]
        after = users[-1].get('sequence', 0) if users else before
    until = min([e.get('sequence', 0) for e in fresh if role(e) == 'user' and e.get('sequence', 0) > after
                 and not ({e.get('id'), e.get('client_message_id')} & resumes)], default=float('inf'))
    return [e for e in fresh if after < e.get('sequence', 0) < until]


def estimate(turn, completed):
    """{'percent', 'source', 'phase', 'label'}: 'Step x of y', a checklist, else the phase."""
    answers = [text_of(e.get('content', e.get('text', ''))) for e in turn if role(e) == 'answer']
    joined = '\n'.join(answers)
    steps = STEP_RE.findall(joined)
    if steps:
        x, y = (int(v) for v in steps[-1])
        if 0 < y and 0 <= x <= y:
            pct = 100 if completed else min(99, round(100 * x / y))
            return dict(percent=pct, source='steps', phase='', label='Step %d of %d' % (x, y))
    for text in reversed(answers):
        done, open_ = len(DONE_BOX.findall(text)), len(OPEN_BOX.findall(text))
        if done + open_ >= 2:
            pct = 100 if completed else min(99, round(100 * done / (done + open_)))
            return dict(percent=pct, source='checklist', phase='', label='%d of %d checked' % (done, done + open_))
    phase = 'starting'
    wrote = False
    for entity in turn:
        kind = role(entity)
        if kind == 'tool':
            name, args = tool_call(entity)
            words = step_words(name)
            if words == 'Writing file':
                wrote, phase = True, 'editing'
            elif words == 'Running command' and (wrote or TEST_WORDS.search(step_target(args) + ' ' + name)):
                phase = 'testing'
            elif phase == 'starting' or phase == 'answering':
                phase = 'exploring' if not wrote else 'editing'
        elif kind == 'thinking' and phase == 'starting':
            phase = 'exploring'
        elif kind == 'answer':
            phase = 'answering'
    if completed:
        phase = 'done'
    return dict(percent=PHASES[phase], source='phase', phase=phase, label=phase.capitalize())


def summarize(transcript, task):
    """Small progress dict of one task's own turn (no clock, no state)."""
    turn = own_turn(transcript, task)
    steps = [e for e in turn if role(e) in ('tool', 'thinking', 'answer')]
    session = (transcript or {}).get('session') or {}
    completed = any(_kind(e) == 'turn_completed' for e in turn) or (
        bool(steps) and session.get('status') in ('idle', 'completed') and not (transcript or {}).get('session_pending'))
    step, target = 'Starting', ''
    if steps:
        last = steps[-1]
        kind = role(last)
        if kind == 'tool':
            name, args = tool_call(last)
            step, target = step_words(name), step_target(args)
        else:
            step = 'Thinking' if kind == 'thinking' else 'Writing answer'
    answers = [text_of(e.get('content', e.get('text', ''))) for e in turn if role(e) == 'answer']
    snippet = ' '.join((answers[-1] if answers else '').split())
    if len(snippet) > SNIPPET:
        snippet = '\u2026' + snippet[-(SNIPPET - 1):]
    stamps = [stamp(e) for e in turn]
    return dict(steps=len(steps), step=step, target=target, snippet=snippet,
                last_activity=max(stamps) if stamps else 0.0, completed=completed,
                progress=estimate(turn, completed))


class ProgressFeed:
    def __init__(self, store, adapter, clock=time.time, poll_s=POLL_S, max_per_minute=MAX_PER_MINUTE):
        self.store = store
        self.adapter = adapter
        self.clock = clock
        self.poll_s = poll_s
        self.max_per_minute = max_per_minute
        self.lock = threading.Lock()
        self.entries = {}
        self.starts = deque()
        self.stop_event = threading.Event()
        self.thread = None

    # --- start/stop -------------------------------------------------
    def start(self):
        if self.thread and self.thread.is_alive():
            return self
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True, name='progress-feed')
        self.thread.start()
        return self

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(2)
        self.thread = None

    def _loop(self):
        while not self.stop_event.wait(TICK_S):
            try:
                self.tick()
            except Exception:
                pass  # report only; the next tick tries again

    # --- scheduling -------------------------------------------------
    def busy(self):
        """{task_id: (task, worker, thread_id)} for Coworker tasks whose request left (sent_at)."""
        found = {}
        for task in self.store.all('tasks'):
            if task.get('state') not in ACTIVE or not task.get('worker_id') or not task.get('sent_at'):
                continue
            worker = self.store.get('workers', task['worker_id']) or {}
            if worker.get('purpose', 'worker') != 'worker' or not worker.get('account_id'):
                continue
            thread = str(task.get('thread_id') or worker.get('thread_id') or '')
            if thread:
                found[task['id']] = (task, worker, thread)
        return found

    def _budget_free(self):
        status = getattr(getattr(self.adapter, 'network_budget', None), 'status', None)
        if not callable(status):
            return True
        try:
            info = status() or {}
        except Exception:
            return False
        return not info.get('paused') and not (info.get('cooldown_seconds') or 0) > 0

    def tick(self):
        """One scheduling step: drop ended tasks, stagger new ones, read at most one due task. Never raises."""
        now = self.clock()
        busy = self.busy()
        with self.lock:
            for ident in [i for i in self.entries if i not in busy]:
                del self.entries[ident]  # the task ended: stop reading its chat
            new = [i for i in busy if i not in self.entries]
            gap = self.poll_s / max(1, len(busy))
            for n, ident in enumerate(new):
                task, worker, _ = busy[ident]
                self.entries[ident] = dict(task_id=ident, worker_id=worker['id'], steps=0, step='Starting', target='',
                                           snippet='', last_activity=0.0, completed=False, stalled=False, stalled_s=0,
                                           progress=estimate([], False), polled_at=0.0, error='',
                                           next_at=now + n * gap, changed_at=float(task.get('sent_at') or now))
            while self.starts and self.starts[0] <= now - 60:
                self.starts.popleft()
            due = sorted((e['next_at'], i) for i, e in self.entries.items() if e['next_at'] <= now)
            if not due or len(self.starts) >= self.max_per_minute:
                self._refresh_stall(now)
                return None
            ident = due[0][1]
        if not self._budget_free():
            return None
        task, worker, thread = busy[ident]
        with self.lock:
            self.starts.append(now)
        try:
            from .network_budget import request_priority
            with request_priority('background'):
                transcript = self.adapter.client(worker['account_id']).transcript(thread, limit=WINDOW)
            info, error = summarize(transcript, task), ''
        except Exception as exc:
            info, error = None, type(exc).__name__
        with self.lock:
            entry = self.entries.get(ident)
            if entry is None:
                return ident
            entry['polled_at'] = now
            entry['error'] = error
            if info is None:
                entry['next_at'] = now + ERROR_BACKOFF_S
            else:
                if info['steps'] > entry['steps'] or info['completed'] != entry['completed']:
                    entry['changed_at'] = max(now if info['steps'] > entry['steps'] else entry['changed_at'], info['last_activity'])
                entry.update(info)
                entry['next_at'] = now + self.poll_s
            self._refresh_stall(now)
        return ident

    def _refresh_stall(self, now):
        for entry in self.entries.values():
            quiet = max(0.0, now - max(entry['changed_at'], entry['last_activity']))
            entry['stalled'] = not entry['completed'] and quiet >= STALL_S
            entry['stalled_s'] = int(quiet) if entry['stalled'] else 0

    # --- payload ----------------------------------------------------
    PUBLIC = ('task_id', 'worker_id', 'steps', 'step', 'target', 'snippet', 'last_activity', 'progress',
              'stalled', 'stalled_s', 'completed', 'polled_at', 'error')

    def payload(self, task_id):
        """Small dict for state.progress[task_id], or None when the task is not busy."""
        with self.lock:
            entry = self.entries.get(task_id)
            return {k: (dict(entry[k]) if k == 'progress' else entry[k]) for k in self.PUBLIC} if entry else None

    def public(self):
        """{task_id: payload} for state.progress and GET /api/progress."""
        with self.lock:
            ids = list(self.entries)
        return {i: p for i, p in ((i, self.payload(i)) for i in ids) if p}
