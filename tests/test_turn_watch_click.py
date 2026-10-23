"""Click mode (owner 2026-10-24): a stopped turn is continued like a click on Notion's 'Continue' button,
without a chat message and without a cap. Fakes only, no network."""
import unittest

from notiontool import turn_watch
from notiontool.turn_watch import CLICK_GRACE_SECONDS, GRACE_SECONDS, MAX_CHAIN_OPERATOR, RESTART_CUT_BEFORE
from tests.test_model_resume import OPUS, WORKER, FakeClient, make_adapter
from tests.test_model_resume import FakeStore as ResumeStore
from tests.test_turn_watch import FakeAdapter, FakeEngine, FakeStore, TurnWatchTests, entity, transcript


class HttpError(Exception):
    def __init__(self, status):
        super().__init__('HTTP %d' % status)
        self.status = status


class ClickAdapter(FakeAdapter):
    def __init__(self):
        super().__init__()
        self.clicks, self.reject = [], None

    def click_continue(self, worker, step_id, task=None):
        if self.reject:
            raise HttpError(self.reject)
        self.clicks.append((worker['thread_id'], step_id))
        return True


class SettingsStore(FakeStore):
    extra = {}

    def settings(self):
        return dict(super().settings(), **self.extra)


class ClickModeTests(unittest.TestCase):
    make = TurnWatchTests.make
    worker = TurnWatchTests.worker
    kinds = TurnWatchTests.kinds

    def setUp(self):
        self.store, self.adapter, self.engine, self.now = SettingsStore(), ClickAdapter(), FakeEngine(), 1000.0
        self.store.extra = {}
        self.store.put('accounts', {'id': 'acc', 'state': 'ready', 'team_threads': {'main': {'id': 'main-thread'}}})
        self.store.put('workers', {'id': 'w-main', 'account_id': 'acc', 'thread_id': 'main-thread', 'state': 'ready',
                                   'purpose': 'main'})
        self.store.put('chats', {'id': 'chat', 'main_worker': 'w-main'})
        self.watch = self.make()

    def stop(self, n=0, user='u1', at=None):
        """The turn stopped after a tool step (Notion's step limit): no reply after the user message."""
        self.adapter.transcripts['main-thread'] = transcript(
            entity(1, 'user_message', user), entity(2, 'assistant_message'), entity(3, 'tool_call'),
            entity(4 + n, 'tool_result', 'last-%d' % n, created_at=self.now if at is None else at))

    def test_click_after_a_short_wait_writes_no_message(self):
        self.stop()
        self.assertIsNone(self.watch.check(self.worker()))
        self.now += CLICK_GRACE_SECONDS + 1
        step = self.watch.check(self.worker())
        self.assertEqual(self.adapter.clicks, [('main-thread', step)])
        self.assertEqual(self.adapter.sent, [])
        self.now += 120
        self.assertIsNone(self.watch.check(self.worker()))  # the same stop is never clicked twice
        self.assertIsNone(self.make().check(self.worker()))  # also after a restart
        self.assertEqual(len(self.adapter.clicks), 1)
        self.assertLess(CLICK_GRACE_SECONDS, GRACE_SECONDS)

    def test_clicks_have_no_cap(self):
        rounds = MAX_CHAIN_OPERATOR + 10
        for n in range(rounds):
            self.stop(n)
            self.now += CLICK_GRACE_SECONDS + 1
            self.watch.check(self.worker())
        self.assertEqual(len(self.adapter.clicks), rounds)
        self.assertNotIn('turn_resume_exhausted', self.kinds())
        self.assertEqual(self.worker()['resume']['chain'], rounds)  # still counted as one chain

    def test_cap_can_be_set(self):
        self.store.extra = {'resume_max_chain': 3}
        for n in range(6):
            self.stop(n)
            self.now += CLICK_GRACE_SECONDS + 1
            self.watch.check(self.worker())
        self.assertEqual(len(self.adapter.clicks), 3)
        self.assertIn('turn_resume_exhausted', self.kinds())

    def test_old_chat_stopped_before_the_start_is_left_alone(self):
        self.stop(at=self.now - RESTART_CUT_BEFORE - 60)
        self.now += 3600
        self.assertIsNone(self.watch.check(self.worker()))
        self.assertEqual((self.adapter.clicks, self.adapter.sent), ([], []))

    def test_setting_off_sends_the_old_message(self):
        self.store.extra = {'resume_click': False}
        self.stop()
        self.now += CLICK_GRACE_SECONDS + 1
        self.assertIsNone(self.watch.check(self.worker()))  # the long grace applies again
        self.now += GRACE_SECONDS
        step = self.watch.check(self.worker())
        self.assertEqual(self.adapter.sent, [('main-thread', step, turn_watch.RESUME_TEXT)])
        self.assertEqual(self.adapter.clicks, [])

    def test_rejected_click_falls_back_to_the_message(self):
        self.adapter.reject = 400
        self.stop()
        self.now += CLICK_GRACE_SECONDS + 1
        step = self.watch.check(self.worker())
        self.assertEqual(self.adapter.sent, [('main-thread', step, turn_watch.RESUME_TEXT)])
        self.assertIn('turn_click_fallback', self.kinds())

    def test_other_click_error_is_retried_later(self):
        self.adapter.reject = 500
        self.stop()
        self.now += CLICK_GRACE_SECONDS + 1
        self.watch.check(self.worker())
        self.assertIn('turn_resume_failed', self.kinds())
        self.adapter.reject = None
        self.watch.check(self.worker())
        self.assertEqual(len(self.adapter.clicks), 1)
        self.assertEqual(self.adapter.sent, [])


class ClickBodyTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.adapter = make_adapter(ResumeStore(accounts=[{'id': 'a1', 'model': OPUS}]), self.client)
        self.adapter.body = lambda c, thread, step, text, model, computer=False, effort=None: {
            'threadId': thread, 'transcript': [{'type': 'config'}, {'type': 'context'},
                                               {'type': 'user', 'id': step, 'value': [[text]]}],
            'submittedUserStepId': step, 'isPartialTranscript': False, 'createThread': True, 'generateTitle': True}

    def test_click_sends_the_continue_button_request_without_a_user_step(self):
        self.assertTrue(self.adapter.click_continue(WORKER, 'step1'))
        path, body = self.client.transport.streams[0]
        self.assertEqual(path, 'runInferenceTranscript')
        self.assertEqual([s['type'] for s in body['transcript']], ['config', 'context'])
        self.assertNotIn('submittedUserStepId', body)
        self.assertEqual((body['isPartialTranscript'], body['createThread'], body['generateTitle']), (True, False, False))
        self.assertEqual(body['threadId'], 't1')

    def test_message_resume_still_writes_its_message(self):
        self.adapter.continue_turn(WORKER, 'continue', 'step1')
        _path, body = self.client.transport.streams[0]
        self.assertEqual(body['transcript'][-1]['value'], [['continue']])
        self.assertEqual(body['submittedUserStepId'], 'step1')

    def test_busy_chat_is_not_clicked(self):
        self.client.transcript = lambda thread_id, limit=10: {'session': {'status': 'running'}}
        self.assertFalse(self.adapter.click_continue(WORKER, 'step1'))
        self.assertEqual(self.client.transport.streams, [])


if __name__ == '__main__':
    unittest.main()
