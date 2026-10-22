import copy
import threading
import unittest

from notiontool import turn_watch
from notiontool.turn_watch import GRACE_SECONDS, MAX_CHAIN, TurnWatch, interrupted_turn


def entity(seq, kind, ident=None, **extra):
    return {'op': 'put', 'entity': dict(sequence=seq, kind=kind, id=ident or 'e%d' % seq, **extra)}


def transcript(*patches, status='idle', pending=False):
    return {'session': {'status': status, 'sequence': 99}, 'session_pending': pending, 'patches': list(patches)}


class FakeStore:
    def __init__(self):
        self.data, self.events, self.lock = {}, [], threading.RLock()

    def settings(self):
        return {'harness_account_id': 'acc', 'task_timeout': 600}

    def get(self, kind, ident, default=None):
        value = self.data.get(kind, {}).get(ident)
        return copy.deepcopy(value) if value is not None else default

    def all(self, kind):
        return [copy.deepcopy(v) for v in self.data.get(kind, {}).values()]

    def put(self, kind, doc):
        self.data.setdefault(kind, {})[doc['id']] = copy.deepcopy(doc)

    def event(self, kind, run_id='', **fields):
        self.events.append((kind, fields))


class FakeClient:
    def __init__(self, owner):
        self.owner, self.info = owner, {'space_id': 'space'}

    def transcript(self, thread, limit=100):
        self.owner.reads.append((thread, limit))
        return self.owner.transcripts[thread]


class FakeAdapter:
    def __init__(self):
        self.transcripts, self.sent, self.reads, self.fail, self.count = {}, [], [], False, 0

    def client(self, account_id):
        return FakeClient(self)

    def new_id(self, space_id, tail='00aa'):
        self.count += 1
        return 'step-%d' % self.count

    def continue_turn(self, worker, text, step_id):
        if self.fail:
            raise OSError('Edge request interrupted')
        self.sent.append((worker['thread_id'], step_id, text))
        return True


class FakeEngine:
    def __init__(self):
        self.lock, self.stop_event = threading.RLock(), threading.Event()


class InterruptedTurnTests(unittest.TestCase):
    def test_completed_turn_is_not_interrupted(self):
        t = transcript(entity(1, 'user_message'), entity(2, 'tool_result'), entity(3, 'assistant_message'),
                       entity(4, 'turn_completed'))
        self.assertIsNone(interrupted_turn(t))

    def test_turn_ending_after_a_tool_result_is_interrupted(self):
        t = transcript(entity(1, 'user_message', 'u1'), entity(2, 'assistant_message'), entity(3, 'tool_call'),
                       entity(4, 'tool_result', 'last'), entity(5, 'turn_completed'))
        found = interrupted_turn(t)
        self.assertEqual((found['key'], found['user_ids']), ('last', ['u1']))

    def test_open_question_to_the_user_is_not_interrupted(self):
        t = transcript(entity(1, 'user_message'), entity(2, 'tool_call', name='ask-survey'))
        self.assertIsNone(interrupted_turn(t))

    def test_running_or_pending_session_is_never_interrupted(self):
        self.assertIsNone(interrupted_turn(transcript(entity(1, 'user_message'), entity(2, 'tool_call'), status='running')))
        self.assertIsNone(interrupted_turn(transcript(entity(1, 'user_message'), entity(2, 'tool_call'), pending=True)))


class TurnWatchTests(unittest.TestCase):
    def setUp(self):
        self.store, self.adapter, self.engine, self.now = FakeStore(), FakeAdapter(), FakeEngine(), 1000.0
        self.store.put('accounts', {'id': 'acc', 'state': 'ready', 'team_threads': {'main': {'id': 'main-thread'}}})
        for ident, thread, extra in (('w-main', 'main-thread', {'purpose': 'main'}), ('w-co', 'co-thread', {}),
                                     ('w-user', 'user-thread', {'state': 'disabled'}),
                                     ('w-pending', 'pending-thread', {'join_state': 'pending'})):
            self.store.put('workers', dict({'id': ident, 'account_id': 'acc', 'thread_id': thread, 'state': 'ready'}, **extra))
        self.store.put('chats', {'id': 'chat', 'main_worker': 'w-main'})
        self.watch = self.make()

    def make(self):
        watch = TurnWatch(self.store, self.adapter, self.engine, clock=lambda: self.now)
        watch.spawn = lambda fn, *args: fn(*args)
        return watch

    def interrupt(self, thread='main-thread', user='u1'):
        self.adapter.transcripts[thread] = transcript(
            entity(1, 'user_message', user), entity(2, 'assistant_message'), entity(3, 'tool_call'),
            entity(4, 'tool_result', 'last-' + user, created_at=self.now))

    def worker(self, ident='w-main'):
        return self.store.get('workers', ident)

    def kinds(self):
        return [kind for kind, _ in self.store.events]

    def test_resume_waits_for_the_grace_period_then_sends_once(self):
        self.interrupt()
        self.assertIsNone(self.watch.check(self.worker()))
        self.now += GRACE_SECONDS + 1
        step = self.watch.check(self.worker())
        self.assertEqual(self.adapter.sent, [('main-thread', step, turn_watch.RESUME_TEXT)])
        self.now += 120
        self.assertIsNone(self.watch.check(self.worker()))
        self.assertIsNone(self.make().check(self.worker()))  # also after a restart
        self.assertEqual(len(self.adapter.sent), 1)
        self.assertIn('turn_interrupted', self.kinds())

    def test_reply_in_time_means_no_resume(self):
        self.adapter.transcripts['main-thread'] = transcript(entity(1, 'user_message'), entity(2, 'tool_result'),
                                                             entity(3, 'assistant_message', created_at=self.now))
        self.now += 600
        self.assertIsNone(self.watch.check(self.worker()))
        self.assertEqual(self.adapter.sent, [])

    def test_chain_of_automatic_resumes_is_capped(self):
        user = 'u0'
        from notiontool.turn_watch import MAX_CHAIN_OPERATOR
        # Idle mode: a Main chat keeps going for the long operator chain (coworkers keep MAX_CHAIN).
        for _ in range(MAX_CHAIN_OPERATOR + 2):
            self.interrupt(user=user)
            self.now += GRACE_SECONDS + 1
            step = self.watch.check(self.worker())
            if step:
                user = step  # the next interruption follows our own continue message
        self.assertEqual(len(self.adapter.sent), MAX_CHAIN_OPERATOR)
        self.assertGreater(MAX_CHAIN_OPERATOR, MAX_CHAIN)
        self.assertEqual(self.kinds().count('turn_resume_exhausted'), 1)

    def test_a_real_user_message_starts_a_new_chain(self):
        for user in ('u1', 'u2', 'u3', 'u4', 'u5', 'u6', 'u7'):
            self.interrupt(user=user)
            self.now += GRACE_SECONDS + 1
            self.watch.check(self.worker())
        self.assertEqual(len(self.adapter.sent), 7)

    def test_failed_send_is_retried_later(self):
        self.interrupt()
        self.now += GRACE_SECONDS + 1
        self.adapter.fail = True
        self.watch.check(self.worker())
        self.assertIn('turn_resume_failed', self.kinds())
        self.adapter.fail = False
        self.watch.check(self.worker())
        self.assertEqual(len(self.adapter.sent), 1)

    def test_only_main_and_busy_coworkers_are_watched(self):
        self.assertEqual({w['id'] for w in self.watch.workers()}, {'w-main'})
        for ident, worker in (('t1', 'w-co'), ('t2', 'w-user'), ('t3', 'w-pending')):
            self.store.put('tasks', {'id': ident, 'worker_id': worker, 'state': 'running'})
        self.assertEqual({w['id'] for w in self.watch.workers()}, {'w-main', 'w-co'})

    def test_coworker_resume_is_attached_to_its_task(self):
        self.store.put('tasks', {'id': 't1', 'worker_id': 'w-co', 'state': 'running'})
        self.interrupt('co-thread')
        self.now += GRACE_SECONDS + 1
        step = self.watch.check(self.worker('w-co'))
        self.assertEqual(self.store.get('tasks', 't1')['resume_steps'], [step])

    def test_checks_read_only_a_small_window(self):
        self.interrupt()
        self.watch.check(self.worker())
        # The Main is measured for the 6 h budget too: at most one larger usage read, then only the small window.
        self.assertEqual(self.adapter.reads[0], ('main-thread', turn_watch.WINDOW))
        self.assertTrue(all(limit in (turn_watch.WINDOW, turn_watch.USAGE_FIRST_WINDOW) for _t, limit in self.adapter.reads))
        before = len(self.adapter.reads)
        self.watch.check(self.worker())  # within the usage throttle
        self.assertEqual(self.adapter.reads[before:], [('main-thread', turn_watch.WINDOW)])



class RealHangTests(unittest.TestCase):
    setUp = TurnWatchTests.setUp
    make = TurnWatchTests.make
    worker = TurnWatchTests.worker

    def test_tool_call_without_result_counts_as_running(self):
        t = transcript(entity(1, 'user_message', 'u1'), entity(2, 'tool_call', 'call', created_at=1.0))
        self.assertTrue(interrupted_turn(t)['pending'])
        t = transcript(entity(1, 'user_message', 'u1'), entity(2, 'tool_call', 'c'), entity(3, 'tool_result', 'r'))
        self.assertFalse(interrupted_turn(t)['pending'])

    def test_quiet_time_counts_from_the_newest_entry(self):
        t = transcript(entity(1, 'user_message', 'u1'), entity(2, 'tool_result', 'r', created_at=100.0),
                       entity(3, 'turn_meta', created_at=500.0))
        self.assertEqual(interrupted_turn(t)['stamp'], 500.0)

    def test_running_tool_is_not_resumed_after_the_short_grace(self):
        self.adapter.transcripts['main-thread'] = transcript(
            entity(1, 'user_message', 'u1'), entity(2, 'tool_call', 'call', created_at=self.now))
        self.now += turn_watch.GRACE_SECONDS + 1
        self.assertIsNone(self.watch.check(self.worker()))
        self.assertEqual(self.adapter.sent, [])
        self.now += turn_watch.PENDING_TOOL_SECONDS
        self.assertIsNotNone(self.watch.check(self.worker()))
        self.assertEqual(len(self.adapter.sent), 1)


if __name__ == '__main__':
    unittest.main()
