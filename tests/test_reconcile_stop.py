"""The Main stops a hung coworker turn and abandons the task in one call (resolve_uncertain stop=True, rule I5)."""
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future
from pathlib import Path

from notiontool.engine import Engine
from notiontool.store import Store

UNCONFIRMED = 'Notion has not confirmed that the turn finished'
SERVER = Path(__file__).resolve().parent.parent / 'notiontool' / 'server.py'


class StopAdapter:
    def __init__(self, answer=None):
        self.calls = []
        self.answer = dict(ok=True, reason='sent') if answer is None else answer

    def run(self, worker, task, prompt, cancel, update):
        self.calls.append(('run', task['id']))
        raise AssertionError('Resolving must never send the prompt again')

    def recover(self, worker, task, cancel):
        self.calls.append(('recover', task['id']))
        raise AssertionError('Resolving must never start a reconciliation check')

    def stop_turn(self, thread_id, worker=None):
        self.calls.append(('stop', thread_id, (worker or {}).get('id')))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class ReconcileStopTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent / '.test-data'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root, ignore_cleanup_errors=True)
        self.store = Store(self.temp.name)
        self.store.put('accounts', dict(id='a', slot=1, email='test@example.test', state='ready', cooldown_until=0))
        self.store.put('runs', dict(id='r1', chat_id='c', state='executing', operator=True, project_path=self.temp.name,
                                    error='', capacity=2, worker_budget=2, fixes=0, main_worker='m',
                                    created=time.time(), updated=time.time()))
        # A coworker hung on a tool call: the prompt was sent, Notion never confirmed the end of the turn.
        self.store.put('workers', dict(id='w1', account_id='a', thread_id='th-w1', purpose='worker', enabled=True,
                                       state='recovering', lease='L1', label='w1', last_used=0))
        self.store.put('tasks', dict(id='t1', run_id='r1', role='executor', title='Hung', prompt='p', state='uncertain',
                                     result='', deps=[], files=[], attempts=1, worker_id='w1', lease='L1',
                                     thread_id='th-w1', sent_at=time.time() - 900, remote_done_at=0,
                                     side_effect_hold=True, reconcile_checks=9, reconcile_at=time.time() + 3600))
        self.engine = None

    def tearDown(self):
        if self.engine:
            self.engine.close()
        time.sleep(.03)
        self.store.close()
        self.temp.cleanup()

    def make(self, answer=None):
        self.adapter = StopAdapter(answer)
        self.engine = Engine(self.store, self.adapter)
        return self.engine

    def kinds(self):
        return [e['kind'] for e in self.store.events(limit=500)]

    def assert_untouched(self):
        task = self.store.get('tasks', 't1')
        self.assertEqual(task['state'], 'uncertain')
        worker = self.store.get('workers', 'w1')
        self.assertEqual((worker['state'], worker['lease']), ('recovering', 'L1'))

    def test_abandon_without_stop_is_still_refused_and_nothing_is_stopped(self):
        engine = self.make()
        with self.assertRaisesRegex(ValueError, UNCONFIRMED):
            engine.resolve_uncertain('t1', 'abandon')
        self.assertEqual(self.adapter.calls, [])
        self.assert_untouched()

    def test_stop_and_abandon_in_one_call_releases_the_coworker_without_resending(self):
        engine = self.make()
        out = engine.resolve_uncertain('t1', 'abandon', 'Hung on a tool call.', stop=True)
        self.assertEqual(out, dict(id='t1', state='failed', resolution='abandon'))
        self.assertEqual(self.adapter.calls, [('stop', 'th-w1', 'w1')])  # one stop request, no run/recover
        task = self.store.get('tasks', 't1')
        self.assertEqual(task['state'], 'failed')
        self.assertIn('was stopped; nothing was resent', task['error'])
        self.assertEqual(task['stopped_turn']['thread_id'], 'th-w1')
        self.assertNotIn('side_effect_hold', task)
        worker = self.store.get('workers', 'w1')
        self.assertEqual(worker['lease'], '')
        self.assertNotEqual(worker['state'], 'recovering')  # free for a re-delegated task
        self.assertIn('task_stop_turn', self.kinds())
        self.assertIn('task_resolved_by_main', self.kinds())

    def test_failed_stop_changes_nothing_and_names_the_reason(self):
        engine = self.make(dict(ok=False, reason='no_verified_endpoint'))
        with self.assertRaisesRegex(ValueError, 'no_verified_endpoint'):
            engine.resolve_uncertain('t1', 'abandon', stop=True)
        self.assert_untouched()
        self.assertIn('task_stop_turn', self.kinds())

    def test_stop_error_is_reported_not_raised_raw(self):
        engine = self.make(OSError('socket closed'))
        with self.assertRaisesRegex(ValueError, 'Could not stop the Notion turn'):
            engine.resolve_uncertain('t1', 'abandon', stop=True)
        self.assert_untouched()

    def test_stop_cancels_a_running_read_back_instead_of_refusing(self):
        engine = self.make()
        cancel = threading.Event()
        engine.inflight[Future()] = dict(task_id='t1', lease='L1', worker_id='w1', cancel=cancel)
        engine.resolve_uncertain('t1', 'abandon', stop=True)
        self.assertTrue(cancel.is_set())
        self.assertEqual(self.store.get('tasks', 't1')['state'], 'failed')

    def test_accept_ignores_stop_and_still_needs_confirmation(self):
        engine = self.make()
        with self.assertRaisesRegex(ValueError, UNCONFIRMED):
            engine.resolve_uncertain('t1', 'accept', stop=True)
        self.assertEqual(self.adapter.calls, [])
        self.assert_untouched()

    def test_confirmed_turn_needs_no_stop_request(self):
        task = self.store.get('tasks', 't1')
        task['remote_done_at'] = time.time() - 60
        self.store.put('tasks', task)
        engine = self.make()
        engine.resolve_uncertain('t1', 'abandon', stop=True)
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.store.get('tasks', 't1')['state'], 'failed')

    def test_server_route_passes_stop_and_keeps_crlf(self):
        data = SERVER.read_bytes()
        self.assertIn(b"body.get('note', ''), stop=body.get('stop') is True)", data)
        self.assertEqual(data.count(b'\n'), data.count(b'\r\n'))  # no bare LF


if __name__ == '__main__':
    unittest.main()
