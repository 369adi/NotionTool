"""Verifier cascade for Phase 6 (pure, no I/O, not wired into engine.py).

run_verifier(task_id, criteria, checks, risk, capture_verify=None, model_review=None)
runs the check for each criterion in a fixed order, cheapest first:

    tests -> static -> diff -> requirements -> screenshot -> model_review

* checks is a dict of injected callables keyed by method. Each callable gets a copy
  of the criterion dict and returns a bool or a dict {passed, detail?, cost?}.
* screenshot criteria go through the injected capture_verify callable (fallback:
  checks['screenshot']). Its result may use the capture_verify.verify() shape
  {changed, summary, ...}; then passed = changed == criterion.get('expect_change', True).
* model_review runs only when risk is 'high' and no required objective check failed.
  It gets a fresh read-only context. It can add a defect but never remove one, so a
  model pass never overrides an objective fail.
* The cascade stops at the first failing required objective check; the rest is
  listed in skipped. A failing optional criterion is a defect but does not stop.
* A missing check, an unknown method or a raising check is a defect, never a crash.

Verdict: {passed, failed_criteria, defects, checks_run, cost, skipped}.
The agreed verdict keys are passed, failed_criteria, defects, checks_run and cost.
skipped is an extra optional key: consumers must not require it, and a verdict
without it is still valid (merge_verdicts treats a missing skipped as []).
checks_run and skipped entries are 'method:criterion_id' in run order.
cost is the sum of each check's reported cost (default 1 per check run).

merge_verdicts(verdicts) combines per-task verdicts into one verdict for the fix
loop: {passed, failed_criteria, defects, checks_run, cost, skipped, passed_tasks}.
* verdicts is a dict {task_id: verdict}, or a list whose items are a verdict dict
  or a (task_id, verdict) pair. Without an explicit task id, the verdict's own
  'task_id' key or the task_id of its first defect is used.
* passed is True only when there is at least one verdict and all of them passed.
  An empty or invalid input gives passed False with one defect 'none' that names
  no task (the fix loop then escalates instead of fixing).
* failed_criteria, defects, checks_run and skipped are concatenated in input order;
  defects without a task_id get the verdict's task id. cost is the sum of the
  valid non-negative costs. passed_tasks lists the task ids whose verdict passed.
* A malformed verdict (not a dict, or no boolean passed) is a defect, never a crash.
  The input verdicts are never mutated.
"""
from types import MappingProxyType

OBJECTIVE_ORDER = ('tests', 'static', 'diff', 'requirements', 'screenshot')
ORDER = OBJECTIVE_ORDER + ('model_review',)
DEFAULT_COST = 1
MODEL_REVIEW_ID = 'model_review'


def freeze(value):
    """Deep read-only copy (used for the model review context)."""
    if isinstance(value, dict):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(freeze(item) for item in value)
    return value


def check_cost(result):
    if isinstance(result, dict):
        cost = result.get('cost')
        if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
            return cost
    return DEFAULT_COST


def interpret(method, criterion, result):
    """Return (passed, detail) for a raw check result."""
    if isinstance(result, bool):
        return result, '' if result else f'{method} check failed'
    if isinstance(result, dict):
        if 'passed' in result:
            passed = result.get('passed') is True
        elif method == 'screenshot' and 'changed' in result:
            passed = bool(result.get('changed')) == bool(criterion.get('expect_change', True))
        else:
            return False, f'{method} check returned no passed flag'
        detail = str(result.get('detail') or result.get('summary') or '')
        if not passed and not detail:
            detail = f'{method} check failed'
        return passed, detail
    return False, f'{method} check returned unsupported result {type(result).__name__}'


def _criterion_id(criterion, index):
    cid = criterion.get('id') if isinstance(criterion, dict) else None
    return str(cid) if cid not in (None, '') else f'c{index + 1}'


def _required(criterion):
    return criterion.get('required', True) is not False


def run_verifier(task_id, criteria, checks, risk, capture_verify=None, model_review=None):
    checks = checks or {}
    verdict = dict(passed=False, failed_criteria=[], defects=[], checks_run=[], cost=0, skipped=[])
    required_ids = set()

    def defect(cid, detail, required=True):
        if cid not in verdict['failed_criteria']:
            verdict['failed_criteria'].append(cid)
        if required:
            required_ids.add(cid)
        verdict['defects'].append(dict(criterion_id=cid, task_id=task_id, detail=detail))

    items = []
    for index, criterion in enumerate(criteria or []):
        cid = _criterion_id(criterion, index)
        if not isinstance(criterion, dict):
            defect(cid, f'criterion is not a dict ({type(criterion).__name__})')
            continue
        method = criterion.get('method')
        if method not in ORDER:
            defect(cid, f'unknown method {method!r}')
            continue
        items.append((ORDER.index(method), index, cid, criterion))
    if not items and not verdict['defects']:
        defect('none', 'no criteria given')
    if verdict['defects']:
        return verdict
    items.sort(key=lambda item: (item[0], item[1]))

    objective_results = []
    stopped = False
    for _, _, cid, criterion in items:
        method = criterion['method']
        if method == 'model_review':
            continue
        if stopped:
            verdict['skipped'].append(f'{method}:{cid}')
            continue
        check = capture_verify if method == 'screenshot' and callable(capture_verify) else checks.get(method)
        verdict['checks_run'].append(f'{method}:{cid}')
        result = None
        if not callable(check):
            passed, detail = False, f'no check registered for method {method!r}'
        else:
            try:
                result = check(dict(criterion))
            except Exception as error:  # a broken check is a failure, not a crash
                passed, detail = False, f'check raised {type(error).__name__}: {error}'
            else:
                passed, detail = interpret(method, criterion, result)
            verdict['cost'] += check_cost(result)
        objective_results.append(dict(criterion_id=cid, method=method, passed=passed, detail=detail))
        if not passed:
            defect(cid, detail, _required(criterion))
            if _required(criterion):
                stopped = True

    review_items = [(cid, c) for _, _, cid, c in items if c['method'] == 'model_review']
    high = str(risk).strip().lower() == 'high'
    if stopped or not high:
        verdict['skipped'].extend(f'model_review:{cid}' for cid, _ in review_items)
    elif not callable(model_review):
        for cid, criterion in review_items:
            verdict['skipped'].append(f'model_review:{cid}')
            defect(cid, 'risk is high but no model_review callable was given', _required(criterion))
    else:
        targets = review_items or [(MODEL_REVIEW_ID, dict(id=MODEL_REVIEW_ID, method='model_review', required=True))]
        for cid, criterion in targets:
            context = freeze(dict(task_id=task_id, criterion=criterion,
                                  criteria=[c for _, _, _, c in items],
                                  objective_results=objective_results))
            verdict['checks_run'].append(f'model_review:{cid}')
            result = None
            try:
                result = model_review(context)
            except Exception as error:
                passed, detail = False, f'model review raised {type(error).__name__}: {error}'
            else:
                passed, detail = interpret('model_review', criterion, result)
            verdict['cost'] += check_cost(result)
            if not passed:
                defect(cid, detail, _required(criterion))

    verdict['passed'] = not required_ids
    return verdict


def _text_id(value):
    return None if value in (None, '') else str(value)


def _task_of(verdict):
    """Task id from the verdict's own 'task_id' key or its first defect."""
    task_id = _text_id(verdict.get('task_id'))
    if task_id is not None:
        return task_id
    defects = verdict.get('defects')
    for item in defects if isinstance(defects, (list, tuple)) else []:
        if isinstance(item, dict) and _text_id(item.get('task_id')) is not None:
            return str(item['task_id'])
    return None


def _verdict_pairs(verdicts):
    """Normalise the merge input to a list of (task_id or None, verdict)."""
    if isinstance(verdicts, dict):
        if 'passed' in verdicts:
            return [(None, verdicts)]
        return [(_text_id(key), value) for key, value in verdicts.items()]
    if not isinstance(verdicts, (list, tuple)):
        return []
    pairs = []
    for item in verdicts:
        if isinstance(item, tuple) and len(item) == 2 and not isinstance(item[0], dict):
            pairs.append((_text_id(item[0]), item[1]))
        else:
            pairs.append((None, item))
    return pairs


def _list(value):
    return list(value) if isinstance(value, (list, tuple)) else []


def merge_verdicts(verdicts):
    merged = dict(passed=False, failed_criteria=[], defects=[], checks_run=[], cost=0,
                  skipped=[], passed_tasks=[])
    pairs = _verdict_pairs(verdicts)
    if not pairs:
        merged['failed_criteria'].append('none')
        merged['defects'].append(dict(criterion_id='none', task_id=None, detail='no verdicts given'))
        return merged

    all_passed = True
    for index, (task_id, verdict) in enumerate(pairs):
        if not isinstance(verdict, dict) or not isinstance(verdict.get('passed'), bool):
            all_passed = False
            cid = f'verdict{index + 1}'
            merged['failed_criteria'].append(cid)
            merged['defects'].append(dict(criterion_id=cid, task_id=task_id,
                                          detail='verdict is malformed (no boolean passed flag)'))
            continue
        task_id = task_id or _task_of(verdict)
        merged['failed_criteria'].extend(str(cid) for cid in _list(verdict.get('failed_criteria')))
        for item in _list(verdict.get('defects')):
            if not isinstance(item, dict):
                continue
            entry = dict(item)
            if _text_id(entry.get('task_id')) is None:
                entry['task_id'] = task_id
            merged['defects'].append(entry)
        merged['checks_run'].extend(_list(verdict.get('checks_run')))
        merged['skipped'].extend(_list(verdict.get('skipped')))
        cost = verdict.get('cost')
        if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
            merged['cost'] += cost
        if verdict['passed']:
            if task_id is not None and task_id not in merged['passed_tasks']:
                merged['passed_tasks'].append(task_id)
        else:
            all_passed = False
    merged['passed'] = all_passed
    return merged
