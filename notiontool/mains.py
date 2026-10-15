"""Multiple Main chats and the v1->v2 data migration.

Every Main is created through the existing Welcome chat creation
(NotionAdapter.welcome_body + reserved thread ID + read-back reconciliation).
Operations are durable ('operations' kind) and keyed by the client's
request_id, so a repeated or resumed request never creates a second chat.
"""
from __future__ import annotations
import json
import shutil
import sqlite3
import time

from .store import uid

TERMINAL_OPS = {'done', 'failed'}


def migrate(store):
    """Schema v1 -> v2: number existing Main chats and start the durable Main counter.

    A consistent SQLite backup is written before any stored record changes.
    """
    if store.schema_version() >= store.SCHEMA_VERSION:
        return False
    backups = store.directory / 'backups'
    backups.mkdir(exist_ok=True)
    target = backups / f'notiontool-schema1-{time.strftime("%Y%m%d-%H%M%S")}.db'
    with store.lock:
        destination = sqlite3.connect(target)
        try:
            store.db.backup(destination)
        finally:
            destination.close()
    chats = sorted(store.all('chats'), key=lambda c: c.get('created', 0))
    number = store.counter('main')
    changed = []
    for chat in chats:
        if chat.get('main_number'):
            number = max(number, int(chat['main_number']))
            continue
        number += 1
        chat.update(main_number=number, state=chat.get('state', 'active'))
        if chat.get('title', '').endswith('Main Chat') or chat.get('title') in ('', 'New chat'):
            chat['title'] = f'Main {number}'
        changed.append(('chats', chat))
    if changed:
        store.batch(changed)
    store.raise_counter('main', number)
    store.set_schema_version(store.SCHEMA_VERSION)
    store.event('migration', message=f'Data migrated to schema {store.SCHEMA_VERSION}; backup {target.name}',
                migrated_chats=len(changed))
    return True


class Mains:
    def __init__(self, app):
        self.app, self.store, self.adapter, self.engine = app, app.store, app.adapter, app.engine

    # ---- durable operation log ------------------------------------------
    def _step(self, op, state, message, **fields):
        op.setdefault('log', []).append(dict(ts=time.time(), state=state, message=message))
        op['log'] = op['log'][-40:]
        op.update(state=state, updated=time.time(), **fields)
        self.store.put('operations', op)
        self.store.event('operation', message=message, operation_id=op['id'], op_kind=op['kind'], state=state,
                         chat_id=op.get('chat_id', ''), next_step=op.get('next_step', ''))
        return op

    def public(self, op):
        return {k: v for k, v in op.items() if k not in ('handoff',)}

    # ---- Create Main ----------------------------------------------------
    def request_create(self, account_id, request_id, folder_id='', project_path='', predecessor=''):
        request_id = str(request_id or '').strip()[:100]
        if not request_id:
            raise ValueError('A request_id is required so retries cannot create a second Main')
        if not account_id or self.store.get('accounts', account_id, {}).get('state') != 'ready':
            raise ValueError('Connect and select a ready Notion account first')
        ident = 'create_main:' + request_id
        with self.store.lock:
            existing = self.store.get('operations', ident)
            if existing:
                return existing, False
            def records(number):
                return [('operations', dict(id=ident, kind='create_main', request_id=request_id, state='reserved',
                    account_id=account_id, number=number, title=f'Main {number}', thread_id='', chat_id='',
                    folder_id=folder_id, project_path=project_path, predecessor=predecessor,
                    created=time.time(), updated=time.time(), error='', next_step='Submit Welcome creation',
                    log=[dict(ts=time.time(), state='reserved', message=f'Main {number} number reserved')]))]
            self.store.next_counter('main', records)
            return self.store.get('operations', ident), True

    def run_create(self, op_id):
        op = self.store.get('operations', op_id)
        if not op or op['state'] == 'done':
            return op
        account_id = op['account_id']
        client = self.adapter.client(account_id)
        self.adapter.require_workspace_ai(account_id, client.identity())
        if not op.get('thread_id'):
            op['thread_id'] = self.adapter.new_id(client.info['space_id'], '00a9')
            self._step(op, 'reserved', 'Thread ID reserved before creation')
        known = self._threads(client)
        if op['thread_id'] not in known:
            if op['state'] in ('submitted', 'unconfirmed'):
                # A creation POST may already have been accepted; never send it twice.
                return self._step(op, 'unconfirmed', 'Welcome creation not yet visible; reconcile later',
                    error='Creation unconfirmed', next_step='Refresh chats, then resume this operation')
            self._step(op, 'submitted', 'Welcome creation submitted', next_step='Confirm thread in chat list')
            try:
                for _ in client.transport.stream('runInferenceTranscript',
                        self.adapter.welcome_body(client, op['thread_id'], client.model()), client.headers(), timeout=180):
                    pass
            except OSError:
                pass
            if op['thread_id'] not in self._threads(client):
                return self._step(op, 'unconfirmed', 'Welcome creation unconfirmed; it will not be resent',
                    error='Creation unconfirmed', next_step='Refresh chats, then resume this operation')
        try:
            self.adapter.ensure_thread_model(client, op['thread_id'], client.model())
        except Exception as exc:
            self.store.event('main_model_warning', message='Model setting not confirmed: ' + type(exc).__name__)
        self.adapter.rename_thread(client, op['thread_id'], op['title'])
        self.adapter.refresh(account_id)
        with self.engine.lock:
            worker = next((w for w in self.store.all('workers')
                           if w['account_id'] == account_id and w['thread_id'] == op['thread_id']), None)
            if not worker:
                return self._step(op, 'unconfirmed', 'Created chat absent from the Notion chat list',
                    error='Not listed yet', next_step='Refresh chats, then resume this operation')
            worker.update(purpose='main', enabled=True, welcome=True, main_number=op['number'])
            chat = self.store.get('chats', 'c_' + op['id'].replace(':', '_')) or dict(
                id='c_' + op['id'].replace(':', '_'), title=op['title'], main_worker=worker['id'],
                folder_id=op.get('folder_id', ''), project_path=op.get('project_path', ''), created=time.time(),
                main_number=op['number'], state='active', predecessor=op.get('predecessor', ''))
            self.store.batch([('workers', worker), ('chats', chat)])
        return self._step(op, 'done', op['title'] + ' created and verified', chat_id=chat['id'], error='', next_step='')

    @staticmethod
    def _threads(client):
        return {t.get('id') or t.get('thread_id') or t.get('threadId') for t in client.threads()}

    def resume(self, op_id):
        op = self.store.get('operations', op_id)
        if not op:
            raise ValueError('Operation not found')
        if op['kind'] == 'create_main':
            return self.run_create(op_id)
        raise ValueError('Unknown operation kind')
