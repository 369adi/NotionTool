"""One explicit account for the harness; existing accounts remain recoverable."""

def selected_account(store):
    settings = store.settings()
    ident = settings.get('harness_account_id', '')
    accounts = store.all('accounts')
    if ident:
        return ident
    if len(accounts) == 1:
        return accounts[0]['id']
    return ''


def runnable_accounts(store):
    """Accounts whose Mains and Coworkers may work: every ready account (B39).

    Before B39 only the one selected harness account ran, so runs of every other Main (Slot 1, the
    operator Main) stayed queued forever with free coworkers. Falls back to the selected account.
    """
    ready = {a['id'] for a in store.all('accounts') if a.get('state') == 'ready'}
    if ready:
        return ready
    selected = selected_account(store)
    return {selected} if selected else set()


def main_chat(store, account_id):
    """Chat of the account's Main worker, chosen by chat_pool.pick_main so both always agree.

    Released and operator chats are never returned.
    """
    from .chat_pool import pick_main
    workers = [w for w in store.all('workers') if w.get('account_id') == account_id]
    account = store.get('accounts', account_id, {}) or {}
    thread = ((account.get('team_threads') or {}).get('main') or {}).get('id', '')
    main = pick_main(workers, thread)
    if not main:
        return None
    chats = [c for c in store.all('chats') if c.get('main_worker') == main['id']
             and c.get('state') != 'released' and not c.get('operator')]
    return max(chats, key=lambda c: (float(c.get('created') or 0), str(c.get('id') or ''))) if chats else None


def select_account(store, ident):
    if not store.get('accounts', ident):
        raise ValueError('Choose an existing account')
    previous = selected_account(store)
    if previous == ident:
        return {'account_id': ident}
    if any(t.get('state') in ('running', 'cancelling') or
           (previous and (t.get('state') == 'uncertain' or t.get('remote_unknown')))
           for t in store.all('tasks')):
        raise ValueError('Wait for active and unconfirmed assignments before changing the harness account')
    if previous and any(r.get('state') in ('queued', 'approach', 'planning', 'executing', 'reviewing', 'fixing', 'integrating', 'paused')
           for r in store.all('runs')):
        raise ValueError('Finish or stop existing runs before changing the harness account')
    settings = store.settings()
    settings['harness_account_id'] = ident
    store.put('settings', settings)
    return {'account_id': ident}
