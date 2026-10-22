import tempfile
import threading
import time
import unittest
from concurrent.futures import Future
from pathlib import Path

from notiontool.engine import Engine, PendingRemote, RateLimited
from notiontool.store import Store


class RecordingAdapter:
    def __init__(self):
        self.runs = []
        self.recoveries = 0

    def run(self, worker, task, prompt, cancel, update):
        self.runs.append((worker['id'], task['id']))
        raise AssertionError('An unconfirmed attempt with side effects must not be sent again')

    def recover(self, worker, task, cancel):
        self.recoveries += 1
        raise PendingRemote()


class RetryGuardTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent / '.test-data'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root, ignore_cleanup_errors=True)
        self.store = Store(self.temp.name)
        self.adapter = RecordingAdapter()
        self.store.put('accounts', dict(id='a', slot=1, email='test@example.test', state='ready', cooldown_until=0))
        for ident in ('first', 'second', 'third'):
            self.store.put('workers', dict(id=ident, account_id='a', thread_id=ident, purpose='worker', enabled=True,
                                           state='ready', lease='', label=ident, last_used=0))
        self.store.put('chats', dict(id='c', title='Main', main_worker='first', folder_id='f', project_path=self.temp.name))
        self.store.put('runs', dict(id='r1', chat_id='c', state='executing', project_path=self.temp.name, error='',
                                       capacity=3, worker_budget=3, main_worker='first'))
        self.engine = Engine(self.store, self.adapter)

    def tearDown(self):
        self.engine.close()
        time.sleep(.03)
        self.store.close()
        self.temp.cleanup()

    def task(self, **extra):
        started = time.time() - 400
        task = dict(id='t1', run_id='r1', role='executor', title='Operator acceptance check', prompt='check', state='running',
                    lease='lease-1', worker_id='first', attempts=1, started=started, submission_phase='inference',
                    sent_at=started + 10, remote_done_at=time.time() - 120, reconcile_checks=3, files=[], deps=[],
                    result='', partial='', attempt_log=[dict(attempt=1, worker_id='first', started=started)])
        task.update(extra)
        self.store.put('tasks', task)
        worker = self.store.get('workers', 'first')
        worker.update(state='busy', lease='lease-1')
        self.store.put('workers', worker)
        return task

    def complete_with(self, exc):
        future = Future()
        future.set_exception(exc)
        info = dict(task_id='t1', lease='lease-1', worker_id='first', run_id='r1', cancel=threading.Event())
        self.engine._complete(future, info)
        return self.store.get('tasks', 't1'), info

    def test_live_case_file_claims_keep_empty_reply_attempt_uncertain(self):
        self.task(files=['notiontool/operator_main.py', 'notiontool/engine.py', 'tests/test_operator_main.py'])
        # Only a proven empty turn may retry; the file claims must still hold it for reconcile.
        task, _ = self.complete_with(PendingRemote(state='done_empty'))
        self.assertEqual(task['state'], 'uncertain')
        self.assertEqual(task['worker_id'], 'first')
        self.assertTrue(task['side_effect_hold'])
        self.assertNotEqual(task['attempt_log'][-1]['state'], 'retry')
        self.assertEqual(self.store.get('workers', 'first')['state'], 'recovering')
        kinds = [e['kind'] for e in self.store.events(limit=500)]
        self.assertIn('retry_refused_side_effects', kinds)
        self.assertNotIn('empty_reply_retry', kinds)

    def test_tool_call_of_the_attempt_blocks_redispatch_to_other_workers(self):
        self.task()
        self.store.event('tool_started', 'r1', task_id='t1', tool='run_shell', message='Shell: edit')
        task, _ = self.complete_with(PendingRemote())
        self.assertEqual(task['state'], 'uncertain')
        for _ in range(5):
            self.engine.tick()
            time.sleep(.02)
        self.assertEqual(self.adapter.runs, [])
        task = self.store.get('tasks', 't1')
        self.assertIn(task['state'], ('uncertain', 'blocked'))
        self.assertEqual(task['worker_id'], 'first')
        for ident in ('second', 'third'):
            self.assertEqual(self.store.get('workers', ident)['lease'], '')

    def test_tool_call_from_an_earlier_attempt_does_not_count(self):
        self.store.event('tool_started', 'r1', task_id='t1', tool='run_shell', message='old attempt')
        time.sleep(.01)
        self.task(attempt_log=[dict(attempt=1, worker_id='first', started=time.time())])
        self.assertFalse(self.engine._attempt_side_effects(self.store.get('tasks', 't1')))

    def test_clean_empty_reply_may_still_retry(self):
        self.task()
        task, _ = self.complete_with(PendingRemote(state='done_empty'))
        self.assertEqual(task['state'], 'retry')
        self.assertFalse(task.get('side_effect_hold'))

    def test_unproven_empty_reply_never_retries(self):
        # b59013e3 / build 23: without proof from the transcript an empty-looking turn is never resent.
        self.task()
        task, _ = self.complete_with(PendingRemote())
        self.assertEqual(task['state'], 'uncertain')
        kinds = [e['kind'] for e in self.store.events(limit=500)]
        self.assertNotIn('empty_reply_retry', kinds)

    def test_done_error_counts_as_proven_empty(self):
        self.task()
        task, _ = self.complete_with(PendingRemote(state='done_error'))
        self.assertEqual(task['state'], 'retry')

    def test_confirmed_failed_turn_with_side_effects_is_not_retried(self):
        self.task(files=['notiontool/engine.py'])
        task, info = self.complete_with(RateLimited(5, 'Turn failed', confirmed=True))
        self.assertEqual(task['state'], 'blocked')
        self.assertNotIn('verified_retry', info)
        self.assertIn('will not be resent', task['error'])

    def test_confirmed_failed_turn_without_side_effects_retries(self):
        self.task()
        task, info = self.complete_with(RateLimited(5, 'Turn failed', confirmed=True))
        self.assertEqual(task['state'], 'retry')
        self.assertTrue(info.get('verified_retry'))


class ReconcileRouteTests(unittest.TestCase):
    def setUp(self):
        from notiontool.server import Application
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.app = Application(self.temp.name)
        self.addCleanup(self.app.store.db.close)

    def test_uncertain_task_without_remote_unknown_can_be_reconciled_without_resend(self):
        self.app.store.put('tasks', dict(id='u1', run_id='r', state='uncertain', remote_unknown=False, reconcile_at=time.time() + 300,
                                         reconcile_checks=4, attempts=1, worker_id='w', lease='keep'))
        self.assertEqual(self.app.action('/api/task/reconcile', {'id': 'u1'}), {'ok': True})
        task = self.app.store.get('tasks', 'u1')
        self.assertEqual((task['state'], task['reconcile_at'], task['reconcile_checks'], task['attempts'], task['lease']),
                         ('uncertain', 0, 0, 1, 'keep'))
        self.assertFalse(task['remote_unknown'])

    def test_overdue_task_still_reconciles(self):
        self.app.store.put('tasks', dict(id='u2', run_id='r', state='blocked', remote_unknown=True, attempts=1))
        self.assertEqual(self.app.action('/api/task/reconcile', {'id': 'u2'}), {'ok': True})
        self.assertEqual(self.app.store.get('tasks', 'u2')['state'], 'uncertain')

    def test_finished_task_is_refused(self):
        self.app.store.put('tasks', dict(id='u3', run_id='r', state='done'))
        with self.assertRaisesRegex(ValueError, 'does not need manual reconciliation'):
            self.app.action('/api/task/reconcile', {'id': 'u3'})


if __name__ == '__main__':
    unittest.main()
