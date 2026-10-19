"""/api/state lists stuck native-compression tasks (server.py snapshot + stale_tasks.py). Report only."""
import tempfile
import time
import unittest

from notiontool.server import Application


class StateStaleContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.app = Application(self.temp.name)
        self.addCleanup(self.app.store.db.close)
        self.app.port = 8790  # snapshot() reads the port that serve() normally sets

    def test_old_checkpoint_task_is_listed_and_left_unchanged(self):
        old = time.time() - 54 * 3600
        task = dict(id='c1', run_id='__context__', state='checkpoint', title='Compress native context',
                    worker_id='gone', lease='', sent_at=old)
        self.app.store.put('tasks', task)
        listed = self.app.snapshot()['stale_context_tasks']
        self.assertEqual([t['id'] for t in listed], ['c1'])
        self.assertEqual(listed[0]['worker_state'], 'missing')
        self.assertEqual(self.app.store.get('tasks', 'c1')['state'], 'checkpoint')

    def test_empty_when_nothing_is_stuck(self):
        self.assertEqual(self.app.snapshot()['stale_context_tasks'], [])

    def test_a_broken_store_never_breaks_the_state(self):
        class Broken:
            def all(self, kind):
                raise RuntimeError('db locked')
        fake = type('Fake', (), {'store': Broken()})()
        self.assertEqual(Application.stale_context(fake), [])


if __name__ == '__main__':
    unittest.main()
