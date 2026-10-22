import unittest

from notiontool import retired_chats


class Store:
    def __init__(self, workers):
        self.objects = {('accounts', 'a'): dict(id='a', state='ready')}
        for w in workers:
            self.objects[('workers', w['id'])] = w
        self.events = []

    def get(self, kind, ident, default=None):
        return self.objects.get((kind, ident), default)

    def all(self, kind):
        return [v for (k, _), v in self.objects.items() if k == kind]

    def put(self, kind, doc):
        self.objects[(kind, doc['id'])] = doc

    def event(self, kind, **fields):
        self.events.append(kind)


class Adapter:
    def __init__(self, titles):
        self.titles = titles
        self.deleted = []

    def client(self, account_id):
        return object()

    def thread_records(self, client, tids):
        return {t: dict(data=dict(title=self.titles.get(t, ''))) for t in tids}

    def delete_thread(self, client, tid):
        self.deleted.append(tid)


class AgeRetiredDeleteTest(unittest.TestCase):
    def test_age_retired_chat_is_deleted_and_marked(self):
        store = Store([dict(id='old', account_id='a', thread_id='t-old', purpose='retired_worker', enabled=False),
                       dict(id='new', account_id='a', thread_id='t-new', purpose='worker', enabled=True)])
        adapter = Adapter({'t-old': 'Coworker 4'})
        result = retired_chats.delete_retired(store, adapter)
        self.assertEqual(adapter.deleted, ['t-old'])
        self.assertEqual([d['thread_id'] for d in result['deleted']], ['t-old'])
        old = store.get('workers', 'old')
        self.assertTrue(old['chat_deleted'])
        self.assertEqual(old['state'], 'missing')
        # second pass: nothing left to delete
        self.assertEqual(retired_chats.delete_retired(store, adapter)['deleted'], [])
        self.assertEqual(adapter.deleted, ['t-old'])

    def test_active_and_user_named_chats_are_kept(self):
        store = Store([dict(id='old', account_id='a', thread_id='t-old', purpose='retired_worker'),
                       dict(id='live', account_id='a', thread_id='t-live', purpose='worker', enabled=True)])
        adapter = Adapter({'t-old': 'My important notes'})
        result = retired_chats.delete_retired(store, adapter)
        self.assertEqual(adapter.deleted, [])
        self.assertEqual(result['skipped'][0]['thread_id'], 't-old')


if __name__ == '__main__':
    unittest.main()
