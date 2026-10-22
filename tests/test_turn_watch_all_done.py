"""turn_watch: one 'all coworkers are finished' message to the operator Main per finished batch."""
import copy
import threading
import unittest

from notiontool.turn_watch import ALL_DONE_TEXT, DONE_QUIET_SECONDS, TurnWatch, turn_idle, user_turns


def entity(seq, kind, ident=None, **extra):
    return {'op': 'put', 'entity': dict(sequence=seq, kind=kind, id=ident or 'e%d' % seq, **extra)}


def transcript(*patches, status='idle', pending=False):
    return {'session': {'status': status, 'sequence': 99}, 'session_pending': pending, 'patches': list(patches)}


IDLE_TURN = transcript(entity(1, 'user_message', 'u1', content=[['delegate it']]),
                       entity(2, 'assistant_message', 'a1', content=[['Delegated, waiting.']]))


class FakeStore:
    def __init__(self, **settings):
        self.lock = threading.RLock()
        self.data, self.events = {}, []
        self.config = dict(harness_account_id='acc', idle_mode=False, **settings)

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
        self.transcripts, self.count, self.sent, self.result = {}, 0, [], True

    def client(self, account_id):
        return FakeClient(self)

    def new_id(self, space_id, tail='00aa'):
        self.count += 1
        return 'step-%d' % self.count

    def continue_turn(self, worker, text, step_id):
        if isinstance(self.result, Exception):
            raise self.result
        if self.result:
            self.sent.append(text)
        return self.result


class FakeEngine:
    def __init__(self):
        self.lock, self.stop_event = threading.RLock(), threading.Event()


OPERATOR = dict(id='acc:op', account_id='acc', thread_id='op-thread', purpose='operator', state='idle')


class AllDoneTests(unittest.TestCase):
    def setUp(self):
        self.now = 10_000.0
        self.store = FakeStore()
        self.store.put('accounts', dict(id='acc', state='ready'))
        self.store.put('workers', OPERATOR)
        self.store.put('chats', dict(id='c1', operator=True, main_worker=OPERATOR['id'], state='active'))
        self.store.put('chats', dict(id='c2', operator=False, main_worker='acc:other', state='active'))
        self.adapter = FakeAdapter()
        self.adapter.transcripts[OPERATOR['thread_id']] = IDLE_TURN
        self.watch = self.new_watch()
        self.delegate('r1', 'c1', t1='done', t2='done')

    def new_watch(self):
        watch = TurnWatch(self.store, self.adapter, FakeEngine(), clock=lambda: self.now)
        watch.spawn = lambda fn, *args: fn(*args)  # send synchronously
        return watch

    def delegate(self, run_id, chat_id, finished=None, **tasks):
        self.store.put('runs', dict(id=run_id, chat_id=chat_id, operator=True, state='executing'))
        for ident, state in tasks.items():
            self.store.put('tasks', dict(id=ident, run_id=run_id, role='executor', state=state, worker_id='acc:cw',
                                         finished=self.now - 60 if finished is None else finished))

    def set_task(self, ident, **changes):
        task = self.store.get('tasks', ident)
        task.update(changes)
        self.store.put('tasks', task)

    def test_fires_once_when_everything_is_done(self):
        self.assertIsNotNone(self.watch.check(OPERATOR))
        self.assertEqual(self.adapter.sent, [ALL_DONE_TEXT])
        self.assertEqual(self.store.get('workers', OPERATOR['id'])['all_done']['ids'], ['t1', 't2'])
        self.assertIn('coworkers_done_sent', self.store.events)

    def test_not_while_a_task_is_active(self):
        for state in ('queued', 'retry', 'running', 'verifying', 'uncertain', 'preparing', 'cancelling', 'checkpoint'):
            self.set_task('t2', state=state)
            self.assertIsNone(self.watch.check(OPERATOR), state)
        self.set_task('t2', state='done', remote_unknown=True)
        self.assertIsNone(self.watch.check(OPERATOR))
        self.assertEqual(self.adapter.sent, [])

    def test_waits_for_quiet_after_the_last_task(self):
        self.set_task('t2', finished=self.now - DONE_QUIET_SECONDS + 5)
        self.assertIsNone(self.watch.check(OPERATOR))
        self.now += 6
        self.assertIsNotNone(self.watch.check(OPERATOR))
        self.assertEqual(self.adapter.sent, [ALL_DONE_TEXT])

    def test_quiet_without_finish_stamp_counts_from_first_sight(self):
        self.set_task('t1', finished=0)
        self.set_task('t2', finished=0)
        self.assertIsNone(self.watch.check(OPERATOR))
        self.now += DONE_QUIET_SECONDS
        self.assertIsNotNone(self.watch.check(OPERATOR))

    def test_not_while_the_main_is_busy(self):
        thread = OPERATOR['thread_id']
        busy = [transcript(entity(1, 'user_message', 'u1'), status='running'),
                transcript(entity(1, 'user_message', 'u1'), pending=True),
                transcript(entity(1, 'user_message', 'u1'), status=None),
                transcript(entity(1, 'user_message', 'u1'), entity(2, 'tool_call', 'c1', tool_use_id='x')),
                transcript(entity(1, 'user_message', 'u1'), entity(2, 'tool_confirmation', 'c1')),
                transcript(entity(1, 'user_message', 'u1'), entity(2, 'tool_step', 'c1', state='awaiting_approval'))]
        for value in busy:
            self.assertFalse(turn_idle(value), value)
            self.adapter.transcripts[thread] = value
            self.assertIsNone(self.watch.check_all_done(OPERATOR, self.adapter.client('acc'), value), value)
        self.assertEqual(self.adapter.sent, [])
        self.adapter.transcripts[thread] = IDLE_TURN
        self.assertIsNotNone(self.watch.check(OPERATOR))

    def test_not_twice_also_after_a_restart(self):
        self.watch.check(OPERATOR)
        self.now += 600
        self.assertIsNone(self.watch.check_all_done(OPERATOR, self.adapter.client('acc'), IDLE_TURN))
        self.assertIsNone(self.new_watch().check_all_done(OPERATOR, self.adapter.client('acc'), IDLE_TURN))
        self.assertEqual(self.adapter.sent, [ALL_DONE_TEXT])

    def test_again_after_a_new_delegation(self):
        self.watch.check(OPERATOR)
        self.delegate('r2', 'c1', t3='running')
        self.assertIsNone(self.watch.check(OPERATOR))
        self.set_task('t3', state='done', finished=self.now)
        self.now += DONE_QUIET_SECONDS + 1
        self.assertIsNotNone(self.watch.check(OPERATOR))
        self.assertEqual(self.adapter.sent, [ALL_DONE_TEXT, ALL_DONE_TEXT])

    def test_other_chats_do_not_count(self):
        self.delegate('r9', 'c2', t9='running')
        self.assertIsNotNone(self.watch.check(OPERATOR))

    def test_off_when_main_auto_continue_is_off(self):
        self.store.config.update(main_auto_continue=False, auto_continue_on_done=True)
        self.assertIsNone(self.watch.check_all_done(OPERATOR, self.adapter.client('acc'), IDLE_TURN))
        self.watch.check(OPERATOR)
        self.assertEqual(self.adapter.sent, [])
        self.assertEqual([e for e in self.store.events if e.startswith('coworkers_done')], [])

    def test_off_when_the_setting_is_off(self):
        self.store.config['auto_continue_on_done'] = False
        self.assertIsNone(self.watch.check_all_done(OPERATOR, self.adapter.client('acc'), IDLE_TURN))
        self.watch.check(OPERATOR)
        self.assertEqual(self.adapter.sent, [])

    def test_no_delegation_no_message(self):
        self.store.data['tasks'] = {}
        self.assertIsNone(self.watch.check_all_done(OPERATOR, self.adapter.client('acc'), IDLE_TURN))
        self.assertEqual(self.adapter.sent, [])

    def test_coworker_chats_never_get_it(self):
        worker = dict(OPERATOR, id='acc:cw', purpose='worker')
        self.assertIsNone(self.watch.check_all_done(worker, self.adapter.client('acc'), IDLE_TURN))

    def test_busy_chat_allows_a_later_attempt(self):
        self.adapter.result = False
        self.watch.check(OPERATOR)
        self.assertIn('coworkers_done_skipped', self.store.events)
        self.adapter.result = True
        self.assertIsNotNone(self.watch.check(OPERATOR))
        self.assertEqual(self.adapter.sent, [ALL_DONE_TEXT])

    def test_unconfirmed_send_is_never_repeated(self):
        self.adapter.result = RuntimeError('stream broke')
        self.watch.check(OPERATOR)
        self.assertIn('coworkers_done_unconfirmed', self.store.events)
        self.adapter.result = True
        self.now += 600
        self.assertIsNone(self.watch.check_all_done(OPERATOR, self.adapter.client('acc'), IDLE_TURN))
        self.assertEqual(self.adapter.sent, [])

    def test_our_message_is_no_user_turn(self):
        entities = [dict(kind='user_message', content=[[ALL_DONE_TEXT]]), dict(kind='user_message', content=[['hi']])]
        self.assertEqual(user_turns(entities), 1)


if __name__ == '__main__':
    unittest.main()
