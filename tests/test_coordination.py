import tempfile
import threading
import time
import unittest
from concurrent.futures import Future
from pathlib import Path

from notiontool.engine import Engine
from notiontool.network_budget import (BREAKER_FAILURES, CircuitOpen, NetworkBudget, RemotePaused, Ticket,
                                       backoff, current_priority, request_priority)
from notiontool.store import Store

try:
    from tests.test_engine import FakeAdapter
except ImportError:  # discovered with -s tests
    from test_engine import FakeAdapter


class MemoryStore:
    def __init__(self):
        self.docs, self.events, self.lock = {}, [], threading.RLock()

    def get(self, kind, ident, default=None):
        with self.lock:
            doc = self.docs.get((kind, ident))
            return dict(doc) if doc is not None else default

    def put(self, kind, doc):
        with self.lock:
            self.docs[(kind, doc['id'])] = dict(doc)

    def event(self, kind, run_id='', **fields):
        with self.lock:
            self.events.append(dict(fields, kind=kind))

    def kinds(self):
        return [e['kind'] for e in self.events]


class Clock:
    def __init__(self, start=1000.0):
        self.now, self.lock = start, threading.Lock()

    def time(self):
        with self.lock:
            return self.now

    def sleep(self, seconds):
        with self.lock:
            self.now += max(0, seconds)


def budget(store=None, clock=None, rng=lambda: .5):
    clock = clock or Clock()
    return NetworkBudget(store, clock=clock.time, sleep=clock.sleep, rng=rng), clock


class WindowPersistenceTests(unittest.TestCase):
    def test_restart_keeps_the_window_and_the_181st_start_waits(self):
        store, clock = MemoryStore(), Clock()
        first, _ = budget(store, clock)
        for _ in range(180):
            first.acquire()
        restarted, _ = budget(store, clock)
        self.assertEqual(restarted.window_count(), 180)
        restarted.acquire()
        self.assertGreaterEqual(clock.time(), 1060.0)

    def test_expired_starts_are_not_restored(self):
        store, clock = MemoryStore(), Clock()
        first, _ = budget(store, clock)
        for _ in range(10):
            first.acquire()
        clock.sleep(61)
        restarted, _ = budget(store, clock)
        self.assertEqual(restarted.window_count(), 0)

    def test_unreadable_window_is_treated_as_full(self):
        store, clock = MemoryStore(), Clock()
        store.put('settings', dict(id='network_window', starts='broken'))
        restarted, _ = budget(store, clock)
        self.assertEqual(restarted.window_count(), 180)
        restarted.acquire()
        self.assertGreaterEqual(clock.time(), 1060.0)

    def test_every_start_is_saved_before_the_request_leaves(self):
        store, clock = MemoryStore(), Clock()
        coordinator, _ = budget(store, clock)
        for count in range(1, 4):
            coordinator.acquire()
            self.assertEqual(len(store.get('settings', 'network_window')['starts']), count)


class PriorityTests(unittest.TestCase):
    def test_critical_then_verify_then_rest_when_a_slot_frees(self):
        coordinator = NetworkBudget(rng=lambda: 0)
        coordinator.blocked_until = time.time() + .4
        order, threads = [], []

        def caller(level):
            with request_priority(level):
                coordinator.acquire()
            order.append(level)

        for level in ('rest', 'verify', 'critical'):
            thread = threading.Thread(target=caller, args=(level,))
            thread.start()
            threads.append(thread)
            time.sleep(.05)
        for thread in threads:
            thread.join(5)
        self.assertEqual(order, ['critical', 'verify', 'rest'])
        self.assertEqual(coordinator.waiting, set())

    def test_priority_context_restores_the_previous_level(self):
        self.assertEqual(current_priority(), 'rest')
        with request_priority('critical'):
            with request_priority('verify'):
                self.assertEqual(current_priority(), 'verify')
            self.assertEqual(current_priority(), 'critical')
        self.assertEqual(current_priority(), 'rest')


class ParallelismTests(unittest.TestCase):
    def test_errors_halve_and_successes_raise_parallelism_stepwise(self):
        coordinator, _ = budget(MemoryStore())
        self.assertEqual(coordinator.parallel_limit(8), 8)
        for expected in (4, 2, 1, 1):
            coordinator.release(coordinator.acquire(source='a'), 'failed')
            self.assertEqual(coordinator.parallel_limit(8), expected)
        seen = []
        for _ in range(40):
            coordinator.release(coordinator.acquire(source='a'), 'ok')
            seen.append(coordinator.parallel_limit(8))
        self.assertTrue(all(b - a in (0, 1) for a, b in zip(seen, seen[1:])))
        self.assertEqual(seen[-1], 8)
        self.assertIsNone(coordinator.parallel)

    def test_rate_limit_halves_parallelism(self):
        coordinator, _ = budget(MemoryStore())
        coordinator.parallel_limit(6)
        coordinator.release(coordinator.acquire(source='a'), 'rate_limited', status=429)
        self.assertEqual(coordinator.parallel_limit(6), 3)

    def test_rising_latency_lowers_by_one_at_most_every_ten_seconds(self):
        coordinator, clock = budget(MemoryStore())
        coordinator.parallel_limit(6)
        for _ in range(10):
            coordinator.release(coordinator.acquire(source='s'), 'ok', latency=1.0)
        self.assertEqual(coordinator.parallel_limit(6), 6)
        coordinator.release(coordinator.acquire(source='s'), 'ok', latency=9.0)
        self.assertEqual(coordinator.parallel_limit(6), 5)
        coordinator.release(coordinator.acquire(source='s'), 'ok', latency=9.0)
        self.assertEqual(coordinator.parallel_limit(6), 5)
        clock.sleep(11)
        coordinator.release(coordinator.acquire(source='s'), 'ok', latency=9.0)
        self.assertEqual(coordinator.parallel_limit(6), 4)

    def test_turn_error_inside_a_successful_stream_halves_parallelism(self):
        store = MemoryStore()
        coordinator, _ = budget(store)
        coordinator.parallel_limit(8)
        coordinator.release(coordinator.acquire(source='inference'), 'ok')
        self.assertTrue(coordinator.turn_failed('inference', 'temporarily-unavailable'))
        self.assertEqual(coordinator.parallel_limit(8), 4)
        event = [e for e in store.events if e['kind'] == 'request_coordination_adjusted'][-1]
        self.assertEqual((event['reason'], event['sub_type'], event['parallel']), ('turn_error', 'temporarily-unavailable', 4))
        coordinator.release(coordinator.acquire(source='inference'), 'ok')  # breaker stays closed

    def test_adapter_turn_failed_reports_to_the_budget(self):
        from types import SimpleNamespace
        from notiontool.notion import NotionAdapter
        store = MemoryStore()
        coordinator, _ = budget(store)
        coordinator.parallel_limit(6)
        adapter = SimpleNamespace(store=store, network_budget=coordinator)
        with self.assertRaises(Exception) as caught:
            NotionAdapter.turn_failed(adapter, dict(id='t1', run_id='r1'),
                                      dict(subType='temporarily-unavailable', message='Something went wrong.'))
        self.assertEqual(type(caught.exception).__name__, 'RateLimited')
        self.assertEqual(coordinator.parallel_limit(6), 3)
        self.assertIn('remote_turn_failed', store.kinds())


class IntervalTests(unittest.TestCase):
    def test_interval_recovers_after_a_quiet_minute_and_is_saved(self):
        store = MemoryStore()
        coordinator, clock = budget(store, rng=lambda: 0)
        coordinator.limited(1)
        self.assertEqual(coordinator.interval, .5)
        clock.sleep(61)
        for _ in range(10):
            coordinator.release(coordinator.acquire(source='s'), 'ok')
        self.assertEqual(coordinator.interval, .25)
        self.assertEqual(store.get('settings', 'network_budget')['interval'], .25)
        self.assertIn('interval_recovered', [e.get('reason') for e in store.events])


class BackoffTests(unittest.TestCase):
    def test_bounds_cap_floor_and_jitter(self):
        self.assertEqual(backoff(0, base=1, cap=60, rng=lambda: 0), 1)
        self.assertEqual(backoff(3, base=1, cap=60, rng=lambda: 0), 8)
        self.assertEqual(backoff(3, base=1, cap=60, rng=lambda: 1), 12)
        self.assertEqual(backoff(10, base=1, cap=60, rng=lambda: 1), 60)
        self.assertEqual(backoff(0, base=30, cap=300, floor=200, rng=lambda: 1), 200)
        values = {backoff(2, base=1, cap=60) for _ in range(30)}
        self.assertGreater(len(values), 1)
        self.assertTrue(all(4 <= v <= 6 for v in values))

    def test_rate_limit_delay_never_undercuts_retry_after(self):
        coordinator, _ = budget(rng=lambda: 1)
        self.assertEqual(coordinator.limited('200'), 200)
        jittered, _ = budget(rng=lambda: 1)
        self.assertEqual(jittered.limited(None), 37.5)


class BreakerTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.coordinator, self.clock = budget(self.store, rng=lambda: 0)

    def fail(self, source='/api/v3/a', outcome='failed'):
        self.coordinator.release(self.coordinator.acquire(source=source), outcome, status=502)

    def test_failures_open_only_that_endpoint_without_using_a_slot(self):
        for _ in range(BREAKER_FAILURES):
            self.fail()
        self.assertIn('circuit_open', self.store.kinds())
        used = self.coordinator.window_count()
        with self.assertRaises(CircuitOpen) as refused:
            self.coordinator.acquire(source='/api/v3/a')
        self.assertGreaterEqual(refused.exception.seconds, 29)
        self.assertEqual(self.coordinator.window_count(), used)
        self.assertIsInstance(self.coordinator.acquire(source='/api/v3/b'), Ticket)
        self.assertEqual(self.coordinator.status()['circuits']['/api/v3/a']['state'], 'open')

    def test_a_success_resets_the_failure_count(self):
        for _ in range(BREAKER_FAILURES - 1):
            self.fail()
        self.coordinator.release(self.coordinator.acquire(source='/api/v3/a'), 'ok')
        for _ in range(BREAKER_FAILURES - 1):
            self.fail()
        self.assertNotIn('circuit_open', self.store.kinds())

    def test_half_open_allows_one_trial_and_closes_on_success(self):
        for _ in range(BREAKER_FAILURES):
            self.fail()
        self.clock.sleep(31)
        trial = self.coordinator.acquire(source='/api/v3/a')
        self.assertTrue(trial.trial)
        with self.assertRaises(CircuitOpen):
            self.coordinator.acquire(source='/api/v3/a')
        self.coordinator.release(trial, 'ok')
        self.assertIn('circuit_closed', self.store.kinds())
        self.assertIsInstance(self.coordinator.acquire(source='/api/v3/a'), Ticket)

    def test_failed_trial_reopens_with_a_longer_cooldown(self):
        for _ in range(BREAKER_FAILURES):
            self.fail()
        self.clock.sleep(31)
        self.fail()
        opened = [e['seconds'] for e in self.store.events if e['kind'] == 'circuit_open']
        self.assertEqual(opened, [30.0, 60.0])
        with self.assertRaises(CircuitOpen) as refused:
            self.coordinator.acquire(source='/api/v3/a')
        self.assertGreater(refused.exception.seconds, 50)

    def test_client_errors_do_not_count(self):
        for _ in range(BREAKER_FAILURES * 2):
            self.fail(outcome='invalid')
        self.assertNotIn('circuit_open', self.store.kinds())


class PauseTests(unittest.TestCase):
    def test_account_block_pauses_everything_and_survives_a_restart(self):
        store = MemoryStore()
        coordinator, clock = budget(store)
        coordinator.release(coordinator.acquire(source='/api/v3/runInferenceTranscript'), 'blocked',
                            status=403, account_id='d84a')
        self.assertTrue(coordinator.status()['paused'])
        used = coordinator.window_count()
        with self.assertRaises(RemotePaused):
            coordinator.acquire(source='/api/v3/other')
        self.assertEqual(coordinator.window_count(), used)
        event = [e for e in store.events if e['kind'] == 'remote_paused'][-1]
        self.assertEqual((event['error_class'], event['account_id'], event['status']), ('ACCOUNT_BLOCKED', 'd84a', 403))
        restarted, _ = budget(store, clock)
        with self.assertRaises(RemotePaused):
            restarted.acquire()
        self.assertTrue(restarted.resume())
        self.assertFalse(restarted.resume())
        self.assertIn('remote_resumed', store.kinds())
        self.assertIsInstance(restarted.acquire(), Ticket)

    def test_a_waiting_caller_is_refused_once_the_pause_starts(self):
        coordinator = NetworkBudget()
        coordinator.blocked_until = time.time() + 5
        errors = []

        def caller():
            try:
                coordinator.acquire()
            except RemotePaused as exc:
                errors.append(exc)

        thread = threading.Thread(target=caller)
        thread.start()
        time.sleep(.1)
        coordinator.pause('HTTP 403', status=403)
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertEqual(coordinator.waiting, set())


class EngineCoordinationTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent / '.test-data'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.store = Store(self.temp.name)
        self.adapter = FakeAdapter()
        self.budget = self.adapter.network_budget = NetworkBudget(self.store, rng=lambda: 0)
        self.store.put('accounts', dict(id='a', slot=1, email='test@example.test', state='ready', cooldown_until=0))
        self.store.put('workers', dict(id='main', account_id='a', thread_id='main', purpose='main', enabled=True,
                                       state='ready', lease='', label='main', last_used=0))
        self.store.put('chats', dict(id='c', title='Main', main_worker='main', folder_id='f', project_path=self.temp.name))
        self.engine = Engine(self.store, self.adapter)

    def tearDown(self):
        self.adapter.gate.set()
        self.engine.close()
        time.sleep(.03)
        self.store.close()
        self.temp.cleanup()

    def complete_with(self, exc, phase='preparing'):
        run = self.engine.submit('c', 'coordination')
        task = self.engine.tasks(run['id'])[0]
        task.update(state='running', lease='saved', worker_id='main', attempts=1, submission_phase=phase,
                    started=time.time())
        self.store.put('tasks', task)
        future = Future()
        future.set_exception(exc)
        self.engine._complete(future, dict(task_id=task['id'], lease='saved', worker_id='main'))
        return self.store.get('tasks', task['id'])

    def test_paused_account_blocks_without_using_an_attempt_and_resumes(self):
        self.budget.pause('HTTP 403', account_id='a', status=403)
        task = self.complete_with(RemotePaused('paused'))
        self.assertEqual((task['state'], task['attempts'], task.get('paused_by')), ('blocked', 0, 'account_blocked'))
        self.assertEqual(self.store.get('workers', 'main')['state'], 'ready')
        result = self.engine.resume_remote()
        self.assertEqual((result['resumed'], result['requeued_tasks']), (True, 1))
        task = self.store.get('tasks', task['id'])
        self.assertEqual(task['state'], 'queued')
        self.assertNotIn('paused_by', task)
        self.assertFalse(self.engine.coordination()['paused'])

    def test_open_circuit_retries_later_without_using_an_attempt(self):
        task = self.complete_with(CircuitOpen('/api/v3/x', 7))
        self.assertEqual((task['state'], task['attempts']), ('retry', 0))
        self.assertAlmostEqual(task['next_at'], time.time() + 7, delta=2)

    def test_refusal_after_the_inference_left_is_reconciled_not_resent(self):
        task = self.complete_with(CircuitOpen('/api/v3/x', 7), phase='inference')
        self.assertEqual(task['state'], 'uncertain')
        self.assertEqual(self.store.get('workers', 'main')['state'], 'recovering')

    def test_worker_choice_follows_the_adaptive_cap(self):
        self.assertEqual(self.engine._parallel_cap(8), 8)
        self.budget.release(self.budget.acquire(source='s'), 'failed')
        self.assertEqual(self.engine._parallel_cap(8), 4)


if __name__ == '__main__':
    unittest.main()
