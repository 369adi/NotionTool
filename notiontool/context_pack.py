"""Context pack: put the code a Coworker needs straight into its task prompt.

Coworkers spent ~44 % of their tool calls searching and reading code (qo, 22.10.2026).
Every tool call costs 2-3 Notion steps, so the Main names the code in the task and
the app inserts it once when the task starts (current file state, never stale).

Markers in the task prompt (written by the Main):
  [[code: notiontool/step_guard.py:120-180]]   lines 120-180 (1-based, inclusive)
  [[code: notiontool/step_guard.py]]           whole file if small, else a pointer
  [[test: python -m pytest tests/test_x.py -q]] the exact check to run at the end
Small files in the task's 'files' list (<= AUTO_MAX_LINES) are added automatically.
For larger .py files in that list the ranked repo map (code_index, PageRank over import
and call edges) fills the rest of BUDGET_CHARS with their most connected symbols.
"""
import os
import re

MARK = re.compile(r'\[\[(code|test):\s*([^\]\n]+?)\s*\]\]')
RANGE = re.compile(r'^(.*?):(\d+)(?:-(\d+))?$')
AUTO_MAX_LINES = 200
WHOLE_MAX_LINES = 400
BUDGET_CHARS = 40000
MAP_SYMBOL_MAX_LINES = 120
MAP_MIN_CHARS = 200
MAP_HEADER = ('Repo map: the most connected symbols of the larger task files '
              '(ranked by imports and calls across the repo).')
NOTE = ('\n\nContext pack: the code shown above is the current file state at task start. '
        'Do not read or search it again; read only lines that are not shown. '
        'Batch several reads into one call where possible, and never poll in a loop.')


def _resolve(root, name):
    if not root:
        return None
    base = os.path.realpath(root)
    path = os.path.realpath(os.path.join(base, name.strip().replace('/', os.sep)))
    if path != base and not path.startswith(base + os.sep):
        return None  # outside the project folder
    return path if os.path.isfile(path) else None


def _lines(path):
    with open(path, encoding='utf-8', errors='replace', newline='') as handle:
        return handle.read().splitlines()


def _block(name, lines, start, end):
    width = len(str(end))
    body = '\n'.join('%*d| %s' % (width, number, lines[number - 1]) for number in range(start, end + 1))
    fence = '```'
    while fence in body:
        fence += '`'
    return 'File %s lines %d-%d of %d:\n%s\n%s\n%s' % (name, start, end, len(lines), fence, body, fence)


def _code(spec, root):
    """(text, chars_of_code, path) for one [[code: ...]] marker."""
    match = RANGE.match(spec.strip())
    name, start, end = (match.group(1), int(match.group(2)), int(match.group(3) or match.group(2))) if match \
        else (spec.strip(), 0, 0)
    path = _resolve(root, name)
    if not path:
        return '(context pack: %s not found in the project folder)' % name, 0, None
    lines = _lines(path)
    if not start:
        if len(lines) > WHOLE_MAX_LINES:
            return ('(context pack: %s has %d lines, too long to include whole; read only the part you need)'
                    % (name, len(lines))), 0, path
        start, end = 1, max(1, len(lines))
    start, end = max(1, min(start, end)), min(max(start, end), len(lines))
    if not lines or start > len(lines):
        return '(context pack: %s has no lines %s)' % (name, spec), 0, path
    text = _block(name, lines, start, end)
    return text, len(text), path


def _map_blocks(root, large, budget, info):
    """Blocks of the best ranked symbols of the large .py files, within the remaining budget."""
    if not large or budget - info['chars'] < MAP_MIN_CHARS:
        return []
    base = os.path.realpath(root)
    files = {}
    for name, path, lines in large:
        files[os.path.relpath(path, base).replace(os.sep, '/')] = (name, lines)
    try:
        from . import code_index
        ranked = code_index.repo_map(base, focus=list(files), limit=0)
    except Exception as error:  # the map is an extra; it must never break a task start
        info['map_error'] = ('%s: %s' % (type(error).__name__, error))[:200]
        return []
    blocks, taken = [], {}
    for symbol in ranked.get('symbols', ()):
        if budget - info['chars'] < MAP_MIN_CHARS:
            break
        if symbol.get('path') not in files:
            continue
        name, lines = files[symbol['path']]
        start = max(1, int(symbol.get('line') or 1))
        end = min(len(lines), max(start, int(symbol.get('end_line') or start)))
        if start > len(lines):
            continue
        ranges = taken.setdefault(symbol['path'], [])
        if any(start <= done_end and done_start <= end for done_start, done_end in ranges):
            continue  # already shown inside a class or method block
        text = _block(name, lines, start, min(end, start + MAP_SYMBOL_MAX_LINES - 1))
        if not blocks:
            text = MAP_HEADER + '\n' + text
        if info['chars'] + len(text) > budget:
            continue  # a smaller symbol may still fit
        ranges.append((start, end))
        info['chars'] += len(text)
        info['blocks'] += 1
        info['symbols'] += 1
        blocks.append(text)
    return blocks


def expand(prompt, files, root, budget=BUDGET_CHARS):
    """Return (prompt, info). info: added (bool), chars, blocks, skipped, tests, symbols."""
    prompt = str(prompt or '')
    info = dict(added=False, chars=0, blocks=0, skipped=0, tests=0, symbols=0)
    seen = set()

    def replace(match):
        kind, spec = match.group(1), match.group(2)
        if kind == 'test':
            info['tests'] += 1
            return 'Check at the end: run exactly `%s` and report its last lines.' % spec.strip()
        text, chars, path = _code(spec, root)
        if chars and info['chars'] + chars > budget:
            info['skipped'] += 1
            return '(context pack: %s left out, budget full; read it yourself)' % spec.strip()
        if path:
            seen.add(path)
        if chars:
            info['chars'] += chars
            info['blocks'] += 1
        return text

    prompt = MARK.sub(replace, prompt)
    extra, large = [], []
    for name in files or ():
        if not isinstance(name, str):
            continue
        path = _resolve(root, name)
        if not path or path in seen:
            continue
        seen.add(path)
        try:
            lines = _lines(path)
        except OSError:
            continue
        if not lines:
            continue
        if len(lines) > AUTO_MAX_LINES:
            if path.endswith('.py'):
                large.append((name, path, lines))
            continue
        text = _block(name, lines, 1, len(lines))
        if info['chars'] + len(text) > budget:
            info['skipped'] += 1
            continue
        info['chars'] += len(text)
        info['blocks'] += 1
        extra.append(text)
    extra.extend(_map_blocks(root, large, budget, info))
    if extra:
        prompt += '\n\n' + '\n\n'.join(extra)
    if info['blocks']:
        info['added'] = True
        prompt += NOTE
    elif info['tests']:
        info['added'] = True
    return prompt, info
