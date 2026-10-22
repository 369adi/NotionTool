"""Auto-continue of the operator Main chat (turn_watch): own setting, chain 20, stop words, paused turns."""
import copy
import threading
import unittest
from unittest import mock

from notiontool import turn_watch
from notiontool.turn_watch import (GRACE_SECONDS, MAX_CHAIN, MAX_CHAIN_OPERATOR, RESUME_TEXT, TurnWatch,
                                   interrupted_turn, stop_requested)


def entity(seq, kind, ident=None, **extra):
    return {'op': 'put', 'entity': dict(sequence=seq, kind=kind, id=ident or 'e%d' % seq, **extra)}


def transcript(*patches, status='idle'):
    return {'session': {'status': status, 'sequence': 99}, 'session_pending': False, 'patches': list(patches)}


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
        return self.owner.transcripts[thread]


class FakeAdapter:
    def __init__(self):
        self.transcripts, self.count = {}, 0

    def client(self, account_id):
        return FakeClient(self)

    def new_id(self, space_id, tail='00aa'):
        self.count += 1
        return 'step-%d' % self.count

    def continue_turn(self, worker, text, step_id):
        return True


class FakeEngine:
    def __init__(self):
        self.lock, self.stop_event = threading.RLock(), threading.Event()


OPERATOR = dict(id='acc:op', account_id='acc', thread_id='op-thread', purpose='operator', state='idle')
COWORKER = dict(id='acc:cw', account_id='acc', thread_id='cw-thread', purpose='worker', state='idle')


def make(**settings):
    store = FakeStore(**settings)
    store.put('accounts', dict(id='acc', state='ready'))
    store.put('workers', OPERATOR)
    store.put('workers', COWORKER)
    store.put('chats', dict(id='c1', operator=True, main_worker=OPERATOR['id'], state='active'))
    adapter = FakeAdapter()
    watch = TurnWatch(store, adapter, FakeEngine(), clock=lambda: 10_000.0)
    watch.spawn = lambda fn, *args: None
    return store, adapter, watch


def tool_turn(user_text='please build it'):
    return transcript(entity(1, 'user_message', 'u1', content=[[user_text]]), entity(2, 'tool_call', created_at=1.0),
                      entity(3, 'tool_result', 'last', created_at=1.0))


class OperatorWorkerTests(unittest.TestCase):
    def ids(self, watch):
        return {w['id'] for w in watch.workers()}

    def test_operator_main_is_watched_by_default(self):
        store, adapter, watch = make()
        self.assertEqual(self.ids(watch), {OPERATOR['id']})

    def test_setting_off_excludes_operator(self):
        store, adapter, watch = make(main_auto_continue=False)
        self.assertEqual(self.ids(watch), set())

    def test_released_operator_chat_is_excluded(self):
        store, adapter, watch = make()
        store.put('chats', dict(id='c1', operator=True, main_worker=OPERATOR['id'], state='released'))
        self.assertEqual(self.ids(watch), set())

    def test_same_exclusions_apply(self):
        for change in (dict(state='retired'), dict(join_state='pending'), dict(thread_id='')):
            store, adapter, watch = make()
            store.put('workers', dict(OPERATOR, **change))
            self.assertEqual(self.ids(watch), set(), change)

    def test_coworker_still_needs_an_active_task(self):
        store, adapter, watch = make()
        store.put('tasks', dict(id='t1', worker_id=COWORKER['id'], state='running'))
        self.assertEqual(self.ids(watch), {OPERATOR['id'], COWORKER['id']})


class LoopGateTests(unittest.TestCase):
    def run_loop(self, **settings):
        store, adapter, watch = make(**settings)
        calls = []
        watch.workers = lambda: calls.append(1) or []
        waits = iter([False, True])
        watch.engine.stop_event = mock.Mock(wait=lambda seconds: next(waits), is_set=lambda: False)
        with mock.patch('notiontool.network_budget.set_thread_priority'):
            watch.loop()
        return calls

    def test_runs_without_background_remote(self):
        self.assertEqual(self.run_loop(background_remote=False), [1])

    def test_turn_watch_off_stops_checks(self):
        self.assertEqual(self.run_loop(background_remote=True, turn_watch=False), [])


class ChainAndStopTests(unittest.TestCase):
    def prime(self, worker, chain):
        store, adapter, watch = make()
        store.put('workers', dict(worker, resume=dict(key='old', chain=chain, steps=['u1'], at=9_999.0)))
        adapter.transcripts[worker['thread_id']] = tool_turn()
        return store, watch

    def test_operator_resumes_up_to_twenty(self):
        self.assertEqual(MAX_CHAIN_OPERATOR, 20)
        store, watch = self.prime(OPERATOR, MAX_CHAIN_OPERATOR - 1)
        self.assertIsNotNone(watch.check(OPERATOR))
        self.assertEqual(store.get('workers', OPERATOR['id'])['resume']['chain'], 20)
        store, watch = self.prime(OPERATOR, MAX_CHAIN_OPERATOR)
        self.assertIsNone(watch.check(OPERATOR))
        self.assertIn('turn_resume_exhausted', store.events)

    def test_coworker_keeps_max_chain(self):
        store, watch = self.prime(COWORKER, MAX_CHAIN)
        self.assertIsNone(watch.check(COWORKER))
        self.assertIn('turn_resume_exhausted', store.events)

    def test_stop_word_blocks_operator(self):
        for text in ('Please STOP now', 'halt', 'bitte warte kurz', 'Aufhören!', 'pause here'):
            store, adapter, watch = make()
            adapter.transcripts[OPERATOR['thread_id']] = tool_turn(text)
            self.assertIsNone(watch.check(OPERATOR), text)
            self.assertIn('turn_resume_stopped', store.events)

    def test_stop_word_is_a_whole_word(self):
        self.assertFalse(stop_requested('fix the stopwatch and the bus stops'))
        self.assertTrue(stop_requested('Stop.'))

    def test_our_resume_text_is_not_the_user_text(self):
        t = transcript(entity(1, 'user_message', 'u1', content=[['stop after this file']]),
                       entity(2, 'tool_result'), entity(3, 'user_message', 'u2', content=[[RESUME_TEXT]]),
                       entity(4, 'tool_result', 'last'))
        self.assertEqual(interrupted_turn(t)['user_text'], 'stop after this file')

    def test_grace_period_kept(self):
        store, adapter, watch = make()
        watch.clock = lambda: 1.0 + GRACE_SECONDS - 1
        adapter.transcripts[OPERATOR['thread_id']] = tool_turn()
        self.assertIsNone(watch.check(OPERATOR))


class PausedTurnTests(unittest.TestCase):
    def test_turn_paused_after_a_tool_step_is_interrupted(self):
        # Notion's long-turn pause leaves an idle session whose newest entry is a tool step.
        t = transcript(entity(1, 'user_message', 'u1'), entity(2, 'assistant_message'), entity(3, 'tool_call', 'last'),
                       entity(4, 'turn_completed'))
        self.assertEqual(interrupted_turn(t)['key'], 'last')

    def test_limit_marker_waits_for_the_users_continue_click(self):
        # User rule: Notion's step-limit 'Fortfahren' is pressed by the user, never resumed automatically.
        t = transcript(entity(1, 'user_message', 'u1'), entity(2, 'assistant_message'),
                       entity(3, 'turn_completed', 'done', stop_reason='step_limit'))
        self.assertIsNone(interrupted_turn(t))

    def test_running_session_is_not_interrupted(self):
        t = transcript(entity(1, 'user_message'), entity(2, 'tool_call'), status='running')
        self.assertIsNone(interrupted_turn(t))


if __name__ == '__main__':
    unittest.main()
