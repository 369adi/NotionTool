"""Wiring batch 5: retry 409 override, handoff cleanup and the Main first-reply mark. Fakes only, no network."""
import inspect
import os
import tempfile
import threading
import unittest
from unittest import mock

from notiontool import handoff_files
from notiontool import main_handoff_ops
from notiontool import server
from notiontool.main_handoff_ops import MainHandoffOps, PROMPTS, RESUME_HEADINGS, sweep_archives

REFUSAL = 'Retry would resend work that already reached Notion: t1 (side-effect hold)'


class FakeStore:
    def __init__(self, directory='', settings=None):
        self.directory = directory
        self.data = {}
        self._settings = dict(settings or {})
        self.events = []

    def get(self, kind, ident, default=None):
        return self.data.get(kind, {}).get(ident, default)

    def all(self, kind):
        return list(self.data.get(kind, {}).values())

    def put(self, kind, doc):
        self.data.setdefault(kind, {})[doc['id']] = dict(doc)

    def settings(self):
        return dict(self._settings)

    def event(self, kind, run_id='', **fields):
        self.events.append((kind, fields))


class FakeEngine:
    def __init__(self, error=None):
        self.lock = threading.Lock()
        self.calls = []
        self.error = error

    def retry_blocked(self, run_id, allow_resend=False):
        self.calls.append((run_id, allow_resend))
        if self.error and not allow_resend:
            raise ValueError(self.error)


class StopAfter:
    """stop_event stand-in: wait() returns False n times, then True."""
    def __init__(self, n):
        self.n = n

    def wait(self, _seconds):
        self.n -= 1
        return self.n < 0

    def is_set(self):
        return self.n < 0


class FakeApp:
    action = server.Application.action
    retry_run_request = server.Application.retry_run_request
    RESEND_REFUSAL = server.Application.RESEND_REFUSAL
    prune_handoffs = server.Application.prune_handoffs
    sweep_main_archives = server.Application.sweep_main_archives

    def __init__(self, store, engine):
        self.store, self.engine = store, engine


class RetryConflictTests(unittest.TestCase):
    def test_route_uses_retry_request(self):
        source = inspect.getsource(server)
        self.assertIn("path=='/api/run/retry':\n                            return self.response(*app.retry_run_request(self.read_body()))",
                      source.replace('\r\n', '\n'))

    def test_refusal_is_409_with_text(self):
        engine = FakeEngine(REFUSAL)
        status, payload = FakeApp(FakeStore(), engine).retry_run_request({'id': 'r1'})
        self.assertEqual((status, payload), (409, {'error': REFUSAL}))
        self.assertEqual(engine.calls, [('r1', False)])

    def test_allow_resend_is_passed(self):
        engine = FakeEngine(REFUSAL)
        status, payload = FakeApp(FakeStore(), engine).retry_run_request({'id': 'r1', 'allow_resend': True})
        self.assertEqual((status, payload), (200, {'ok': True}))
        self.assertEqual(engine.calls, [('r1', True)])

    def test_other_errors_keep_generic_handling(self):
        app = FakeApp(FakeStore(), FakeEngine('Wait for reconciliation before retrying'))
        with self.assertRaises(ValueError):
            app.retry_run_request({'id': 'r1'})
        with self.assertRaises(KeyError):
            FakeApp(FakeStore(), FakeEngine()).retry_run_request({})


class PruneWiringTests(unittest.TestCase):
    def test_start_prunes_once_before_the_sweep_thread(self):
        starts = [fn for name, fn in vars(server.Application).items()
                  if callable(fn) and "name='main-archive-sweep'" in inspect.getsource(fn)]
        self.assertEqual(len(starts), 1)
        source = inspect.getsource(starts[0])
        self.assertEqual(source.count('self.prune_handoffs()'), 1)
        self.assertLess(source.index('self.prune_handoffs()'), source.index("name='main-archive-sweep'"))

    def test_timer_prunes_handoffs_folder_each_tick(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = FakeApp(FakeStore(tmp, {'background_remote': False}), FakeEngine())
            app.engine.stop_event = StopAfter(2)
            with mock.patch.object(handoff_files, 'gc', return_value=[]) as gc:
                app.sweep_main_archives()
            self.assertEqual(gc.call_count, 2)
            for call in gc.call_args_list:
                self.assertEqual(os.path.normcase(call.args[0]), os.path.normcase(os.path.join(tmp, 'handoffs')))

    def test_prune_deletes_only_inside_handoffs(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, 'handoffs')
            record = handoff_files.write_handoff(folder, 'w1', dict(kind='x'), 1.0)
            handoff_files.mark_acked(folder, 'w1', 2.0)
            outside = os.path.join(tmp, 'w2.json')
            with open(outside, 'w') as fh:
                fh.write('{"acked_at": 1, "first_task_done_at": 1, "created": 0}')
            store = FakeStore(tmp)
            with open(record['path'], 'r') as fh:
                data = fh.read()
            with open(record['path'], 'w') as fh:
                fh.write(data.replace('"first_task_done_at": null', '"first_task_done_at": 3.0'))
            deleted = FakeApp(store, FakeEngine()).prune_handoffs()
            self.assertFalse(os.path.exists(record['path']))
            self.assertTrue(os.path.exists(outside))
            self.assertTrue(all(os.path.dirname(p) == folder for p in deleted))

    def test_prune_failure_only_writes_event(self):
        store = FakeStore('x')
        with mock.patch.object(handoff_files, 'gc', side_effect=OSError('disk')):
            self.assertEqual(FakeApp(store, FakeEngine()).prune_handoffs(), [])
        self.assertEqual(store.events[0][0], 'handoff_prune_failed')


class FirstReplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = FakeStore(self.tmp.name)
        self.ops = MainHandoffOps(self.store, None, 'acc1', clock=lambda: 10.0)
        handoff_files.write_handoff(self.ops.folder(), self.ops.file_id(), dict(kind='main_handoff'), 1.0)
        handoff_files.mark_acked(self.ops.folder(), self.ops.file_id(), 2.0)
        self.store.put(PROMPTS, dict(id='h1:prompt', chat_id='new', account_id='acc1', acked=True, before_seq=0))

    def tearDown(self):
        self.tmp.cleanup()

    def sweep(self, reply):
        with mock.patch.object(MainHandoffOps, '_reply_text', return_value=reply):
            return sweep_archives(self.store, None, clock=lambda: 10.0)

    def test_marked_once_after_first_resumed_reply(self):
        real = handoff_files.mark_first_task_done
        with mock.patch.object(main_handoff_ops.handoff_files, 'mark_first_task_done', side_effect=real) as mark:
            self.assertNotIn('first_done', self.sweep('ack-token only'))
            mark.assert_not_called()
            reply = 'ack\n' + RESUME_HEADINGS[0] + '\n...'
            self.assertEqual(self.sweep(reply)['first_done'], 1)
            self.sweep(reply)
        mark.assert_called_once_with(self.ops.folder(), self.ops.file_id(), 10.0)
        self.assertFalse(os.path.exists(handoff_files.handoff_path(self.ops.folder(), self.ops.file_id())))
        self.assertTrue(self.store.get(PROMPTS, 'h1:prompt')['first_task_done'])
        self.assertEqual([k for k, _ in self.store.events], ['main_handoff_first_reply'])

    def test_unacked_prompt_is_ignored(self):
        self.store.put(PROMPTS, dict(id='h1:prompt', chat_id='new', account_id='acc1', acked=False))
        with mock.patch.object(main_handoff_ops.handoff_files, 'mark_first_task_done') as mark:
            self.sweep(RESUME_HEADINGS[0])
        mark.assert_not_called()


if __name__ == '__main__':
    unittest.main()
