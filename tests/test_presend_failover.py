"""An Edge error before anything was sent moves the task to another coworker (live bug t531ad0a, Coworker 9)."""
import tempfile
import time
import unittest
from concurrent.futures import Future
from pathlib import Path

from notiontool.engine import Engine, PRESEND_RETRY_LIMIT
from notiontool.store import Store

EDGE_ERROR = 'Edge request interrupted; reconcile before reuse (TypeError: Failed to fetch)'


class PlainAdapter:
    response_mode = 'plain'

    def run(self, worker, task, prompt, cancel, update):
        raise AssertionError('The tests drive _complete directly; nothing is sent')

    def recover(self, worker, task, cancel):
        raise AssertionError('No reconciliation check in these tests')


class PresendFailoverTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent / '.test-data'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root, ignore_cleanup_errors=True)
        self.store = Store(self.temp.name)
        self.store.put('accounts', dict(id='a', slot=1, email='test@example.test', state='ready', cooldown_until=0))
        self.store.put('chats', dict(id='c', title='Main', main_worker='m', folder_id='f', project_path=self.temp.name))
        self.store.put('runs', dict(id='r1', chat_id='c', state='executing', project_path=self.temp.name, error='',
                                    capacity=2, worker_budget=2, fixes=0, main_worker='m',
                                    created=time.time(), updated=time.time()))
        self.engine = Engine(self.store, PlainAdapter())

    def tearDown(self):
        self.engine.close()
        time.sleep(.03)
        self.store.close()
        self.temp.cleanup()

    def start_attempt(self, lease, worker='w1', attempts=1, **extra):
        started = time.time() - 5
        self.store.put('workers', dict(id=worker, account_id='a', thread_id=worker, purpose='worker', enabled=True,
                                       state='busy', lease=lease, label=worker, last_used=0))
        task = self.store.get('tasks', 't1') or dict(id='t1', run_id='r1', role='reviewer', title='Review', prompt='p',
                                                     result='', deps=[], files=[], error='')
        task.update(state='running', lease=lease, worker_id=worker, attempts=attempts, started=started,
                    submission_phase='preparing', attempt_log=[dict(started=started)])
        task.update(extra)
        self.store.put('tasks', task)
        return dict(task_id='t1', lease=lease, worker_id=worker)

    def fail(self, info, exc):
        future = Future()
        future.set_exception(exc)
        with self.engine.lock:
            self.engine._complete(future, info)
        return self.store.get('tasks', 't1')

    def kinds(self):
        return [e['kind'] for e in self.store.events(limit=500)]

    def test_unsent_edge_error_moves_the_task_to_another_coworker(self):
        task = self.fail(self.start_attempt('L1'), OSError(EDGE_ERROR))
        self.assertEqual(task['state'], 'retry')
        self.assertEqual(task['attempts'], 0)
        self.assertIn('w1', task['exclude_workers'])
        self.assertEqual(task['presend_retries'], 1)
        self.assertEqual(self.store.get('workers', 'w1')['lease'], '')
        self.assertIn('presend_failover', self.kinds())

    def test_failover_is_bounded(self):
        for n in range(PRESEND_RETRY_LIMIT):
            task = self.fail(self.start_attempt('L%d' % n, worker='w%d' % n), OSError(EDGE_ERROR))
            self.assertEqual(task['state'], 'retry')
        task = self.fail(self.start_attempt('LX', worker='wx'), OSError(EDGE_ERROR))
        self.assertEqual(task['state'], 'blocked')
        self.assertEqual(sorted(task['exclude_workers']), sorted('w%d' % n for n in range(PRESEND_RETRY_LIMIT)))

    def test_sent_turn_is_never_moved(self):
        task = self.fail(self.start_attempt('L1', sent_at=time.time() - 2, submission_phase='inference'), OSError(EDGE_ERROR))
        self.assertNotEqual(task['state'], 'retry')
        self.assertNotIn('presend_failover', self.kinds())


if __name__ == '__main__':
    unittest.main()
