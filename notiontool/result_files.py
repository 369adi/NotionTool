"""Result files: a coworker's full result on the PC, a short summary in the chat.

The full text lives in ROOT/<run>--<task>.md (UTF-8, LF). The Main and dependent tasks get
summary_of(reply) plus the path and call read(path) only when needed. prune() deletes files
older than 24 h.

ROOT sits in the same data folder as the handoff files (store.directory/'handoffs', default
desktop_shell.DATA_DIR): <repo>/data, or the folder next to dist/ in the frozen build.
Standard library only; no network access.
"""

import os
import re
import sys
import tempfile
import time

from .handoff_files import file_key, redact_text

MAX_AGE = 86400
SUFFIX = '.md'


def data_dir():
    """The app data folder; same rule as desktop_shell.DATA_DIR (handoffs live in <this>/handoffs)."""
    if getattr(sys, 'frozen', False):
        base = os.path.dirname(os.path.dirname(os.path.realpath(sys.executable)))
    else:
        base = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    return os.path.join(base, 'data')


ROOT = os.path.join(data_dir(), 'results')


def _root():
    return os.path.realpath(str(ROOT))


def path_for(run_id, task_id):
    """Path of the result file for one task; always directly inside ROOT."""
    name = file_key(str(run_id)) + '--' + file_key(str(task_id)) + SUFFIX
    root = _root()
    path = os.path.realpath(os.path.join(root, name))
    if os.path.dirname(path) != root:
        raise ValueError('result path for %r/%r leaves the results folder' % (run_id, task_id))
    return path


def write(run_id, task_id, text):
    """Write the full result atomically (UTF-8, LF, secrets redacted) and return its path."""
    if not isinstance(text, str):
        raise TypeError('result text must be a str, got %s' % type(text).__name__)
    path = path_for(run_id, task_id)
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    body = redact_text(text.replace('\r\n', '\n').replace('\r', '\n'))
    fd, tmp = tempfile.mkstemp(prefix='.' + os.path.basename(path) + '.', suffix='.tmp', dir=folder)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def read(path, max_chars=None):
    """Text of a result file, cut to max_chars. Missing, unreadable or outside ROOT -> ''."""
    try:
        full = os.path.realpath(str(path))
        if os.path.dirname(full) != _root():
            return ''
        with open(full, 'r', encoding='utf-8') as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError, TypeError, ValueError):
        return ''
    if max_chars is not None and max_chars >= 0:
        return text[:int(max_chars)]
    return text


_STATUS = re.compile(r'^(RESULT:|Verdict:|APPROVED\b|Changes needed\b|All checks passed\b)', re.I)
_TESTS = re.compile(r'(\bRan \d+ tests?\b|^OK\b|\bFAILED\b|\b\d+ (passed|failed)\b|\berrors?=\d+|\bexit(_| )?code\b)', re.I)
_FILE = re.compile(r'(?<![\w/.-])[\w.-]+(?:/[\w.-]+)*\.(?:py|js|ts|json|md|txt|css|html|toml|yml|yaml|ini|cfg)\b')
_SENTENCE = re.compile(r'(?<=[.!?])\s+')
_NOISE = re.compile(r'^(```|\||---|#+\s*$)')


def _clean(line):
    return re.sub(r'^[-*>#\s]+', '', line).strip()


def summary_of(reply_text, limit=600):
    """Short summary of a reply: first sentences, changed files, test result lines, status line.

    The status line (RESULT:/verdict) is always kept at the end; the whole text is at most limit
    characters.
    """
    if not isinstance(reply_text, str):
        raise TypeError('reply_text must be a str, got %s' % type(reply_text).__name__)
    limit = max(40, int(limit))
    lines = [l.strip() for l in reply_text.replace('\r', '').split('\n')]
    lines = [l for l in lines if l and not _NOISE.match(l)]
    if not lines:
        return ''
    status = ''
    for line in reversed(lines):
        if _STATUS.match(_clean(line)):
            status = _clean(line)
            break
    tests, files, prose = [], [], []
    for line in lines:
        text = _clean(line)
        if not text or text == status:
            continue
        if _TESTS.search(text):
            if text not in tests and len(tests) < 3:
                tests.append(text)
        elif _FILE.search(text):
            names = [m.group(0) for m in _FILE.finditer(text)]
            for name in names:
                if name not in files and len(files) < 6:
                    files.append(name)
        elif not _STATUS.match(text):
            prose.append(text)
    first = ' '.join(_SENTENCE.split(' '.join(prose[:3]))[:2]).strip()
    parts = []
    if first:
        parts.append(first)
    if files:
        parts.append('Files: ' + ', '.join(files))
    parts.extend('Tests: ' + t for t in tests)
    status = status[:limit]
    room = limit - (len(status) + 1 if status else 0)
    out = []
    for part in parts:
        used = sum(len(p) + 1 for p in out)
        left = room - used
        if len(part) <= left:
            out.append(part)
        else:
            if left > 20:
                out.append(part[:left - 1].rstrip() + '\u2026')
            break
    if status:
        out.append(status)
    return '\n'.join(out)[:limit]


def prune(now=None, max_age=MAX_AGE):
    """Delete result files (and stale temp files) older than max_age seconds; return their paths.

    Files younger than max_age are kept. A missing ROOT is fine. Never raises for one bad file.
    """
    now = time.time() if now is None else float(now)
    root = _root()
    deleted = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return deleted
    for name in names:
        if not (name.endswith(SUFFIX) or (name.startswith('.') and name.endswith('.tmp'))):
            continue
        full = os.path.join(root, name)
        try:
            if os.path.isfile(full) and now - os.path.getmtime(full) > max_age:
                os.remove(full)
                deleted.append(full)
        except OSError:
            continue
    return deleted
