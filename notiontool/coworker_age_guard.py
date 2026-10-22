"""I32: hard 23 h age guard for Coworker chats.

The normal path is the automatic age rotation in team_rotation.py (coworker_max_age_hours, default 22).
This guard is the safety net when that rotation fails: a Coworker (purpose 'worker') whose chat is
23 hours or older is never given a NEW task (executor, reviewer, fixer, any fallback). The limit is
always HARD_MAX_AGE_HOURS, independent of the settings.

- Unknown created_at counts as NOT too old; team_rotation.TeamRotator.backfill stamps it.
- A task already running on a chat that passes 23 h finishes normally; nothing is aborted.
- When no young Coworker is free the task simply stays queued (never failed, never resent, I5).
- Event coworker_too_old (worker_id, age_hours) at most once per worker per EVENT_INTERVAL_S.
"""
import threading
import time

from .team_rotation import age_seconds

HARD_MAX_AGE_HOURS = 23
HARD_MAX_AGE_S = HARD_MAX_AGE_HOURS * 3600
EVENT_INTERVAL_S = 3600

_lock = threading.Lock()


def too_old(worker, now=None):
    """True when the chat is HARD_MAX_AGE_HOURS or older; False while created_at is unknown."""
    if not worker:
        return False
    age = age_seconds(worker, time.time() if now is None else now)
    return age is not None and age >= HARD_MAX_AGE_S


def age_hours(worker, now=None):
    age = age_seconds(worker or {}, time.time() if now is None else now)
    return round(age / 3600.0, 1) if age is not None else None


def is_coworker(worker):
    return (worker or {}).get('purpose', 'worker') == 'worker'


def _marks(store):
    marks = getattr(store, '_coworker_too_old_at', None)
    if marks is None:
        marks = {}
        try:
            setattr(store, '_coworker_too_old_at', marks)
        except Exception:
            pass
    return marks


def note(store, worker, now=None, run_id=''):
    """Emit coworker_too_old once per worker per hour. Returns True when the event was written."""
    now = time.time() if now is None else now
    ident = (worker or {}).get('id', '')
    with _lock:
        marks = _marks(store)
        last = marks.get(ident)
        if last is not None and now - last < EVENT_INTERVAL_S:
            return False
        marks[ident] = now
    try:
        store.event('coworker_too_old', run_id or '', worker_id=ident, age_hours=age_hours(worker, now),
                    message='Coworker chat is %s h old (limit %d h); it gets no new task until it is rotated'
                    % (age_hours(worker, now), HARD_MAX_AGE_HOURS))
    except Exception:
        return False
    return True


def young(store, workers, now=None, run_id=''):
    """workers without the Coworkers that are too old (Mains and operators are kept); notes the skipped ones."""
    now = time.time() if now is None else now
    keep = []
    for worker in workers:
        if is_coworker(worker) and too_old(worker, now):
            note(store, worker, now, run_id)
            continue
        keep.append(worker)
    return keep
