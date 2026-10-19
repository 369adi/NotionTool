"""Checkable done definition per task, built from planner output.

Pure functions only: no file access, no network, no new dependencies.
Every criterion is a dict with exactly these keys:
    id (str), text (str), method (one of METHODS), target (list of str), required (bool)
"""
import re

METHODS = ('tests', 'static', 'diff', 'requirements', 'screenshot', 'model_review')
FALLBACK_METHOD = 'requirements'
MAX_CRITERIA = 50
KEYS = ('id', 'text', 'method', 'target', 'required')

_ID = re.compile(r'[A-Za-z0-9_.-]{1,40}')
_LIST_KEYS = ('criteria', 'done_definition', 'acceptance_criteria', 'acceptance')
_TEXT_KEYS = ('text', 'criterion', 'description', 'check')
_BULLET = re.compile(r'^\s*(?:[-*+]|\d+[.)])\s+')
_FALSE = frozenset(('false', 'no', 'n', '0', 'optional'))


def _clean(value):
    """Return value as a single-line string, or '' when it is not text-like."""
    if isinstance(value, bool):
        return ''
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return ' '.join(value.split())
    return ''


def _required(value):
    """Coerce a required flag to bool. Unknown values count as required."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str) and value.strip().lower() in _FALSE:
        return False
    return True


def _targets(value):
    """Coerce a target to a list of unique non-empty strings."""
    if isinstance(value, str):
        items = value.splitlines()
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        return []
    out = []
    for item in items:
        text = _clean(item) if isinstance(item, str) else ''
        if text and text not in out:
            out.append(text)
    return out


def _raw_items(planner_output):
    """Turn any planner output shape into a flat list of raw items."""
    value = planner_output
    if isinstance(value, dict):
        for key in _LIST_KEYS:
            if key in value:
                value = value[key]
                break
        else:
            return [value]
    if value is None:
        return []
    if isinstance(value, str):
        lines = (_BULLET.sub('', part) for part in value.splitlines())
        return [line for line in lines if line.strip()]
    if isinstance(value, dict):
        return [value]
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _one(item):
    """Normalise one raw item into a criterion, or None when it is unusable."""
    if isinstance(item, str):
        item = {'text': item}
    if not isinstance(item, dict):
        return None
    text = ''
    for key in _TEXT_KEYS:
        text = _clean(item.get(key))
        if text:
            break
    raw_method = _clean(item.get('method')).lower()
    method = raw_method or FALLBACK_METHOD
    note = ''
    if method not in METHODS:
        note = "(unknown method '" + raw_method[:40] + "' mapped to requirements)"
        method = FALLBACK_METHOD
    target = _targets(item['target'] if 'target' in item else item.get('targets'))
    if not text:
        if not target:
            return None
        text = method + ' check: ' + ', '.join(target)
    if note:
        text = text + ' ' + note
    return {
        'id': _clean(item.get('id')),
        'text': text,
        'method': method,
        'target': target,
        'required': _required(item.get('required')),
    }


def fallback_criterion(task_id=None):
    """The single required criterion used when the planner gave nothing usable."""
    name = _clean(task_id)
    subject = 'Task ' + name if name else 'The task'
    return {
        'id': 'c1',
        'text': subject + ' fulfils its stated requirements.',
        'method': FALLBACK_METHOD,
        'target': [],
        'required': True,
    }


def build_done_definition(task_id, planner_output):
    """Build a list of checkable criteria from (possibly malformed) planner output.

    Valid unique ids are kept; missing, invalid or duplicate ids get the next
    free id c1, c2, ... in input order, so the result is stable for the same
    input. The input is never modified.
    """
    found = []
    for item in _raw_items(planner_output):
        criterion = _one(item)
        if criterion is not None:
            found.append(criterion)
            if len(found) >= MAX_CRITERIA:
                break
    if not found:
        return [fallback_criterion(task_id)]
    used = set()
    for criterion in found:
        cid = criterion['id']
        if cid and _ID.fullmatch(cid) and cid.lower() not in used:
            used.add(cid.lower())
        else:
            criterion['id'] = ''
    number = 0
    for criterion in found:
        if not criterion['id']:
            number += 1
            while 'c%d' % number in used:
                number += 1
            criterion['id'] = 'c%d' % number
            used.add(criterion['id'])
    return found


def validate_criterion(c):
    """Return (ok, problems) for one criterion. problems is a list of English messages."""
    if not isinstance(c, dict):
        return False, ['criterion must be a dict']
    problems = []
    for key in KEYS:
        if key not in c:
            problems.append('missing key: ' + key)
    for key in c:
        if key not in KEYS:
            problems.append('unexpected key: ' + str(key))
    if 'id' in c:
        cid = c['id']
        if not (isinstance(cid, str) and _ID.fullmatch(cid)):
            problems.append('id must be 1-40 letters, digits, _, . or -')
    if 'text' in c:
        text = c['text']
        if not (isinstance(text, str) and text.strip()):
            problems.append('text must be a non-empty string')
    if 'method' in c and c['method'] not in METHODS:
        problems.append('method must be one of: ' + ', '.join(METHODS))
    if 'target' in c:
        target = c['target']
        if not (isinstance(target, list) and all(isinstance(t, str) and t.strip() for t in target)):
            problems.append('target must be a list of non-empty strings')
    if 'required' in c and not isinstance(c['required'], bool):
        problems.append('required must be a bool')
    return not problems, problems
