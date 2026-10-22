"""Refresh Main (settings button): create a fresh Welcome Main, then retire the old Main once the new one exists.

The old Main is only retired after the new Main's create operation is 'done' and its chat is listed as a
worker. Retired means purpose 'archived_main' + enabled False (hidden from the app); the Notion chat itself
is kept, so nothing is lost if the user wants to read it again.
"""
import time


def active_mains(store, account_id):
    return [w for w in store.all('workers') if w.get('account_id') == account_id
            and w.get('purpose') == 'main' and w.get('enabled') is not False]


def start(server, account_id, request_id=''):
    request_id = str(request_id or ('main-refresh-' + time.strftime('%Y%m%d-%H%M%S'))).strip()[:100]
    old_ids = [w['id'] for w in active_mains(server.store, account_id)]
    op, new = server.mains.request_create(account_id, request_id, predecessor=old_ids[0] if old_ids else '')

    def run():
        server.mains.run_create(op['id'])
        return finish(server.store, op['id'], old_ids)

    if new or op['state'] not in ('done', 'failed'):
        server.job_once('Refresh Main', run, 'op:' + op['id'])
    return dict(server.mains.public(op), duplicate=not new, replacing=len(old_ids))


def finish(store, op_id, old_ids):
    op = store.get('operations', op_id) or {}
    if op.get('state') != 'done' or not op.get('thread_id'):
        store.event('main_refresh_waiting', message='New Main not confirmed yet; the old Main stays active')
        raise RuntimeError('New Main not confirmed yet; the old Main stays active')
    account_id, tid = op['account_id'], op['thread_id']
    new_worker = next((w for w in store.all('workers')
                       if w.get('account_id') == account_id and w.get('thread_id') == tid), None)
    if not new_worker:
        store.event('main_refresh_waiting', message='New Main not listed yet; the old Main stays active')
        raise RuntimeError('New Main not listed yet; the old Main stays active')
    records, retired = [], 0
    for wid in old_ids:
        old = store.get('workers', wid)
        if not old or old['id'] == new_worker['id']:
            continue
        old.update(purpose='archived_main', enabled=False, retired_by='main_refresh', retired_at=time.time())
        records.append(('workers', old))
        retired += 1
        for chat in store.all('chats'):
            if chat.get('main_worker') == old['id']:
                chat.update(main_worker=new_worker['id'], predecessor_worker=old['id'])
                records.append(('chats', chat))
    account = store.get('accounts', account_id)
    if account is not None:
        ledger = account.get('team_threads') or {}
        ledger['main'] = {'id': tid, 'pending': False}
        account['team_threads'] = ledger
        records.append(('accounts', account))
    store.batch(records, [])
    store.event('main_refreshed', message='%s is the new Main; %d old Main retired' % (op.get('title') or 'Main', retired))
    return dict(thread_id=tid, retired=retired)
