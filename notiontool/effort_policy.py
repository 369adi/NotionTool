"""Effort policy: ordered effort levels and one-step escalation after failures.

Pure functions only. No I/O, no network, no state outside the returned values.

Levels are ordered low < standard < high < max. Each level maps to four
settings that never go down from one level to the next:
decomposition_depth, context_budget_tokens, review_rounds, max_fix_rounds.

route_for_request() decides direct vs. role-team run from difficulty.classify();
it is not yet called by operator_roles.choose_quality(), so it does not change
real routing yet. The effort levels are not wired into engine.py yet.
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


# ---- Notion reasoning effort per request ------------------------------------------------
# The levels Notion's model catalog uses (getAvailableModels -> modelConfiguration.
# supportedReasoningEfforts / defaultReasoningEffort), ordered weakest to strongest.
# Evidence for the names and the request field: work/effort_field.md.
REASONING_ORDER = ("low", "medium", "high", "xhigh", "max")

# Rule I47 (user 2026-10-21): no Coworker request ever runs on 'max'; their top level is 'xhigh'.
# Only Main chats (the user's own pick) keep the full range.
COWORKER_TOP_EFFORT = "xhigh"
MAIN_PURPOSES = ("main", "operator", "spare_main", "external")


def cap_coworker(effort, purpose=""):
    """Levels above COWORKER_TOP_EFFORT become COWORKER_TOP_EFFORT for every non-Main request."""
    if purpose in MAIN_PURPOSES or not isinstance(effort, str):
        return effort
    level = effort.strip().lower()
    if level in REASONING_ORDER and REASONING_ORDER.index(level) > REASONING_ORDER.index(COWORKER_TOP_EFFORT):
        return COWORKER_TOP_EFFORT
    return effort

# task_class groups for the reasoning effort rule (task_router class names).
# User 2026-10-21: xhigh only for the hardest work (risk high); reviews run on high, never lower.
TOP_EFFORT_CLASSES = ()
STEADY_EFFORT_CLASSES = ("review",)
LIGHT_EFFORT_CLASSES = ("routine", "docs", "test")


def _levels(values):
    names = {str(v).strip().lower() for v in (values or ()) if isinstance(v, str)}
    return tuple(level for level in REASONING_ORDER if level in names)


def catalog_efforts(entry):
    """Return (supported, default) for one getAvailableModels entry.

    supported is ordered by REASONING_ORDER; unknown names are dropped. default is
    None unless it is one of the supported levels. A missing entry gives ((), None).
    """
    if not isinstance(entry, dict):
        return (), None
    conf = entry.get("modelConfiguration")
    conf = conf if isinstance(conf, dict) else {}
    supported = _levels(conf.get("supportedReasoningEfforts", entry.get("supportedReasoningEfforts")))
    default = conf.get("defaultReasoningEffort", entry.get("defaultReasoningEffort"))
    default = default.strip().lower() if isinstance(default, str) else None
    return supported, (default if default in supported else None)


def clamp_effort(target, supported):
    """Clamp target to the supported levels: the strongest supported level not above
    target, else the weakest supported level. No supported levels -> None."""
    supported = _levels(supported)
    if not supported:
        return None
    if target in supported:
        return target
    index = REASONING_ORDER.index(target) if target in REASONING_ORDER else 0
    below = [level for level in supported if REASONING_ORDER.index(level) <= index]
    return below[-1] if below else supported[0]


def choose_reasoning_effort(purpose="", task_class="", risk="", supported=(), default=None):
    """Pick the reasoning effort for one request. Returns (effort or None, reason).

    - Main chat                                 -> the highest supported level.
    - risk high (only the hardest work)         -> COWORKER_TOP_EFFORT (xhigh), never max.
    - task_class review                         -> high (never lower, never xhigh unless risk high).
    - routine/docs/test, or risk low            -> the model's default, else low.
    - everything else (code/research/design/unclassified, medium risk) -> high.
    The result is always clamped to the supported levels of the model.
    """
    supported = _levels(supported)
    default = default if default in supported else None
    cls = str(task_class or "").strip().lower()
    rk = str(risk or "").strip().lower()
    if not supported:
        return None, "no supportedReasoningEfforts for this model in the cached catalog; no effort set"
    if purpose == "main":
        rule, target = "Main chat: highest supported effort", supported[-1]
    elif cls in TOP_EFFORT_CLASSES:
        rule, target = "task_class %s: highest supported effort" % cls, supported[-1]
    elif rk == "high":
        rule, target = "risk high (hardest work): top Coworker effort", COWORKER_TOP_EFFORT
    elif cls in STEADY_EFFORT_CLASSES:
        rule, target = "task_class %s: high effort" % cls, "high"
    elif cls in LIGHT_EFFORT_CLASSES or rk == "low":
        what = ("task_class " + cls) if cls in LIGHT_EFFORT_CLASSES else "risk low"
        if default:
            rule, target = "%s: model default effort" % what, default
        else:
            rule, target = "%s: low effort (model has no default)" % what, "low"
    else:
        rule, target = "%s, risk %s: high effort" % ("task_class " + cls if cls else "no task_class",
                                                    rk or "medium"), "high"
    effort = clamp_effort(cap_coworker(target, purpose), supported)
    reason = rule + " -> " + effort
    if effort != target:
        reason += " (clamped from %s)" % target
    return effort, reason + "; supported: " + ", ".join(supported)


# ---- Request difficulty -> route ----------------------------------------------------------
# route_for_request() decides whether a request goes to the full role-team run
# (plan/critique/review) or directly to single coworkers, from the CONTENT of the request
# (notiontool.difficulty.classify), never from its length. A 'team' result only ever raises
# the route; a route that is already 'team' is never lowered. Not yet called by
# operator_roles.choose_quality(): until that wiring lands, real routing is unchanged.
ROUTES = ("direct", "team")


def route_for_request(text, context=None, current=None):
    """Return {'route', 'raised', 'score', 'reasons', 'reason'} for one operator request.

    current is the route decided so far ('direct', 'team' or None = 'direct').
    - classify -> team: the route becomes 'team' (raised is True if it was 'direct').
    - classify -> direct: the route stays as current.
    Unknown current values raise EffortPolicyError; bad text/context raise TypeError.
    """
    from . import difficulty

    if current is not None and current not in ROUTES:
        raise EffortPolicyError(
            "unknown route %r; expected one of %s" % (current, ", ".join(ROUTES))
        )
    base = current or "direct"
    verdict = difficulty.classify(text, context)
    to_team = verdict["route"] == "team"
    route = "team" if to_team else base
    raised = to_team and base != "team"
    if raised:
        reason = "difficulty %.3f -> team: raised %s -> team" % (verdict["score"], base)
    elif route == "team" and not to_team:
        reason = "route already team, kept (difficulty %.3f -> direct)" % verdict["score"]
    else:
        reason = "difficulty %.3f -> %s: route %s" % (verdict["score"], verdict["route"], route)
    return {"route": route, "raised": raised, "score": verdict["score"],
            "reasons": list(verdict["reasons"]), "reason": reason}
