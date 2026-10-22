"""Problem inbox backend: overlay errors reported by the page (POST /api/ui/error, action ui_error).

The Live view (web/desktop/live.js, liveUtil.problems) builds the Problems list itself from the
pushed state: failed/blocked tasks, unclear tasks, welcome/team warnings, harness disconnects,
failed proof tests and 'ui_error' events. This module only records overlay errors as store
events, deduped and rate limited, so a broken render loop can never flood the store.
Nothing here sends a Notion request or creates a chat.
"""
import threading
import time

EVENT_KIND = 'ui_error'
DESK_ACTION_NAMES = ('ui_error',)
MAX_PER_WINDOW = 5        # recorded errors per window (all messages together)
WINDOW_SECONDS = 60.0
REPEAT_SECONDS = 600.0    # the same part+source+message is recorded at most once in this time
MESSAGE_LIMIT = 200


def _text(value, limit):
    return ' '.join(str(value if value is not None else '').split())[:limit]


class UiErrorLimiter:
    """Dedupe + rate limit for overlay error reports. Thread safe."""

    def __init__(self, clock=time.monotonic, max_per_window=MAX_PER_WINDOW,
                 window=WINDOW_SECONDS, repeat=REPEAT_SECONDS):
        self.clock = clock
        self.max_per_window = max_per_window
        self.window = window
        self.repeat = repeat
        self._recent = []
        self._seen = {}
        self._lock = threading.Lock()

    def allow(self, key):
        """'ok', 'duplicate' or 'rate_limited'."""
        with self._lock:
            now = self.clock()
            self._recent = [t for t in self._recent if now - t < self.window]
            for old in [k for k, t in self._seen.items() if now - t >= self.repeat]:
                del self._seen[old]
            if key in self._seen:
                return 'duplicate'
            if len(self._recent) >= self.max_per_window:
                return 'rate_limited'
            self._recent.append(now)
            self._seen[key] = now
            return 'ok'


def limiter(server):
    """The single UiErrorLimiter of this server (created on first use)."""
    found = getattr(server, '_nt_ui_errors', None)
    if found is None:
        found = UiErrorLimiter()
        server._nt_ui_errors = found
    return found


def record(server, body):
    """Record one overlay error {message, source, line, part} as a 'ui_error' event."""
    body = body if isinstance(body, dict) else {}
    message = _text(body.get('message'), MESSAGE_LIMIT)
    if not message:
        raise ValueError('An error message is required')
    part = _text(body.get('part'), 40)
    source = _text(body.get('source'), 120)
    try:
        line = max(0, int(body.get('line') or 0))
    except (TypeError, ValueError):
        line = 0
    status = limiter(server).allow((part, source, message))
    if status != 'ok':
        return dict(ok=True, recorded=False, reason=status)
    text = 'Overlay error in %s: %s' % (part or source or 'overlay', message)
    if line:
        text += ' (line %d)' % line
    server.store.event(EVENT_KIND, message=text)
    return dict(ok=True, recorded=True)


def handle(server, body):
    """POST /api/ui/error."""
    return record(server, body)


def desk_handlers(server):
    return {'ui_error': lambda args: record(server, args)}


def install_desk_actions(desktop_shell=None):
    """Add the ui_error action to desktop_shell (DESK_ACTIONS + desktop_handlers) at runtime.

    desktop_shell.py is owned by other work, so this hooks in without editing it. Idempotent,
    and it keeps any wrapper installed before (restart_guard.install_desk_actions).
    """
    if desktop_shell is None:
        from . import desktop_shell
    missing = tuple(a for a in DESK_ACTION_NAMES if a not in desktop_shell.DESK_ACTIONS)
    if missing:
        desktop_shell.DESK_ACTIONS = tuple(desktop_shell.DESK_ACTIONS) + missing
    original = desktop_shell.desktop_handlers
    if getattr(original, 'nt_problem_actions', False):
        return desktop_shell

    def desktop_handlers(server, *args, **kwargs):
        handlers = original(server, *args, **kwargs)
        handlers.update(desk_handlers(server))
        return handlers
    desktop_handlers.nt_problem_actions = True
    if getattr(original, 'nt_restart_actions', False):
        desktop_handlers.nt_restart_actions = True
    desktop_shell.desktop_handlers = desktop_handlers
    return desktop_shell
