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

from notiontool import token_budget
from notiontool.engine import (CANCELLED_BEFORE_SUBMISSION, LIMIT_CHECK_SECONDS, LIMIT_PAUSED_BY,
                               LIMIT_RESUME_EVENT, LIMIT_STOP_EVENT, Engine, PendingRemote)
from notiontool.store import Store


class Clock:
    def __init__(self):
        self.now = time.time()

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

    def run(self, worker, task, prompt, cancel, update):
        with self.lock:
            self.runs.append(task['id'])
        if worker.get('purpose') != 'worker':
            while not self.main_gate.wait(0.01):
                if cancel.is_set():
                    raise PendingRemote()  # a cancelled Main turn would show up as a wrong state
            return json.dumps(dict(status='done', summary='Main answer'))
        if not cancel.wait(30):
            raise TimeoutError('the coworker turn was never cancelled')
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
        for ident, purpose in (('main', 'main'), ('w1', 'worker'), ('w2', 'worker')):
            self.store.put('workers', dict(id=ident, account_id='a', thread_id='th-' + ident, purpose=purpose,
                                           enabled=True, state='ready', lease='', label=ident, last_used=0))
        # 'held' is not an active stage: tick() completes, checks the limit and reconciles, but plans nothing.
        self.store.put('runs', dict(id='r1', chat_id='c', state='held', project_path=self.temp.name, capacity=3))
        self.engine = Engine(self.store, self.adapter)
        self.clock = Clock()
        self.engine.clock = self.clock

    def tearDown(self):
        self.adapter.main_gate.set()
        thread = getattr(self.engine, '_limit_stop_thread', None)
        if thread is not None:
            thread.join(5)
        self.engine.close()
        time.sleep(.05)
        self.store.close()
        self.temp.cleanup()

    # -- helpers ---------------------------------------------------------------------------------
    def start(self, task_id, worker_id, sent):
        task = dict(id=task_id, run_id='r1', role='executor', title=task_id, prompt='p', state='running',
                    worker_id=worker_id, lease='lease-' + task_id, attempts=1, started=time.time(), next_at=0,
                    deps=[], files=[], thread_id='th-' + worker_id, step_id='s-' + task_id,
                    submission_phase='inference' if sent else 'preparing')
        if sent:
            task['sent_at'] = time.time()
        self.store.put('tasks', task)
        worker = self.store.get('workers', worker_id)
        worker.update(state='busy', lease=task['lease'])
        self.store.put('workers', worker)
        cancel = threading.Event()
        future = self.engine.pool.submit(self.adapter.run, dict(worker), dict(task), 'p', cancel, lambda text: None)
        self.engine.inflight[future] = dict(task_id=task_id, lease=task['lease'], worker_id=worker_id,
                                            run_id='r1', cancel=cancel, recovery=False)
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

    def stop_both(self):
        cancels = self.start('t-pre', 'w1', sent=False), self.start('t-sent', 'w2', sent=True)
        self.engine.tick()  # first check: below the limit
        self.ledger(9_000_000)  # 9M coworker tokens in 15 min > 8M hard limit
        self.clock.advance(LIMIT_CHECK_SECONDS + 0.5)
        self.engine.tick()  # exactly ONE loop tick
        return cancels

    # -- tests -----------------------------------------------------------------------------------
    def test_hard_limit_cancels_both_coworker_turns_in_one_tick_and_keeps_main(self):
        main_cancel = self.start('t-main', 'main', sent=True)
        pre_cancel, sent_cancel = self.stop_both()
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

        self.pump(lambda: all(self.task(t)['state'] != 'running' for t in ('t-pre', 't-sent')))
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
            self.clock.advance(LIMIT_CHECK_SECONDS + 0.5)
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

    def test_below_hard_limit_nothing_is_cancelled(self):
        cancels = [self.start('t-a', 'w1', sent=True), self.start('t-b', 'w2', sent=True),
                   self.start('t-main', 'main', sent=True)]
        self.ledger(6_000_000)  # throttle level (5M soft) but below the 8M hard limit
        for _ in range(4):
            self.clock.advance(LIMIT_CHECK_SECONDS + 0.5)
            self.engine.tick()
        self.assertEqual(self.engine.limit_state()['level'], 'throttle')
        self.assertFalse(self.engine.limit_state()['hard'])
        self.assertFalse(any(c.is_set() for c in cancels))
        self.assertEqual(self.events(LIMIT_STOP_EVENT), [])
        self.assertIsNone(self.engine.limit_latch)
        self.assertEqual([self.task(t)['state'] for t in ('t-a', 't-b')], ['running', 'running'])
        for cancel in cancels:
            cancel.set()  # end the fake turns

    def test_after_the_limit_clears_tasks_resume_and_the_sent_turn_is_read_back(self):
        self.stop_both()
        self.pump(lambda: all(self.task(t)['state'] != 'running' for t in ('t-pre', 't-sent')))
        self.ledger(0)
        self.clock.advance(LIMIT_CHECK_SECONDS + 0.5)
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


if __name__ == '__main__':
    unittest.main()
