"""Coworker turns through the real NotionAdapter.run / recover on the simulated Notion.

Every test sets one fault with FakeState.set_fault, then calls the real FakeNotionAdapter.run (which in turn
calls the real recover / reconcile_empty_stream) and checks the result and the events the real code wrote.
Only three things are replaced, all for speed or because the fake cannot answer them:
  - verify_response_model (the fake has no per-step model records, so the real check always ends 'pending')
  - the pre-seeded chat config: setUp stores the confirmed thread_configs record itself, so run() skips the
    config write that the fake cannot show
  - for the slow-timeout test, the stream timeout, which run() fixes at 60 s, is lowered to 0.3 s.
unittest only; every test finishes in well under 2 s.
"""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from notiontool import fake_notion
from notiontool.engine import PendingRemote
from notiontool.fake_notion import FakeState
from notiontool.store import Store

DONE = 'RESULT: DONE'


class FakeAdapterRunTests(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.state = FakeState(path=str(self.tmp / 'fake.json'))
        directory = self.tmp / 'app'
        directory.mkdir()
        self.store = Store(directory)
        self.addCleanup(self.store.close)  # runs before tmp.cleanup (Windows keeps the SQLite file open)
        self.adapter = fake_notion.FakeNotionAdapter(self.store, state=self.state, force=True)
        self.adapter.refresh_on_login = False
        self.adapter.stream_turn_poll_s = 0.05  # the stall watch reacts to a cancel within 50 ms
        self.adapter.verify_response_model = mock.Mock(return_value=[])
        fake_notion.seed_scenario(self.store, self.state)
        self.thread = self.state.threads_of('ready', 'coworker')[0]['id']
        self.worker = self.store.get('workers', 'ready:' + self.thread)
        self.client = self.adapter.client('ready')
        # The confirmed chat config is a store record; with it run() skips the config write the fake cannot show.
        self.store.put('thread_configs', dict(id=self.thread, key=[self.thread, self.worker.get('generation', 0),
                                                                 self.client.model('opus')], at=time.time()))
        self.store.put('tasks', {'id': 't1', 'run_id': 'r1', 'role': 'executor', 'lease': 'assignment',
                                 'state': 'running'})

    # --- helpers ---------------------------------------------------------------------------

    def task(self):
        return self.store.get('tasks', 't1')

    def run_turn(self, cancel=None):
        return self.adapter.run(self.worker, self.task(), 'Say hello', cancel or threading.Event(), lambda text: None)

    def recover(self):
        return self.adapter.recover(self.worker, self.task(), threading.Event())

    def events(self, kind):
        return [e for e in self.store.events() if e['kind'] == kind]

    def own_turn(self):
        """Entities Notion holds for this task's turn: from our user step on."""
        task = self.task()
        entities = [p['entity'] for p in self.client.transcript(self.thread)['patches'] if p['op'] == 'put']
        return [e for e in entities if e.get('sequence', 0) > task['before_seq']]

    def kinds(self):
        return [e['kind'] for e in self.own_turn()]

    def sends(self):
        return [c['step_id'] for c in self.state.calls('runInferenceTranscript')]

    def session_status(self):
        return (self.client.transcript(self.thread).get('session') or {}).get('status')

    def assert_one_send_of_the_task(self):
        self.assertEqual(self.sends(), [self.task()['step_id']])  # never resent
        self.assertEqual(len(self.events('remote_sent')), 1)

    # --- hang ------------------------------------------------------------------------------

    def test_hang_timeout(self):
        self.state.set_fault(self.thread, 'hang', seconds=0.4)
        started = time.monotonic()
        with self.assertRaises(PendingRemote):
            self.run_turn()
        self.assertGreaterEqual(time.monotonic() - started, 0.4)
        self.assertLess(time.monotonic() - started, 10)
        done = self.events('remote_done')
        self.assertEqual([(e['outcome'], e['error_class']) for e in done], [('unknown', 'REMOTE_UNKNOWN')])
        self.assertIn('TimeoutError', done[0]['detail'])
        self.assertIn('hang', done[0]['detail'])
        self.assertEqual(done[0]['lines'], 1)  # the hang sends one patch-start line, never any answer text
        self.assertEqual(self.events('first_output'), [])
        # Output had started (one line), so run() does not reconcile by itself: it stays unconfirmed, never resends.
        self.assertEqual(self.events('recover_timing'), [])
        self.assertEqual(self.kinds(), ['user_message'])
        self.assertEqual(self.own_turn()[0]['client_message_id'], self.task()['step_id'])
        self.assertEqual(self.session_status(), 'running')
        self.assertEqual(self.events('empty_stream_reconciled'), [])
        self.assert_one_send_of_the_task()
        # A later recover still has nothing to confirm and does not resend.
        with self.assertRaises(PendingRemote):
            self.recover()
        self.assert_one_send_of_the_task()
        self.adapter.verify_response_model.assert_not_called()

    def test_hang_cancel(self):
        self.state.set_fault(self.thread, 'hang', seconds=30)
        cancel = threading.Event()
        timer = threading.Timer(0.3, cancel.set)
        timer.start()
        self.addCleanup(timer.cancel)
        started = time.monotonic()
        with self.assertRaises(PendingRemote):
            self.run_turn(cancel)
        self.assertLess(time.monotonic() - started, 10)  # the cancel, not the 30 s hang, ended the call
        self.assertEqual([(e['outcome'], e['error_class']) for e in self.events('remote_done')],
                         [('stream_end', '')])
        self.assertEqual(self.kinds(), ['user_message'])
        self.assertEqual(self.session_status(), 'running')
        task = self.task()
        self.assertTrue(task['remote_running'])
        self.assertFalse(task['remote_working'])  # running, but no step of ours arrived: not 'working'
        self.assertEqual(task['remote_steps'], 0)
        self.assertEqual(len(self.events('recover_timing')), 1)
        self.assert_one_send_of_the_task()
        self.adapter.verify_response_model.assert_not_called()

    # --- interrupted -----------------------------------------------------------------------

    def test_interrupted(self):
        self.state.set_fault(self.thread, 'interrupted')
        with self.assertRaises(PendingRemote):
            self.run_turn()
        done = self.events('remote_done')
        self.assertEqual([(e['outcome'], e['error_class']) for e in done], [('unknown', 'REMOTE_UNKNOWN')])
        self.assertIn('OSError', done[0]['detail'])
        self.assertIn('interrupted', done[0]['detail'])
        self.assertGreater(done[0]['lines'], 0)  # output had started, so run() does not reconcile by itself
        self.assertEqual(len(self.events('first_output')), 1)
        self.assertEqual(self.events('recover_timing'), [])
        self.assertEqual(self.events('empty_stream_reconciled'), [])
        # Notion committed only the first half of the answer and never a turn_completed entry.
        self.assertEqual(self.kinds(), ['user_message', 'assistant_message'])
        self.assertNotIn(DONE, fake_text(self.own_turn()))
        self.assert_one_send_of_the_task()
        self.adapter.verify_response_model.assert_not_called()
        # Like the real Notion, the fake keeps the session 'running' after the cut (no turn_completed), so the
        # later recover does not accept the half answer as the reply: the task stays pending, never resent.
        self.assertEqual(self.session_status(), 'running')
        with self.assertRaises(PendingRemote):
            self.recover()
        self.assertEqual(self.events('recover_timing')[-1]['window'], self.adapter.RECOVER_WINDOW)
        self.assertEqual(self.kinds(), ['user_message', 'assistant_message'])
        self.assertEqual(self.session_status(), 'running')
        self.adapter.verify_response_model.assert_not_called()
        self.assert_one_send_of_the_task()

    # --- lost_stream -----------------------------------------------------------------------

    def test_lost_stream(self):
        self.state.set_fault(self.thread, 'lost_stream')
        with self.assertRaises(PendingRemote):
            self.run_turn()
        done = self.events('remote_done')
        self.assertEqual([(e['outcome'], e['error_class']) for e in done], [('unknown', 'REMOTE_UNKNOWN')])
        self.assertIn('OSError', done[0]['detail'])
        self.assertIn('lost', done[0]['detail'])
        self.assertEqual(done[0]['lines'], 0)
        self.assertEqual(self.events('first_output'), [])
        # Notion never committed the step: our marker is in no transcript window, so recover widened 12 -> 30 -> 100.
        self.assertEqual(self.own_turn(), [])
        self.assertFalse(any(e.get('client_message_id') == self.task()['step_id']
                             for p in self.client.transcript(self.thread)['patches'] for e in [p.get('entity', {})]))
        self.assertEqual([e['window'] for e in self.events('recover_timing')], [self.adapter.RECOVER_WINDOW, 30, 100])
        self.assertEqual(self.events('empty_stream_reconciled'), [])
        self.assertEqual(self.session_status(), 'idle')
        self.assert_one_send_of_the_task()  # run() did not resend although nothing was committed
        self.adapter.verify_response_model.assert_not_called()

    # --- empty_stream ----------------------------------------------------------------------

    def test_empty_stream(self):
        self.state.set_fault(self.thread, 'empty_stream')
        reply = self.run_turn()
        self.assertTrue(str(reply).endswith(DONE))
        self.assertEqual(self.kinds(), ['user_message', 'assistant_message', 'turn_completed'])
        self.assertEqual(str(reply), fake_text(self.own_turn()))  # the committed reply, not stream text
        # A real empty stream: no line at all, then the connection ends with an OSError; run() reconciles by itself.
        done = self.events('remote_done')
        # The stream died before the first patch-start line: an empty stream is unknown, and run() reconciles it.
        self.assertEqual([(e['outcome'], e['error_class']) for e in done], [('unknown', 'REMOTE_UNKNOWN')])
        self.assertIn('empty stream', done[0]['detail'])
        self.assertEqual(done[0]['lines'], 0)
        self.assertEqual(self.events('first_output'), [])  # the stream carried no answer text at all
        self.assertEqual(len(self.events('empty_stream_reconciled')), 1)
        self.assertEqual(len(self.events('recover_timing')), 1)
        self.adapter.verify_response_model.assert_called_once()
        self.assertIsNotNone(self.task().get('remote_done_at'))
        self.assert_one_send_of_the_task()

    # --- slow ------------------------------------------------------------------------------

    def test_slow(self):
        self.state.set_fault(self.thread, 'slow', seconds=0.4)
        started = time.monotonic()
        reply = self.run_turn()
        self.assertGreaterEqual(time.monotonic() - started, 0.4)
        self.assertTrue(str(reply).endswith(DONE))
        self.assertEqual(self.kinds(), ['user_message', 'assistant_message', 'turn_completed'])
        self.assertEqual([(e['outcome'], e['error_class']) for e in self.events('remote_done')], [('stream_end', '')])
        self.assertEqual(len(self.events('first_output')), 1)
        self.assertGreaterEqual(self.events('remote_done')[0]['seconds'], 0.4)
        self.adapter.verify_response_model.assert_called_once()
        self.assert_one_send_of_the_task()

    def test_slow_past_the_call_timeout(self):
        self.state.set_fault(self.thread, 'slow', seconds=5)
        transport = self.client.transport
        original = transport.stream

        def short(path, body, headers=None, timeout=60, cancel=None):
            return original(path, body, headers, timeout=0.3, cancel=cancel)  # run() passes 60; patched down
        started = time.monotonic()
        with mock.patch.object(transport, 'stream', short):
            outcome = self.attempt_run()
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(outcome, 'pending')  # a timeout after output started is unconfirmed, never a failure
        done = self.events('remote_done')
        self.assertEqual([(e['outcome'], e['error_class']) for e in done], [('unknown', 'REMOTE_UNKNOWN')])
        self.assertIn('TimeoutError', done[0]['detail'])
        self.assertIn('slow reply', done[0]['detail'])
        self.assertEqual(done[0]['lines'], 1)
        self.assertEqual(self.events('recover_timing'), [])  # run() did not reconcile by itself
        # The fake commits the late reply anyway; the real recover then confirms it without a second send.
        self.assertEqual(self.kinds(), ['user_message', 'assistant_message', 'turn_completed'])
        reply = self.recover()
        self.assertTrue(str(reply).endswith(DONE))
        self.assertEqual(str(reply), fake_text(self.own_turn()))
        self.adapter.verify_response_model.assert_called_once()
        self.assert_one_send_of_the_task()

    def attempt_run(self):
        try:
            return 'reply:' + str(self.run_turn())
        except PendingRemote:
            return 'pending'

    # --- error_step (the design lists it; cheap, so covered as well) ----------------------------

    def test_error_step(self):
        self.state.set_fault(self.thread, 'error_step')
        with self.assertRaises(PendingRemote):
            self.run_turn()
        self.assertEqual(self.kinds(), ['user_message', 'agent_error', 'turn_completed'])
        self.assertEqual(fake_text(self.own_turn()), '')  # no reply text
        self.assertEqual([(e['outcome'], e['error_class']) for e in self.events('remote_done')], [('stream_end', '')])
        self.assertEqual(self.task()['remote_steps'], 2)
        self.adapter.verify_response_model.assert_not_called()
        self.assert_one_send_of_the_task()


def fake_text(entities):
    from notiontool.notion import text_content
    return '\n'.join(text_content(e.get('content', [])) for e in entities if e.get('kind') == 'assistant_message')


if __name__ == '__main__':
    unittest.main()
