"""Readable diffs between two file snapshots, and a short compare summary.

Standard library only (difflib, hashlib). No file I/O and no network: callers
pass the bytes of both sides. Paths use normalize_path from task_cache.py so
they match the snapshot and cache keys. Not wired into engine.py yet
(NOT WIRED).
"""
import difflib
import hashlib

from notiontool.task_cache import normalize_path

DEFAULT_MAX_LINES = 400
DEFAULT_MAX_PATHS = 20
SHA_PREFIX = 12
CATEGORIES = ("changed", "added", "removed", "unchanged")
REPORTED = ("changed", "added", "removed")


def line_ending_style(data):
    """Return 'LF', 'CRLF', 'CR', 'mixed' or 'none' for raw bytes."""
    data = bytes(data or b"")
    crlf = data.count(b"\r\n")
    cr = data.count(b"\r") - crlf
    lf = data.count(b"\n") - crlf
    kinds = [name for name, count in (("CRLF", crlf), ("LF", lf), ("CR", cr)) if count]
    if not kinds:
        return "none"
    if len(kinds) == 1:
        return kinds[0]
    return "mixed"


def normalize_newlines(data):
    """CRLF and lone CR become LF."""
    return bytes(data).replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _sha_prefix(data):
    return hashlib.sha256(data).hexdigest()[:SHA_PREFIX]


def _decode(data):
    """UTF-8 text, or None for binary data (NUL byte or invalid UTF-8)."""
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _side(data):
    if data is None:
        return "missing"
    return "%d bytes sha256 %s" % (len(data), _sha_prefix(data))


def _truncate(lines, max_lines):
    limit = max(1, int(max_lines))
    if len(lines) <= limit:
        return lines
    left_out = len(lines) - limit
    return lines[:limit] + ["... %d more lines not shown (max_lines=%d)" % (left_out, limit)]


def unified_diff(before_bytes, after_bytes, path, max_lines=DEFAULT_MAX_LINES):
    """Return a text diff of two byte strings for one file.

    - identical bytes: one 'no changes' line
    - only line endings differ: one 'line endings changed X -> Y' line
    - line endings and content differ: that line first, then a unified diff
      of the LF-normalized content
    - binary or invalid UTF-8: one 'binary file differs' line with sizes and
      sha256 prefixes
    - None on one side: the file was added or removed
    Output longer than max_lines is cut, with a note giving the lines left out.
    """
    name = normalize_path(path)
    if before_bytes is None and after_bytes is None:
        return "%s: no changes (missing on both sides)" % name
    before = None if before_bytes is None else bytes(before_bytes)
    after = None if after_bytes is None else bytes(after_bytes)
    if before is not None and before == after:
        return "%s: no changes" % name

    lines = []
    if before is None:
        lines.append("%s: file added (%d bytes)" % (name, len(after)))
    elif after is None:
        lines.append("%s: file removed (%d bytes)" % (name, len(before)))

    old_raw = before if before is not None else b""
    new_raw = after if after is not None else b""
    old_text = _decode(old_raw)
    new_text = _decode(new_raw)
    if old_text is None or new_text is None:
        lines.append("%s: binary file differs (before %s, after %s)" % (name, _side(before), _side(after)))
        return "\n".join(lines)

    old_norm = normalize_newlines(old_raw).decode("utf-8")
    new_norm = normalize_newlines(new_raw).decode("utf-8")
    if before is not None and after is not None:
        old_style = line_ending_style(old_raw)
        new_style = line_ending_style(new_raw)
        if old_norm == new_norm:
            return "%s: line endings changed %s -> %s" % (name, old_style, new_style)
        if old_style != new_style and "none" not in (old_style, new_style):
            lines.append("%s: line endings changed %s -> %s" % (name, old_style, new_style))

    fromfile = "/dev/null" if before is None else "a/" + name
    tofile = "/dev/null" if after is None else "b/" + name
    body = list(difflib.unified_diff(
        old_norm.splitlines(), new_norm.splitlines(),
        fromfile=fromfile, tofile=tofile, lineterm=""))
    lines.extend(body)
    if not body and before is not None and after is not None:
        state = "added" if new_norm.endswith("\n") else "removed"
        lines.append("%s: final newline %s" % (name, state))
    return "\n".join(_truncate(lines, max_lines))


def _paths(value):
    if not value:
        return []
    return sorted({normalize_path(item) for item in value})


def summary(compare_result, max_paths=DEFAULT_MAX_PATHS):
    """Short English report of {changed, added, removed, unchanged}.

    First the counts per category, then the paths of each category except
    unchanged, at most max_paths each with a '... and N more' note.
    """
    result = compare_result or {}
    groups = {key: _paths(result.get(key)) for key in CATEGORIES}
    limit = max(0, int(max_paths))
    lines = ["Snapshot comparison: %d changed, %d added, %d removed, %d unchanged." % tuple(
        len(groups[key]) for key in CATEGORIES)]
    if not any(groups[key] for key in REPORTED):
        lines.append("No files changed.")
        return "\n".join(lines)
    for key in REPORTED:
        paths = groups[key]
        if not paths:
            continue
        lines.append("%s (%d):" % (key.capitalize(), len(paths)))
        lines.extend("- " + item for item in paths[:limit])
        if len(paths) > limit:
            lines.append("- ... and %d more" % (len(paths) - limit))
    return "\n".join(lines)
