"""Operator Main usage check in turn_watch: throttle, one larger first read, user turns, main_switch.check."""
import copy
import threading
import unittest
from unittest import mock

from notiontool.turn_watch import RESUME_TEXT, USAGE_FIRST_WINDOW, WINDOW, TurnWatch, user_turns


def entity(seq, kind, ident=None, **extra):
    return {'op': 'put', 'entity': dict(sequence=seq, kind=kind, id=ident or 'e%d' % seq, **extra)}


def transcript(*patches):
    return {'session': {'status': 'idle', 'sequence': 99}, 'session_pending': False, 'patches': list(patches)}


class FakeStore:
    def __init__(self, **settings):
        self.data, self.events = {}, []
        self.config = dict(harness_account_id='acc', **settings)

    def settings(self):
        return dict(self.config)

    def get(self, kind, ident, default=None):
        value = self.data.get(kind, {}).get(ident)
        return copy.deepcopy(value) if value is not None else default

    def all(self, kind):
        return [copy.deepcopy(v) for v in self.data.get(kind, {}).values()]

    def put(self, kind, doc):
        self.data.setdefault(kind, {})[doc['id']] = copy.deepcopy(doc)

    def event(self, kind, run_id='', **fields):
        self.events.append(kind)


class FakeClient:
    def __init__(self, owner):
        self.owner, self.info = owner, {'space_id': 'space'}

    def transcript(self, thread, limit=100):
        self.owner.reads.append((thread, limit))
        return self.owner.transcripts[thread]


class FakeAdapter:
    def __init__(self):
        self.transcripts, self.reads = {}, []

    def client(self, account_id):
        return FakeClient(self)

    def new_id(self, space_id, tail='00aa'):
        return 'step'

    def continue_turn(self, worker, text, step_id):
        return True


class FakeEngine:
    def __init__(self):
        self.lock, self.stop_event = threading.RLock(), threading.Event()


OPERATOR = dict(id='acc:op', account_id='acc', thread_id='op-thread', purpose='operator', state='idle')
COWORKER = dict(id='acc:cw', account_id='acc', thread_id='cw-thread', purpose='worker', state='idle')
ANSWERED = transcript(entity(1, 'user_message', 'u1', content=[['first request']]),
                      entity(2, 'user_message', 'u2', content=[[RESUME_TEXT]]),
                      entity(3, 'user_message', 'u3', content=[['second request']]),
                      entity(4, 'assistant_message', 'a1', content=[['done']]))


class Clock:
    def __init__(self):
        self.now = 10_000.0

    def __call__(self):
        return self.now


def make(**settings):
    store = FakeStore(**settings)
    store.put('workers', OPERATOR)
    store.put('workers', COWORKER)
    adapter = FakeAdapter()
    adapter.transcripts = {OPERATOR['thread_id']: ANSWERED, COWORKER['thread_id']: ANSWERED}
    clock = Clock()
    watch = TurnWatch(store, adapter, FakeEngine(), clock=clock)
    watch.spawn = lambda fn, *args: None
    return store, adapter, watch, clock


def observations(store, worker):
    return (store.get('thread_context', worker['account_id'] + ':' + worker['thread_id']) or {}).get('observations', 0)


class UsageTests(unittest.TestCase):
    def test_only_operator_workers_are_measured(self):
        store, adapter, watch, clock = make()
        watch.check(COWORKER)
        self.assertEqual(observations(store, COWORKER), 0)
        self.assertIsNone(store.get('workers', COWORKER['id']).get('context_usage'))
        watch.check(OPERATOR)
        self.assertEqual(observations(store, OPERATOR), 1)
        self.assertIsNotNone(store.get('workers', OPERATOR['id']).get('context_usage'))

    def test_throttled_to_300_seconds(self):
        store, adapter, watch, clock = make()
        watch.check(OPERATOR)
        clock.now += 299
        watch.check(OPERATOR)
        self.assertEqual(observations(store, OPERATOR), 1)
        clock.now += 1
        watch.check(OPERATOR)
        self.assertEqual(observations(store, OPERATOR), 2)

    def test_throttle_setting(self):
        store, adapter, watch, clock = make(main_context_check_seconds=60)
        watch.check(OPERATOR)
        clock.now += 60
        watch.check(OPERATOR)
        self.assertEqual(observations(store, OPERATOR), 2)

    def test_one_larger_first_read_only(self):
        store, adapter, watch, clock = make()
        for _ in range(3):
            watch.check(OPERATOR)
            clock.now += 300
        big = [r for r in adapter.reads if r[1] == USAGE_FIRST_WINDOW]
        self.assertEqual(big, [(OPERATOR['thread_id'], USAGE_FIRST_WINDOW)])
        self.assertEqual(len([r for r in adapter.reads if r[1] == WINDOW]), 3)

    def test_no_larger_read_when_history_already_recorded(self):
        store, adapter, watch, clock = make()
        ident = OPERATOR['account_id'] + ':' + OPERATOR['thread_id']
        store.put('thread_context', dict(id=ident, observations=1, entities={}))
        watch.check(OPERATOR)
        self.assertFalse([r for r in adapter.reads if r[1] == USAGE_FIRST_WINDOW])

    def test_turns_count_user_messages_without_resume_text(self):
        store, adapter, watch, clock = make()
        watch.check(OPERATOR)
        self.assertEqual(store.get('workers', OPERATOR['id'])['context_usage']['turns'], 2)
        self.assertEqual(user_turns([dict(role='user', text='hi'), dict(type='user_message', text=RESUME_TEXT),
                                     dict(kind='assistant_message', text='x')]), 1)

    def test_check_sets_context_ask_and_logs_once(self):
        store, adapter, watch, clock = make(main_context_ask_turns=2)
        with mock.patch('notiontool.main_switch.check', wraps=__import__('notiontool.main_switch').main_switch.check) as spy:
            watch.check(OPERATOR)
            clock.now += 300
            watch.check(OPERATOR)
        self.assertEqual(spy.call_count, 2)
        ask = store.get('workers', OPERATOR['id'])['context_ask']
        self.assertTrue(ask['ask'])
        self.assertEqual(ask['reasons'], ['turns'])
        self.assertEqual(store.events.count('main_context_ask'), 1)

    def test_small_chat_gets_no_ask(self):
        store, adapter, watch, clock = make()
        watch.check(OPERATOR)
        self.assertFalse(store.get('workers', OPERATOR['id']).get('context_ask'))
        self.assertNotIn('main_context_ask', store.events)

    def test_usage_stored_only_when_changed(self):
        store, adapter, watch, clock = make()
        watch.check(OPERATOR)
        puts = []
        original = store.put
        store.put = lambda kind, doc: (puts.append(kind), original(kind, doc))
        clock.now += 300
        watch.check(OPERATOR)
        self.assertNotIn('workers', puts)

    def test_errors_never_raise_and_log_once_per_hour(self):
        store, adapter, watch, clock = make()
        with mock.patch('notiontool.chat_context.ChatContext.observe', side_effect=OSError('disk')):
            watch.check(OPERATOR)
            clock.now += 300
            watch.check(OPERATOR)
            self.assertEqual(store.events.count('operator_usage_failed'), 1)
            clock.now += 3600
            watch.check(OPERATOR)
        self.assertEqual(store.events.count('operator_usage_failed'), 2)
        self.assertNotIn('turn_watch_waiting', store.events)


if __name__ == '__main__':
    unittest.main()
