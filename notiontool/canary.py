"""Live backtest ("canary") for features that touch coworkers, runs or the UI.

Started only by the Main, on purpose, usually right after a restart:

    python -m notiontool.canary --suite roles|parallel|health [--timeout S] --port P --key K

or in the harness process: run_suite(engine, 'roles').

What it does:
- queues small REAL runs through the normal operator delegate path (operator_main.delegate or
  POST /api/operator/delegate), so real open coworkers do the work;
- waits with a timeout and polls the store (runs, tasks, workers, events);
- checks the outcome itself (stages, workers, fix rounds, review verdict, the sandbox files and a hidden
  test of the delivered code), not only that something finished;
- writes a short plain-language report to work/canary/report-<time>.md and returns it as JSON.

Rules it keeps:
- never resends a prompt; on timeout it cancels its own run once and reports;
- sandbox only: every file it asks for lives under <project>/work/canary/<suite>-<id>/ and that folder
  is removed afterwards (only the report stays);
- sends no chat itself, restarts nothing, never touches Notion pages.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SANDBOX = 'work/canary'
SUITES = ('roles', 'parallel', 'health')
MAX_PARALLEL = 10
POLL_S = 5.0
TIMEOUTS = {'roles': 1800.0, 'parallel': 1200.0, 'health': 60.0}
HEALTH_WINDOW_S = 30.0
HEARTBEAT_S = 3.0  # desktop_state_cache.HEARTBEAT_S
TERMINAL_RUN = frozenset({'done', 'failed', 'cancelled', 'blocked', 'awaiting_main'})
TERMINAL_TASK = frozenset({'done', 'failed', 'cancelled', 'blocked', 'skipped'})
ACTIVE_TASK = frozenset({'running', 'sending', 'uncertain', 'reviewing', 'cancelling', 'claimed'})
STAGES = ('Designing', 'Building', 'Reviewing')
ROLE_STAGE = {'planner': 'Designing', 'approach': 'Designing', 'executor': 'Building', 'fixer': 'Building',
              'tester': 'Reviewing', 'reviewer': 'Reviewing'}
TITLE_STAGE = (('canary design', 'Designing'), ('canary build', 'Building'), ('fix:', 'Building'),
               ('canary review', 'Reviewing'))
MAIN_PURPOSES = ('main', 'operator')
HANG_EVENTS = ('turn_interrupted', 'turn_resume_exhausted', 'task_stalled', 'stall_detected')
AGENT_EVENTS = ('agent_created', 'notion_agent_created')
RESULT_DONE = re.compile(r'^\s*RESULT:\s*DONE\s*$', re.M | re.I)
REVIEW_PASS = re.compile(r'REVIEW:\s*PASS', re.I)
REVIEW_FAIL = re.compile(r'REVIEW:\s*FAIL', re.I)


# ---- small helpers ---------------------------------------------------------------------------

def check(name, passed, detail, **numbers):
    return dict(name=name, passed=bool(passed), detail=str(detail), numbers=numbers)


def _num(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def started(task):
    return _num(task.get('started'))


def finished(task):
    return _num(task.get('finished'))


def text(task):
    return str((task or {}).get('result') or '')


def stage_of(task):
    """Designing / Building / Reviewing for a task, from the canary title first, else the pipeline role."""
    title = str(task.get('title') or '').strip().lower()
    for prefix, stage in TITLE_STAGE:
        if title.startswith(prefix):
            return stage
    return ROLE_STAGE.get(str(task.get('role') or '').lower(), '')


def peak_overlap(intervals):
    """Most intervals open at the same moment; an end at time t closes before a start at t opens."""
    marks = []
    for begin, end in intervals:
        if begin and end and end >= begin:
            marks += [(begin, 1), (end, -1)]
    marks.sort(key=lambda m: (m[0], m[1]))
    peak = current = 0
    for _, step in marks:
        current += step
        peak = max(peak, current)
    return peak


def counters_of(status):
    """Find the desktop_state_cache counters dict anywhere inside a status payload."""
    if isinstance(status, dict):
        if isinstance(status.get('counters'), dict) and ('pushes' in status['counters'] or 'heartbeats' in status['counters']):
            return status['counters']
        for value in status.values():
            found = counters_of(value)
            if found is not None:
                return found
    elif isinstance(status, list):
        for value in status:
            found = counters_of(value)
            if found is not None:
                return found
    return None


# ---- checks (pure; unit-tested in tests/test_canary.py) -------------------------------------

def check_roles(run, tasks, sandbox_test=None):
    run, tasks = run or {}, list(tasks or [])
    first = {}
    for task in sorted(tasks, key=lambda t: started(t) or float('inf')):
        stage = stage_of(task)
        if stage and started(task):
            first.setdefault(stage, started(task))
    seen = sorted(first, key=lambda s: first[s])
    out = [check('Stages in order', seen == list(STAGES),
                 'Seen: %s (expected Designing -> Building -> Reviewing)' % (' -> '.join(seen) or 'none'),
                 stages_seen=len(seen), stages_expected=len(STAGES))]

    builders = {t.get('worker_id') for t in tasks if stage_of(t) == 'Building' and t.get('worker_id')}
    reviewers = {t.get('worker_id') for t in tasks if stage_of(t) == 'Reviewing' and t.get('worker_id')}
    both = builders & reviewers
    out.append(check('Reviewer is another coworker', bool(builders and reviewers and not both),
                     '%d builder(s), %d reviewer(s), %d did both' % (len(builders), len(reviewers), len(both)),
                     builders=len(builders), reviewers=len(reviewers), both=len(both)))

    reviews = sorted([t for t in tasks if stage_of(t) == 'Reviewing'], key=lambda t: started(t) or float('inf'))
    fix_tasks = [t for t in tasks if str(t.get('title') or '').strip().lower().startswith('fix:')]
    failed_reviews = [t for t in reviews if REVIEW_FAIL.search(text(t))]
    rounds = max(int(_num(run.get('fixes'))), len(failed_reviews))
    first_failed = bool(reviews) and bool(REVIEW_FAIL.search(text(reviews[0])))
    out.append(check('Exactly one fix round', rounds == 1 and first_failed and bool(fix_tasks),
                     '%d fix round(s), %d fix task(s); the first review %s' % (
                         rounds, len(fix_tasks), 'found the open point' if first_failed else 'did not find the open point'),
                     fix_rounds=rounds, fix_tasks=len(fix_tasks), reviews=len(reviews)))

    last = reviews[-1] if reviews else {}
    verdict = 'pass' if REVIEW_PASS.search(text(last)) else ('fail' if REVIEW_FAIL.search(text(last)) else 'none')
    bad = run.get('state') in ('failed', 'blocked', 'cancelled')
    approved = bool(last) and last.get('state') == 'done' and verdict == 'pass' and not bad and sandbox_test is True
    out.append(check('Final result approved', approved,
                     'Last review: %s; request: %s; our own test of the code: %s' % (
                         verdict, run.get('state') or 'unknown',
                         {True: 'passed', False: 'failed', None: 'not run'}[sandbox_test]),
                     approved=int(approved)))
    return out


def check_parallel(run, tasks, expected, files, mains_before, mains_after, agents_before, agents_after, total_s):
    run, tasks = run or {}, list(tasks or [])
    capacity = int(_num(run.get('capacity'))) or expected
    goal = max(1, min(expected, capacity))
    done = [t for t in tasks if t.get('state') == 'done' and RESULT_DONE.search(text(t))]
    peak = peak_overlap([(started(t), finished(t)) for t in tasks])
    used = {t.get('worker_id') for t in tasks if t.get('worker_id')}
    out = [check('All tasks done', len(done) == expected and len(tasks) == expected,
                 '%d of %d tasks finished with RESULT: DONE' % (len(done), expected), done=len(done), expected=expected),
           check('Ran at the same time', peak >= goal,
                 'Peak %d at once (goal %d = coworkers %d, limit %d); %d coworker(s) used' % (
                     peak, goal, expected, capacity, len(used)),
                 peak=peak, goal=goal, coworkers_used=len(used)),
           check('Total time', len(done) == expected, 'Took %.0f s for %d task(s)' % (total_s, expected),
                 seconds=round(total_s, 1))]

    markers = {meta['marker'] for meta in files.values()}
    missing = [p for p, m in files.items() if m.get('content') is None or m['marker'] not in m['content']]
    mixed = [p for p, m in files.items() if m.get('content') and any(x in m['content'] for x in markers - {m['marker']})]
    claimed = [f for t in tasks for f in (t.get('files') or [])]
    shared = len(claimed) - len(set(claimed))
    out.append(check('No file conflicts', not missing and not mixed and shared == 0,
                     '%d file(s) missing or wrong, %d with another task\'s text, %d file(s) planned twice' % (
                         len(missing), len(mixed), shared),
                     missing=len(missing), mixed=len(mixed), shared=shared))
    out.append(check('No second Main', mains_after <= mains_before,
                     'Main chats before %d, after %d' % (mains_before, mains_after),
                     mains_before=mains_before, mains_after=mains_after))
    flagged = [t for t in tasks if t.get('agent_red_flag')]
    out.append(check('No Notion agent created', agents_after <= agents_before and not flagged,
                     'Agents before %d, after %d; %d task(s) flagged for agent use' % (agents_before, agents_after, len(flagged)),
                     agents_before=agents_before, agents_after=agents_after, flagged=len(flagged)))
    return out


def check_health(before, after, events, active_workers, window_s):
    b, a = counters_of(before), counters_of(after)
    if a is None or b is None:
        msg = 'Desktop app status not available (is the desktop app running?)'
        return [check('State pushes and heartbeats arrive', False, msg),
                check('No offline condition', False, msg),
                check('No false hang', False, msg)]

    def delta(key):
        return int(_num(a.get(key))) - int(_num(b.get(key)))

    pushes, beats = delta('pushes'), delta('heartbeats')
    need = max(1, int(window_s // HEARTBEAT_S) // 2)
    out = [check('State pushes and heartbeats arrive', pushes + beats >= need,
                 '%d state push(es) and %d heartbeat(s) in %.0f s (need at least %d)' % (pushes, beats, window_s, need),
                 pushes=pushes, heartbeats=beats, need=need)]
    timeouts, failures, dropped = delta('timeouts'), delta('failures'), delta('dropped')
    out.append(check('No offline condition', timeouts == 0 and failures == 0,
                     '%d push timeout(s), %d failed push(es), %d dropped' % (timeouts, failures, dropped),
                     timeouts=timeouts, failures=failures, dropped=dropped))
    hangs = [e for e in events or [] if e.get('kind') in HANG_EVENTS]
    false = [e for e in hangs if (e.get('worker_id') or '') not in set(active_workers or ())]
    out.append(check('No false hang', not false,
                     '%d hang report(s), %d for a coworker with no open task' % (len(hangs), len(false)),
                     hang_reports=len(hangs), false_hangs=len(false)))
    return out


# ---- access to the harness ------------------------------------------------------------------

class InProcess:
    """Uses the running engine directly (run_suite(engine, suite) inside the harness)."""
    canary_harness = True

    def __init__(self, engine, status=None):
        self.engine, self.store, self._status = engine, engine.store, status

    def delegate(self, goal, tasks, summary, request_id):
        from .operator_main import delegate
        return delegate(self.engine, '', goal, tasks, request_id, summary=summary)

    def run(self, run_id):
        return self.store.get('runs', run_id) or {}

    def tasks(self, run_id):
        if hasattr(self.engine, 'tasks'):
            return list(self.engine.tasks(run_id))
        return [t for t in self.store.all('tasks') if t.get('run_id') == run_id]

    def all(self, kind):
        try:
            return list(self.store.all(kind))
        except Exception:
            return []

    def events(self, after=0):
        return list(self.store.events(after, 500))

    def last_event(self):
        rows = self.store.events(0, 1)
        return rows[-1].get('seq', 0) if rows else 0

    def settings(self):
        return self.store.settings()

    def project_path(self):
        from .operator_main import operator_chat
        return (operator_chat(self.store) or {}).get('project_path') or ''

    def status(self):
        if callable(self._status):
            return self._status()
        for name in ('desk_stats', 'desktop_stats', 'stats'):
            stats = getattr(self.engine, name, None)
            if stats is not None and hasattr(stats, 'snapshot'):
                return stats.snapshot()
        return None

    def cancel(self, run_id):
        self.engine.cancel(run_id)


class HttpHarness:
    """Talks to the running harness over its local API (CLI mode). Needs the host API key."""
    canary_harness = True

    def __init__(self, port, key, project='', timeout=30.0):
        self.port, self.key, self.project, self.timeout = int(port), str(key), project, timeout

    def _call(self, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request('http://127.0.0.1:%d%s' % (self.port, path), data=data,
                                     method='GET' if body is None else 'POST',
                                     headers={'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read().decode('utf-8', 'replace')
        return json.loads(raw) if raw.strip() else None

    def state(self):
        return self._call('/api/state') or {}

    def delegate(self, goal, tasks, summary, request_id):
        return self._call('/api/operator/delegate', dict(goal=goal, tasks=tasks, summary=summary, request_id=request_id))

    def all(self, kind):
        value = self.state().get(kind)
        return list(value.values()) if isinstance(value, dict) else list(value or [])

    def run(self, run_id):
        return next((r for r in self.all('runs') if r.get('id') == run_id), {})

    def tasks(self, run_id):
        state = self.state()
        rows = [t for t in state.get('tasks') or [] if t.get('run_id') == run_id]
        if not rows:
            run = next((r for r in state.get('runs') or [] if r.get('id') == run_id), {})
            rows = list(run.get('tasks') or [])
        return rows

    def events(self, after=0):
        return [e for e in self.all('events') if _num(e.get('seq')) > after]

    def last_event(self):
        return max([int(_num(e.get('seq'))) for e in self.all('events')] or [0])

    def settings(self):
        return self.state().get('settings') or {}

    def project_path(self):
        if self.project:
            return self.project
        chat = next((c for c in self.all('chats') if c.get('operator')), {})
        return chat.get('project_path') or ''

    def status(self):
        for body in (None, {}):
            try:
                return self._call('/api/app/status', body)
            except (urllib.error.URLError, ValueError):
                continue
        return None

    def cancel(self, run_id):
        self._call('/api/run/cancel', {'id': run_id})


# ---- plans ----------------------------------------------------------------------------------

RESULT_LINE = 'End your answer with a final line RESULT: DONE.'
SANDBOX_RULE = 'Only create or change the files named here, inside %s. Do not touch any other file. Do not create Notion agents.'


def roles_plan(rel):
    design, calc, test = rel + '/design.md', rel + '/calc.py', rel + '/test_calc.py'
    points = ('1. calc.py has clamp(x, low, high) that returns x limited to the range low..high. '
              '2. test_calc.py tests a value inside, below and above the range with unittest. '
              '3. clamp raises ValueError when low > high, and test_calc.py tests that.')
    rule = SANDBOX_RULE % rel
    return [
        dict(id='design', title='Canary design: plan a clamp function',
             prompt='Write a short plan (max 10 lines) for a Python function clamp in %s. Acceptance points: %s %s %s'
                    % (design, points, rule, RESULT_LINE),
             acceptance='design.md lists the three acceptance points', files=[design],
             summary='Plan a tiny test function'),
        dict(id='build', title='Canary build: write clamp', depends_on=['design'], files=[calc, test],
             prompt=('Read %s and write %s and %s. Acceptance points: %s This is a backtest of the review step: in this '
                     'FIRST attempt leave point 3 open on purpose (no ValueError, no test for it). If you are later '
                     'asked to fix the review findings, complete point 3. %s %s') % (design, calc, test, points, rule, RESULT_LINE),
             acceptance='Points 1 and 2 done; point 3 left open on purpose in the first attempt',
             summary='Write the test function'),
        dict(id='review', title='Canary review: check clamp', depends_on=['build'], files=[],
             prompt=('You are the reviewer. Do not change any file. Check all three acceptance points by reading %s and %s '
                     'and running python -m unittest discover -s %s. Points: %s Write one line REVIEW: PASS if all three '
                     'are met, else REVIEW: FAIL: <open point>, and list the needed fix. %s') % (calc, test, rel, points, RESULT_LINE),
             acceptance='A REVIEW: PASS or REVIEW: FAIL line based on the actual files', summary='Check the test function'),
    ]


def parallel_plan(rel, n, token):
    tasks, files = [], {}
    for i in range(1, n + 1):
        path, marker = '%s/p%d.txt' % (rel, i), 'CANARY-%s-p%d' % (token, i)
        files[path] = dict(marker=marker)
        tasks.append(dict(id='p%d' % i, title='Canary parallel %d of %d' % (i, n), files=[path],
                          prompt='Create the file %s with exactly this one line: %s %s %s' % (
                              path, marker, SANDBOX_RULE % rel, RESULT_LINE),
                          acceptance='%s contains exactly the line %s' % (path, marker),
                          summary='Write test file %d of %d' % (i, n)))
    return tasks, files


HIDDEN_TEST = ('import sys; sys.path.insert(0, sys.argv[1]); import calc\n'
               'assert calc.clamp(5, 0, 3) == 3 and calc.clamp(-1, 0, 3) == 0 and calc.clamp(2, 0, 3) == 2\n'
               'try:\n    calc.clamp(1, 3, 0)\nexcept ValueError:\n    pass\nelse:\n    raise SystemExit("no ValueError")\n')


def sandbox_test(box):
    """Run the coworker's own tests and our hidden test in the sandbox. True only if both pass."""
    if not (box / 'calc.py').is_file():
        return False
    try:
        own = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', str(box), '-p', 'test_*.py'],
                             cwd=str(box), capture_output=True, timeout=120)
        hidden = subprocess.run([sys.executable, '-c', HIDDEN_TEST, str(box)], cwd=str(box), capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return own.returncode == 0 and hidden.returncode == 0


# ---- runner ---------------------------------------------------------------------------------

def _guard(path, sandbox_root):
    path, sandbox_root = Path(path).resolve(), Path(sandbox_root).resolve()
    if path != sandbox_root and sandbox_root not in path.parents:
        raise ValueError('Canary path outside the sandbox: %s' % path)
    return path


def _wait(h, run_id, timeout, sleep, clock):
    begin = clock()
    while True:
        run, tasks = h.run(run_id), h.tasks(run_id)
        if run.get('state') in TERMINAL_RUN and all(t.get('state') in TERMINAL_TASK for t in tasks):
            return run, tasks, False
        if clock() - begin >= timeout:
            # Never resend: stop our own request once and report what is there.
            try:
                h.cancel(run_id)
            except Exception:
                pass
            return h.run(run_id), h.tasks(run_id), True
        sleep(POLL_S)


def _operator_account(h):
    from .operator_main import OPERATOR
    workers = h.all('workers')
    op = next((w for w in workers if w.get('purpose') == OPERATOR), None)
    return (op or {}).get('account_id', ''), workers


def _mains(workers):
    return sum(1 for w in workers if w.get('purpose') in MAIN_PURPOSES and w.get('state') != 'released')


def _agents(h, after):
    return len(h.all('agents')) + sum(1 for e in h.events(after) if e.get('kind') in AGENT_EVENTS)


def _time_check(timed_out, seconds, timeout):
    return check('Finished in time', not timed_out, ('Stopped after %.0f s (limit %.0f s); nothing was resent'
                                                     if timed_out else 'Done in %.0f s (limit %.0f s)') % (seconds, timeout),
                 seconds=round(seconds, 1), limit=timeout)


def _roles(h, base, rel, box, token, timeout, sleep, clock):
    begin = clock()
    plan = roles_plan(rel)
    res = h.delegate('Canary backtest: roles (design, build, review, one fix round)', plan,
                     'Backtest of the review and fix steps', 'canary-roles-' + token)
    run, tasks, timed_out = _wait(h, res['run_id'], timeout, sleep, clock)
    return [_time_check(timed_out, clock() - begin, timeout)] + check_roles(run, tasks, sandbox_test(box)), res['run_id']


def _parallel(h, base, rel, box, token, timeout, sleep, clock):
    account, workers = _operator_account(h)
    n = min(MAX_PARALLEL, sum(1 for w in workers if w.get('purpose') == 'worker' and w.get('enabled')
                              and w.get('account_id') == account))
    if n < 1:
        return [check('Coworkers available', False, 'No enabled coworker in the active account', coworkers=0)], ''
    mains_before, after_seq = _mains(workers), h.last_event()
    agents_before = len(h.all('agents'))
    plan, files = parallel_plan(rel, n, token)
    begin = clock()
    res = h.delegate('Canary backtest: %d tasks at the same time' % n, plan, 'Backtest of parallel coworkers',
                     'canary-parallel-' + token)
    run, tasks, timed_out = _wait(h, res['run_id'], timeout, sleep, clock)
    total = clock() - begin
    for path, meta in files.items():
        target = _guard(base / path, base / SANDBOX)
        meta['content'] = target.read_text(encoding='utf-8', errors='replace') if target.is_file() else None
    checks = [_time_check(timed_out, total, timeout)] + check_parallel(
        run, tasks, n, files, mains_before, _mains(h.all('workers')), agents_before, _agents(h, after_seq), total)
    return checks, res['run_id']


def _health(h, window, sleep, clock):
    after_seq = h.last_event()
    before = h.status()
    active = {t.get('worker_id') for t in h.all('tasks') if t.get('state') in ACTIVE_TASK}
    sleep(window)
    after = h.status()
    active |= {t.get('worker_id') for t in h.all('tasks') if t.get('state') in ACTIVE_TASK}
    return check_health(before, after, h.events(after_seq), active, window), ''


def render_report(report):
    lines = ['# Canary backtest: %s' % report['suite'], '',
             '%s - %s: %d of %d checks passed - took %.0f s' % (
                 report['when'], 'PASSED' if report['passed'] else 'FAILED',
                 sum(c['passed'] for c in report['checks']), len(report['checks']), report['seconds']), '',
             '| Check | Result | Details |', '|---|---|---|']
    for c in report['checks']:
        lines.append('| %s | %s | %s |' % (c['name'], 'passed' if c['passed'] else 'FAILED', c['detail'].replace('|', '/')))
    lines += ['', 'Sandbox: %s (%s)' % (report['sandbox'], 'removed' if report['cleaned'] else 'NOT removed'), '']
    return '\n'.join(lines)


def run_suite(engine, suite, *, timeout=None, base_dir=None, sleep=time.sleep, clock=time.time,
              health_window=HEALTH_WINDOW_S, status=None):
    """Run one live backtest suite and return the report dict (also written as Markdown)."""
    if suite not in SUITES:
        raise ValueError('Unknown suite %r; use one of %s' % (suite, ', '.join(SUITES)))
    h = engine if getattr(engine, 'canary_harness', False) else InProcess(engine, status=status)
    timeout = float(timeout or TIMEOUTS[suite])
    begin, stamp = clock(), time.strftime('%Y%m%d-%H%M%S')
    token = stamp + '-' + uuid.uuid4().hex[:6]
    base = Path(base_dir or h.project_path() or ROOT).resolve()
    sandbox_root = base / SANDBOX
    rel = '%s/%s-%s' % (SANDBOX, suite, token)
    box = _guard(base / rel, sandbox_root)
    run_id = ''
    try:
        if suite != 'health':
            box.mkdir(parents=True, exist_ok=True)
        if suite == 'roles':
            checks, run_id = _roles(h, base, rel, box, token, timeout, sleep, clock)
        elif suite == 'parallel':
            checks, run_id = _parallel(h, base, rel, box, token, timeout, sleep, clock)
        else:
            checks, run_id = _health(h, min(health_window, timeout), sleep, clock)
    except Exception as exc:  # report, never retry
        checks = [check('Suite ran', False, 'Stopped: %s: %s' % (type(exc).__name__, exc))]
    cleaned = True
    if box.exists():
        shutil.rmtree(_guard(box, sandbox_root), ignore_errors=True)
        cleaned = not box.exists()
    report = dict(suite=suite, when=time.strftime('%Y-%m-%d %H:%M:%S'), passed=all(c['passed'] for c in checks) and bool(checks),
                  seconds=round(clock() - begin, 1), checks=checks, run_id=run_id, sandbox=rel, cleaned=cleaned)
    sandbox_root.mkdir(parents=True, exist_ok=True)
    path = _guard(sandbox_root / ('report-%s-%s.md' % (stamp, suite)), sandbox_root)
    with open(path, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(render_report(report))
    report['report_path'] = str(path)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m notiontool.canary',
                                     description='Live backtest with real open coworkers (start it on purpose only).')
    parser.add_argument('--suite', required=True, choices=SUITES)
    parser.add_argument('--timeout', type=float, default=None, help='seconds before the run is stopped (never resent)')
    parser.add_argument('--port', type=int, default=int(os.environ.get('NOTIONTOOL_PORT') or 0), help='harness API port')
    parser.add_argument('--key', default=os.environ.get('NOTIONTOOL_API_KEY', ''), help='host API key (Bearer)')
    parser.add_argument('--project', default='', help='project folder; default: the operator chat folder')
    args = parser.parse_args(argv)
    if not args.port or not args.key:
        print('Need --port and --key (or NOTIONTOOL_PORT / NOTIONTOOL_API_KEY): the harness API port and host API key.',
              file=sys.stderr)
        return 2
    report = run_suite(HttpHarness(args.port, args.key, project=args.project), args.suite, timeout=args.timeout,
                       base_dir=args.project or None)
    print(json.dumps(report, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
