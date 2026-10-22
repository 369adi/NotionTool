"""Read-only report of native context-compression tasks stuck in state 'checkpoint'.

ChatContext (chat_context.py) stores a task with run_id '__context__', role 'context_compactor',
title 'Compress native context', state 'checkpoint' and lease '' before it compresses a worker's
native context. cleanup_blocker skips '__context__' tasks, and nothing else ends them when the
compression never finishes, so they can stay in 'checkpoint' forever.

stale_context_tasks only reports such tasks; it never writes, deletes or changes a record.

Auto cleanup (owner rule 2026-10-22: stale or broken runs and tasks end without asking the owner)
------------------------------------------------------------------------------------------------
Live example 22 Oct 2026: the 'Resolve conflict: task/...' tasks t914d1f2, t645508aa and t22b5817 stayed
queued for two days in runs r4c50e0e, ree80b0a2 and r82c79803 that were already done. Engine.cancel does not
fit: it would turn a done run into a cancelled one. sweep() ends such records instead; the coordinator loop
starts it every auto_cleanup_seconds (Engine._auto_cleanup_maybe). Every action writes one store event
'auto_cleanup' with run_id, task_id, rule and reason:
  (a) A queued/retry task of a run that is done, cancelled or stale is cancelled. The run state is unchanged.
  (b) A 'Resolve conflict' follow-up (queued, retry or blocked) is cancelled when its run is finished or
      missing, its worktree folder is gone, or git says its branch is gone. The run state is unchanged.
  (c) A queued/retry task that has waited longer than stale_queued_hours (default 6) on a run that cannot
      progress (run missing, blocked, failed or awaiting_main) is cancelled. Paused runs belong to rule (e).
  (d) An uncertain task whose Notion turn finished (remote_done_at) more than 30 min ago, or for which
      Engine._unsent_proof proves the prompt never arrived, is abandoned through Engine.resolve_uncertain.
      Without such proof the task is left alone.
  (e) A blocked or paused run without a live task and without any update for stale_run_hours (default 24)
      moves to the final state 'cancelled', marked stale=True (stale_from, stale_at, stale_reason), so the chat
      list shows no active run and the live stream sends run_finished (server.py already treats 'cancelled'
      as finished). It stays in the history and is never deleted. Its queued, retry and
      blocked tasks are cancelled and leased coworker threads are released, so it holds no files any more.
Never touched: running, cancelling and sending tasks, tasks with an unconfirmed remote turn (remote_unknown),
tasks of a running reconciliation, and Main or operator workers. Nothing is ever sent to Notion (rule I5):
the sweep changes local records only; rule (d) at most reads the thread record inside resolve_uncertain.
cleanup_plan() is read-only. `python -m notiontool.stale_tasks --db data/notiontool.db` prints a dry run
against a database opened with mode=ro.
"""
import contextlib
import json
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

CONTEXT_RUN = '__context__'
STATE = 'checkpoint'
MAX_AGE = 6 * 3600
TIME_FIELDS = ('sent_at', 'started', 'created')


def _stamp(task):
    """First positive time of sent_at, started, created (0 when none is set)."""
    for field in TIME_FIELDS:
        try:
            value = float(task.get(field) or 0)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            return value
    return 0.0


def _lease_matches(worker, task):
    """True only when the worker holds a non-empty lease equal to the task lease."""
    lease = worker.get('lease') or ''
    return bool(lease) and lease == (task.get('lease') or '')


def stale_context_tasks(store, now=None, max_age=MAX_AGE):
    """Tasks of run '__context__' in state 'checkpoint' that are older than max_age seconds and whose
    worker is missing, or ready without a lease that matches the task lease.

    Returns dicts (id, state, title, worker_id, age_seconds, worker_state), oldest first.
    worker_state is 'missing' when the worker record does not exist. Tasks without any time are
    skipped because their age is unknown. Pure and read-only.
    """
    now = time.time() if now is None else now
    result = []
    for task in store.all('tasks'):
        if task.get('run_id') != CONTEXT_RUN or task.get('state') != STATE:
            continue
        stamp = _stamp(task)
        if not stamp or now - stamp <= max_age:
            continue
        worker_id = task.get('worker_id') or ''
        worker = store.get('workers', worker_id) if worker_id else None
        if worker:
            if worker.get('state') != 'ready' or _lease_matches(worker, task):
                continue
            worker_state = worker.get('state', '')
        else:
            worker_state = 'missing'
        result.append(dict(id=task.get('id', ''), state=task.get('state', ''), title=task.get('title', ''),
                           worker_id=worker_id, age_seconds=int(now - stamp), worker_state=worker_state))
    result.sort(key=lambda item: -item['age_seconds'])
    return result


# ---- Auto cleanup (owner rule 2026-10-22) ----------------------------------------------------------
CLEANUP_EVENT = 'auto_cleanup'
# Rule (e) final run state. 'cancelled' (allowed by the owner rule: "'stale' or cancelled") because server.py
# already treats it as finished: the chat list picks no active run and the live stream sends run_finished.
# The run is marked stale=True with stale_from, stale_at and stale_reason, so history shows why it ended.
STALE_RUN = 'cancelled'
FINISHED_RUNS = ('done', 'cancelled', 'stale')  # 'stale': records written by an earlier build of this sweep
STUCK_RUNS = ('blocked', 'failed', 'awaiting_main')
STALE_RUN_STATES = ('blocked', 'paused')
WAITING = ('queued', 'retry')
SENDING = ('running', 'cancelling', 'sending')
LIVE = SENDING + ('uncertain', 'checkpoint')
PROTECTED_PURPOSES = ('main', 'operator')
STALE_QUEUED_HOURS = 6
STALE_RUN_HOURS = 24
REMOTE_DONE_GRACE = 30 * 60
PROOF_MIN_AGE = 30 * 60   # ask for an unsent proof only this long after the send
PROOF_RECHECK = 30 * 60   # at most one thread-record read per task in this window
CLEANUP_INTERVAL = 120    # seconds between sweeps (setting auto_cleanup_seconds)
CLEANUP_FIRST_DELAY = 60  # first sweep after start: restart recovery settles first
RUN_TIMES = ('updated', 'created', 'confirmed_at', 'resolved_at')
TASK_TIMES = ('created', 'started', 'finished', 'sent_at', 'resolved_at', 'remote_done_at', 'ready_at', 'next_at')
NO_SEND = 'Nothing was sent to Notion.'


def _num(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _hours(settings, key, default):
    try:
        value = float((settings or {}).get(key, default))
    except (TypeError, ValueError, AttributeError):
        return float(default)
    return max(0.1, min(24 * 365, value))


def is_resolve_conflict(task):
    """The follow-up that Engine._patch_result adds for a merge conflict (patch_mode.follow_up_task)."""
    return bool(task.get('patch_origin')) or str(task.get('title') or '').startswith('Resolve conflict')


def _untouchable(task):
    """Running, cancelling or sending tasks, and any non-uncertain task with an unconfirmed remote turn."""
    if task.get('state') in SENDING:
        return True
    return bool(task.get('remote_unknown')) and task.get('state') != 'uncertain'


def _live(task):
    return task.get('state') in LIVE or bool(task.get('remote_unknown'))


def _waiting_since(task):
    return max(_num(task.get(f)) for f in ('created', 'ready_at', 'next_at', 'finished', 'started'))


def _activity(run, tasks):
    times = [_num(run.get(f)) for f in RUN_TIMES]
    for task in tasks:
        times += [_num(task.get(f)) for f in TASK_TIMES]
    return max(times)


def _path_exists(path):
    try:
        return Path(path).exists()
    except (OSError, ValueError):
        return True  # unknown: keep the task


def git_branch_exists(project, branch):
    """True or False from git rev-parse (read-only); None when unknown (no project folder, no git, other error)."""
    if not project or not branch or not Path(project).is_dir():
        return None
    try:
        out = subprocess.run(['git', '-C', str(project), 'rev-parse', '--verify', '--quiet', 'refs/heads/' + branch],
                             capture_output=True, text=True, timeout=15,
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except Exception:
        return None
    if out.returncode == 0:
        return True
    if out.returncode == 1:
        return False
    return None


def event_time(store, run_id):
    """Newest event of the run except our own auto_cleanup events (0 when none or unknown)."""
    db = getattr(store, 'db', None)
    if db is None or not run_id:
        return 0.0
    lock = getattr(store, 'lock', None) or contextlib.nullcontext()
    try:
        with lock:
            row = db.execute('SELECT MAX(ts) FROM events WHERE run_id=? AND kind<>?', (run_id, CLEANUP_EVENT)).fetchone()
    except Exception:
        return 0.0
    return _num(row[0] if row else 0)


def _action(rule, op, task, run_id, reason, run_state=''):
    task = task or {}
    return dict(rule=rule, op=op, run_id=run_id, task_id=task.get('id', ''), state=task.get('state', ''),
                lease=task.get('lease', '') or '', title=task.get('title', ''), run_state=run_state, reason=reason)


def _conflict_reason(task, run, worktree_exists, branch_exists):
    if not run:
        return 'its run %s no longer exists' % task.get('run_id', '')
    if run.get('state') in FINISHED_RUNS:
        return 'its run %s is already %s' % (run.get('id', ''), run.get('state'))
    folder = task.get('patch_worktree') or ''
    if folder and not worktree_exists(folder):
        return 'its worktree %s no longer exists' % folder
    branch = task.get('patch_branch') or ''
    if branch and branch_exists(run.get('project_path') or '', branch) is False:
        return 'its branch %s no longer exists' % branch
    return ''


def _stuck_reason(run, run_id):
    if not run:
        return 'run %s no longer exists' % run_id
    if run.get('state') in STUCK_RUNS:
        return 'run %s is %s' % (run_id, run.get('state'))
    return ''


def cleanup_plan(store, now=None, settings=None, inflight=(), worktree_exists=None, branch_exists=None,
                 last_event=None):
    """The actions the sweep would take, in order. Read-only: it never writes a record.

    op 'cancel' (rules a, b, c), 'stale' (rule e), 'resolve' (rule d with remote_done_at) or 'proof' (rule d,
    taken only when Engine._unsent_proof proves the prompt never arrived)."""
    now = time.time() if now is None else float(now)
    settings = store.settings() if settings is None else settings
    queued_age = _hours(settings, 'stale_queued_hours', STALE_QUEUED_HOURS) * 3600
    run_age = _hours(settings, 'stale_run_hours', STALE_RUN_HOURS) * 3600
    worktree_exists = worktree_exists or _path_exists
    branch_exists = branch_exists or git_branch_exists
    last_event = last_event or (lambda run_id: event_time(store, run_id))
    inflight = set(inflight or ())
    runs = {r.get('id'): r for r in store.all('runs')}
    tasks = store.all('tasks')
    workers = {w.get('id'): w for w in store.all('workers')}
    by_run = {}
    for task in tasks:
        by_run.setdefault(task.get('run_id'), []).append(task)
    actions = []
    for task in tasks:
        run_id = task.get('run_id', '')
        if run_id == CONTEXT_RUN or task.get('id') in inflight or _untouchable(task):
            continue
        run = runs.get(run_id)
        state = task.get('state')
        run_state = (run or {}).get('state', '')
        if state == 'uncertain':
            worker = workers.get(task.get('worker_id') or '') or {}
            if worker.get('purpose') in PROTECTED_PURPOSES:
                continue
            done_at = _num(task.get('remote_done_at'))
            sent = _num(task.get('sent_at'))
            if done_at > 0:
                if now - done_at > REMOTE_DONE_GRACE:
                    actions.append(_action('d', 'resolve', task, run_id, run_state=run_state, reason=(
                        'Notion confirmed the turn finished %d min ago, but its answer never reached the harness'
                        % ((now - done_at) // 60))))
            elif sent > 0 and now - sent > PROOF_MIN_AGE:
                actions.append(_action('d', 'proof', task, run_id, run_state=run_state,
                                       reason='Notion proves the prompt never arrived'))
            continue
        if is_resolve_conflict(task) and state in WAITING + ('blocked',):
            why = _conflict_reason(task, run, worktree_exists, branch_exists)
            if why:
                actions.append(_action('b', 'cancel', task, run_id, run_state=run_state, reason=(
                    'Auto cleanup: this Resolve conflict task is obsolete because %s. The run state stays as it is. %s'
                    % (why, NO_SEND))))
                continue
        if state not in WAITING:
            continue
        if run and run_state in FINISHED_RUNS:
            actions.append(_action('a', 'cancel', task, run_id, run_state=run_state, reason=(
                'Auto cleanup: run %s is already %s, so this %s task can never start. The run stays %s. %s'
                % (run_id, run_state, state, run_state, NO_SEND))))
            continue
        since = _waiting_since(task)
        why = _stuck_reason(run, run_id)
        if why and since > 0 and now - since > queued_age:
            actions.append(_action('c', 'cancel', task, run_id, run_state=run_state, reason=(
                'Auto cleanup: waited %.1f h (limit %.1f h) on a run that cannot progress: %s. %s'
                % ((now - since) / 3600, queued_age / 3600, why, NO_SEND))))
    for run_id, run in runs.items():
        state = run.get('state')
        if state not in STALE_RUN_STATES:
            continue
        run_tasks = by_run.get(run_id, [])
        if any(_live(t) or t.get('id') in inflight for t in run_tasks):
            continue
        last = max(_activity(run, run_tasks), last_event(run_id))
        if last <= 0 or now - last <= run_age:
            continue
        actions.append(_action('e', 'stale', None, run_id, run_state=state, reason=(
            'Auto cleanup: run was %s with no live task and no update for %.1f h (limit %.1f h); it is now cancelled as stale. '
            'It stays in the history; its waiting tasks are cancelled and its coworker threads are released. %s'
            % (state, (now - last) / 3600, run_age / 3600, NO_SEND))))
    return actions


def _cleanup_event(store, action, task_id=None, reason=None):
    reason = action['reason'] if reason is None else reason
    store.event(CLEANUP_EVENT, action['run_id'], task_id=action['task_id'] if task_id is None else task_id,
                rule=action['rule'], reason=reason, message=reason[:300])


def _release_lease(engine, task):
    """Free the coworker thread this task holds; Main and operator workers are never touched."""
    worker_id = task.get('worker_id') or ''
    worker = engine.store.get('workers', worker_id) if worker_id else None
    if worker and worker.get('purpose') not in PROTECTED_PURPOSES and _lease_matches(worker, task):
        engine._release(worker_id, task.get('lease', ''))


def _inflight(engine):
    return {info.get('task_id') for info in engine.inflight.values()}


def _cancel_task(engine, action, now, inflight):
    store = engine.store
    task = store.get('tasks', action['task_id'])
    if (not task or task.get('id') in inflight or _untouchable(task) or task.get('state') != action['state']
            or (task.get('lease') or '') != action['lease']):
        return False  # changed since the plan: left to the next sweep
    if action['rule'] == 'a' and (store.get('runs', action['run_id']) or {}).get('state') not in FINISHED_RUNS:
        return False
    _release_lease(engine, task)
    task.update(state='cancelled', error=action['reason'][:600], auto_cleanup=dict(rule=action['rule'], at=now))
    store.put('tasks', task)
    _cleanup_event(store, action)
    return True


def _stale_run(engine, action, now, inflight):
    store = engine.store
    run = store.get('runs', action['run_id'])
    if not run or run.get('state') != action['run_state']:
        return False
    tasks = [t for t in store.all('tasks') if t.get('run_id') == run['id']]
    if any(_live(t) or t.get('id') in inflight for t in tasks):
        return False
    for task in tasks:
        _release_lease(engine, task)
        if task.get('state') in WAITING + ('blocked',):
            why = ('Auto cleanup: cancelled because its run %s became stale (it was %s with no update for a long '
                   'time). %s' % (run['id'], action['run_state'], NO_SEND))
            task.update(state='cancelled', error=why, auto_cleanup=dict(rule='e', at=now))
            store.put('tasks', task)
            _cleanup_event(store, action, task_id=task['id'], reason=why)
    run.update(state=STALE_RUN, stale=True, stale_from=action['run_state'], stale_at=now, updated=now,
               stale_reason=action['reason'], error=run.get('error') or action['reason'][:600])
    store.put('runs', run)
    _cleanup_event(store, action, task_id='')
    store.event('stage_changed', run['id'], message=STALE_RUN + ' (stale)')
    return True


def sweep(engine, now=None, worktree_exists=None, branch_exists=None, check_proof=True):
    """Apply cleanup_plan() to the engine's store. Returns the actions that were taken.

    Local changes happen under engine.lock and are re-checked there. Rule (d) runs after that through
    Engine.resolve_uncertain, which reads Notion's thread record before it takes the lock and never sends."""
    store = engine.store
    now = engine.clock() if now is None else now
    with engine.lock:
        inflight = _inflight(engine)
    actions = cleanup_plan(store, now=now, inflight=inflight, worktree_exists=worktree_exists,
                           branch_exists=branch_exists)
    taken = []
    with engine.lock:
        inflight = _inflight(engine)
        for action in actions:
            if action['op'] == 'cancel' and _cancel_task(engine, action, now, inflight):
                taken.append(action)
            elif action['op'] == 'stale' and _stale_run(engine, action, now, inflight):
                taken.append(action)
    checked = engine.__dict__.setdefault('_auto_cleanup_proof_at', {})
    for action in actions:
        if action['op'] not in ('resolve', 'proof'):
            continue
        if action['op'] == 'proof':
            if not check_proof or now - checked.get(action['task_id'], now - PROOF_RECHECK) < PROOF_RECHECK:
                continue
            checked[action['task_id']] = now
        try:
            engine.resolve_uncertain(action['task_id'], 'abandon',
                                     note='Auto cleanup (owner rule 2026-10-22): ' + action['reason'] + '.')
        except ValueError:
            continue  # no proof, a reconciliation is running, or the task changed: left alone
        checked.pop(action['task_id'], None)
        _cleanup_event(store, action,
                       reason='Auto cleanup: abandoned without resending: ' + action['reason'] + '. ' + NO_SEND)
        taken.append(action)
    return taken


class ReadOnlyStore:
    """The part of Store that cleanup_plan() reads, on a database opened with mode=ro (dry run)."""
    def __init__(self, path):
        uri = Path(path).resolve().as_uri() + '?mode=ro'
        self.db = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self.lock = threading.RLock()

    def get(self, kind, ident, default=None):
        with self.lock:
            row = self.db.execute('SELECT doc FROM objects WHERE kind=? AND id=?', (kind, str(ident))).fetchone()
        return json.loads(row[0]) if row else default

    def all(self, kind):
        with self.lock:
            rows = self.db.execute('SELECT doc FROM objects WHERE kind=? ORDER BY rowid', (kind,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def settings(self):
        from .store import Store
        return Store.settings(self)

    def close(self):
        self.db.close()


def dry_run(db_path, now=None):
    """cleanup_plan() against a database opened read-only. No proof is read: 'proof' actions are candidates."""
    store = ReadOnlyStore(db_path)
    try:
        return cleanup_plan(store, now=now)
    finally:
        store.close()


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='Dry run of the auto cleanup rules (read-only).')
    parser.add_argument('--db', required=True, help='path to notiontool.db (opened with mode=ro)')
    args = parser.parse_args(argv)
    actions = dry_run(args.db)
    counts = {}
    for action in actions:
        counts[action['rule']] = counts.get(action['rule'], 0) + 1
    print('auto_cleanup dry run: %d action(s) %s' % (
        len(actions), ' '.join('%s=%d' % (rule, counts[rule]) for rule in sorted(counts))))
    labels = dict(cancel='cancel task', stale='run -> stale', resolve='abandon task',
                  proof='abandon only if Notion proves the prompt never arrived (read, no send)')
    for action in actions:
        print('(%s) %s | run %s [%s] | task %s [%s] %s | %s' % (
            action['rule'], labels[action['op']], action['run_id'], action['run_state'], action['task_id'] or '-',
            action['state'] or '-', action['title'][:50], action['reason']))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
