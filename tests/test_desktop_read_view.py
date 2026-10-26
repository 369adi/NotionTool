"""Lag fix 2026-10-25: desktop builds never wait on the store lock; Store.all keeps rowid order without SQL sort."""
import sqlite3
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

from notiontool.store import Store
from notiontool import desktop_state_cache as dsc


class ReadViewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = Store(self.dir)
        self.views = []

    def tearDown(self):
        for view in self.views:
            try:
                view.db.close()   # only the view's own read connection (view.close is the real store's)
            except Exception:
                pass
        try:
            self.store.close()
        except Exception:
            pass
        self.tmp.cleanup()

    def _cache(self, builder):
        server = types.SimpleNamespace(store=self.store)
        cache = dsc.DesktopStateCache(server, self.dir, builder=builder, settings_loader=lambda d: {},
                                      settings_path=self.dir / 'none.json')
        return cache

    def _track(self, cache):
        if cache._view is not None:
            self.views.append(cache._view)

    def test_all_keeps_rowid_order_without_sql_sort(self):
        for ident in ('c', 'a', 'b'):
            self.store.put('tasks', dict(id=ident, state='queued'))
        self.store.put('tasks', dict(id='c', state='done'))  # update keeps its rowid (first)
        self.assertEqual([t['id'] for t in self.store.all('tasks')], ['c', 'a', 'b'])
        self.assertEqual(self.store.all('tasks')[0]['state'], 'done')
        src = Path(dsc.__file__).resolve().parent.joinpath('store.py').read_text(encoding='utf-8')
        self.assertNotIn("WHERE kind=? ORDER BY rowid'", src)

    def test_view_reads_own_connection_and_writes_through(self):
        self.store.put('workers', dict(id='w1', label='One'))
        view = dsc.read_view(self.store)
        self.assertIsNotNone(view)
        self.views.append(view)
        self.assertIsNot(view.db, self.store.db)
        self.assertIsNot(view.lock, self.store.lock)
        self.assertEqual(view.get('workers', 'w1')['label'], 'One')
        view.put('workers', dict(id='w2', label='Two'))          # goes to the real store
        self.assertEqual(self.store.get('workers', 'w2')['label'], 'Two')
        self.assertEqual(view.get('workers', 'w2')['label'], 'Two')  # committed data is visible at once
        view.event('read_view_test', message='x')
        self.assertTrue(any(e.get('kind') == 'read_view_test' for e in self.store.events(limit=10)))
        with self.assertRaises(sqlite3.Error):
            view.db.execute("INSERT INTO objects VALUES('workers','w3','{}')")

    def test_no_view_for_fakes(self):
        self.assertIsNone(dsc.read_view(types.SimpleNamespace(all=lambda kind: [])))

    def test_build_does_not_wait_for_store_lock(self):
        for i in range(30):
            self.store.put('tasks', dict(id='t%d' % i, state='done', run_id='r', worker_id='w'))
        self.store.put('workers', dict(id='w', label='W'))

        def builder(srv, app):
            return dict(tasks=len(srv.store.all('tasks')), worker=srv.store.get('workers', 'w')['label'],
                        events=len(srv.store.events(limit=5)))

        cache = self._cache(builder)
        held, release = threading.Event(), threading.Event()

        def hog():
            with self.store.lock:          # a long engine write / scan
                held.set()
                release.wait(10)

        thread = threading.Thread(target=hog, daemon=True)
        thread.start()
        held.wait(5)
        try:
            start = time.perf_counter()
            state, rebuilt = cache.get()
            elapsed = time.perf_counter() - start
        finally:
            release.set()
            thread.join(5)
            self._track(cache)
        self.assertTrue(rebuilt)
        self.assertEqual(state['tasks'], 30)
        self.assertEqual(state['worker'], 'W')
        self.assertLess(elapsed, 2.0)

    def test_broken_view_falls_back_to_store(self):
        calls = []

        def builder(srv, app):
            inner = srv.store._store
            calls.append(getattr(inner, 'read_only_view', False))
            if calls[-1]:
                self.views.append(inner)
                raise RuntimeError('view broke')
            return dict(ok=True)

        cache = self._cache(builder)
        state, _ = cache.get()
        self.assertEqual(state, dict(ok=True))
        self.assertEqual(calls, [True, False])
        self.assertIs(cache.read_store(), self.store)
        self.assertEqual(cache.stats.snapshot()['counters'].get('read_view_failures'), 1)


if __name__ == '__main__':
    unittest.main()
