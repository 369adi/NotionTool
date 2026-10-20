"""Perfection levels and destructive change guard.

Pure functions only: no file I/O, no network, no imports from engine,
notion or server. Not wired into engine.py yet (NOT WIRED).
"""

STAGES = ("tests", "static", "diff", "requirements", "screenshot", "model_review")
MODEL_REVIEW_POLICIES = ("never", "high_risk", "always")
LEVEL_ORDER = ("standard", "high", "perfection")
DEFAULT_LEVEL = "standard"
CHANGE_KINDS = ("delete", "overwrite", "edit")
MASS_EDIT_THRESHOLD = 10

LEVELS = {
    "standard": {
        "verifier_depth": ("tests", "static"),
        "max_fix_rounds": 1,
        "model_review": "never",
        "request_budget": 20,
        "time_budget_s": 600,
    },
    "high": {
        "verifier_depth": ("tests", "static", "diff", "requirements"),
        "max_fix_rounds": 2,
        "model_review": "high_risk",
        "request_budget": 50,
        "time_budget_s": 1800,
    },
    "perfection": {
        "verifier_depth": STAGES,
        "max_fix_rounds": 4,
        "model_review": "always",
        "request_budget": 120,
        "time_budget_s": 3600,
    },
}


def _level_key(name):
    """Return a known level name, or DEFAULT_LEVEL for anything else."""
    if isinstance(name, dict):
        name = name.get("name")
    if isinstance(name, str):
        key = name.strip().lower()
        if key in LEVELS:
            return key
    return DEFAULT_LEVEL


def get_level(name):
    """Return a copy of the level config. Unknown names fall back to standard."""
    key = _level_key(name)
    config = dict(LEVELS[key])
    config["verifier_depth"] = tuple(config["verifier_depth"])
    config["name"] = key
    return config


def to_fix_loop_limits(level):
    """Return the fix loop limits for a level name or a get_level() config.

    max_fix_rounds is passed as max_iterations; time_budget_s and request_budget
    are passed on unchanged. max_rounds_without_improvement is not set, so the
    fix loop keeps its own default. Unknown levels fall back to standard.
    Note: fix_loop.decide counts every verdict as an iteration, so a failing
    verdict at iteration max_iterations escalates without another fix round.
    """
    config = get_level(level)
    return {
        "max_iterations": config["max_fix_rounds"],
        "time_budget_s": config["time_budget_s"],
        "request_budget": config["request_budget"],
    }


def escalate_level(current, failed, reason):
    """Move up one level only after a failure with a non-empty reason.

    Returns (level_config, reason_text). In every other case the level stays.
    """
    key = _level_key(current)
    if failed is not True:
        return get_level(key), "no failure: staying at %s" % key
    if not isinstance(reason, str) or not reason.strip():
        return get_level(key), "missing reason: staying at %s" % key
    text = reason.strip()
    index = LEVEL_ORDER.index(key)
    if index + 1 >= len(LEVEL_ORDER):
        return get_level(key), "already at highest level %s: %s" % (key, text)
    target = LEVEL_ORDER[index + 1]
    return get_level(target), "escalated from %s to %s: %s" % (key, target, text)


def _file_count(value):
    if value is None:
        return 1
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def guard_change(change):
    """Decide whether a change may run. Returns (allowed, reason).

    Deletions, overwrites of non-generated files and mass edits (file_count
    above MASS_EDIT_THRESHOLD) need (backup or dry_run) and approved.
    Flags only count when they are exactly True.
    """
    if not isinstance(change, dict):
        return False, "denied: invalid change, expected a mapping"
    kind = change.get("kind")
    kind = kind.strip().lower() if isinstance(kind, str) else ""
    if kind not in CHANGE_KINDS:
        return False, "denied: unknown change kind %r" % change.get("kind")
    count = _file_count(change.get("file_count"))
    if count is None:
        return False, "denied: invalid file_count %r" % change.get("file_count")
    path = change.get("path") or "<no path>"

    risks = []
    if kind == "delete":
        risks.append("deletion")
    if kind == "overwrite" and change.get("generated") is not True:
        risks.append("overwrite of a non-generated file")
    if count > MASS_EDIT_THRESHOLD:
        risks.append("mass edit of %d files (threshold %d)" % (count, MASS_EDIT_THRESHOLD))
    if not risks:
        return True, "allowed: %s of %s is not destructive" % (kind, path)

    what = " and ".join(risks)
    safety = change.get("backup") is True or change.get("dry_run") is True
    if not safety:
        return False, "denied: %s on %s requires a backup or dry run" % (what, path)
    if change.get("approved") is not True:
        return False, "denied: %s on %s requires explicit approval" % (what, path)
    return True, "allowed: %s on %s with backup or dry run and approval" % (what, path)
