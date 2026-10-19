"""The Main resolves an unconfirmed task without resending it (Engine.resolve_uncertain, rule I5)."""
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future
from pathlib import Path

from notiontool.engine import Engine
from notiontool.operator_main import result_done
from notiontool.store import Store


class NoSendAdapter:
    def __init__(self):
        self.calls = []

    def run(self, worker, task, prompt, cancel, update):
        self.calls.append(('run', task['id']))
        raise AssertionError('Resolving must never send anything')

    def recover(self, worker, task, cancel):
        self.calls.append(('recover', task['id']))
        raise AssertionError('Resolving must never start a reconciliation check')


class ResolveUncertainTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent / '.test-data'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root, ignore_cleanup_errors=True)
        self.store = Store(self.temp.name)
        self.adapter = NoSendAdapter()
        self.store.put('accounts', dict(id='a', slot=1, email='test@example.test', state='ready', cooldown_until=0))
        self.store.put('workers', dict(id='w1', account_id='a', thread_id='w1', purpose='worker', enabled=True,
                                       state='recovering', lease='L1', label='w1', last_used=0))
        self.store.put('chats', dict(id='c', title='Main', main_worker='m', folder_id='f', project_path=self.temp.name))
        self.store.put('runs', dict(id='r1', chat_id='c', state='executing', operator=True, project_path=self.temp.name,
                                    error='', capacity=2, worker_budget=2, fixes=0, main_worker='m',
                                    created=time.time(), updated=time.time()))
        self.engine = Engine(self.store, self.adapter)

    def tearDown(self):
        self.engine.close()
        time.sleep(.03)
        self.store.close()
        self.temp.cleanup()

    def put_task(self, **extra):
        task = dict(id='t1', run_id='r1', role='executor', title='Gate', prompt='p', state='uncertain', result='',
                    deps=[], files=['a.py'], attempts=1, worker_id='w1', lease='L1',
                    remote_done_at=time.time() - 120, side_effect_hold=True, reconcile_checks=9,
                    reconcile_at=time.time() + 3600, error='Awaiting confirmation from Notion; request will not be resent')
        task.update(extra)
        # The declared file exists, so the machine check (engine._machine_check) passes.
        Path(self.temp.name, 'a.py').write_text('x = 1\n', encoding='utf-8')
        # The engine frees leases without a task when it starts, so the reserved thread is set up with its task.
        self.store.put('workers', dict(id='w1', account_id='a', thread_id='w1', purpose='worker', enabled=True,
                                       state='recovering', lease='L1', label='w1', last_used=0))
        self.store.put('tasks', task)
        return task

    def kinds(self):
        return [e['kind'] for e in self.store.events(limit=500) if e.get('run_id') == 'r1']

    def advance(self):
        with self.engine.lock:
            self.engine._advance(self.store.get('runs', 'r1'))
        return self.store.get('runs', 'r1')

    def assert_untouched(self):
        task = self.store.get('tasks', 't1')
        self.assertEqual(task['state'], 'uncertain')
        self.assertTrue(task.get('side_effect_hold'))
        worker = self.store.get('workers', 'w1')
        self.assertEqual((worker['state'], worker['lease']), ('recovering', 'L1'))

    def test_accept_finishes_task_releases_thread_and_run_is_done(self):
        self.put_task()
        out = self.engine.resolve_uncertain('t1', 'accept', 'Checked engine.py and 8 passing tests.')
        self.assertEqual(out, dict(id='t1', state='done', resolution='accept'))
        task = self.store.get('tasks', 't1')
        self.assertEqual(task['state'], 'done')
        self.assertTrue(result_done(task['result']))
        self.assertIn('Checked engine.py', json.loads(task['result'])['summary'])
        for key in ('remote_unknown', 'side_effect_hold', 'reconcile_at'):
            self.assertNotIn(key, task)
        self.assertEqual(task['main_resolution'], 'accept')
        worker = self.store.get('workers', 'w1')
        self.assertEqual((worker['state'], worker['lease']), ('ready', ''))
        self.assertEqual(self.advance()['state'], 'done')
        self.assertIn('task_resolved_by_main', self.kinds())
        self.assertEqual(self.adapter.calls, [])

    def test_abandon_fails_task_without_resend_and_run_stops(self):
        self.put_task()
        out = self.engine.resolve_uncertain('t1', 'abandon', 'Work incomplete.')
        self.assertEqual(out['state'], 'failed')
        task = self.store.get('tasks', 't1')
        self.assertEqual(task['state'], 'failed')
        self.assertIn('nothing was resent', task['error'])
        self.assertIn('Work incomplete.', task['error'])
        worker = self.store.get('workers', 'w1')
        self.assertEqual((worker['state'], worker['lease']), ('ready', ''))
        self.assertEqual(self.advance()['state'], 'blocked')
        self.assertEqual(self.adapter.calls, [])

    def test_overdue_reserved_task_can_be_resolved(self):
        self.put_task(state='blocked', remote_unknown=True)
        self.engine.resolve_uncertain('t1', 'accept')
        task = self.store.get('tasks', 't1')
        self.assertEqual(task['state'], 'done')
        self.assertNotIn('remote_unknown', task)

    def test_refuses_without_notion_completion(self):
        self.put_task(remote_done_at=0)
        with self.assertRaisesRegex(ValueError, 'not confirmed'):
            self.engine.resolve_uncertain('t1', 'accept')
        self.assert_untouched()

    def test_refuses_tasks_that_are_not_unconfirmed(self):
        self.put_task(state='done', result=json.dumps(dict(status='done', summary='x')))
        with self.assertRaisesRegex(ValueError, 'Only an unconfirmed task'):
            self.engine.resolve_uncertain('t1', 'abandon')
        self.assertEqual(self.store.get('tasks', 't1')['state'], 'done')

    def test_refuses_unknown_task_and_bad_resolution(self):
        with self.assertRaisesRegex(ValueError, 'Task not found'):
            self.engine.resolve_uncertain('nope', 'accept')
        self.put_task()
        with self.assertRaisesRegex(ValueError, 'accept'):
            self.engine.resolve_uncertain('t1', 'resend')
        self.assert_untouched()

    def test_refuses_while_reconciliation_check_runs(self):
        self.put_task()
        future = Future()
        with self.engine.lock:
            self.engine.inflight[future] = dict(task_id='t1', lease='L1', worker_id='w1', run_id='r1',
                                                cancel=threading.Event(), recovery=True)
        try:
            with self.assertRaisesRegex(ValueError, 'reconciliation check is running'):
                self.engine.resolve_uncertain('t1', 'accept')
        finally:
            with self.engine.lock:
                self.engine.inflight.pop(future, None)
        self.assert_untouched()

    def test_resolved_task_no_longer_holds_its_files(self):
        self.put_task()
        probe = dict(id='t2', run_id='r1', files=['a.py'])
        run = self.store.get('runs', 'r1')
        self.assertTrue(self.engine._conflicts(probe, run))
        self.engine.resolve_uncertain('t1', 'accept')
        self.assertFalse(self.engine._conflicts(probe, run))
        self.engine._reconcile()
        self.assertEqual(self.adapter.calls, [])



class MainResolutionRouteTests(unittest.TestCase):
    """The existing routes carry the new decisions; no new endpoint (I2)."""

    def setUp(self):
        from notiontool.server import Application
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.app = Application(self.temp.name)
        self.addCleanup(self.app.store.db.close)
        self.app.store.put('accounts', dict(id='a', slot=1, email='test@example.test', state='ready', cooldown_until=0))

    def worker(self, ident, **extra):
        worker = dict(id=ident, account_id='a', thread_id=ident, purpose='worker', enabled=True, state='ready',
                      lease='', label=ident, last_used=0)
        worker.update(extra)
        self.app.store.put('workers', worker)
        return worker

    def events_text(self):
        return ' '.join(str(event) for event in self.app.store.events(limit=100))

    def test_accept_through_the_reconcile_route(self):
        self.worker('w', state='recovering', lease='L')
        self.app.store.put('tasks', dict(id='u1', run_id='r', state='uncertain', remote_unknown=False, attempts=1,
                                         remote_done_at=time.time() - 60, reconcile_at=time.time() + 300,
                                         worker_id='w', lease='L', files=[]))
        result = self.app.action('/api/task/reconcile', {'id': 'u1', 'resolution': 'accept', 'note': 'checked on disk'})
        self.assertEqual((result['ok'], result['state'], result['resolution']), (True, 'done', 'accept'))
        worker = self.app.store.get('workers', 'w')
        self.assertEqual((worker['state'], worker['lease']), ('ready', ''))
        self.assertIn('checked on disk', self.app.store.get('tasks', 'u1')['result'])

    def test_bad_resolution_is_refused_and_nothing_changes(self):
        self.app.store.put('tasks', dict(id='u2', run_id='r', state='uncertain', remote_done_at=time.time(), attempts=1,
                                         reconcile_at=time.time() + 300))
        with self.assertRaises(ValueError):
            self.app.action('/api/task/reconcile', {'id': 'u2', 'resolution': 'resend'})
        self.assertEqual(self.app.store.get('tasks', 'u2')['state'], 'uncertain')

    def test_free_coworker_can_request_a_fresh_context(self):
        self.worker('w2', context_usage=dict(estimated_tokens=950000))
        result = self.app.action('/api/chat/compress', {'worker_id': 'w2'})
        self.assertEqual((result['ok'], result['worker_id'], result['same_chat']), (True, 'w2', False))
        self.assertIn('950000', result['reason'])
        self.assertTrue(self.app.store.get('workers', 'w2')['compress_requested'])
        self.assertIn('compress_requested', self.events_text())

    def test_fresh_context_refused_for_main_busy_and_unknown_workers(self):
        self.worker('m', purpose='main')
        self.worker('b', state='busy', lease='x')
        self.worker('rec', state='recovering', lease='y')
        for ident in ('m', 'nobody'):
            with self.assertRaisesRegex(ValueError, 'Coworker not found'):
                self.app.action('/api/chat/compress', {'worker_id': ident})
        for ident in ('b', 'rec'):
            with self.assertRaisesRegex(ValueError, 'Only a free coworker'):
                self.app.action('/api/chat/compress', {'worker_id': ident})
            self.assertFalse(self.app.store.get('workers', ident).get('compress_requested'))
        self.assertFalse(self.app.store.get('workers', 'm').get('compress_requested'))

    def test_rotation_settings_are_validated_and_saved(self):
        self.app.action('/api/settings', {'coworker_delete_old_chat': False, 'coworker_rotate_mode': 'same_thread',
                                          'main_context_policy': 'auto'})
        settings = self.app.store.settings()
        self.assertEqual((settings['coworker_delete_old_chat'], settings['coworker_rotate_mode'], settings['main_context_policy']),
                         (False, 'same_thread', 'auto'))
        for body in ({'coworker_rotate_mode': 'new_chat'}, {'main_context_policy': 'always'}):
            with self.assertRaises(ValueError):
                self.app.action('/api/settings', body)
        self.assertEqual(self.app.store.settings()['coworker_rotate_mode'], 'same_thread')
        self.app.action('/api/settings', {'coworker_delete_old_chat': 'true'})
        self.assertIs(self.app.store.settings()['coworker_delete_old_chat'], True)


if __name__ == '__main__':
    unittest.main()
