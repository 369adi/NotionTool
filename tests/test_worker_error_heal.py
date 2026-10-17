"""Phase 4 Fix M: a cancel releases the worker; an errored idle worker heals after a backoff."""
import time
import unittest

from notiontool.engine import CANCELLED_BEFORE_SUBMISSION
from tests import test_engine as fixtures


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class Failed:
    def __init__(self, exc):
        self.exc = exc

    def done(self):
        return True

    def result(self):
        raise self.exc


class WorkerErrorHealTests(unittest.TestCase):
    worker = fixtures.EngineTests.worker
    tearDown = fixtures.EngineTests.tearDown

    def setUp(self):
        fixtures.EngineTests.setUp(self)
        self.clock = FakeClock()
        self.engine.clock = self.clock
        self.events = []
        original = self.store.event

        def record(kind, *args, **fields):
            self.events.append((kind, fields))
            return original(kind, *args, **fields)
        self.store.event = record

    def recovered(self):
        return [f for k, f in self.events if k == 'worker_recovered']

    def lease(self, ident, lease='L1'):
        worker = self.store.get('workers', ident)
        worker.update(state='busy', lease=lease)
        self.store.put('workers', worker)

    def fail(self, ident, run_state='executing', exc=None):
        self.store.put('runs', dict(id='r1', chat_id='c', state=run_state, capacity=1))
        self.store.put('tasks', dict(id='t1', run_id='r1', role='executor', title='Part', state='running',
                                     lease='L1', worker_id=ident, attempts=1, files=[], started=time.time()))
        self.lease(ident)
        self.engine._complete(Failed(exc or ValueError(CANCELLED_BEFORE_SUBMISSION)),
                              dict(task_id='t1', lease='L1', worker_id=ident, run_id='r1'))
        return self.store.get('tasks', 't1'), self.store.get('workers', ident)

    def test_cancel_in_cancelled_run_releases_worker_ready(self):
        task, worker = self.fail('w0', run_state='cancelled')
        self.assertEqual((worker['state'], worker['lease']), ('ready', ''))
        self.assertEqual(task['state'], 'cancelled')
        self.assertEqual(task['attempts'], 0)
        self.assertEqual(task['error'], '')

    def test_cancel_in_live_run_retries_now_with_attempt_back(self):
        before = time.time()
        task, worker = self.fail('w0')
        self.assertEqual(worker['state'], 'ready')
        self.assertEqual(task['state'], 'retry')
        self.assertEqual(task['attempts'], 0)
        self.assertLessEqual(task['next_at'], time.time())
        self.assertGreaterEqual(task['next_at'], before)

    def test_other_value_error_still_marks_worker_error(self):
        task, worker = self.fail('w0', exc=ValueError('Assignment cancelled before submission, or not'))
        self.assertEqual(worker['state'], 'error')
        self.assertEqual(task['state'], 'blocked')

    def test_no_recovery_before_60_seconds(self):
        self.fail('w0', exc=RuntimeError('boom'))
        self.engine._heal_workers()
        self.clock.now += 59.9
        self.engine._heal_workers()
        self.assertEqual(self.store.get('workers', 'w0')['state'], 'error')
        self.assertEqual(self.recovered(), [])
        self.clock.now += 0.1
        self.engine._heal_workers()
        self.assertEqual(self.store.get('workers', 'w0')['state'], 'ready')
        self.assertEqual(self.recovered(), [dict(worker_id='w0', backoff=60,
                                                 message='Worker returned to ready after 60s in error')])

    def test_backoff_steps_and_one_event_per_recovery(self):
        for n, step in enumerate([60, 120, 240, 480, 600, 600], 1):
            self.lease('w1', 'L%d' % n)
            self.engine._release('w1', 'L%d' % n, 'error')  # the worker fails again after recovering
            self.engine._heal_workers()
            self.clock.now += step - 0.5
            self.engine._heal_workers()
            self.assertEqual(self.store.get('workers', 'w1')['state'], 'error', step)
            self.clock.now += 0.5
            for _ in range(3):
                self.engine._heal_workers()
            self.assertEqual(self.store.get('workers', 'w1')['state'], 'ready', step)
            self.assertEqual(len(self.recovered()), n)
            self.assertEqual(self.recovered()[-1]['backoff'], step)

    def test_clean_release_restarts_the_series(self):
        for n in (1, 2):
            self.lease('w2', 'L%d' % n)
            self.engine._release('w2', 'L%d' % n, 'error')
            self.engine._heal_workers()
            self.clock.now += 600
            self.engine._heal_workers()
        self.lease('w2', 'ok')
        self.engine._release('w2', 'ok')
        self.lease('w2', 'L3')
        self.engine._release('w2', 'L3', 'error')
        self.engine._heal_workers()
        self.assertEqual(self.store.get('workers', 'w2')['heal_backoff'], 60)

    def test_leased_worker_is_never_touched(self):
        worker = self.store.get('workers', 'w3')
        worker.update(state='error', lease='held')
        self.store.put('workers', worker)
        for _ in range(3):
            self.engine._heal_workers()
            self.clock.now += 10000
        self.assertEqual(self.store.get('workers', 'w3'), worker)
        self.assertEqual(self.recovered(), [])

    def test_tick_runs_the_heal_step(self):
        worker = self.store.get('workers', 'w4')
        worker.update(state='error', lease='')
        self.store.put('workers', worker)
        self.engine.tick()
        self.assertEqual(self.store.get('workers', 'w4')['heal_at'], self.clock.now + 60)
        self.clock.now += 60
        self.engine.tick()
        self.assertEqual(self.store.get('workers', 'w4')['state'], 'ready')
        self.assertEqual(len(self.recovered()), 1)


if __name__ == '__main__':
    unittest.main()
