# I5 audit (work/audit/RETRY_HELD.md): a manual retry must not resend work that already reached Notion.
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future
from pathlib import Path

from notiontool.engine import Engine, RateLimited
from notiontool.network_budget import RemotePaused
from notiontool.notion import ThreadMissing
from notiontool.store import Store

try:
    from tests.test_engine import FakeAdapter
except ImportError:  # run from inside tests/
    from test_engine import FakeAdapter

WAIT = 'Wait for reconciliation before retrying'
RESEND = 'Retry would resend work that already reached Notion'


class RetryHeldTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent / '.test-data'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root, ignore_cleanup_errors=True)
        self.store = Store(self.temp.name)
        self.adapter = FakeAdapter()
        self.store.put('accounts', dict(id='a', slot=1, email='test@example.test', state='ready', cooldown_until=0))
        for ident in ('first', 'second'):
            self.store.put('workers', dict(id=ident, account_id='a', thread_id=ident, purpose='worker', enabled=True,
                                           state='ready', lease='', label=ident, last_used=0))
        self.store.put('chats', dict(id='c', title='Main', main_worker='first', folder_id='f', project_path=self.temp.name))
        self.run_record('r1')
        self.engine = Engine(self.store, self.adapter)

    def tearDown(self):
        self.adapter.gate.set()
        self.engine.close()
        time.sleep(.03)
        self.store.close()
        self.temp.cleanup()

    def run_record(self, run_id, state='executing'):
        self.store.put('runs', dict(id=run_id, chat_id='c', state=state, blocked_stage='executing', project_path=self.temp.name,
                                    error='', capacity=2, worker_budget=2, main_worker='first'))

    def block(self, run_id='r1'):
        run = self.store.get('runs', run_id)
        run.update(state='blocked', blocked_stage='executing', error='A task is blocked')
        self.store.put('runs', run)

    def task(self, task_id='t1', run_id='r1', worker_id='first', **extra):
        # A running, submitted attempt: sent_at and submission_phase as notion.run writes them.
        started = time.time() - 30
        task = dict(id=task_id, run_id=run_id, role='executor', title='Part ' + task_id, prompt='do it', state='running',
                    lease='lease-' + task_id, worker_id=worker_id, attempts=1, started=started, submission_phase='inference',
                    sent_at=started + 1, remote_done_at=0, files=[], deps=[], result='', partial='',
                    attempt_log=[dict(attempt=1, worker_id=worker_id, started=started)])
        task.update(extra)
        self.store.put('tasks', task)
        worker = self.store.get('workers', worker_id)
        if worker:
            worker.update(state='busy', lease=task['lease'])
            self.store.put('workers', worker)
        return task

    def complete(self, task_id='t1', exc=None, text=None, recovery=False):
        task = self.store.get('tasks', task_id)
        future = Future()
        if exc is not None:
            future.set_exception(exc)
        else:
            future.set_result(text)
        info = dict(task_id=task_id, lease=task['lease'], worker_id=task['worker_id'], run_id=task['run_id'],
                    cancel=threading.Event())
        if recovery:
            info['recovery'] = True
        with self.engine.lock:
            self.engine._complete(future, info)
        return self.store.get('tasks', task_id)

    def kinds(self, kind):
        return [e for e in self.store.events(limit=500) if e['kind'] == kind]

    def assert_refused(self, task_id='t1', run_id='r1'):
        before = self.store.get('tasks', task_id)
        with self.assertRaises(ValueError) as caught:
            self.engine.retry_blocked(run_id)
        self.assertIn(RESEND, str(caught.exception))
        self.assertIn(task_id, str(caught.exception))
        after = self.store.get('tasks', task_id)
        self.assertEqual(after['state'], before['state'])
        self.assertEqual(after['attempts'], before['attempts'])
        self.assertEqual(self.store.get('runs', run_id)['state'], 'blocked')
        return str(caught.exception)

    def set_max_attempts(self, value):
        settings = self.store.settings()
        settings.update(max_attempts=value)
        self.store.put('settings', settings)

    # 1
    def test_side_effect_hold_is_not_resent(self):
        self.store.put('tasks', dict(id='t1', run_id='r1', role='executor', title='Part', prompt='p', state='blocked',
                                     lease='', worker_id='first', attempts=1, side_effect_hold=True, sent_at=100,
                                     submission_phase='inference', files=[], deps=[]))
        self.block()
        message = self.assert_refused()
        self.assertIn('used tools or claimed files', message)
        self.engine.tick()
        time.sleep(.02)
        self.engine.tick()
        self.assertNotIn(('r1', 'executor'), self.adapter.roles)
        self.assertTrue(self.store.get('tasks', 't1')['side_effect_hold'])

    # 2
    def test_thread_missing_after_submit_is_not_resent(self):
        self.task()
        task = self.complete(exc=ThreadMissing('first', 'getThreadTranscript'), recovery=True)
        self.assertEqual(task['state'], 'blocked')
        self.assertFalse(task.get('remote_unknown'))
        self.block()
        self.assertIn('no confirmed outcome', self.assert_refused())
        self.assertEqual(self.store.get('workers', 'first')['state'], 'missing')

    # 3
    def test_reconcile_missing_worker_is_not_resent(self):
        self.task(worker_id='ghost', state='uncertain', reconcile_at=0)
        self.block()
        self.engine.tick()
        task = self.store.get('tasks', 't1')
        self.assertEqual(task['state'], 'blocked')
        self.assertFalse(task.get('remote_unknown'))
        self.assert_refused()

    # 4
    def test_http_5xx_after_send_is_not_resent(self):
        self.task()
        task = self.complete(exc=ValueError('Notion inference: HTTP 502'))
        self.assertEqual(task['state'], 'blocked')
        self.block()
        self.assertIn('no confirmed outcome', self.assert_refused())

    # 5
    def test_confirmed_turn_failure_with_tools_is_not_resent(self):
        self.set_max_attempts(1)
        self.task(model_verification='turn_error')  # notion.py marks the turn before raising RateLimited(confirmed=True)
        self.store.event('tool_started', 'r1', task_id='t1', tool='run_shell', message='Shell: edit')
        task = self.complete(exc=RateLimited(20, 'Turn failed', confirmed=True))
        self.assertEqual(task['state'], 'blocked')
        self.assertFalse(task.get('side_effect_hold'))  # the :759 check is skipped at max_attempts
        self.block()
        self.assertIn('submitted turn used tools', self.assert_refused())

    # 6
    def test_unsubmitted_blocks_are_retried(self):
        # (a) dependency failed: never dispatched in this attempt
        self.store.put('tasks', dict(id='t2', run_id='r1', role='executor', title='dep', prompt='p', state='blocked',
                                     lease='', worker_id='', attempts=0, deps=['t9'], files=[],
                                     error='A dependency failed'))
        # (b) PC tools not connected: blocked before the lease and the preparing marker
        self.store.put('tasks', dict(id='t3', run_id='r1', role='executor', title='bridge', prompt='p', state='blocked',
                                     lease='', worker_id='', attempts=0, deps=[], files=['a.py'],
                                     error='Connect NotionTool PC tools before executing tasks that change project files'))
        # (c) RemotePaused before inference; an old attempt's sent_at does not count
        self.task(submission_phase='preparing', sent_at=50)
        task = self.complete(exc=RemotePaused('ACCOUNT_BLOCKED'))
        self.assertEqual(task['state'], 'blocked')
        self.assertEqual(task.get('paused_by'), 'account_blocked')
        self.block()
        self.engine.retry_blocked('r1')
        for ident in ('t1', 't2', 't3'):
            task = self.store.get('tasks', ident)
            self.assertEqual(task['state'], 'queued', ident)
            self.assertEqual(task['attempts'], 0, ident)
        self.assertEqual(self.store.get('runs', 'r1')['state'], 'executing')
        self.assertEqual(self.kinds('manual_resend'), [])

    # 7
    def test_clean_completed_failure_is_retried(self):
        reply = json.dumps(dict(status='failed', summary='Could not do it', evidence=[]))
        self.task(remote_done_at=time.time())
        self.assertEqual(self.complete(text=reply)['state'], 'failed')
        self.block()
        self.engine.retry_blocked('r1')
        self.assertEqual(self.store.get('tasks', 't1')['state'], 'queued')
        # same reply, but this attempt used a tool
        self.run_record('r2')
        self.task('t2', 'r2', 'second', remote_done_at=time.time())
        self.store.event('tool_started', 'r2', task_id='t2', tool='write_file', message='write')
        self.assertEqual(self.complete('t2', text=reply)['state'], 'failed')
        self.block('r2')
        self.assertIn('submitted turn used tools', self.assert_refused('t2', 'r2'))

    # 8
    def test_confirmed_turn_error_without_tools_is_retried(self):
        self.set_max_attempts(1)
        self.task(model_verification='turn_error')
        task = self.complete(exc=RateLimited(20, 'Turn failed', confirmed=True))
        self.assertEqual(task['state'], 'blocked')
        self.block()
        self.engine.retry_blocked('r1')
        self.assertEqual(self.store.get('tasks', 't1')['state'], 'queued')

    # 9
    def test_allow_resend_overrides_and_logs(self):
        self.store.put('tasks', dict(id='t1', run_id='r1', role='executor', title='Part', prompt='p', state='blocked',
                                     lease='', worker_id='first', attempts=1, side_effect_hold=True, sent_at=100,
                                     submission_phase='inference', files=[], deps=[]))
        self.block()
        self.engine.retry_blocked('r1', allow_resend=True)
        task = self.store.get('tasks', 't1')
        self.assertEqual(task['state'], 'queued')
        self.assertEqual(task['attempts'], 0)
        self.assertNotIn('side_effect_hold', task)
        events = self.kinds('manual_resend')
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].get('task_id'), 't1')

    # 10
    def test_unreconciled_tasks_still_wait(self):
        for extra in (dict(state='uncertain'), dict(state='blocked', remote_unknown=True)):
            with self.subTest(**extra):
                self.store.put('tasks', dict(dict(id='t1', run_id='r1', role='executor', title='Part', prompt='p',
                                                  lease='', worker_id='first', attempts=1, sent_at=100,
                                                  submission_phase='inference', files=[], deps=[]), **extra))
                self.block()
                with self.assertRaises(ValueError) as caught:
                    self.engine.retry_blocked('r1', allow_resend=True)
                self.assertEqual(str(caught.exception), WAIT)

    # resume_remote applies the same rule but skips instead of raising
    def test_resume_remote_skips_hazardous_tasks(self):
        common = dict(run_id='r1', role='executor', prompt='p', state='blocked', lease='', worker_id='first', attempts=0,
                      files=[], deps=[], paused_by='account_blocked')
        self.store.put('tasks', dict(common, id='t1', title='sent', sent_at=100, submission_phase='inference'))
        self.store.put('tasks', dict(common, id='t2', title='not sent', submission_phase='preparing'))
        self.block()
        result = self.engine.resume_remote()
        self.assertEqual(result['requeued_tasks'], 1)
        held = self.store.get('tasks', 't1')
        self.assertEqual(held['state'], 'blocked')
        self.assertEqual(held['paused_by'], 'account_blocked')
        self.assertEqual(self.store.get('tasks', 't2')['state'], 'queued')
        refused = self.kinds('resend_refused')
        self.assertEqual([e.get('task_id') for e in refused], ['t1'])
        self.assertEqual(self.store.get('runs', 'r1')['state'], 'blocked')


if __name__ == '__main__':
    unittest.main()
