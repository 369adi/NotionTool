"""Parse duration strings like '1h30m' into seconds."""
import re

_UNITS = {"d": 86400, "h": 3600, "m": 60, "s": 1}
_PART = re.compile(r"(\d+)([dhms])")


def parse(text):
    """Return the total number of seconds for text such as '1h30m'.

    Supports d, h, m, s. Raises ValueError for empty or invalid text.
    """
    if not isinstance(text, str):
        raise ValueError("duration must be a string")
    s = text.strip().lower()
    if not s:
        raise ValueError("empty duration")
    pos = 0
    total = 0
    for m in _PART.finditer(s):
        if m.start() != pos:
            raise ValueError(f"invalid duration: {text!r}")
        total += int(m.group(1)) * _UNITS[m.group(2)]
        pos = m.end()
    if pos != len(s):
        raise ValueError(f"invalid duration: {text!r}")
    return total
