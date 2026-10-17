"""Stuck-turn watchdog for coworker turns.

Pure module: standard library only, no file I/O, no network, no clock.
Nothing here is wired yet (NOT WIRED).

Field names follow the real records:
- engine.py:595 inflight entries: task_id, lease, worker_id, run_id, cancel.
- task timestamps use the '*_at' style: sent_at, first_output_at,
  remote_done_at (engine.py:826, notion.py:908, notion.py:917).
- worker: id, label, thread_id, account_id (turn_watch.py:89, notion.py:1047).
- bridge.py tool events carry task_id (bridge.py:269, :457).

A turn record passed to decide_stalled():
    {turn_id, thread_id, task_id, worker_id, worker_label,
     sent_at, last_tool_at, last_output_at, last_event_at}
turn_id falls back to thread_id + ':' + task_id when missing.

An open gateway tool call: {thread_id, task_id, ...}. It blocks a stall when
its thread_id or its task_id matches the turn, so a long-running command
never counts as stalled.
"""

from __future__ import annotations

import copy
import math

DEFAULT_STALL_AFTER_S = 120
DEFAULT_MAX_STALLS = 3
HANDLED_LIMIT = 500
PROGRESS_FIELDS = ('sent_at', 'last_tool_at', 'last_output_at', 'last_event_at')

REASON_STALLED = 'stalled'
REASON_EMPTY_REPLY = 'empty_reply'


def _num(value, default=0.0):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(result) else result


def _int_setting(settings, key, default, minimum):
    try:
        value = int(settings.get(key, default))
    except (TypeError, ValueError):
        return default
    return max(minimum, value)


def turn_id(turn):
    explicit = (turn or {}).get('turn_id')
    if explicit:
        return str(explicit)
    return str(turn.get('thread_id') or '') + ':' + str(turn.get('task_id') or '')


def last_progress(turn):
    """Newest of turn start, last tool call, last output text and last event."""
    return max(_num((turn or {}).get(field)) for field in PROGRESS_FIELDS)


def _normal_state(state):
    state = state or {}
    stalls = {}
    for task_id, entry in (state.get('stalls') or {}).items():
        entry = entry or {}
        stalls[str(task_id)] = {
            'count': int(_num(entry.get('count'), 0)),
            'worker_ids': [str(w) for w in (entry.get('worker_ids') or [])],
        }
    return {
        'handled_turn_ids': [str(t) for t in (state.get('handled_turn_ids') or [])],
        'stalls': stalls,
    }


def _has_open_call(turn, open_tool_calls):
    thread = turn.get('thread_id')
    task = turn.get('task_id')
    for call in open_tool_calls or []:
        call = call or {}
        if thread and call.get('thread_id') == thread:
            return True
        if task and call.get('task_id') == task:
            return True
    return False


def stop_turn(thread_id, task_id, reason, **extra):
    action = {'kind': 'stop_turn', 'thread_id': thread_id, 'task_id': task_id, 'reason': reason}
    action.update(extra)
    return action


def decide_stalled(turns, open_tool_calls, now, settings, state=None):
    """Return a list of actions for stalled turns. Inputs are never changed."""
    settings = settings or {}
    if settings.get('stall_watch') is False:
        return []
    stall_after = _num(settings.get('stall_after_s', DEFAULT_STALL_AFTER_S), DEFAULT_STALL_AFTER_S)
    max_stalls = _int_setting(settings, 'max_stalls', DEFAULT_MAX_STALLS, 1)
    current = _normal_state(state)
    handled = set(current['handled_turn_ids'])
    stalls = copy.deepcopy(current['stalls'])
    now = _num(now)
    actions = []
    for turn in turns or []:
        turn = turn or {}
        tid = turn_id(turn)
        if tid in handled:
            continue
        idle = now - last_progress(turn)
        if idle < stall_after or _has_open_call(turn, open_tool_calls):
            continue
        handled.add(tid)
        task_id = turn.get('task_id')
        worker_id = turn.get('worker_id')
        entry = stalls.setdefault(str(task_id), {'count': 0, 'worker_ids': []})
        entry['count'] += 1
        if worker_id and str(worker_id) not in entry['worker_ids']:
            entry['worker_ids'].append(str(worker_id))
        info = dict(turn_id=tid, worker_id=worker_id, worker_label=turn.get('worker_label'),
                    idle_s=round(idle, 1), stall_number=entry['count'])
        actions.append(stop_turn(turn.get('thread_id'), task_id, REASON_STALLED, **info))
        if entry['count'] >= max_stalls:
            actions.append({'kind': 'fail', 'task_id': task_id, 'reason': REASON_STALLED,
                            'turn_id': tid, 'worker_id': worker_id,
                            'worker_label': turn.get('worker_label'), 'stall_number': entry['count']})
        else:
            requeue = {'kind': 'requeue', 'task_id': task_id, 'avoid_worker_id': worker_id,
                       'turn_id': tid, 'worker_label': turn.get('worker_label'),
                       'stall_number': entry['count']}
            if entry['count'] >= 2:
                requeue['avoid_worker_ids'] = list(entry['worker_ids'])
            actions.append(requeue)
    return actions


def next_state(state, actions):
    """Apply actions from decide_stalled to state. Never changes its inputs."""
    new_state = copy.deepcopy(_normal_state(state))
    handled = new_state['handled_turn_ids']
    for action in actions or []:
        if (action or {}).get('kind') != 'stop_turn' or action.get('reason') != REASON_STALLED:
            continue
        tid = action.get('turn_id')
        if tid and tid not in handled:
            handled.append(str(tid))
        task_id = str(action.get('task_id'))
        entry = new_state['stalls'].setdefault(task_id, {'count': 0, 'worker_ids': []})
        entry['count'] = max(entry['count'] + 1, int(_num(action.get('stall_number'), 0)))
        worker_id = action.get('worker_id')
        if worker_id and str(worker_id) not in entry['worker_ids']:
            entry['worker_ids'].append(str(worker_id))
    new_state['handled_turn_ids'] = handled[-HANDLED_LIMIT:]
    return new_state


def on_empty_reply(task, thread_id):
    """Stop the old Notion turn first, then retry the task."""
    task = task or {}
    task_id = task.get('id')
    worker_id = task.get('worker_id')
    return [
        stop_turn(thread_id, task_id, REASON_EMPTY_REPLY, worker_id=worker_id),
        {'kind': 'retry', 'task_id': task_id, 'avoid_worker_id': worker_id, 'reason': REASON_EMPTY_REPLY},
    ]


def _who(action):
    label = action.get('worker_label') or action.get('worker_id') or 'Coworker'
    return str(label)


def _minutes(seconds):
    minutes = max(1, int(round(_num(seconds) / 60.0)))
    return '%d min' % minutes


def event_text(action):
    """One short English line for the overview."""
    action = action or {}
    kind = action.get('kind')
    task = str(action.get('task_id') or '')[:10]
    if kind == 'stop_turn' and action.get('reason') == REASON_STALLED:
        return '%s stalled for %s; stopped and re-queued' % (_who(action), _minutes(action.get('idle_s')))
    if kind == 'stop_turn':
        return 'Stopped the old turn of task %s after an empty reply' % task
    if kind == 'requeue':
        return 'Task %s re-queued on another coworker' % task
    if kind == 'retry':
        return 'Task %s retried after an empty reply' % task
    if kind == 'fail':
        return 'Task %s failed: stalled %d times' % (task, int(_num(action.get('stall_number'), 0)))
    return 'Stall watch: ' + str(kind)
