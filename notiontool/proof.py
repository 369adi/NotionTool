"""Proof record for one finished task: changed files, reported test results and a screenshot path.

Standard library plus snapshots.py. The repo is not always a git repo, so the base is a file
snapshot taken when the task starts (start) and when it claims a new file (extend), stored under
work/proof-snapshots. record() compares only the task's write scope with that base and reads the
coworker's result text for 'Ran N tests' + OK/FAILED lines and a screenshot path.
Nothing here sends anything (rule I5); every function is safe to call from the engine and never
raises for missing or odd data. Missing parts are reported as not checked.
"""
import difflib
import hashlib
import os
import re
import time

from notiontool import snapshots

STORE_DIR = 'work/proof-snapshots'
VERSION = 1
MAX_FILES = 50
MAX_BYTES = 2 * 1024 * 1024
MAX_TESTS = 10
MAX_TEXT = 200000

_RAN = re.compile(r'\bRan (\d+) tests? in [0-9.]+ ?s\b')
_OK = re.compile(r'^\s*OK\b(?:\s*\(([^)]*)\))?', re.M)
_FAILED = re.compile(r'^\s*FAILED\s*\(([^)]*)\)', re.M)
_COUNT = re.compile(r'(failures|errors|skipped|expected failures|unexpected successes)=(\d+)')
_IMAGE = re.compile(r'(?:[A-Za-z]:[\\/]|[\\/]{1,2}|\.{1,2}[\\/])?(?:[^\s"\'<>|*?`()\[\]]+[\\/])*'
                    r'[^\s"\'<>|*?`()\[\]\\/]+\.(?:png|jpe?g|webp|gif|bmp)\b', re.I)
_SHOT_WORD = re.compile(r'screenshot|screen shot|screen_capture|screen capture|capture', re.I)


def _base_id(task_id):
    return 'proof-' + snapshots.safe_task_id(task_id)[:80]


def _extra_id(task_id, rel):
    return _base_id(task_id) + '.' + hashlib.sha256(rel.encode('utf-8')).hexdigest()[:12]


def _inside(paths, root):
    out = []
    for path in paths or []:
        try:
            rel = snapshots.relative_path(str(path), root)
        except (ValueError, TypeError):
            continue
        if rel not in out:
            out.append(rel)
    return out[:MAX_FILES]


def start(task_id, files, root, store_dir=STORE_DIR):
    """Take the base snapshot of the write scope when a task starts. Returns True on success."""
    try:
        snapshots.take(_base_id(task_id), _inside(files, root), root, store_dir=store_dir)
        return True
    except Exception:
        return False


def extend(task_id, path, root, store_dir=STORE_DIR):
    """Base for a file claimed later (claims come before the write). Returns True on success."""
    try:
        rels = _inside([path], root)
        if not rels:
            return False
        try:
            base = snapshots.load(_base_id(task_id), root, store_dir=store_dir)
            if rels[0] in (base.get('files') or {}) or rels[0] in (base.get('missing') or []):
                return True
        except KeyError:
            pass
        snapshots.take(_extra_id(task_id, rels[0]), rels, root, store_dir=store_dir)
        return True
    except Exception:
        return False


def _lines(data):
    return data.decode('utf-8', 'replace').replace('\r\n', '\n').replace('\r', '\n').splitlines()


def line_counts(before, after):
    """(added, removed) line counts between two byte strings (None = no file); None for binary or huge."""
    for data in (before, after):
        if data is not None and (len(data) > MAX_BYTES or b'\0' in data[:8192]):
            return None
    old = _lines(before) if before is not None else []
    new = _lines(after) if after is not None else []
    added = removed = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag in ('replace', 'delete'):
            removed += i2 - i1
        if tag in ('replace', 'insert'):
            added += j2 - j1
    return added, removed


def _read(path):
    try:
        with open(path, 'rb') as handle:
            return handle.read()
    except OSError:
        return None


def _base_for(task_id, rel, base, root, store_dir):
    """(known, bytes_or_None) for rel: from the start snapshot or the claim snapshot."""
    for snap in (base, None):
        if snap is None:
            try:
                snap = snapshots.load(_extra_id(task_id, rel), root, store_dir=store_dir)
            except (KeyError, OSError, ValueError):
                return False, None
        if rel in (snap.get('files') or {}):
            return True, snapshots.stored_bytes(snap, rel)
        if rel in (snap.get('missing') or []):
            return True, None
    return False, None


def file_changes(task_id, files, root, store_dir=STORE_DIR):
    """Changed files in the write scope, or None when no base snapshot exists."""
    try:
        base = snapshots.load(_base_id(task_id), root, store_dir=store_dir)
    except (KeyError, OSError, ValueError):
        return None
    root = base.get('root') or root
    out, unknown = [], []
    for rel in _inside(files, root):
        known, before = _base_for(task_id, rel, base, root, store_dir)
        if not known:
            unknown.append(rel)
            continue
        after = _read(os.path.join(root, *rel.split('/')))
        if before == after:
            continue
        change = 'added' if before is None else 'removed' if after is None else 'changed'
        counts = line_counts(before, after)
        out.append(dict(path=rel, change=change, added=counts[0] if counts else None,
                        removed=counts[1] if counts else None, binary=counts is None))
    return dict(files=out, unknown=unknown)


def _text(value):
    text = value if isinstance(value, str) else '' if value is None else str(value)
    text = text[:MAX_TEXT]
    if '\\n' in text and text.count('\n') < text.count('\\n'):
        text = text.replace('\\r\\n', '\n').replace('\\n', '\n')
    return text


def parse_tests(text):
    """Test runs reported in the result text: [{ran, status, failures, errors, skipped}]."""
    text = _text(text)
    marks = list(_RAN.finditer(text))
    runs = []
    for index, mark in enumerate(marks[:MAX_TESTS]):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        tail = text[mark.end():min(end, mark.end() + 400)]
        ok, failed = _OK.search(tail), _FAILED.search(tail)
        if failed and (not ok or failed.start() < ok.start()):
            status, detail = 'failed', failed.group(1)
        elif ok:
            status, detail = 'passed', ok.group(1) or ''
        else:
            status, detail = 'unknown', ''
        counts = {k: int(v) for k, v in _COUNT.findall(detail)}
        runs.append(dict(ran=int(mark.group(1)), status=status, failures=counts.get('failures', 0),
                         errors=counts.get('errors', 0), skipped=counts.get('skipped', 0)))
    return runs


def tests_status(runs):
    """'failed' if any run failed, 'passed' if all known runs passed, else 'not_checked'."""
    states = [r.get('status') for r in runs or []]
    if 'failed' in states:
        return 'failed'
    if 'passed' in states:
        return 'passed'
    return 'not_checked'


def parse_screenshot(text):
    """First image path on a line that talks about a screenshot or capture, else ''."""
    for line in _text(text).splitlines():
        if not _SHOT_WORD.search(line):
            continue
        match = _IMAGE.search(line)
        if match:
            return match.group(0)[:300]
    return ''


def record(task, result_text, root, store_dir=STORE_DIR, clock=time.time):
    """Proof dict for a finished task. Never raises."""
    task = task if isinstance(task, dict) else {}
    proof = dict(version=VERSION, created=clock(), files_checked=False, files=[], unknown=[],
                 tests=[], tests_status='not_checked', screenshot='')
    try:
        changes = file_changes(task.get('id'), task.get('files') or [], root, store_dir) if task.get('id') else None
        if changes is not None:
            proof.update(files_checked=True, files=changes['files'], unknown=changes['unknown'])
    except Exception:
        pass
    try:
        runs = parse_tests(result_text)
        proof.update(tests=runs, tests_status=tests_status(runs))
    except Exception:
        pass
    try:
        proof['screenshot'] = parse_screenshot(result_text)
    except Exception:
        pass
    return proof
