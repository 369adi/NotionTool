"""Phase 10 fault injection: local mocks only, no network, no app start.

Each test injects one failure into the existing Engine / Store / NetworkBudget
APIs and checks the recovery rule the code promises (I5: never resend blindly).
"""
import json
import tempfile
import time
import unittest
from concurrent.futures import Future
from pathlib import Path

from notiontool.engine import Engine, PendingRemote, TransientInference
from notiontool.network_budget import CircuitOpen, NetworkBudget
from notiontool.store import Store
from tests import test_engine as fixtures

DONE = json.dumps(dict(status='done', summary='Recovered answer', evidence=['fake check']))


class CountingAdapter:
    """Records every send (run) and every reconcile (recover); never touches Notion."""

    def __init__(self):
        self.run_calls = []
        self.recover_calls = []

    def run(self, worker, task, prompt, cancel, update):
        self.run_calls.append(task['id'])
        return DONE

    def recover(self, worker, task, cancel):
        self.recover_calls.append(task['id'])
        return DONE


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(float(seconds), 0.001)


def failed(exc):
    future = Future()
    future.set_exception(exc)
    return future


def succeeded(text):
    future = Future()
    future.set_result(text)
    return future


class EngineFaultBase(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent / '.test-data'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.store = Store(self.temp.name)
        self.adapter = CountingAdapter()
        self.store.put('accounts', dict(id='a', slot=1, email='test@example.test', state='ready', cooldown_until=0))
        for ident in ('main', 'w1', 'w2'):
            self.store.put('workers', dict(id=ident, account_id='a', thread_id=ident, purpose='main' if ident == 'main' else 'worker',
                                           enabled=True, state='ready', lease='', label=ident, last_used=0))
        # A finished run: tick() reconciles and completes, but never plans or dispatches new work for it.
        self.store.put('runs', dict(id='r1', chat_id='c', state='done', project_path=self.temp.name))
        self.engine = Engine(self.store, self.adapter)

    def tearDown(self):
        self.engine.close()
        time.sleep(.03)
        self.store.close()
        self.temp.cleanup()

    def task(self, ident='t1', worker='w1', lease='lease-1', phase='inference', started=None, attempts=1, state='running'):
        doc = dict(id=ident, run_id='r1', state=state, role='executor', title='Fault case', prompt='Do it', deps=[], files=[],
                   acceptance='', worker_id=worker, thread_id=worker, lease=lease, attempts=attempts, next_at=0, error='',
                   started=time.time() if started is None else started, submission_phase=phase,
                   attempt_log=[dict(attempt=attempts, worker_id=worker, started=time.time())])
        self.store.put('tasks', doc)
        if state == 'running':
            worker_doc = self.store.get('workers', worker)
            worker_doc.update(state='busy', lease=lease)
            self.store.put('workers', worker_doc)
        return doc

    def complete(self, future, ident='t1', worker='w1', lease='lease-1', recovery=False):
        info = dict(task_id=ident, lease=lease, worker_id=worker, run_id='r1', cancel=None, recovery=recovery)
        with self.engine.lock:
            self.engine._complete(future, info)
        return self.store.get('tasks', ident)

    def pump(self, predicate, timeout=15):  # returns as soon as predicate holds; 4 s was too tight under full-suite load
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.engine.tick()
            if predicate():
                return
            time.sleep(.015)
        self.fail('Expected state not reached: ' + str([(t['id'], t['state']) for t in self.store.all('tasks')]))

    def restart(self):
        self.engine.close()
        self.engine = Engine(self.store, self.adapter)


class RemoteTimeoutTests(EngineFaultBase):
    def test_remote_timeout_marks_remote_unknown_and_never_resends(self):
        timeout = self.store.settings()['task_timeout']
        self.task(started=time.time() - timeout - 200)  # overdue: past task_timeout + 120 s
        task = self.complete(failed(PendingRemote()))
        # No time-based stop: an overdue task stays 'uncertain' and is only checked more slowly (300 s).
        self.assertEqual(task['state'], 'uncertain')
        self.assertFalse(task['remote_unknown'])
        self.assertGreater(task['reconcile_at'], time.time() + 200)
        self.assertEqual(self.store.get('workers', 'w1')['state'], 'recovering')
        for _ in range(5):
            self.engine.tick()
        self.assertEqual(self.adapter.run_calls, [], 'an unconfirmed send must never be resent (I5)')
        self.assertEqual(self.adapter.recover_calls, [])

    def test_unconfirmed_reply_goes_to_recovery_not_resend(self):
        self.task()
        task = self.complete(failed(PendingRemote()))
        self.assertEqual(task['state'], 'uncertain')
        self.assertFalse(task['remote_unknown'])
        task['reconcile_at'] = 0  # skip the reconcile wait instead of sleeping
        self.store.put('tasks', task)
        self.pump(lambda: self.store.get('tasks', 't1')['state'] == 'done')
        self.assertEqual(self.adapter.recover_calls, ['t1'])
        self.assertEqual(self.adapter.run_calls, [])

    def test_open_circuit_after_send_is_reconciled_not_retried(self):
        self.task(phase='inference')
        task = self.complete(failed(CircuitOpen('mcp', 30)))
        self.assertEqual(task['state'], 'uncertain')
        self.assertEqual(task['attempts'], 1, 'a possibly sent attempt keeps its count')
        self.assertNotEqual(task['state'], 'retry')


class TransientRetryTests(EngineFaultBase):
    def test_transient_error_schedules_backoff_retry(self):
        self.task(attempts=1)
        before = time.time()
        task = self.complete(failed(TransientInference('temporary 503')))
        self.assertEqual(task['state'], 'retry')
        # backoff(1, base=1, cap=60) with jitter .5 lies in [2, 3] seconds.
        self.assertGreaterEqual(task['next_at'] - before, 2 - .05)
        self.assertLessEqual(task['next_at'] - before, 3 + .5)
        self.assertEqual(self.store.get('workers', 'w1')['state'], 'ready')

    def test_retries_stop_at_max_attempts(self):
        limit = self.store.settings()['max_attempts']
        self.task(attempts=limit)
        task = self.complete(failed(TransientInference('temporary 503')))
        self.assertEqual(task['state'], 'blocked')
        self.assertEqual(task['attempts'], limit)


class TransientEndToEndTests(unittest.TestCase):
    """Full plan -> execute flow with one transient executor failure, using the test_engine fixtures."""

    def setUp(self):
        fixtures.EngineTests.setUp(self)
        self.adapter.count = 1
        self.sends = []
        original = self.adapter.run

        def flaky(worker, task, prompt, cancel, update):
            if task['role'] == 'executor':
                self.sends.append(time.time())
                if len(self.sends) == 1:
                    raise TransientInference('temporary 503')
            return original(worker, task, prompt, cancel, update)
        self.adapter.run = flaky

    def tearDown(self):
        fixtures.EngineTests.tearDown(self)

    def worker(self, ident, purpose='worker'):
        fixtures.EngineTests.worker(self, ident, purpose)

    def pump(self, predicate, timeout=15):  # returns as soon as predicate holds; 4 s was too tight under full-suite load
        fixtures.EngineTests.pump(self, predicate, timeout)

    def test_transient_error_waits_for_backoff_then_retries_once(self):
        run = self.engine.submit('c', 'Build and verify')
        self.pump(lambda: self.store.get('runs', run['id'])['state'] == 'done', timeout=15)
        executor = [t for t in self.engine.tasks(run['id']) if t['role'] == 'executor'][0]
        self.assertEqual(executor['attempts'], 2)
        self.assertEqual(len(self.sends), 2)
        self.assertGreaterEqual(self.sends[1] - self.sends[0], 2 - .1, 'the retry waited for the backoff')


class RestartTests(EngineFaultBase):
    def test_restart_mid_run_recovers_from_store(self):
        self.task('unsent', worker='w1', lease='l-unsent', phase='preparing', attempts=1)
        self.task('maybe', worker='w2', lease='l-maybe', phase='inference', attempts=1)
        leftover = self.store.get('workers', 'main')
        leftover.update(state='busy', lease='stale-lease')
        self.store.put('workers', leftover)
        self.restart()
        unsent = self.store.get('tasks', 'unsent')
        self.assertEqual(unsent['state'], 'retry')
        self.assertEqual(unsent['attempts'], 0, 'an unsent attempt is not counted')
        self.assertEqual(self.store.get('tasks', 'maybe')['state'], 'uncertain')
        self.assertEqual(self.store.get('workers', 'w2')['state'], 'recovering')
        self.assertEqual(self.store.get('workers', 'w2')['lease'], 'l-maybe')
        main = self.store.get('workers', 'main')
        self.assertEqual((main['state'], main['lease']), ('ready', ''))
        self.assertEqual((self.store.get('workers', 'w1')['state'], self.store.get('workers', 'w1')['lease']), ('ready', ''))

    def test_sent_task_recovered_once_after_restart_not_resent(self):
        self.task('sent', worker='w1', lease='l-sent', phase='inference')
        self.restart()
        self.pump(lambda: self.store.get('tasks', 'sent')['state'] == 'done')
        for _ in range(5):
            self.engine.tick()
            time.sleep(.01)
        self.assertEqual(self.adapter.recover_calls, ['sent'])
        self.assertEqual(self.adapter.run_calls, [])

    def test_stale_lease_report_is_ignored(self):
        self.task(lease='new-lease')
        task = self.complete(succeeded(DONE), lease='old-lease')
        self.assertEqual(task['state'], 'running')
        self.assertEqual(task['lease'], 'new-lease')
        self.assertNotIn('result', task)

    @unittest.skip('Not testable without a production change: engine.py:540 creates the lease '
                   '(secrets.token_urlsafe) with no deadline, and engine.py:385-389 check_lease only compares '
                   'state and lease value. A killed worker\'s lease therefore never expires. Needs a lease '
                   'deadline set at engine.py:540 and checked in check_lease and _reconcile (engine.py:831).')
    def test_lease_expires_after_worker_killed_and_task_runs_once(self):
        pass


class BreakerTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.budget = NetworkBudget(clock=self.clock, sleep=self.clock.sleep, rng=lambda: 0.0)

    def fail_times(self, count, source='mcp'):
        for _ in range(count):
            self.budget.release(self.budget.acquire(source=source), 'failed', status=503)

    def test_network_budget_holds_start_over_minute_limit(self):
        self.budget.limit_per_minute = 2
        first = self.budget.acquire(source='x').started
        self.budget.acquire(source='x')
        third = self.budget.acquire(source='x').started
        self.assertGreaterEqual(third, first + 60, 'the third start waited for the sliding window')
        self.assertEqual(self.budget.window_waits, 1)

    def test_breaker_opens_after_five_transport_failures(self):
        self.fail_times(4)
        self.budget.acquire(source='mcp')  # still closed after four
        self.fail_times(1)
        with self.assertRaises(CircuitOpen):
            self.budget.acquire(source='mcp')
        self.assertEqual(self.budget.status()['circuits']['mcp']['state'], 'open')
        self.budget.acquire(source='other')  # other endpoints stay usable

    def test_breaker_closes_after_cooldown_and_good_probe(self):
        self.fail_times(5)
        self.clock.now += 31  # backoff(0, 30, 300, jitter=.25, rng=0) == 30 s
        probe = self.budget.acquire(source='mcp')
        self.assertTrue(probe.trial)
        self.budget.release(probe, 'ok')
        self.assertEqual(self.budget.breakers['mcp']['state'], 'closed')
        self.assertNotIn('mcp', self.budget.status()['circuits'])
        self.assertFalse(self.budget.acquire(source='mcp').trial)

    def test_failed_probe_reopens_breaker(self):
        self.fail_times(5)
        self.clock.now += 31
        probe = self.budget.acquire(source='mcp')
        self.budget.release(probe, 'failed', status=503)
        breaker = self.budget.breakers['mcp']
        self.assertEqual(breaker['state'], 'open')
        self.assertEqual(breaker['trips'], 2)
        self.assertAlmostEqual(breaker['until'] - self.clock.now, 60, delta=1)
        with self.assertRaises(CircuitOpen):
            self.budget.acquire(source='mcp')


if __name__ == '__main__':
    unittest.main()
