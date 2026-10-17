"""Pure reconcile policy for tasks whose Notion completion is unconfirmed.

Invariant I5: never resend unless Notion provably ended the turn with an
error before any output. This module only decides; it never sends anything,
never touches the store and never uses the network. The engine wires it later.

Context in the current code (not wired yet):
- engine.py marks tasks 'uncertain' with 'Awaiting confirmation from Notion;
  request will not be resent', or 'blocked' with remote_unknown=True when
  overdue (engine.py:728-730 and :748-749).
- server.py /api/task/reconcile resets a remote_unknown task back to
  'uncertain' (server.py:602-608).
"""

import math

WAIT = 'wait'
RECONCILE = 'reconcile'
ACCEPT_OUTPUT = 'accept_output'
REQUEUE_PROVEN_ERROR = 'requeue_proven_error'
ESCALATE = 'escalate'

ACTIONS = (WAIT, RECONCILE, ACCEPT_OUTPUT, REQUEUE_PROVEN_ERROR, ESCALATE)

DEFAULT_SETTINGS = {
    'reconcile_after_s': 90,
    'escalate_after_s': 900,
    'max_reconcile_attempts': 3,
}

REMOTE_STATES = ('running', 'done', 'error', 'unknown', 'missing')
TASK_STATES = ('uncertain', 'remote_unknown', 'blocked')

_MISSING = object()


def _field(obj, name, default=_MISSING):
    """Read a field from a mapping or an object; return default when absent."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _is_number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _has_output(value):
    if value is None or value is _MISSING:
        return False
    if isinstance(value, str):
        return value.strip() != ''
    if isinstance(value, (list, tuple, dict, set, bytes)):
        return len(value) > 0
    return True


def _settings(settings):
    """Merge settings over the defaults. Returns (values, error_reason)."""
    merged = dict(DEFAULT_SETTINGS)
    if settings is not None:
        for key in DEFAULT_SETTINGS:
            value = _field(settings, key)
            if value is not _MISSING and value is not None:
                merged[key] = value
    for key in ('reconcile_after_s', 'escalate_after_s'):
        if not _is_number(merged[key]) or merged[key] < 0:
            return None, 'Invalid setting %s=%r' % (key, merged[key])
    limit = merged['max_reconcile_attempts']
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        return None, 'Invalid setting max_reconcile_attempts=%r' % (limit,)
    if merged['reconcile_after_s'] > merged['escalate_after_s']:
        return None, 'Invalid settings: reconcile_after_s is greater than escalate_after_s'
    return merged, None


def decide(task, remote_status, now, settings=None):
    """Return (action, reason) for an unconfirmed task.

    action is one of ACTIONS; reason is a non-empty English string.
    requeue_proven_error is returned only when remote_status proves an error
    before any output (rule 3). Every other doubt gives wait, reconcile or
    escalate. Any unexpected exception gives escalate.
    """
    try:
        return _decide(task, remote_status, now, settings)
    except Exception as exc:  # defensive: a policy bug must never resend
        return ESCALATE, 'Policy error (%s); escalating instead of resending' % type(exc).__name__


def _decide(task, remote_status, now, settings):
    if task is None:
        return ESCALATE, 'Task record is missing'
    task_output = _field(task, 'output', None)
    remote_had_output = _field(remote_status, 'had_output', None)

    # Rule 1: existing output from either side is accepted.
    if _has_output(task_output):
        return ACCEPT_OUTPUT, 'Task already has output; accepting it without a resend'
    if remote_had_output is True:
        return ACCEPT_OUTPUT, 'Notion reports output for this turn; accepting it without a resend'

    # Rule 6 (validation) before the state rules.
    if remote_status is None:
        return ESCALATE, 'Remote status is missing; cannot prove anything, escalating'
    values, problem = _settings(settings)
    if problem:
        return ESCALATE, problem
    task_state = _field(task, 'state')
    if task_state not in TASK_STATES:
        return ESCALATE, 'Unexpected task state %r' % (task_state,)
    if task_state == 'blocked' and _field(task, 'remote_unknown', False) is not True:
        return ESCALATE, 'Blocked task is not marked remote_unknown'
    remote_state = _field(remote_status, 'state')
    if not isinstance(remote_state, str) or remote_state not in REMOTE_STATES:
        return ESCALATE, 'Unexpected remote state %r' % (remote_state,)
    if remote_had_output not in (True, False, None) or isinstance(remote_had_output, int) and not isinstance(remote_had_output, bool):
        return ESCALATE, 'Invalid remote had_output %r' % (remote_had_output,)
    error_before = _field(remote_status, 'error_before_output', None)
    if error_before not in (True, False, None) or isinstance(error_before, int) and not isinstance(error_before, bool):
        return ESCALATE, 'Invalid remote error_before_output %r' % (error_before,)

    # Rule 2: done is accepted.
    if remote_state == 'done':
        return ACCEPT_OUTPUT, 'Notion reports the turn as done; accepting the result'

    # Rule 3: the only path to a requeue.
    if remote_state == 'error':
        if error_before is True and remote_had_output is False and not _has_output(task_output):
            return REQUEUE_PROVEN_ERROR, 'Notion proved the turn ended with an error before any output'
        return ESCALATE, 'Remote error is not proven to be before any output; not resending'

    # Rule 4: still running.
    if remote_state == 'running':
        return WAIT, 'Notion reports the turn is still running'

    # Rule 5: unknown or missing.
    since = _field(task, 'uncertain_since')
    if not _is_number(now):
        return ESCALATE, 'Invalid current time %r' % (now,)
    if not _is_number(since):
        return ESCALATE, 'Invalid or missing uncertain_since %r' % (since,)
    elapsed = now - since
    if elapsed < 0:
        return ESCALATE, 'uncertain_since lies in the future'
    attempts = _field(task, 'reconcile_attempts', 0)
    if attempts is None:
        attempts = 0
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
        return ESCALATE, 'Invalid reconcile_attempts %r' % (attempts,)
    if elapsed < values['reconcile_after_s']:
        return WAIT, 'Remote state is %s; waiting %.0f s before reconciling' % (
            remote_state, values['reconcile_after_s'] - elapsed)
    if attempts < values['max_reconcile_attempts'] and elapsed < values['escalate_after_s']:
        return RECONCILE, 'Remote state is %s after %.0f s; reconcile attempt %d of %d' % (
            remote_state, elapsed, attempts + 1, values['max_reconcile_attempts'])
    if attempts >= values['max_reconcile_attempts']:
        return ESCALATE, 'Remote state is still %s after %d reconcile attempts' % (remote_state, attempts)
    return ESCALATE, 'Remote state is still %s after %.0f s (limit %s s)' % (
        remote_state, elapsed, values['escalate_after_s'])
