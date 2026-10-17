"""Point chats and open runs at the account's current Main after the Main changed."""

OPEN_RUN_STATES = ('planning', 'executing', 'reviewing', 'fixing', 'integrating', 'queued', 'blocked', 'paused')


def _stale(store, worker_id, main_id):
    if not worker_id or worker_id == main_id:
        return False
    worker = store.get('workers', worker_id)
    return not worker or worker.get('state') == 'missing' or worker.get('purpose') != 'main' or not worker.get('enabled')


def _account_of(worker_id):
    return str(worker_id or '').split(':', 1)[0]


def repoint(store, account_id, main):
    """Return counts of chats and runs moved to main. Only touches records of this account whose Main is gone."""
    if not main or main.get('purpose') != 'main' or main.get('account_id') != account_id:
        return {'chats': 0, 'runs': 0}
    main_id = main['id']
    chats = store.all('chats')
    taken = any(c.get('main_worker') == main_id for c in chats)
    moved_chats = 0
    for chat in chats:
        old = chat.get('main_worker')
        if taken or _account_of(old) != account_id or not _stale(store, old, main_id):
            continue
        chat['main_worker'] = main_id
        store.put('chats', chat)
        store.event('chat_main_repointed', message='Chat moved to the new Main: ' + chat.get('title', ''), chat_id=chat['id'])
        taken = True
        moved_chats += 1
    moved_runs = 0
    for run in store.all('runs'):
        old = run.get('main_worker')
        if run.get('state') not in OPEN_RUN_STATES or _account_of(old) != account_id or not _stale(store, old, main_id):
            continue
        run['main_worker'] = main_id
        store.put('runs', run)
        store.event('run_main_repointed', message='Run moved to the new Main', run_id=run['id'])
        moved_runs += 1
    return {'chats': moved_chats, 'runs': moved_runs}
