"""parse_duration: '1h30m' style durations to whole seconds."""
import re
from decimal import ROUND_HALF_UP, Decimal

_UNITS = {'d': 86400, 'h': 3600, 'm': 60, 's': 1}
_ORDER = 'dhms'
_PLAIN = re.compile(r'[0-9]+')
_PART = re.compile(r'([0-9]+(?:\.[0-9]+)?)([A-Za-z]*)')
_SPACE = re.compile(r'\s*')


def parse_duration(text: str) -> int:
    """Seconds for a duration such as '1h30m', '2d4h', '45s', '1h 30m 10s', '1.5h' or '90'.

    Units d, h, m, s (case-insensitive), each at most once, in that order. Spaces are allowed
    between parts, not between a number and its unit. A plain integer means seconds. Decimal
    values are rounded to the nearest second (halves round up).
    """
    if not isinstance(text, str):
        raise TypeError('parse_duration() needs a str, got %s' % type(text).__name__)
    s = text.strip()
    if not s:
        raise ValueError('empty duration')
    if '-' in s:
        raise ValueError('negative values are not allowed: %r' % text)
    if _PLAIN.fullmatch(s):
        return int(s)
    total = Decimal(0)
    last = -1
    pos = 0
    while pos < len(s):
        if pos:
            pos = _SPACE.match(s, pos).end()
        match = _PART.match(s, pos)
        if not match:
            raise ValueError('invalid duration: %r' % text)
        number, unit = match.group(1), match.group(2).lower()
        if not unit:
            raise ValueError('number without unit in %r' % text)
        if unit not in _UNITS:
            raise ValueError('unknown unit %r in %r' % (match.group(2), text))
        index = _ORDER.index(unit)
        if index == last:
            raise ValueError('repeated unit %r in %r' % (unit, text))
        if index < last:
            raise ValueError('units out of order in %r (use d, h, m, s)' % text)
        last = index
        total += Decimal(number) * _UNITS[unit]
        pos = match.end()
    return int(total.quantize(Decimal(1), rounding=ROUND_HALF_UP))
