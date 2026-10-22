"""refresh() never writes a stale worker snapshot back (live bug 22 Oct 04:42, Coworker 9). Fakes only."""
import unittest

from tests.test_thread_list_guard import T1, T2, Base, FakeClient


class RotatingClient(FakeClient):
    """threads() is slow in reality; a context switch moves the worker to T2 while it runs."""

    def __init__(self, store, listed):
        super().__init__(threads=listed)
        self.store = store

    def threads(self):
        worker = self.store.get('workers', 'a:' + T1)
        worker.update(thread_id=T2, label='Welcome to Notion', retired_threads=[T1], generation=1)
        self.store.put('workers', worker)
        return super().threads()


class StaleSnapshotTests(Base):
    def test_old_chat_still_listed_does_not_revert_thread(self):
        self.own_worker('Coworker 9')
        adapter = self.adapter(RotatingClient(self.store, [{'id': T1, 'title': 'Coworker 9'},
                                                           {'id': T2, 'title': 'Welcome to Notion'}]))
        adapter.refresh('a')
        worker = self.store.get('workers', 'a:' + T1)
        self.assertEqual(worker['thread_id'], T2)
        self.assertEqual(worker['state'], 'ready')
        # The new chat belongs to the rotated worker; no second, unnamed worker record.
        self.assertEqual([w['id'] for w in self.store.all('workers') if w['thread_id'] == T2], ['a:' + T1])

    def test_old_chat_gone_does_not_mark_rotated_worker_missing(self):
        self.own_worker('Coworker 9')
        adapter = self.adapter(RotatingClient(self.store, [{'id': T2, 'title': 'Welcome to Notion'}]))
        adapter.refresh('a')
        worker = self.store.get('workers', 'a:' + T1)
        self.assertEqual(worker['thread_id'], T2)
        self.assertNotEqual(worker['state'], 'missing')

    def test_retired_worker_is_not_marked_missing_again(self):
        worker = self.own_worker('Coworker 9')
        worker.update(state='retired', enabled=False, retired_reason='chat no longer in Notion', missing_since=1)
        self.store.put('workers', worker)
        adapter = self.adapter(FakeClient(threads=[{'id': T2, 'title': 'Welcome to Notion'}]))
        adapter.refresh('a')
        self.assertEqual(self.store.get('workers', 'a:' + T1)['state'], 'retired')

    def test_unlisted_idle_worker_is_still_marked_missing(self):
        self.own_worker('Coworker 3')
        adapter = self.adapter(FakeClient(threads=[{'id': T2, 'title': 'Welcome to Notion'}]))
        adapter.refresh('a')
        self.assertEqual(self.store.get('workers', 'a:' + T1)['state'], 'missing')


if __name__ == '__main__':
    unittest.main()
