"""Request templates (Live 'New request', Ctrl+K) and history search (Live 'History').

Templates only build draft text. Nothing here sends a chat (I5): the overlay puts the draft into the
Notion chat composer and the user reviews and sends it.

History search: GET /api/history/search?q=&limit= and the desk action history_search look through all
runs and tasks (title, plain summary, result text, files) and return at most SEARCH_LIMIT results.
"""
import time

SEARCH_LIMIT = 50
QUERY_LIMIT = 200
FIELD_LIMIT = 2000
DESK_ACTION_NAMES = ('request_templates', 'request_draft', 'history_search')

FIELDS = (
    ('what', 'What', 'What should change, in one or two sentences'),
    ('where', 'Where', 'Page, file, screen or link'),
    ('done_when', 'Done when', 'How we know it is finished'),
)

TEMPLATES = (
    dict(id='ui_bug', title='Fix a UI bug', hint='Something on screen looks or works wrong.', checks=(
        'Reproduce the bug first and describe what you saw.',
        'Fix it with the smallest change that works.',
        'Run the related tests and the desktop tests, and node --check for changed scripts.',
        'Check the screen again and say what changed.')),
    dict(id='feature', title='Build a feature', hint='Add something new.', checks=(
        'Split the work into small steps before you start.',
        'Keep existing behaviour and settings unchanged.',
        'Add tests for the new behaviour and run the related tests.',
        'Explain how to use it in plain words.')),
    dict(id='review', title='Review and test', hint='Check existing work and run the tests.', checks=(
        'Read the changed files and list problems you find.',
        'Run the full test suite and report failures with the test name.',
        'Do not change code unless a fix is clearly needed; ask first for larger changes.',
        'End with a short verdict: ready or not ready.')),
    dict(id='research', title='Research', hint='Find out how something works or what to do.', checks=(
        'Collect the facts first and name your sources.',
        'Do not change any files.',
        'Give two or three options with pros and cons.',
        'End with one clear recommendation.')),
)

_STATE_OUTCOME = {'done': 'Finished.', 'completed': 'Finished.', 'checked': 'Finished.', 'failed': 'Did not finish.',
                  'error': 'Did not finish.', 'blocked': 'Blocked.', 'cancelled': 'Stopped before the end.',
                  'running': 'Still running.', 'queued': 'Waiting.', 'pending': 'Waiting.', 'paused': 'Paused.',
                  'awaiting_main': 'Waits for your OK.', 'uncertain': 'Answer lost.'}


def templates():
    """The templates as plain data for the overlay."""
    return [dict(id=t['id'], title=t['title'], hint=t['hint'], checks=list(t['checks']),
                 fields=[dict(name=n, label=l, placeholder=p) for n, l, p in FIELDS]) for t in TEMPLATES]


def template(template_id):
    for t in TEMPLATES:
        if t['id'] == template_id or t['title'].lower() == str(template_id or '').strip().lower():
            return t
    raise ValueError('Unknown template: %s' % (str(template_id or '')[:60] or 'none'))


def _clean(value, limit=FIELD_LIMIT):
    if value is None:
        return ''
    text = value if isinstance(value, str) else str(value)
    return '\n'.join(' '.join(line.split()) for line in text.strip().splitlines()).strip()[:limit]


def draft(template_id, values=None):
    """Draft text for the Main. Empty fields stay visible as (fill in) so the user notices them."""
    t = template(template_id)
    values = values if isinstance(values, dict) else {}
    lines = ['New request: %s' % t['title'], '']
    for name, label, _ in FIELDS:
        lines.append('%s: %s' % (label, _clean(values.get(name)) or '(fill in)'))
    lines += ['', 'Checks before you report done:']
    lines += ['%d. %s' % (i, step) for i, step in enumerate(t['checks'], 1)]
    return '\n'.join(lines)


def _summaries():
    try:
        from .operator_main import plain_run_summary, plain_task_summary, run_outcome
        return plain_run_summary, plain_task_summary, run_outcome
    except Exception:  # keep search working if that module changes
        return (lambda r: _clean(r.get('goal') or r.get('title'), 200), lambda t: _clean(t.get('summary') or t.get('title'), 200),
                lambda r: '')


def _stamp(value):
    if isinstance(value, (int, float)) and value > 0:
        return float(value / 1000.0 if value > 1e11 else value)
    if isinstance(value, str) and value.strip():
        try:
            return _stamp(float(value))
        except ValueError:
            pass
        try:
            from datetime import datetime
            return datetime.fromisoformat(value.strip().replace('Z', '+00:00')).timestamp()
        except ValueError:
            return None
    return None


def _date(at):
    return time.strftime('%Y-%m-%d %H:%M', time.localtime(at)) if at else ''


def _files(value):
    return [_clean(f, 200) for f in (value if isinstance(value, (list, tuple)) else [])][:20]


def _snippet(text, terms, width=160):
    low = text.lower()
    pos = min([p for p in (low.find(t) for t in terms) if p >= 0] or [0])
    start = max(0, pos - width // 3)
    piece = ' '.join(text[start:start + width].split())
    return ('...' if start else '') + piece + ('...' if start + width < len(text) else '')


def _match(fields, terms):
    """All terms must appear somewhere; returns (label, text) of the first field that holds a term."""
    hay = ' '.join(text for _, text in fields).lower()
    if not all(t in hay for t in terms):
        return None
    for label, text in fields:
        if any(t in text.lower() for t in terms):
            return label, text
    return fields[0] if fields else ('', '')


def search(store, query, limit=SEARCH_LIMIT):
    """Search all runs and tasks. Newest first, at most limit (<= SEARCH_LIMIT) results."""
    q = _clean(query, QUERY_LIMIT)
    try:
        limit = max(1, min(SEARCH_LIMIT, int(limit or SEARCH_LIMIT)))
    except (TypeError, ValueError):
        limit = SEARCH_LIMIT
    terms = [t for t in q.lower().split() if t]
    if not terms:
        return dict(ok=True, query=q, results=[], total=0, limit=limit)
    run_summary, task_summary, run_outcome = _summaries()
    runs = [r for r in (store.all('runs') or []) if isinstance(r, dict)]
    goals = {str(r.get('id')): run_summary(r) for r in runs}
    hits = []
    for r in runs:
        summary = run_summary(r)
        outcome = _clean(run_outcome(r), 300) or _STATE_OUTCOME.get(str(r.get('state') or ''), '')
        fields = [('Title', _clean(r.get('goal') or r.get('title'))), ('Summary', summary),
                  ('Result', _clean(r.get('summary') or r.get('outcome'))), ('Outcome', outcome)]
        found = _match([f for f in fields if f[1]], terms)
        if found:
            at = _stamp(r.get('finished')) or _stamp(r.get('created'))
            hits.append(dict(kind='run', id=str(r.get('id') or ''), run_id=str(r.get('id') or ''), title=summary,
                             outcome=outcome, state=str(r.get('state') or ''), at=at, date=_date(at),
                             field=found[0], snippet=_snippet(found[1], terms)))
    for t in (store.all('tasks') or []):
        if not isinstance(t, dict):
            continue
        files = _files(t.get('files'))
        state = str(t.get('state') or '')
        outcome = _clean(t.get('error'), 300) if state in ('failed', 'error', 'blocked') and t.get('error') else ''
        outcome = outcome or _STATE_OUTCOME.get(state, '')
        fields = [('Title', _clean(t.get('title'))), ('Summary', task_summary(t)),
                  ('Result', _clean(t.get('result'))), ('Files', ' '.join(files))]
        found = _match([f for f in fields if f[1]], terms)
        if found:
            at = _stamp(t.get('finished')) or _stamp(t.get('created'))
            run_id = str(t.get('run_id') or '')
            hits.append(dict(kind='task', id=str(t.get('id') or ''), run_id=run_id, title=task_summary(t) or _clean(t.get('title'), 200),
                             request=goals.get(run_id, ''), outcome=outcome, state=state, at=at, date=_date(at),
                             field=found[0], snippet=_snippet(found[1], terms), files=files[:5]))
    hits.sort(key=lambda h: h['at'] or 0, reverse=True)
    return dict(ok=True, query=q, results=hits[:limit], total=len(hits), limit=limit)


def http_search(server, query):
    """GET /api/history/search?q=&limit= (query is the parse_qs dict)."""
    query = query if isinstance(query, dict) else {}
    first = lambda name: (query.get(name) or [''])[0]
    return search(server.store, first('q'), first('limit') or SEARCH_LIMIT)


def desk_handlers(server):
    def _args(a):
        return a if isinstance(a, dict) else {}
    return {
        'request_templates': lambda args: dict(ok=True, templates=templates()),
        'request_draft': lambda args: dict(ok=True, text=draft(_args(args).get('id'), _args(args).get('values'))),
        'history_search': lambda args: search(server.store, _args(args).get('q'), _args(args).get('limit')),
    }


def install_desk_actions(desktop_shell=None):
    """Add the template and history actions to desktop_shell at runtime (it is owned by other work).

    Idempotent; keeps the nt_* markers of wrappers installed before (restart_guard, problems).
    """
    if desktop_shell is None:
        from . import desktop_shell
    missing = tuple(a for a in DESK_ACTION_NAMES if a not in desktop_shell.DESK_ACTIONS)
    if missing:
        desktop_shell.DESK_ACTIONS = tuple(desktop_shell.DESK_ACTIONS) + missing
    original = desktop_shell.desktop_handlers
    if getattr(original, 'nt_template_actions', False):
        return desktop_shell

    def desktop_handlers(server, *args, **kwargs):
        handlers = original(server, *args, **kwargs)
        handlers.update(desk_handlers(server))
        return handlers
    for name in dir(original):
        if name.startswith('nt_'):
            setattr(desktop_handlers, name, getattr(original, name))
    desktop_handlers.nt_template_actions = True
    desktop_shell.desktop_handlers = desktop_handlers
    return desktop_shell
