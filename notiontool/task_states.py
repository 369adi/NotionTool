"""Declared task states, the allowed moves between them, and the one helper that writes a task state (rank 13).

Log-only first: set_state() never refuses a move. An illegal move (one that is not in TRANSITIONS) is still
written, and an 'illegal_transition' event (task id, old, new, caller) shows where the table and the code
disagree. Nothing in here raises, so a bad table can never stop the coordinator.

The table lists the moves the engine really makes today (read from every task state write in engine.py).
A state a task already has may be written again (a repeated reconcile check stays 'uncertain').
"""
import os
import sys

# Every task state. 'checkpoint' is written by chat_context; 'preparing' is only a submission phase today
# but is part of the lease set, so it is declared here as well.
TASK_STATES = (
    'queued', 'retry', 'running', 'cancelling', 'uncertain', 'checkpoint', 'preparing',
    'blocked', 'failed', 'done', 'cancelled', 'skipped',
)

# old state -> states it may move to.
TRANSITIONS = {
    # waiting for dispatch
    'queued': frozenset({'running', 'blocked', 'cancelled'}),
    'retry': frozenset({'running', 'blocked', 'cancelled'}),
    # a turn is on its way or its answer is not confirmed yet
    'running': frozenset({'done', 'failed', 'blocked', 'retry', 'uncertain', 'cancelling', 'cancelled'}),
    'cancelling': frozenset({'cancelled', 'retry', 'uncertain', 'blocked'}),
    'uncertain': frozenset({'done', 'failed', 'blocked', 'retry', 'cancelled'}),
    # parked / finished
    'blocked': frozenset({'queued', 'retry', 'done', 'failed', 'cancelled', 'skipped'}),
    'failed': frozenset({'queued', 'retry', 'skipped'}),
    # done/failed/blocked -> retry: _no_tool_access runs after the reported status was stored
    'done': frozenset({'retry'}),
    'cancelled': frozenset(),
    'skipped': frozenset(),
    # written by chat_context, not by the engine
    'checkpoint': frozenset({'done', 'cancelled'}),
    'preparing': frozenset(),
}

# Tasks whose assignment token (capability) still works: engine write checks and bridge.assignment().
# An uncertain task may still be working in Notion, so it stays live. A tuple, as before.
RUN_LIVE_STATES = ('running', 'uncertain')

# Tasks that still hold their file write leases (file_leases.release_for_tasks). A frozenset, as before.
# This is a different, wider set than RUN_LIVE_STATES: never use one in place of the other.
LEASE_LIVE_STATES = frozenset({'running', 'cancelling', 'uncertain', 'checkpoint', 'preparing'})


def is_legal(old, new):
    """True when moving a task from old to new is allowed. No state yet (a new task) and a repeat are allowed."""
    if new not in TRANSITIONS:
        return False
    if old is None or old == '' or old == new:
        return True
    return new in TRANSITIONS.get(old, ())


def _caller():
    try:
        code = sys._getframe(2).f_code
        return '%s:%s' % (os.path.basename(code.co_filename), code.co_name)
    except Exception:
        return ''


def set_state(store, task, new, reason='', caller='', **fields):
    """Write task['state'] = new (and any other task fields) and return True when the move is allowed.

    An illegal move is still written; it only adds an 'illegal_transition' event. Never raises.
    """
    legal = True
    try:
        old = task.get('state')
        legal = is_legal(old, new)
        if not legal:
            who = caller or _caller()
            store.event('illegal_transition', task.get('run_id') or '', commit=False, task_id=task.get('id', ''),
                        old=str(old), new=str(new), caller=who, reason=str(reason or '')[:200],
                        message='Task state %s -> %s is not in the declared table (%s)' % (old, new, who))
    except Exception:
        pass
    task['state'] = new
    if fields:
        task.update(fields)
    return legal
