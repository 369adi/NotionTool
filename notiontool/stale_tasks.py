"""Read-only report of native context-compression tasks stuck in state 'checkpoint'.

ChatContext (chat_context.py) stores a task with run_id '__context__', role 'context_compactor',
title 'Compress native context', state 'checkpoint' and lease '' before it compresses a worker's
native context. cleanup_blocker skips '__context__' tasks, and nothing else ends them when the
compression never finishes, so they can stay in 'checkpoint' forever.

stale_context_tasks only reports such tasks; it never writes, deletes or changes a record.
"""
import time

CONTEXT_RUN = '__context__'
STATE = 'checkpoint'
MAX_AGE = 6 * 3600
TIME_FIELDS = ('sent_at', 'started', 'created')


def _stamp(task):
    """First positive time of sent_at, started, created (0 when none is set)."""
    for field in TIME_FIELDS:
        try:
            value = float(task.get(field) or 0)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            return value
    return 0.0


def _lease_matches(worker, task):
    """True only when the worker holds a non-empty lease equal to the task lease."""
    lease = worker.get('lease') or ''
    return bool(lease) and lease == (task.get('lease') or '')


def stale_context_tasks(store, now=None, max_age=MAX_AGE):
    """Tasks of run '__context__' in state 'checkpoint' that are older than max_age seconds and whose
    worker is missing, or ready without a lease that matches the task lease.

    Returns dicts (id, state, title, worker_id, age_seconds, worker_state), oldest first.
    worker_state is 'missing' when the worker record does not exist. Tasks without any time are
    skipped because their age is unknown. Pure and read-only.
    """
    now = time.time() if now is None else now
    result = []
    for task in store.all('tasks'):
        if task.get('run_id') != CONTEXT_RUN or task.get('state') != STATE:
            continue
        stamp = _stamp(task)
        if not stamp or now - stamp <= max_age:
            continue
        worker_id = task.get('worker_id') or ''
        worker = store.get('workers', worker_id) if worker_id else None
        if worker:
            if worker.get('state') != 'ready' or _lease_matches(worker, task):
                continue
            worker_state = worker.get('state', '')
        else:
            worker_state = 'missing'
        result.append(dict(id=task.get('id', ''), state=task.get('state', ''), title=task.get('title', ''),
                           worker_id=worker_id, age_seconds=int(now - stamp), worker_state=worker_state))
    result.sort(key=lambda item: -item['age_seconds'])
    return result
