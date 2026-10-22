"""Owner rule: at the hard token limit every RUNNING coworker turn stops at once (engine.py).

No network and no Notion account: the adapter below is a local fake whose turns block until
their cancel Event is set, like a stream that the engine closes.
"""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from notiontool import token_budget
from notiontool.engine import (CANCELLED_BEFORE_SUBMISSION, LIMIT_CHECK_SECONDS, LIMIT_PAUSED_BY,
                               LIMIT_RESUME_EVENT, LIMIT_STOP_EVENT, Engine, PendingRemote)
from notiontool.store import Store

HARD = 9_000_000  # coworker tokens in 15 min: above the 8M burst hard limit
COMPRESSION_CANCEL = 'Assignment cancelled before context compression'


class Clock:
    def __init__(self):
        self.now = float(int(time.time()))  # whole seconds: clock steps stay exact

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class TurnAdapter:
    """Fake turns: a coworker turn blocks until its cancel Event is set, then ends like the real adapter."""

    def __init__(self):
        self.lock = threading.Lock()
        self.runs, self.recovers, self.stops = [], [], []
        self.main_gate = threading.Event()
        self.recover_done = False
        self.instant = set()  # task ids whose coworker turn finishes at once with a done reply

    def run(self, worker, task, prompt, cancel, update):
        with self.lock:
            self.runs.append(task['id'])
        if worker.get('purpose') != 'worker':
            while not self.main_gate.wait(0.01):
                if cancel.is_set():
                    raise PendingRemote()  # a cancelled Main turn would show up as a wrong state
            return json.dumps(dict(status='done', summary='Main answer'))
        if task['id'] in self.instant:
            return json.dumps(dict(status='done', summary='Finished before the limit check'))
        if not cancel.wait(30):
            raise TimeoutError('the coworker turn was never cancelled')
        if task.get('compress'):
            raise ValueError(COMPRESSION_CANCEL)  # cancelled while the context was being compressed
        if task.get('submission_phase') == 'inference':
            raise PendingRemote()  # stream closed after the send: the outcome is unknown
        raise ValueError(CANCELLED_BEFORE_SUBMISSION)  # stopped before anything was sent

    def recover(self, worker, task, cancel):
        with self.lock:
            self.recovers.append(task['id'])
        if self.recover_done:
            return json.dumps(dict(status='done', summary='Read back after the limit stop', evidence=[]))
        raise PendingRemote()

    def stop_turn(self, thread_id, worker=None):
        with self.lock:
            self.stops.append(thread_id)
        return dict(ok=False, reason='no_verified_endpoint')


class LimitStopTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent / '.test-data'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.store = Store(self.temp.name)
        self.adapter = TurnAdapter()
        self.store.put('accounts', dict(id='a', slot=1, email='test@example.test', state='ready', cooldown_until=0))
        for ident, purpose in (('main', 'main'), ('w1', 'worker'), ('w2', 'worker'), ('w3', 'worker')):
            self.store.put('workers', dict(id=ident, account_id='a', thread_id='th-' + ident, purpose=purpose,
                                           enabled=True, state='ready', lease='', label=ident, last_used=0))
        # 'held' is not an active stage: tick() completes, checks the limit and reconciles, but plans nothing.
        self.store.put('runs', dict(id='r1', chat_id='c', state='held', project_path=self.temp.name, capacity=3))
        self.futures = {}
        self.engine = Engine(self.store, self.adapter)
        self.clock = Clock()
        self.engine.clock = self.clock

    def tearDown(self):
        self.adapter.main_gate.set()
        thread = getattr(self.engine, '_limit_stop_thread', None)
        if thread is not None:
            thread.join(5)
        self.engine.close()  # sets every remaining cancel Event, so no fake turn keeps blocking
        time.sleep(.05)
        self.store.close()
        self.temp.cleanup()

    # -- helpers ---------------------------------------------------------------------------------
    def start(self, task_id, worker_id, sent, recovery=False, compress=False):
        task = dict(id=task_id, run_id='r1', role='executor', title=task_id, prompt='p', state='running',
                    worker_id=worker_id, lease='lease-' + task_id, attempts=1, started=time.time(), next_at=0,
                    deps=[], files=[], thread_id='th-' + worker_id, step_id='s-' + task_id,
                    submission_phase='inference' if sent else 'preparing')
        if sent:
            task['sent_at'] = time.time()
        if compress:
            task['compress'] = True
        self.store.put('tasks', task)
        worker = self.store.get('workers', worker_id)
        worker.update(state='recovering' if recovery else 'busy', lease=task['lease'])
        self.store.put('workers', worker)
        cancel = threading.Event()
        future = self.engine.pool.submit(self.adapter.run, dict(worker), dict(task), 'p', cancel, lambda text: None)
        self.futures[task_id] = future
        self.engine.inflight[future] = dict(task_id=task_id, lease=task['lease'], worker_id=worker_id,
                                            run_id='r1', cancel=cancel, recovery=recovery)
        return cancel

    def ledger(self, coworker_tokens):
        items = [[self.clock.now, coworker_tokens, 'w', 1]] if coworker_tokens else []
        self.store.put(token_budget.RATE_KIND, dict(id='rolling', items=items))

    def events(self, kind):
        return [e for e in self.store.events(limit=2000) if e['kind'] == kind]

    def task(self, ident):
        return self.store.get('tasks', ident)

    def pump(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.engine.tick()
            if predicate():
                return
            time.sleep(0.01)
        self.fail('condition not reached in %d s' % timeout)

    def hit_limit(self):
        """Below the limit at the first check; then the ledger goes over 8M/15 min and ONE tick follows,
        exactly one check interval later."""
        self.engine.tick()
        self.ledger(HARD)
        self.clock.advance(LIMIT_CHECK_SECONDS)
        self.engine.tick()

    def stop_both(self):
        cancels = self.start('t-pre', 'w1', sent=False), self.start('t-sent', 'w2', sent=True)
        self.hit_limit()
        return cancels

    def settle(self, *task_ids):
        self.pump(lambda: all(self.task(t)['state'] != 'running' for t in task_ids))

    # -- tests -----------------------------------------------------------------------------------
    def test_hard_limit_cancels_both_coworker_turns_in_one_tick_and_keeps_main(self):
        main_cancel = self.start('t-main', 'main', sent=True)
        pre_cancel, sent_cancel = self.stop_both()  # one tick, LIMIT_CHECK_SECONDS after the last check
        self.assertTrue(pre_cancel.is_set())
        self.assertTrue(sent_cancel.is_set())
        self.assertFalse(main_cancel.is_set(), 'the Main chat turn must not be touched')

        stops = self.events(LIMIT_STOP_EVENT)
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0]['count'], 2)
        self.assertEqual(stops[0]['source'], 'burst')
        self.assertIn('15 min', stops[0]['reason'])
        self.assertEqual(stops[0]['main_kept'], 1)
        self.assertEqual(sorted(stops[0]['task_ids']), ['t-pre', 't-sent'])

        self.settle('t-pre', 't-sent')
        pre, sent = self.task('t-pre'), self.task('t-sent')
        # Not sent yet: waits for the limit, no attempt used, the chat stays healthy (no presend failover).
        self.assertEqual(pre['state'], 'retry')
        self.assertEqual(pre['paused_by'], LIMIT_PAUSED_BY)
        self.assertEqual(pre['attempts'], 0)
        self.assertGreater(pre['next_at'], self.clock.now)
        self.assertFalse(pre.get('exclude_workers'))
        self.assertEqual(self.store.get('workers', 'w1')['state'], 'ready')
        # Already sent: uncertain, read back by reconcile, never resent (I5).
        self.assertEqual(sent['state'], 'uncertain')
        self.assertEqual(sent['paused_by'], LIMIT_PAUSED_BY)
        self.assertIn('never resent', sent['error'])
        self.assertEqual(self.store.get('workers', 'w2')['state'], 'recovering')
        for task in (pre, sent):
            self.assertNotIn(task['state'], ('failed', 'blocked'))
        self.assertEqual(self.events('presend_failover'), [])

        for _ in range(4):  # the limit stays hard: no second event, no resend
            self.clock.advance(LIMIT_CHECK_SECONDS)
            self.engine.tick()
        self.assertEqual(len(self.events(LIMIT_STOP_EVENT)), 1)
        self.assertEqual(sorted(self.adapter.runs), ['t-main', 't-pre', 't-sent'])
        self.assertEqual(self.task('t-main')['state'], 'running')
        self.assertFalse(main_cancel.is_set())

        # stop_turn is asked off the lock; 'no_verified_endpoint' is fine and logged.
        self.engine._limit_stop_thread.join(5)
        self.assertEqual(sorted(self.adapter.stops), ['th-w1', 'th-w2'])
        logged = self.events('limit_stop_turn')
        self.assertEqual(len(logged), 2)
        self.assertTrue(all(e['reason'] == 'no_verified_endpoint' and not e['ok'] for e in logged))

    def test_the_limit_is_checked_within_one_check_interval(self):
        self.assertLessEqual(LIMIT_CHECK_SECONDS, 1.0, 'the design caches the limit state for at most 1 s')
        pre_cancel = self.start('t-pre', 'w1', sent=False)
        sent_cancel = self.start('t-sent', 'w2', sent=True)
        self.engine.tick()  # check at t0: below the limit
        self.ledger(HARD)
        self.clock.advance(LIMIT_CHECK_SECONDS / 2)
        self.engine.tick()  # inside the interval: the cached state is used, nothing is stopped yet
        self.assertFalse(pre_cancel.is_set() or sent_cancel.is_set())
        self.assertEqual(self.events(LIMIT_STOP_EVENT), [])
        self.clock.advance(LIMIT_CHECK_SECONDS / 2)
        self.engine.tick()  # one tick after the interval: both coworkers stop
        self.assertTrue(pre_cancel.is_set() and sent_cancel.is_set())
        self.assertEqual(self.events(LIMIT_STOP_EVENT)[0]['count'], 2)

    def test_below_hard_limit_nothing_is_cancelled(self):
        cancels = [self.start('t-a', 'w1', sent=True), self.start('t-b', 'w2', sent=True),
                   self.start('t-main', 'main', sent=True)]
        self.ledger(6_000_000)  # throttle level (5M soft) but below the 8M hard limit
        for _ in range(4):
            self.clock.advance(LIMIT_CHECK_SECONDS)
            self.engine.tick()
        self.assertEqual(self.engine.limit_state()['level'], 'throttle')
        self.assertFalse(self.engine.limit_state()['hard'])
        self.assertFalse(any(c.is_set() for c in cancels))
        self.assertEqual(self.events(LIMIT_STOP_EVENT), [])
        self.assertIsNone(self.engine.limit_latch)
        self.assertEqual([self.task(t)['state'] for t in ('t-a', 't-b')], ['running', 'running'])

    def test_after_the_limit_clears_tasks_resume_and_the_sent_turn_is_read_back(self):
        self.stop_both()
        self.settle('t-pre', 't-sent')
        self.ledger(0)
        self.clock.advance(LIMIT_CHECK_SECONDS)
        self.engine.tick()  # first clear check: hysteresis keeps the latch
        self.assertIsNotNone(self.engine.limit_latch)
        self.assertEqual(self.events(LIMIT_RESUME_EVENT), [])
        self.clock.advance(6)
        self.engine.tick()  # second clear check, >= 5 s later: resume
        self.assertIsNone(self.engine.limit_latch)
        resumed = self.events(LIMIT_RESUME_EVENT)
        self.assertEqual(len(resumed), 1)
        self.assertEqual(resumed[0]['resumed'], 1)
        pre = self.task('t-pre')
        self.assertEqual(pre['state'], 'retry')
        self.assertNotIn('paused_by', pre)
        self.assertLessEqual(pre['next_at'], self.clock.now)

        self.adapter.recover_done = True
        sent = self.task('t-sent')
        sent['reconcile_at'] = 0
        self.store.put('tasks', sent)
        self.pump(lambda: self.task('t-sent')['state'] == 'done')
        sent = self.task('t-sent')
        self.assertTrue(sent['limit_reply_charged'])
        self.assertNotIn('paused_by', sent)
        self.assertIn('t-sent', self.adapter.recovers)
        self.assertEqual(self.adapter.runs.count('t-sent'), 1, 'a limit-stopped sent turn is never resent')
        record = token_budget._record(self.store, token_budget.window_start(time.time()))
        self.assertGreater(record['output'], 0)  # the reply missed by the cancelled stream is charged once

    def test_six_hour_budget_used_up_is_a_hard_limit(self):
        now = self.clock.now
        start = token_budget.window_start(now)
        self.store.put(token_budget.KIND, dict(id=str(int(start)), start=start, end=start + token_budget.WINDOW_SECONDS,
                                               used=10 ** 12, input=10 ** 12, output=0, turns=1, by_worker={}))
        state = self.engine.limit_state(now)
        self.assertTrue(state['hard'])
        self.assertEqual(state['source'], '6h')
        cancel = self.start('t-a', 'w1', sent=True)
        main_cancel = self.start('t-main', 'main', sent=True)
        self.engine.tick()
        self.assertTrue(cancel.is_set())
        self.assertFalse(main_cancel.is_set())
        self.assertEqual(self.events(LIMIT_STOP_EVENT)[0]['source'], '6h')

    # -- design tests requested by the review -----------------------------------------------------
    def test_no_coworker_task_is_handed_out_while_stopped_but_a_main_task_still_runs(self):
        self.hit_limit()
        self.assertIsNotNone(self.engine.limit_latch)
        self.store.put('runs', dict(id='r2', chat_id='c2', state='executing', project_path=self.temp.name,
                                    capacity=3))
        first_start = self.clock.now - 1
        for ident in ('q-co', 'q-main'):
            self.store.put('tasks', dict(id=ident, run_id='r2', role='executor', title=ident, prompt='p',
                                         state='queued', worker_id='', lease='', attempts=0, started=0,
                                         next_at=first_start, deps=[], files=[]))
        target = {'q-co': 'w1', 'q-main': 'main'}
        dispatched = []

        def dispatch(task, run, worker):
            dispatched.append((task['id'], worker['id']))
            task.update(state='running', worker_id=worker['id'])
            self.store.put('tasks', task)

        with mock.patch.object(self.engine, '_advance'), \
                mock.patch.object(self.engine, '_choose_worker',
                                  side_effect=lambda task, run, settings: dict(self.store.get('workers', target[task['id']]))), \
                mock.patch.object(self.engine, '_dispatch', side_effect=dispatch):
            for _ in range(2):  # still stopped on both ticks
                self.clock.advance(LIMIT_CHECK_SECONDS)
                self.engine.tick()
            self.assertEqual(dispatched, [('q-main', 'main')], 'the Main task still runs while stopped')
            held = self.task('q-co')
            self.assertEqual(held['state'], 'queued')
            self.assertEqual(held['next_at'], first_start, 'the gate only skips; the next start time is unchanged')
            self.assertNotIn('paused_by', held)

            self.ledger(0)  # the limit clears: two clear checks >= 5 s apart, then the coworker task goes out
            self.clock.advance(LIMIT_CHECK_SECONDS)
            self.engine.tick()
            self.clock.advance(6)
            self.engine.tick()
        self.assertIsNone(self.engine.limit_latch)
        self.assertEqual(dispatched, [('q-main', 'main'), ('q-co', 'w1')])

    def test_a_recovery_read_is_not_stopped(self):
        read_cancel = self.start('t-read', 'w1', sent=True, recovery=True)
        turn_cancel = self.start('t-turn', 'w2', sent=True)
        self.hit_limit()
        self.assertFalse(read_cancel.is_set(), 'a recovery read only reads; it is never stopped')
        self.assertTrue(turn_cancel.is_set())
        stops = self.events(LIMIT_STOP_EVENT)
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0]['count'], 1)
        self.assertEqual(stops[0]['task_ids'], ['t-turn'])
        self.assertEqual(self.task('t-read')['state'], 'running')

    def test_a_coworker_turn_that_starts_after_the_stop_is_stopped_by_the_next_check(self):
        self.stop_both()
        self.engine._limit_stop_thread.join(5)
        late = self.start('t-late', 'w3', sent=True)  # e.g. a turn whose dispatch raced the stop
        self.engine.tick()  # same check interval: the cached state is used
        self.assertFalse(late.is_set())
        self.clock.advance(LIMIT_CHECK_SECONDS)
        self.engine.tick()  # next check
        self.assertTrue(late.is_set())
        more = self.events('limit_stop_more')
        self.assertEqual(len(more), 1)
        self.assertEqual(more[0]['count'], 1)
        self.assertEqual(more[0]['task_ids'], ['t-late'])
        self.assertEqual(len(self.events(LIMIT_STOP_EVENT)), 1)
        self.settle('t-late')
        self.assertEqual(self.task('t-late')['state'], 'uncertain')
        self.assertEqual(self.adapter.runs.count('t-late'), 1)

    def test_a_run_cancelled_while_stopped_ends_cancelled_not_retry(self):
        self.start('t-x', 'w1', sent=False)
        self.start('t-y', 'w2', sent=False, compress=True)
        self.hit_limit()
        run = self.store.get('runs', 'r1')
        run['state'] = 'cancelled'  # the user cancels the run while the limit holds its turns
        self.store.put('runs', run)
        self.settle('t-x', 't-y')
        for ident, worker in (('t-x', 'w1'), ('t-y', 'w2')):
            task = self.task(ident)
            self.assertEqual(task['state'], 'cancelled', ident)
            self.assertNotIn('paused_by', task)
            self.assertEqual(self.store.get('workers', worker)['state'], 'ready')
        self.assertEqual(self.events('presend_failover'), [])

    def test_a_turn_that_finished_before_the_check_ends_done_and_is_not_counted(self):
        self.adapter.instant.add('t-quick')
        self.ledger(HARD)
        self.start('t-quick', 'w1', sent=True)
        slow_cancel = self.start('t-slow', 'w2', sent=True)
        self.futures['t-quick'].result(5)  # finished, but not yet completed by the engine
        self.engine.tick()  # completes t-quick first, then checks the limit
        quick = self.task('t-quick')
        self.assertEqual(quick['state'], 'done')
        self.assertNotIn('paused_by', quick)
        self.assertTrue(slow_cancel.is_set())
        stops = self.events(LIMIT_STOP_EVENT)
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0]['count'], 1)
        self.assertEqual(stops[0]['task_ids'], ['t-slow'])

    def test_a_cancel_during_context_compression_uses_no_attempt(self):
        self.start('t-c', 'w1', sent=False, compress=True)
        self.hit_limit()
        self.settle('t-c')
        task = self.task('t-c')
        self.assertEqual(task['state'], 'retry')
        self.assertEqual(task['attempts'], 0)
        self.assertEqual(task['paused_by'], LIMIT_PAUSED_BY)
        self.assertFalse(task.get('exclude_workers'))
        self.assertFalse(task.get('presend_retries'))
        self.assertEqual(self.store.get('workers', 'w1')['state'], 'ready')
        self.assertEqual(self.events('presend_failover'), [])

    def test_restart_clears_the_token_limit_pause_mark_on_retry_tasks(self):
        self.stop_both()
        self.settle('t-pre', 't-sent')
        self.assertEqual(self.task('t-pre')['paused_by'], LIMIT_PAUSED_BY)
        self.assertTrue(self.task('t-pre')['error'].startswith('Stopped by token limit'))
        self.engine._limit_stop_thread.join(5)
        self.engine.close()

        self.engine = Engine(self.store, self.adapter)  # app restart: the latch lived in memory only
        self.engine.clock = self.clock
        pre = self.task('t-pre')
        self.assertEqual(pre['state'], 'retry')
        self.assertNotIn('paused_by', pre)
        self.assertEqual(pre['error'], '')
        self.assertEqual(pre['attempts'], 0)
        sent = self.task('t-sent')  # a sent turn keeps its mark: it is read back (and its reply charged once)
        self.assertEqual(sent['state'], 'uncertain')
        self.assertEqual(sent['paused_by'], LIMIT_PAUSED_BY)

        self.engine.tick()  # the limit is still hit: the first check rebuilds the latch from the ledger
        self.assertIsNotNone(self.engine.limit_latch)
        self.assertEqual(self.task('t-pre')['state'], 'retry')
        self.assertEqual(self.adapter.runs.count('t-pre'), 1, 'nothing is resent after the restart')
        self.assertEqual(self.adapter.runs.count('t-sent'), 1)


if __name__ == '__main__':
    unittest.main()
