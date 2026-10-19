"""ThreadMissing in the result handler: task blocked, never retried or resent, worker retired as 'missing'. Fakes only, no network."""
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future
from pathlib import Path

from notiontool.engine import CANCELLED_BEFORE_SUBMISSION, Engine
from notiontool.notion import ThreadMissing
from notiontool.store import Store

THREAD = 'first-thread'


class RecordingAdapter:
    def __init__(self):
        self.runs, self.recoveries = [], []

    def run(self, worker, task, prompt, cancel, update):
        self.runs.append((worker['id'], task['id']))
        raise AssertionError('a task whose thread is missing must never be sent again')

    def recover(self, worker, task, cancel):
        self.recoveries.append((worker['id'], task['id']))
        raise AssertionError('a task whose thread is missing must never be reconciled again')


class Base(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent / '.test-data'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root, ignore_cleanup_errors=True)
        self.store = Store(self.temp.name)
        self.adapter = RecordingAdapter()
        self.store.put('accounts', dict(id='a', slot=1, email='test@example.test', state='ready', cooldown_until=0))
        self.store.put('workers', dict(id='first', account_id='a', thread_id=THREAD, purpose='worker', enabled=True,
                                       state='ready', lease='', label='first', last_used=0))
        self.store.put('chats', dict(id='c', title='Main', main_worker='first', folder_id='f', project_path=self.temp.name))
        self.store.put('runs', dict(id='r1', chat_id='c', state='executing', project_path=self.temp.name, error='',
                                    capacity=1, worker_budget=1, main_worker='first'))
        self.engine = Engine(self.store, self.adapter)

    def tearDown(self):
        self.engine.close()
        time.sleep(.03)
        self.store.close()
        self.temp.cleanup()

    def task(self, recovery):
        started = time.time() - 30
        task = dict(id='t1', run_id='r1', role='executor', title='Edit files', prompt='edit', lease='lease-1',
                    worker_id='first', attempts=1, started=started, submission_phase='inference', sent_at=started + 1,
                    files=[], deps=[], result='', partial='', attempt_log=[dict(attempt=1, worker_id='first', started=started)])
        task.update(dict(state='uncertain', remote_unknown=True, reconcile_checks=2) if recovery else dict(state='running'))
        self.store.put('tasks', task)
        worker = self.store.get('workers', 'first')
        worker.update(state='recovering' if recovery else 'busy', lease='lease-1')
        self.store.put('workers', worker)

    def complete_with(self, exc, recovery):
        self.task(recovery)
        future = Future()
        future.set_exception(exc)
        info = dict(task_id='t1', lease='lease-1', worker_id='first', run_id='r1', cancel=threading.Event(), recovery=recovery)
        self.engine._complete(future, info)
        return self.store.get('tasks', 't1'), self.store.get('workers', 'first')

    def assert_missing_handled(self, task, worker):
        self.assertEqual(task['state'], 'blocked')
        self.assertEqual(task['error'], 'The assigned Notion thread is missing; nothing was resent')
        self.assertFalse(task.get('remote_unknown'))
        self.assertNotEqual(task['attempt_log'][-1]['state'], 'retry')
        self.assertEqual((worker['state'], worker['enabled'], worker['lease']), ('missing', False, ''))
        # Neither healing nor reconciling brings the task or the worker back.
        self.engine._heal_workers()
        self.engine._reconcile()
        time.sleep(.05)
        self.assertEqual(self.store.get('workers', 'first')['state'], 'missing')
        self.assertEqual(self.store.get('tasks', 't1')['state'], 'blocked')
        self.assertEqual((self.adapter.runs, self.adapter.recoveries), ([], []))
        kinds = [e['kind'] for e in self.store.events(limit=500)]
        self.assertIn('worker_thread_missing', kinds)


class ThreadMissingTests(Base):
    def test_missing_during_recovery_blocks_task_and_retires_worker(self):
        task, worker = self.complete_with(ThreadMissing(THREAD, 'getThreadTranscript'), recovery=True)
        self.assert_missing_handled(task, worker)

    def test_missing_during_normal_run_blocks_task_and_retires_worker(self):
        task, worker = self.complete_with(ThreadMissing(THREAD, 'runInferenceTranscript'), recovery=False)
        self.assert_missing_handled(task, worker)

    def test_rotated_worker_keeps_its_new_thread(self):
        # The 404 names an old thread; the worker already moved on, so only the task is blocked.
        task, worker = self.complete_with(ThreadMissing('old-thread', 'getThreadTranscript'), recovery=True)
        self.assertEqual(task['state'], 'blocked')
        self.assertEqual(task['error'], 'The assigned Notion thread is missing; nothing was resent')
        self.assertEqual((worker['state'], worker['enabled'], worker['lease']), ('error', True, ''))


class OtherErrorsUnchangedTests(Base):
    def test_generic_error_still_blocks_and_marks_worker_error(self):
        task, worker = self.complete_with(RuntimeError('boom'), recovery=False)
        self.assertEqual((task['state'], task['error']), ('blocked', 'boom'))
        self.assertEqual((worker['state'], worker['enabled'], worker['lease']), ('error', True, ''))

    def test_plain_value_error_is_not_treated_as_missing(self):
        task, worker = self.complete_with(ValueError('Notion getThreadTranscript: bad request'), recovery=True)
        self.assertEqual(task['state'], 'blocked')
        self.assertEqual(worker['state'], 'error')
        self.assertTrue(worker['enabled'])

    def test_cancel_before_submission_still_requeues(self):
        task, worker = self.complete_with(ValueError(CANCELLED_BEFORE_SUBMISSION), recovery=False)
        self.assertEqual((task['state'], task['attempts']), ('retry', 0))
        self.assertEqual(worker['state'], 'ready')


if __name__ == '__main__':
    unittest.main()
