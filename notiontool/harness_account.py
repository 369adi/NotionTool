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


def main_chat(store, account_id):
    chats = [c for c in store.all('chats') if store.get('workers', c.get('main_worker'), {}).get('account_id') == account_id]
    account = store.get('accounts', account_id, {})
    thread = account.get('team_threads', {}).get('main', {}).get('id')
    return next((c for c in chats if store.get('workers', c['main_worker'], {}).get('thread_id') == thread),
                chats[0] if chats else None)


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
