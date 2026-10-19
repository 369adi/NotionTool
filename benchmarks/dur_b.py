"""parse_duration: '1h 30m' -> 5400 seconds."""
import re
from decimal import ROUND_HALF_UP, Decimal

__all__ = ['parse_duration']

# Units in the only allowed (descending) order, with their size in seconds.
_UNITS = (('d', 86400), ('h', 3600), ('m', 60), ('s', 1))
_ORDER = {u: i for i, (u, _) in enumerate(_UNITS)}
_PART = re.compile(r'([0-9]+(?:\.[0-9]+)?)([a-z]+)')
_SPACE = re.compile(r'\s*')
_PLAIN = re.compile(r'[0-9]+')


def parse_duration(text: str) -> int:
    """Return the number of seconds in a duration such as '1h30m', '2d4h', '45s' or '90'.

    Units d, h, m, s (case-insensitive), each at most once and in that order; spaces are
    allowed between parts but not between a number and its unit. A plain integer means
    seconds. Decimals are allowed ('1.5h'); the total is rounded to the nearest second
    (halves round up). Raises TypeError for non-str input and ValueError for invalid text.
    """
    if not isinstance(text, str):
        raise TypeError('parse_duration() expects a str, got %s' % type(text).__name__)
    s = text.strip().lower()
    if not s:
        raise ValueError('empty duration')
    if _PLAIN.fullmatch(s):
        return int(s)
    total = Decimal(0)
    last = -1
    pos = 0
    while pos < len(s):
        m = _PART.match(s, pos)
        if not m:
            raise ValueError('invalid duration %r at position %d' % (text, pos))
        number, unit = m.group(1), m.group(2)
        if unit not in _ORDER:
            raise ValueError('unknown unit %r in %r' % (unit, text))
        idx = _ORDER[unit]
        if idx == last:
            raise ValueError('repeated unit %r in %r' % (unit, text))
        if idx < last:
            raise ValueError('unit %r out of order in %r (use d, h, m, s)' % (unit, text))
        last = idx
        total += Decimal(number) * _UNITS[idx][1]
        pos = _SPACE.match(s, m.end()).end()
    return int(total.quantize(Decimal(1), rounding=ROUND_HALF_UP))
