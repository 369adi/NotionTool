"""Fix loop controller for Phase 6 (pure state machine, no I/O, NOT WIRED into engine.py).

new_state(limits=None) -> state
decide(state, verdict, now=None) -> (action, reason, new_state)

action is one of:

* 'done'     - verdict.passed is True.
* 'fix'      - the verdict failed and another round is allowed. new_state['fixes'] maps
               task_id -> [{criterion_id, detail}] for the affected tasks only. Tasks
               that passed (no defect, or listed in verdict['passed_tasks']) never appear.
* 'escalate' - the problem cannot be solved automatically: a limit is used up while the
               verdict still fails (max_iterations, time_budget_s, request_budget), there
               was no improvement for max_rounds_without_improvement rounds in a row, or
               the failing verdict has no defect that names a task.
* 'stop'     - the loop cannot go on for a reason that is not the code's fault: the
               verdict is malformed, or decide() is called again on a finished state.

Stop-vs-escalate mapping used (unchanged from batch 1): every used-up limit and the
no-improvement rule map to 'escalate'; only a malformed verdict or a call on a finished
state maps to 'stop'.

Improvement is measured against the best round so far. A round counts as an
improvement when at least one of these holds:

* fewer failed criteria in total than the best round so far;
* fewer failed criteria for some task than that task's best count so far (a task that
  no longer has any defect counts as 0);
* the set of passed criteria grows: passed = criterion ids from verdict['checks_run']
  (strings 'method:criterion_id' or dicts with criterion_id) minus the failed ids. Any
  passed id that was not passed in an earlier round counts as growth.

The last rule matters because the verifier stops at the first failing required check:
when a fix exposes the next failing check, the failed count stays at 1 while the passed
set grows. The first failing round sets the baseline. After
max_rounds_without_improvement (default 2) rounds in a row without improvement the loop
escalates. requests are counted from verdict['cost'] (non-negative numbers only). Time
is only checked when now is a number; with now=None the time budget is not checked, and
max_iterations still bounds the loop. The input state and verdict are never mutated.
"""
import copy

ACTIONS = ('fix', 'done', 'stop', 'escalate')
TERMINAL = ('done', 'stop', 'escalate')
DEFAULT_LIMITS = {
    'max_iterations': 5,
    'time_budget_s': 1800,
    'request_budget': 50,
    'max_rounds_without_improvement': 2,
}


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value:  # NaN
        return None
    return value


def normalise_limits(limits=None):
    out = dict(DEFAULT_LIMITS)
    raw = limits if isinstance(limits, dict) else {}
    for key in ('max_iterations', 'max_rounds_without_improvement'):
        value = raw.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
            out[key] = value
    for key in ('time_budget_s', 'request_budget'):
        value = _number(raw.get(key))
        if value is not None and value > 0:
            out[key] = value
    return out


def new_state(limits=None):
    return {
        'limits': normalise_limits(limits),
        'iteration': 0,
        'started_at': None,
        'elapsed_s': None,
        'requests_used': 0,
        'best_failed': None,
        'best_failed_by_task': {},
        'best_passed': [],
        'rounds_without_improvement': 0,
        'fixes': {},
        'history': [],
        'last_action': None,
        'last_reason': '',
    }


def _failed_ids(verdict):
    failed = verdict.get('failed_criteria')
    ids = set(str(item) for item in failed) if isinstance(failed, (list, tuple)) else set()
    defects = verdict.get('defects')
    if isinstance(defects, (list, tuple)):
        for item in defects:
            if isinstance(item, dict) and item.get('criterion_id') not in (None, ''):
                ids.add(str(item['criterion_id']))
    return ids


def _failed_count(verdict):
    return len(_failed_ids(verdict))


def _failed_by_task(verdict):
    """Return {task_id: number of distinct failed criterion ids} from the defects."""
    per_task = {}
    defects = verdict.get('defects')
    for item in defects if isinstance(defects, (list, tuple)) else []:
        if not isinstance(item, dict) or item.get('task_id') in (None, ''):
            continue
        per_task.setdefault(str(item['task_id']), set()).add(str(item.get('criterion_id') or ''))
    return {task: len(ids) for task, ids in per_task.items()}


def passed_ids(verdict, failed=None):
    """Criterion ids from checks_run minus the failed ids."""
    failed = _failed_ids(verdict) if failed is None else failed
    ran = set()
    checks = verdict.get('checks_run')
    for item in checks if isinstance(checks, (list, tuple)) else []:
        cid = None
        if isinstance(item, str) and item:
            cid = item.split(':', 1)[1] if ':' in item else item
        elif isinstance(item, dict) and item.get('criterion_id') not in (None, ''):
            cid = str(item['criterion_id'])
        if cid:
            ran.add(cid)
    return ran - set(failed)


def group_defects(verdict):
    """Return {task_id: [{criterion_id, detail}]} for failing tasks only."""
    passed_tasks = verdict.get('passed_tasks')
    skip = set(str(t) for t in passed_tasks) if isinstance(passed_tasks, (list, tuple)) else set()
    grouped = {}
    defects = verdict.get('defects')
    for item in defects if isinstance(defects, (list, tuple)) else []:
        if not isinstance(item, dict):
            continue
        task_id = item.get('task_id')
        if task_id in (None, '') or str(task_id) in skip:
            continue
        entry = {'criterion_id': str(item.get('criterion_id') or ''), 'detail': str(item.get('detail') or '')}
        bucket = grouped.setdefault(str(task_id), [])
        if entry not in bucket:
            bucket.append(entry)
    return grouped


def _measure_improvement(st, verdict):
    """Update the best-so-far fields in st; return (failed, improvement text or '')."""
    failed_ids = _failed_ids(verdict)
    failed = len(failed_ids)
    by_task = _failed_by_task(verdict)
    passed = passed_ids(verdict, failed_ids)
    best = _number(st.get('best_failed'))
    best_tasks = st.get('best_failed_by_task') if isinstance(st.get('best_failed_by_task'), dict) else {}
    seen = set(str(x) for x in st.get('best_passed') or [] if isinstance(st.get('best_passed'), list))

    notes = []
    if best is None:
        notes.append('baseline round')
    else:
        if failed < best:
            notes.append(f'failed criteria {best} -> {failed}')
        better = sorted(t for t, b in best_tasks.items()
                        if _number(b) is not None and by_task.get(t, 0) < b)
        if better:
            notes.append('fewer failures for ' + ', '.join(better))
        grown = sorted(passed - seen)
        if grown:
            notes.append('newly passed ' + ', '.join(grown))

    st['best_failed'] = failed if best is None else min(best, failed)
    merged = {}
    for task in set(best_tasks) | set(by_task):
        old = _number(best_tasks.get(task))
        now_count = by_task.get(task, 0)
        merged[task] = now_count if old is None else min(old, now_count)
    st['best_failed_by_task'] = merged
    st['best_passed'] = sorted(seen | passed)
    return failed, '; '.join(notes)


def _finish(state, action, reason, failed=None, cost=0):
    state['last_action'] = action
    state['last_reason'] = reason
    if action != 'fix':
        state['fixes'] = {}
    state['history'].append({'iteration': state['iteration'], 'action': action,
                             'failed': failed, 'cost': cost, 'reason': reason})
    return action, reason, state


def decide(state, verdict, now=None):
    base = state if isinstance(state, dict) else new_state()
    st = copy.deepcopy(base)
    fresh = new_state(st.get('limits'))
    for key, value in fresh.items():
        st.setdefault(key, value)
    st['limits'] = normalise_limits(st.get('limits'))
    if not isinstance(st.get('history'), list):
        st['history'] = []
    limits = st['limits']

    if st.get('last_action') in TERMINAL:
        return _finish(st, 'stop', f"loop already finished with {st['last_action']}")

    if not isinstance(verdict, dict) or not isinstance(verdict.get('passed'), bool):
        return _finish(st, 'stop', 'verdict is malformed (no boolean passed flag)')

    st['iteration'] += 1
    cost = _number(verdict.get('cost'))
    cost = cost if cost is not None and cost >= 0 else 0
    st['requests_used'] += cost

    moment = _number(now)
    if moment is not None:
        if _number(st.get('started_at')) is None:
            st['started_at'] = moment
        elapsed = moment - st['started_at']
        if elapsed >= 0:
            st['elapsed_s'] = elapsed

    if verdict['passed']:
        return _finish(st, 'done', 'verdict passed', 0, cost)

    failed, improvement = _measure_improvement(st, verdict)
    if improvement:
        st['rounds_without_improvement'] = 0
    else:
        st['rounds_without_improvement'] += 1

    if st['rounds_without_improvement'] >= limits['max_rounds_without_improvement']:
        return _finish(st, 'escalate', f"no improvement for {st['rounds_without_improvement']} rounds "
                       f"(best {st['best_failed']} failed criteria, now {failed})", failed, cost)
    if st['requests_used'] >= limits['request_budget']:
        return _finish(st, 'escalate', f"request budget used up ({st['requests_used']}/{limits['request_budget']}) "
                       f"with {failed} failed criteria", failed, cost)
    if st['elapsed_s'] is not None and st['elapsed_s'] >= limits['time_budget_s']:
        return _finish(st, 'escalate', f"time budget used up ({st['elapsed_s']}s/{limits['time_budget_s']}s) "
                       f"with {failed} failed criteria", failed, cost)
    if st['iteration'] >= limits['max_iterations']:
        return _finish(st, 'escalate', f"max iterations reached ({st['iteration']}/{limits['max_iterations']}) "
                       f"with {failed} failed criteria", failed, cost)

    fixes = group_defects(verdict)
    if not fixes:
        return _finish(st, 'escalate', 'verdict failed but no defect names a task to fix', failed, cost)
    st['fixes'] = fixes
    tasks = ', '.join(sorted(fixes))
    note = f" ({improvement})" if improvement else ' (no improvement)'
    return _finish(st, 'fix', f"round {st['iteration']}: {failed} failed criteria, send defects to {tasks}{note}",
                   failed, cost)
