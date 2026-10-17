"""Effort policy: ordered effort levels and one-step escalation after failures.

Pure functions only. No I/O, no network, no state outside the returned values.

Levels are ordered low < standard < high < max. Each level maps to four
settings that never go down from one level to the next:
decomposition_depth, context_budget_tokens, review_rounds, max_fix_rounds.

Not wired into engine.py yet.
"""

from __future__ import annotations

LEVELS = ("low", "standard", "high", "max")

SETTING_KEYS = (
    "decomposition_depth",
    "context_budget_tokens",
    "review_rounds",
    "max_fix_rounds",
)

_SETTINGS = {
    "low": {
        "decomposition_depth": 1,
        "context_budget_tokens": 8000,
        "review_rounds": 1,
        "max_fix_rounds": 1,
    },
    "standard": {
        "decomposition_depth": 2,
        "context_budget_tokens": 16000,
        "review_rounds": 1,
        "max_fix_rounds": 2,
    },
    "high": {
        "decomposition_depth": 3,
        "context_budget_tokens": 32000,
        "review_rounds": 2,
        "max_fix_rounds": 3,
    },
    "max": {
        "decomposition_depth": 4,
        "context_budget_tokens": 64000,
        "review_rounds": 3,
        "max_fix_rounds": 4,
    },
}


class EffortPolicyError(ValueError):
    """Raised for an unknown level or a missing escalation reason."""


def _check_level(level):
    if not isinstance(level, str) or level not in _SETTINGS:
        raise EffortPolicyError(
            "unknown effort level %r; expected one of %s" % (level, ", ".join(LEVELS))
        )
    return level


def settings_for(level):
    """Return a copy of the settings dict for a level."""
    return dict(_SETTINGS[_check_level(level)])


def level_index(level):
    """Return the 0-based position of a level in LEVELS."""
    return LEVELS.index(_check_level(level))


def _result(level, reason):
    return {"level": level, "settings": settings_for(level), "reason": reason}


def initial():
    """Start every task at the lowest level."""
    return _result("low", "initial effort level: start low")


def escalate(level, failure, reason):
    """Decide the next effort level.

    - level must be a known level, otherwise EffortPolicyError.
    - reason must be a non-empty string, otherwise EffortPolicyError.
    - Without a failure the level stays the same.
    - With a failure the level goes up exactly one step, but never above max;
      at max it stays at max and the reason says so.
    """
    _check_level(level)
    if not isinstance(reason, str) or not reason.strip():
        raise EffortPolicyError("escalate() requires a non-empty reason")
    reason = reason.strip()
    if not failure:
        return _result(level, "no failure, staying at %s: %s" % (level, reason))
    index = LEVELS.index(level)
    if index == len(LEVELS) - 1:
        return _result(level, "already at max, staying at max: %s" % reason)
    new_level = LEVELS[index + 1]
    return _result(
        new_level, "failure, escalated %s -> %s: %s" % (level, new_level, reason)
    )
