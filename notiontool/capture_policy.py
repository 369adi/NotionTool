# Capture policy for worker screen captures.
# capture_allowed is a pure decision function: it reads only its arguments
# and never touches files, the network or persistent state.

VISUAL_CLASSES = ('ui', 'visual', 'game', 'design', 'screenshot')
EXPLICIT_CLASS = 'screenshot'
DEFAULT_MAX_CAPTURES = 4
# capture_all_coworkers (default True when the key is missing): every coworker may capture
# to check the current state, for every task class, with this default limit per task.
ALL_COWORKERS_MAX_CAPTURES = 12

REASON_TEXTS = {
    'allowed': 'Screen capture allowed.',
    'explicit_request': 'Screen capture allowed because the task explicitly asks for a screenshot.',
    'class_not_visual': 'Screen capture refused: this task class is not a visual task.',
    'not_enabled': 'Screen capture refused: screen capture is not enabled for this project.',
    'limit_reached': 'Screen capture refused: the maximum number of captures for this task is reached.',
    'pair_incomplete': 'Screen capture refused: a before capture now would leave its before/after pair incomplete.',
    'invalid_count': 'Screen capture refused: the number of captures already taken is invalid.',
}


def _norm(value):
    if not isinstance(value, str):
        return ''
    return value.strip().lower()


def _settings(project_settings):
    return project_settings if isinstance(project_settings, dict) else {}


def visual_classes(project_settings=None):
    # project_settings['capture_classes'] overrides the default visual classes.
    raw = _settings(project_settings).get('capture_classes')
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set, frozenset)):
        raw = VISUAL_CLASSES
    return frozenset(c for c in (_norm(x) for x in raw) if c)


def all_coworkers(project_settings=None):
    # Only an explicit False switches back to the old visual-class rules.
    return _settings(project_settings).get('capture_all_coworkers', True) is not False


def max_captures(project_settings=None):
    settings = _settings(project_settings)
    default = ALL_COWORKERS_MAX_CAPTURES if all_coworkers(settings) else DEFAULT_MAX_CAPTURES
    raw = settings.get('max_captures')
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return default
    return raw


def _taken(already_taken):
    if already_taken is None:
        return 0
    if isinstance(already_taken, bool) or not isinstance(already_taken, int) or already_taken < 0:
        return None
    return already_taken


def capture_allowed(task_class, project_settings, already_taken):
    # Returns (allowed, reason) where reason is a short snake_case code.
    settings = _settings(project_settings)
    taken = _taken(already_taken)
    if taken is None:
        return False, 'invalid_count'
    if all_coworkers(settings):
        # Every class (also an empty one) is allowed, capture_enabled counts as True
        # and the before/after pair rule is skipped; only the per-task limit applies.
        if taken >= max_captures(settings):
            return False, 'limit_reached'
        return True, 'allowed'
    cls = _norm(task_class)
    if not cls or cls not in visual_classes(settings):
        return False, 'class_not_visual'
    explicit = cls == EXPLICIT_CLASS or settings.get('explicit_screenshot') is True
    enabled = settings.get('capture_enabled') is True
    if not enabled and not explicit:
        return False, 'not_enabled'
    limit = max_captures(settings)
    if taken >= limit:
        return False, 'limit_reached'
    # Captures come in before/after pairs: an even count means a before capture.
    # Refuse a before capture that would be the last one allowed.
    if taken % 2 == 0 and taken + 1 >= limit:
        return False, 'pair_incomplete'
    return True, 'allowed' if enabled else 'explicit_request'


def reason_text(reason):
    return REASON_TEXTS.get(reason, 'Screen capture refused: unknown policy reason.')
