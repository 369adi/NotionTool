"""A chat deleted in Notion must not block its name until the next periodic refresh (live bug 2026-10-22)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from notiontool import own_chats as oc

ACC = 'acc1'


class Store:
    def __init__(self, workers):
        self.data = {'workers': {w['id']: w for w in workers}}
        self.events = []

    def all(self, kind):
        return list(self.data.get(kind, {}).values())

    def get(self, kind, ident, default=None):
        return self.data.get(kind, {}).get(ident, default)

    def put(self, kind, doc):
        self.data.setdefault(kind, {})[doc['id']] = doc

    def event(self, kind, *args, **kw):
        self.events.append((kind, kw))


class Adapter:
    def __init__(self, store, deleted=(), fail=False):
        self.store, self.deleted, self.fail, self.calls = store, set(deleted), fail, 0

    def refresh(self, account_id):
        self.calls += 1
        if self.fail:
            raise RuntimeError('network down')
        for w in self.store.all('workers'):
            if w['thread_id'] in self.deleted:
                w.update(state='missing', enabled=False)


class Server:
    def __init__(self, store, adapter):
        self.store, self.adapter = store, adapter


def worker(tid, label, purpose=oc.PRIVATE_PURPOSE):
    return dict(id=ACC + ':' + tid, account_id=ACC, thread_id=tid, label=label, purpose=purpose, state='ready')


class RefreshIfDuplicateTests(unittest.TestCase):
    def test_deleted_chat_frees_its_name_after_one_refresh(self):
        store = Store([worker('t1', 'Ideen')])
        adapter = Adapter(store, deleted={'t1'})
        body = {'account_id': ACC, 'name': ' ideen '}
        with self.assertRaises(ValueError):
            oc.validate_name(body['name'], oc._titles(store, ACC))  # the old behaviour: name looks taken
        self.assertTrue(oc._refresh_if_duplicate(Server(store, adapter), body))
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(oc.validate_name(body['name'], oc._titles(store, ACC)), 'ideen')

    def test_free_name_never_reads_notion(self):
        store = Store([worker('t1', 'Ideen')])
        adapter = Adapter(store)
        self.assertFalse(oc._refresh_if_duplicate(Server(store, adapter), {'account_id': ACC, 'name': 'Neu'}))
        self.assertEqual(adapter.calls, 0)

    def test_existing_chat_stays_a_duplicate(self):
        store = Store([worker('t1', 'Ideen')])
        adapter = Adapter(store)  # nothing was deleted in Notion
        oc._refresh_if_duplicate(Server(store, adapter), {'account_id': ACC, 'name': 'Ideen'})
        with self.assertRaises(ValueError) as ctx:
            oc.validate_name('Ideen', oc._titles(store, ACC))
        self.assertEqual(str(ctx.exception), oc.MSG_DUPLICATE % 'Ideen')

    def test_failed_refresh_only_logs(self):
        store = Store([worker('t1', 'Ideen')])
        adapter = Adapter(store, deleted={'t1'}, fail=True)
        self.assertTrue(oc._refresh_if_duplicate(Server(store, adapter), {'account_id': ACC, 'name': 'Ideen'}))
        self.assertEqual(store.events[0][0], 'app_chat_name_refresh_skipped')
        with self.assertRaises(ValueError):
            oc.validate_name('Ideen', oc._titles(store, ACC))

    def test_other_account_is_ignored(self):
        store = Store([dict(worker('t1', 'Ideen'), account_id='other')])
        adapter = Adapter(store)
        self.assertFalse(oc._refresh_if_duplicate(Server(store, adapter), {'account_id': ACC, 'name': 'Ideen'}))
        self.assertEqual(adapter.calls, 0)


if __name__ == '__main__':
    unittest.main()
