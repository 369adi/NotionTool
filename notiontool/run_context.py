"""Context for one run and the prompt budget for one task.

Pure functions only: no store access, no file writes and no network use.
Not wired into engine.py yet. Token estimates reuse
context_archive.estimate_tokens, which is ceil(utf8 bytes / 3).

Isolation rule: build_run_context takes only this run's goal and the project
briefing; for a goal dict it reads only 'id' and the title. task_prompt_budget
reads only the dependency results it is given, and of those only the id,
title, order key and result text fields. Chat messages, reports and all other
fields are never read, so content from other runs cannot leak into a prompt.

Drop rule (same as context_budget.build_context): dependency results are
added newest first, and adding stops at the first entry that would push the
prompt over token_budget. That entry and every older one are dropped whole.
"""
import copy
import json
import math
import os

from .context_archive import estimate_tokens

DEFAULT_BRIEFING_CAP = 2000
GOAL_MAX_CHARS = 500
MAX_FILES_LISTED = 20
TRUNCATION_MARKER = '\n[... project briefing truncated ...]'
EMPTY_SECTION = '(none)'
GOAL_TEXT_FIELDS = ('title', 'goal', 'name')
RESULT_ID_FIELDS = ('task_id', 'id')
RESULT_TEXT_FIELDS = ('result', 'text', 'content', 'summary')
RESULT_ORDER_FIELDS = ('finished', 'updated', 'created', 'ts', 'sequence', 'seq')

__all__ = [
    'DEFAULT_BRIEFING_CAP', 'MAX_FILES_LISTED', 'TRUNCATION_MARKER', 'build_run_context',
    'estimate_tokens', 'files_block', 'goal_reference', 'result_block', 'safe_count',
    'task_prompt_budget', 'trim_briefing',
]


def _is_number(value):
    """True for finite int or float values; bool does not count as a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def safe_count(value):
    """Non-negative int from value; bad, negative, bool or non-finite values give 0."""
    if isinstance(value, bool):
        return 0
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    if not math.isfinite(number) or number <= 0:
        return 0
    return int(number)


def _one_line(value, limit):
    """Collapse whitespace to single spaces and cut to limit characters."""
    text = ' '.join(str(value if value is not None else '').split())
    if len(text) > limit:
        text = text[:max(0, limit - 3)].rstrip() + '...'
    return text


def goal_reference(goal):
    """One-line reference to this run's own goal.

    goal may be a string or a dict. For a dict only 'id' and the first
    non-empty string of 'title', 'goal' or 'name' are read.
    """
    if isinstance(goal, dict):
        ident = _one_line(goal.get('id'), 80)
        title = ''
        for field in GOAL_TEXT_FIELDS:
            value = goal.get(field)
            if isinstance(value, str) and value.strip():
                title = _one_line(value, GOAL_MAX_CHARS)
                break
        if ident and title:
            return f'[goal {ident}] {title}'
        return f'[goal {ident}]' if ident else title
    if not isinstance(goal, str):
        return ''
    return _one_line(goal, GOAL_MAX_CHARS)


def trim_briefing(text, cap=DEFAULT_BRIEFING_CAP):
    """Keep the start of text within cap characters.

    A cut text ends with TRUNCATION_MARKER and is never longer than cap.
    When cap is not larger than the marker, only the plain start is kept.
    A bad or negative cap counts as 0.
    """
    text = text.strip() if isinstance(text, str) else ''
    cap = safe_count(cap)
    if len(text) <= cap:
        return text
    if cap <= len(TRUNCATION_MARKER):
        return text[:cap]
    return text[:cap - len(TRUNCATION_MARKER)].rstrip() + TRUNCATION_MARKER


def build_run_context(goal, project_briefing, cap=DEFAULT_BRIEFING_CAP):
    """Return 'Run goal:' plus 'Project briefing:' (cut to cap characters)."""
    reference = goal_reference(goal) or EMPTY_SECTION
    briefing = trim_briefing(project_briefing, cap) or EMPTY_SECTION
    return f'Run goal: {reference}\n\nProject briefing:\n{briefing}\n'


def result_order(entry):
    """Numeric order key of a result (time fields first, then sequence), or None."""
    for field in RESULT_ORDER_FIELDS:
        value = entry.get(field)
        if _is_number(value):
            return value
    return None


def result_id(entry, index):
    """Id of a result ('task_id', then 'id'), or '#<input index>'."""
    for field in RESULT_ID_FIELDS:
        value = entry.get(field)
        if value not in (None, ''):
            return _one_line(value, 80)
    return f'#{index}'


def plain_body(entry):
    """First string of 'result', 'text', 'content' or 'summary', or ''."""
    for field in RESULT_TEXT_FIELDS:
        value = entry.get(field)
        if isinstance(value, str):
            return value.strip()
    return ''


def json_result(entry):
    """The first result text field as a dict when it is a dict or a JSON object string, else None."""
    for field in RESULT_TEXT_FIELDS:
        value = entry.get(field)
        if isinstance(value, dict):
            return value
        if isinstance(value, str) and value.strip().startswith('{'):
            try:
                data = json.loads(value)
            except ValueError:
                return None
            return data if isinstance(data, dict) else None
    return None


def result_file_of(entry):
    """Path of an existing result file ('result_file' field or inside the JSON result), else ''."""
    path = entry.get('result_file')
    if not isinstance(path, str):
        data = json_result(entry)
        path = data.get('result_file') if data else None
    if isinstance(path, str) and path.strip() and os.path.isfile(path.strip()):
        return path.strip()
    return ''


def short_result(entry, path):
    """Short summary plus the result file path, for a result whose full text is in a file."""
    data = json_result(entry) or {}
    summary = data.get('summary')
    if not isinstance(summary, str) or not summary.strip():
        summary = entry.get('summary') if isinstance(entry.get('summary'), str) else ''
    if not summary.strip():
        from .result_files import summary_of
        summary = summary_of(plain_body(entry))
    return f'{summary.strip()}\nFull result: {path}'.strip()


def result_body(entry):
    """Short summary plus result file path when the file exists, else the first result text field."""
    path = result_file_of(entry)
    return short_result(entry, path) if path else plain_body(entry)


def result_block(entry, ident):
    """Exact text added for one dependency result."""
    title = _one_line(entry.get('title'), 200)
    head = f'[result {ident}] {title}' if title else f'[result {ident}]'
    return f'{head}\n{result_body(entry)}\n\n'


def files_block(files):
    """Up to MAX_FILES_LISTED claimed files plus a '(+N more)' note."""
    if isinstance(files, str):
        files = [files]
    names = [_one_line(name, 300) for name in (files or ())]
    names = list(dict.fromkeys(name for name in names if name))
    if not names:
        return ''
    lines = [f'- {name}\n' for name in names[:MAX_FILES_LISTED]]
    if len(names) > MAX_FILES_LISTED:
        lines.append(f'(+{len(names) - MAX_FILES_LISTED} more)\n')
    return 'Files in scope:\n' + ''.join(lines) + '\n'


def newest_first(dep_results):
    """(index, entry) pairs of dict entries, newest first.

    Entries without a numeric order key go last; ties and missing keys keep
    the input order.
    """
    items = [(index, entry) for index, entry in enumerate(dep_results or ()) if isinstance(entry, dict)]

    def key(item):
        index, entry = item
        order = result_order(entry)
        return (0, -order, index) if order is not None else (1, 0, index)

    return sorted(items, key=key)


def task_prompt_budget(dep_results, files, token_budget):
    """Return dict(text, tokens, dropped) for one task.

    dep_results are the results of the task's own dependencies only. The
    file list always comes first and counts toward the budget. tokens is
    estimate_tokens(text). dropped holds deep copies of the entries left out,
    oldest first. A bad or negative token_budget counts as 0.
    """
    budget = safe_count(token_budget)
    text = files_block(files)
    dropped = []
    full = False
    for index, entry in newest_first(dep_results):
        if not full:
            candidate = text + result_block(entry, result_id(entry, index))
            if estimate_tokens(candidate) <= budget:
                text = candidate
                continue
            full = True
        dropped.append(copy.deepcopy(entry))
    dropped.reverse()
    return dict(text=text, tokens=estimate_tokens(text), dropped=dropped)
