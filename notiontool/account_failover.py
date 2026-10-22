"""B41: runs never stay on a dead account.

When an account is AI-disabled, blocked or removed, every unfinished run whose Main sits on it is moved
to a ready Main of a working account (the harness account first). Its tasks that were parked because of
the dead account go back to the queue, so only coworkers of working accounts get the work. Generic for
every user: nothing is tied to one email or account id.
"""
import time

DEAD_STATES = {'ai_disabled', 'blocked', 'disabled', 'removed', 'banned', 'suspended'}
DONE_RUN_STATES = {'done', 'failed', 'cancelled'}
REQUEUE_TASK_STATES = {'blocked', 'awaiting_main', 'retry'}


def account_dead(account):
    if not account:
        return True
    if account.get('state') in DEAD_STATES:
        return True
    return (account.get('ai_access') or {}).get('state') == 'disabled'


def _pick_main(store, accounts):
    from .harness_account import selected_account
    preferred = selected_account(store)
    mains = [w for w in store.all('workers') if w.get('purpose') == 'main' and w.get('enabled') is not False
             and w.get('state') in ('ready', 'busy') and not account_dead(accounts.get(w.get('account_id')))]
    if not mains:
        return None
    mains.sort(key=lambda w: (w.get('account_id') != preferred, w.get('state') != 'ready', w.get('id', '')))
    return mains[0]


def rehome_runs(store, now=None):
    """Move unfinished runs off dead accounts. Returns the list of moved run ids."""
    now = time.time() if now is None else now
    accounts = {a['id']: a for a in store.all('accounts')}
    workers = {w['id']: w for w in store.all('workers')}

    def dead_worker(ident):
        w = workers.get(ident)
        return not w or account_dead(accounts.get(w.get('account_id')))

    moved = []
    target = None
    for run in store.all('runs'):
        if run.get('state') in DONE_RUN_STATES or not run.get('main_worker'):
            continue
        if not dead_worker(run['main_worker']):
            continue
        target = target or _pick_main(store, accounts)
        if not target:
            return moved  # no working account at all: leave everything as it is
        old = run['main_worker']
        run.update(main_worker=target['id'], account_id=target.get('account_id'), rehomed_at=now,
                   rehomed_from=old)
        if run.get('state') in ('blocked', 'awaiting_main'):
            run['state'] = 'executing' if run.get('plan') or run.get('planned') else 'queued'
            run.pop('error', None)
        store.put('runs', run)
        requeued = 0
        for task in store.all('tasks'):
            if task.get('run_id') != run['id']:
                continue
            excl = [x for x in (task.get('exclude_workers') or []) if not dead_worker(x)]
            changed = excl != (task.get('exclude_workers') or [])
            if task.get('state') in REQUEUE_TASK_STATES and (not task.get('worker_id') or dead_worker(task.get('worker_id'))
                                                             or task.get('state') == 'awaiting_main'):
                task.update(state='queued', worker_id=None, next_at=0, error='')
                requeued += 1
                changed = True
            if changed:
                task['exclude_workers'] = excl
                store.put('tasks', task)
        store.event('run_rehomed', run['id'], old_main=old, new_main=target['id'],
                    account_id=target.get('account_id'), requeued=requeued)
        moved.append(run['id'])
    return moved
