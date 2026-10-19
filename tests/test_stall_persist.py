"""Stall reports survive a harness restart (server.py check_stalls + meta key stall_reported). No network."""
import json
import tempfile
import time
import unittest

from notiontool import server
from notiontool.server import Application


class StallPersistTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.now = time.time()

    def app(self):
        app = Application(self.temp.name)
        self.addCleanup(app.store.db.close)
        return app

    def put_task(self, app, ident, state='running', idle=600):
        app.store.put('workers', dict(id='w1', label='Coworker Ada', purpose='worker', thread_id='th1'))
        app.store.put('tasks', dict(id=ident, run_id='r1', state=state, worker_id='w1', thread_id='th1',
                                    sent_at=self.now - idle))

    def stalled_events(self, app, ident=None):
        rows = app.store.db.execute("SELECT doc FROM events WHERE kind='task_stalled'").fetchall()
        docs = [json.loads(r[0]) for r in rows]
        return [d for d in docs if ident is None or d.get('task_id') == ident]

    def stored_ids(self, app):
        row = app.store.db.execute("SELECT value FROM meta WHERE key='stall_reported'").fetchone()
        return json.loads(row[0]) if row else None

    def test_second_application_does_not_report_the_same_id_again(self):
        first = self.app()
        self.put_task(first, 't1')
        self.assertIn('t1', first.check_stalls(now=self.now))
        first.check_stalls(now=self.now + 60)
        self.assertEqual(len(self.stalled_events(first, 't1')), 1)
        self.assertEqual(self.stored_ids(first), ['t1'])
        first.store.db.close()

        second = self.app()
        self.assertNotIn('stall_reported', second.__dict__)  # loaded lazily
        task = second.store.get('tasks', 't1')
        self.assertEqual(task['state'], 'uncertain')  # the engine recovers running turns on start
        second.check_stalls(now=self.now + 60)
        self.assertEqual(self.stored_ids(second), ['t1'])  # still open: not pruned while recovering
        task['state'] = 'running'  # reconcile found the turn still running
        second.store.put('tasks', task)
        for tick in range(3):
            self.assertIn('t1', second.check_stalls(now=self.now + 120 + 60 * tick))  # still flagged
        self.assertEqual(len(self.stalled_events(second, 't1')), 1)
        self.assertEqual(second.store.get('tasks', 't1')['state'], 'running')  # report only

    def test_new_stall_after_restart_is_reported_once(self):
        first = self.app()
        self.put_task(first, 't1')
        first.check_stalls(now=self.now)
        first.store.db.close()
        second = self.app()
        self.put_task(second, 't2')
        second.check_stalls(now=self.now)
        second.check_stalls(now=self.now + 60)
        self.assertEqual(len(self.stalled_events(second, 't1')), 1)
        self.assertEqual(len(self.stalled_events(second, 't2')), 1)
        self.assertEqual(self.stored_ids(second), ['t1', 't2'])

    def test_finished_turns_are_pruned(self):
        app = self.app()
        self.put_task(app, 't1')
        self.put_task(app, 't2')
        app.check_stalls(now=self.now)
        self.assertEqual(self.stored_ids(app), ['t1', 't2'])
        task = app.store.get('tasks', 't1')
        task['state'] = 'done'
        app.store.put('tasks', task)
        task = app.store.get('tasks', 't2')
        task['remote_done_at'] = self.now
        app.store.put('tasks', task)
        self.assertEqual(app.check_stalls(now=self.now + 60), {})
        self.assertEqual(self.stored_ids(app), [])

    def test_list_is_capped_to_the_newest_ids(self):
        app = self.app()
        old = ['old%d' % i for i in range(server.STALL_REPORTED_CAP)]
        with app.store.lock, app.store.db:
            app.store.db.execute("INSERT INTO meta VALUES('stall_reported',?)", (json.dumps(old),))
        tasks = old[-10:] + ['new%d' % i for i in range(5)]
        for ident in tasks:
            self.put_task(app, ident)
        app.check_stalls(now=self.now)
        ids = self.stored_ids(app)
        self.assertEqual(ids, tasks)  # ids of closed turns pruned, open ones kept in order
        self.assertEqual([d['task_id'] for d in self.stalled_events(app)], tasks[-5:])
        self.assertLessEqual(len(ids), server.STALL_REPORTED_CAP)

    def test_cap_applies_to_open_turns_too(self):
        app = self.app()
        with mock_cap(3):
            for ident in ('a1', 'a2', 'a3', 'a4', 'a5'):
                self.put_task(app, ident)
            app.check_stalls(now=self.now)
            self.assertEqual(self.stored_ids(app), ['a3', 'a4', 'a5'])

    def test_unreadable_stored_value_starts_empty(self):
        app = self.app()
        with app.store.lock, app.store.db:
            app.store.db.execute("INSERT INTO meta VALUES('stall_reported','not json')")
        self.put_task(app, 't1')
        app.check_stalls(now=self.now)
        self.assertEqual(len(self.stalled_events(app, 't1')), 1)
        self.assertEqual(self.stored_ids(app), ['t1'])
        self.assertEqual([e for e in app.store.events() if e['kind'] == 'stall_check_failed'], [])


def mock_cap(value):
    from unittest import mock
    return mock.patch.object(server, 'STALL_REPORTED_CAP', value)


if __name__ == '__main__':
    unittest.main()
