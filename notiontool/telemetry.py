"""Versioned task telemetry: event schema, error classes and per-task metrics.

Every event row keeps seq, ts, run_id and kind; its document carries the schema
version v and the identifiers task_id, worker_id and project_id.
"""
import json
import re

EVENT_SCHEMA = 1
IDENTIFIERS = ('task_id', 'worker_id', 'project_id')

# Normal path of one task, in the order the events are written.
TASK_CHAIN = ('task_queued', 'task_ready', 'lease_acquired', 'context_built', 'remote_sent',
              'first_output', 'remote_done', 'verify_started', 'verify_done',
              'task_finished', 'task_attempt_finished')
# Written by the request coordinator; circuit_* are reserved for the circuit breaker.
COORDINATION_EVENTS = ('request_coordination_adjusted', 'circuit_open', 'circuit_closed')
FALLBACK_EVENTS = ('model_fallback', 'context_reset_fallback', 'approach_fallback')

ERROR_CLASSES = ('TRANSIENT', 'INVALID_REQUEST', 'REMOTE_UNKNOWN', 'MODEL_MISMATCH',
                 'TASK_FAILED', 'ACCOUNT_BLOCKED', 'LOCAL')

# Matched by class name along the MRO, so this module never imports the engine.
_BY_CLASS = {
    'RateLimited': 'TRANSIENT', 'TransientInference': 'TRANSIENT',
    'ContextCheckpointPending': 'TRANSIENT', 'ResetNotAccepted': 'TRANSIENT',
    'PendingRemote': 'REMOTE_UNKNOWN', 'ModelVerificationError': 'MODEL_MISMATCH',
    'ContractError': 'TASK_FAILED',
    'CircuitOpen': 'TRANSIENT',
    'RemotePaused': 'ACCOUNT_BLOCKED',
}


def http_class(status):
    """Error class of an HTTP status returned by Notion."""
    try:
        status = int(status or 0)
    except (TypeError, ValueError):
        return 'LOCAL'
    if status in (401, 403):
        return 'ACCOUNT_BLOCKED'
    if status == 429 or status >= 500:
        return 'TRANSIENT'
    if 400 <= status < 500:
        return 'INVALID_REQUEST'
    return 'LOCAL'


def classify(exc):
    """One error class per failure; '' when there was no failure."""
    if exc is None:
        return ''
    for cls in type(exc).__mro__:
        if cls.__name__ in _BY_CLASS:
            return _BY_CLASS[cls.__name__]
    if type(exc).__name__ == 'NotionHttpError' and hasattr(exc, 'status'):
        return http_class(exc.status)
    text = str(exc)
    match = re.search(r'HTTP (\d{3})', text)
    if match:
        return http_class(match.group(1))
    lowered = text.lower()
    if 'login required' in lowered or 'sign in to this notion account' in lowered:
        return 'ACCOUNT_BLOCKED'
    return 'LOCAL'


def task_events(store, task_id):
    """All retained events of one task in write order."""
    with store.lock:
        rows = store.db.execute("SELECT seq, ts, run_id, kind, doc FROM events "
                                "WHERE json_extract(doc, '$.task_id') = ? ORDER BY seq", (task_id,)).fetchall()
    return [dict(json.loads(doc), seq=seq, ts=ts, run_id=run_id, kind=kind) for seq, ts, run_id, kind, doc in rows]


def chain_gaps(store, task_id):
    """TASK_CHAIN kinds that are missing or out of order for one task; [] means complete."""
    kinds = [event['kind'] for event in task_events(store, task_id)]
    gaps, position = [], 0
    for kind in TASK_CHAIN:
        if kind in kinds[position:]:
            position = kinds.index(kind, position) + 1
        else:
            gaps.append(kind)
    return gaps


def _span(start, end):
    return round(end - start, 3) if start and end and end >= start else None


def task_metrics(store, run_id=None):
    """Runtime, queue wait, time to first output, errors and fallbacks per task, in seconds."""
    tasks = [t for t in store.all('tasks') if run_id is None or t.get('run_id') == run_id]
    with store.lock:
        rows = store.db.execute(
            "SELECT json_extract(doc, '$.task_id'), kind, COUNT(*) FROM events WHERE kind IN (%s) GROUP BY 1, 2"
            % ','.join('?' * len(FALLBACK_EVENTS)), FALLBACK_EVENTS).fetchall()
    fallbacks = {}
    for task_id, kind, total in rows:
        fallbacks.setdefault(task_id or '', {})[kind] = total
    result = []
    for task in tasks:
        log = task.get('attempt_log') or []
        first, last = (log[0], log[-1]) if log else ({}, {})
        result.append(dict(
            task_id=task['id'], run_id=task.get('run_id', ''), role=task.get('role', ''),
            state=task.get('state', ''), attempts=task.get('attempts', 0),
            total=_span(task.get('created'), task.get('finished')),
            queue_wait=_span(task.get('created'), first.get('started')),
            ready_wait=_span(task.get('ready_at'), first.get('started')),
            time_to_first_output=_span(last.get('sent'), last.get('first_output')),
            remote=_span(last.get('sent'), last.get('remote_done')),
            after_stream=_span(last.get('remote_done'), last.get('finished')),
            verify_messages_read=task.get('verify_messages_read'),
            errors=[a['error_class'] for a in log if a.get('error_class')],
            fallbacks=fallbacks.get(task['id'], {}),
            attempt_log=log))
    return result


def rollup(store):
    """Daily event counts that outlived the retained event window."""
    with store.lock:
        rows = store.db.execute('SELECT day, kind, total FROM event_rollup ORDER BY day, kind').fetchall()
    return [dict(day=day, kind=kind, total=total) for day, kind, total in rows]
