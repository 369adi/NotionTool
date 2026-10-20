"""Release a Main chat whose Notion thread belongs to the user: the harness never writes to that thread again."""
import time

from .main_repoint import OPEN_RUN_STATES

RELEASED = 'released'
EXTERNAL = 'external'
CLOSED_TASK_STATES = ('done', 'failed', 'cancelled')


def _holds(task, worker_id):
    """True when a task still reserves the thread: unfinished, unconfirmed by Notion, or leased."""
    if task.get('worker_id') != worker_id:
        return False
    return task.get('state') not in CLOSED_TASK_STATES or bool(task.get('remote_unknown')) or bool(task.get('lease'))


def release_chat(store, chat_id, now=None):
    """Detach chat_id from its Main thread for good and return a summary.

    The chat record and its history stay (state 'released'). Its worker becomes 'external':
    disabled, unleased, never picked as Main or Coworker again. Tasks of finished runs that
    still reserve the thread are closed without resending anything (I5).
    """
    stamp = time.time() if now is None else now
    with store.lock:
        chat = store.get('chats', chat_id)
        if not chat:
            raise ValueError('Chat not found')
        if chat.get('state') == RELEASED:
            return dict(ok=True, chat_id=chat_id, worker_id=chat.get('released_worker', ''), closed_tasks=0, already=True)
        runs = {r['id']: r for r in store.all('runs')}
        if any(r.get('chat_id') == chat_id and r.get('state') in OPEN_RUN_STATES for r in runs.values()):
            raise ValueError('Cancel the open runs of this chat first')
        worker_id = chat.get('main_worker') or ''
        worker = store.get('workers', worker_id) if worker_id else None
        if worker and worker.get('running_turn'):
            raise ValueError('Wait until the current turn in this chat has ended')
        held = [t for t in store.all('tasks') if worker_id and _holds(t, worker_id)]
        if any((runs.get(t.get('run_id')) or {}).get('state') in OPEN_RUN_STATES for t in held):
            raise ValueError('An open run still uses this Notion chat')
        for task in held:
            task.update(state='cancelled', remote_unknown=False, lease='', next_at=0,
                        error='Main chat released to its user; nothing was resent')
            store.put('tasks', task)
        if worker:
            worker.update(purpose=EXTERNAL, enabled=False, state='ready', lease='',
                          released_chat=chat_id, released_at=stamp)
            store.put('workers', worker)
            account = store.get('accounts', worker.get('account_id', ''))
            ledger = (account or {}).get('team_threads') or {}
            if (ledger.get('main') or {}).get('id') == worker.get('thread_id'):
                ledger.pop('main')
                account['team_threads'] = ledger
                store.put('accounts', account)
        chat.update(state=RELEASED, main_worker='', released_worker=worker_id, released_at=stamp)
        store.put('chats', chat)
        store.event('chat_released', message='Main chat released to its user: ' + chat.get('title', ''), chat_id=chat_id)
    return dict(ok=True, chat_id=chat_id, worker_id=worker_id, closed_tasks=len(held), already=False)
