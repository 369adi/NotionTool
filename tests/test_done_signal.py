"""Late done detection: after the reply stream closed, the real end of the turn is seen within one poll.

End-to-end with a fake clock: the adapter's recover reports pending until the clock reaches the real end of the
turn, then returns a verified reply. The tests drive the real Engine._reconcile and Engine._complete steps (as
Engine.tick does) until the task is done. All times are 1792525000 + x, from the live cases of done_dig:
(a) t93d1a665: stream_end at 488.12 (remote_done_at 487.86), final reply at 656.02 (thread seq 759); the check that
    started at 649.07 (seq 25338) was negative, the one at 716.23 (seq 25350) positive. Old lag: 60.2 s.
(b) t54d0fd4b: the stream broke at 538.72 (seq 25299; first_output 0, remote_done 0). 697.31 (thread seq 598) is only
    the created_at of the final assistant message, not the end of the turn: the check that started at 709.513
    (seq 25348) still raised PendingRemote (failure seq 25349, ts 715.55), and the first check that confirmed the
    turn started at 777.20 (seq 25366). recover confirms only with a turn_completed entry or session status 'idle'
    (notion.py:1471, :1367 before commit 8ffd7af); that status is not logged, so the exact end is unknown. It lies
    in that interval, so every end time in it is tested.
"""
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future, wait
from pathlib import Path
from unittest import mock

from notiontool import engine as engine_mod
from notiontool.engine import Engine, PendingRemote, reconcile_backoff, reconcile_delay
from notiontool.network_budget import HARD_LIMIT_PER_MINUTE
from notiontool.notion import VerifiedReply
from notiontool.store import Store

BASE = 1792525000.0
RECOVER_S = 2.0  # one recover takes 0.9-4.3 s live (recover_timing transcript_s)
ONE_POLL = engine_mod.DONE_POLL_SECONDS + RECOVER_S  # one poll plus the time of the confirming check
OLD_SEQUENCE = [5, 10, 20, 40, 60, 60, 60, 60]
REPLY = '{"status": "done", "summary": "work finished"}'
CASE_A_CLOSE, CASE_A_END = BASE + 488.12, BASE + 656.02
CASE_B_CLOSE = BASE + 538.72
CASE_B_OPEN = BASE + 709.513  # seq 25348: this check still found the turn open (failure seq 25349, ts 715.55)
CASE_B_CONFIRMED = BASE + 777.20  # seq 25366: the first check that found the turn completed


class FakeClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class PollAdapter:
    """recover reports pending until the clock reaches real_end, then returns the verified reply."""

    def __init__(self, clock):
        self.clock = clock
        self.real_end = float('inf')
        self.recovers = []

    def run(self, worker, task, prompt, cancel, update):
        raise AssertionError('nothing may be sent')

    def recover(self, worker, task, cancel):
        self.recovers.append(self.clock.now)
        self.clock.now += RECOVER_S  # the transcript read takes time and shows the turn as it is when it arrives
        if self.clock.now >= self.real_end:
            return VerifiedReply(REPLY, False)
        raise PendingRemote()  # no done_empty proof: the empty-reply retry (Engine._empty_remote_reply) stays off

    def turn_running(self, worker):
        return self.clock.now < self.real_end


class DoneSignalTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent / '.test-data'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root, ignore_cleanup_errors=True)
        self.clock = FakeClock(BASE + 300)
        self.patch = mock.patch('time.time', self.clock)
        self.patch.start()
        self.store = Store(self.temp.name)
        self.adapter = PollAdapter(self.clock)
        self.store.put('accounts', dict(id='a', slot=1, email='test@example.test', state='ready', cooldown_until=0))
        self.store.put('chats', dict(id='c', title='Main', main_worker='m', folder_id='f', project_path=self.temp.name))
        self.store.put('runs', dict(id='r1', chat_id='c', state='executing', operator=True, project_path=self.temp.name,
                                    error='', capacity=2, worker_budget=2, fixes=0, main_worker='m',
                                    created=BASE, updated=BASE))
        self.engine = Engine(self.store, self.adapter)

    def tearDown(self):
        self.engine.close()
        self.patch.stop()
        time.sleep(.03)
        self.store.close()
        self.temp.cleanup()

    def put_task(self, sent, **extra):
        task = dict(id='t1', run_id='r1', role='executor', title='Work', prompt='p', state='running', result='',
                    deps=[], files=[], attempts=1, worker_id='w1', lease='L1', started=sent, sent_at=sent,
                    first_output_at=0, remote_done_at=0, reconcile_checks=0, partial='',
                    attempt_log=[dict(attempt=1, worker_id='w1', started=sent)])
        task.update(extra)
        self.store.put('workers', dict(id='w1', account_id='a', thread_id='w1', purpose='worker', enabled=True,
                                       state='busy', lease='L1', label='w1', last_used=0))
        self.store.put('tasks', task)
        return task

    def fail(self, recovery, cancel=None, **stored):
        """One check that finds the turn still open (PendingRemote), handled by Engine._complete."""
        if stored:
            task = self.store.get('tasks', 't1')
            task.update(stored)
            self.store.put('tasks', task)
        future = Future()
        future.set_exception(PendingRemote())
        info = dict(task_id='t1', lease='L1', worker_id='w1', run_id='r1', cancel=cancel or threading.Event(),
                    recovery=recovery)
        with self.engine.lock:
            self.engine._complete(future, info)
        task = self.store.get('tasks', 't1')
        return task['reconcile_at'] - self.clock.now, task

    def step(self):
        """Engine.tick's completion and reconcile steps (engine.py:1152) for the checks that are due now."""
        with self.engine.lock:
            self.engine._reconcile()
            futures = list(self.engine.inflight)
        wait(futures, timeout=10)
        with self.engine.lock:
            for future, info in list(self.engine.inflight.items()):
                if future.done():
                    del self.engine.inflight[future]
                    if info.get('done_at'):
                        info['complete_wait_s'] = round(max(0.0, time.time() - info['done_at']), 3)
                    self.engine._complete(future, info)

    def until_done(self, close, real_end, **stored):
        """The attempt's own check fails at the stream close (notion.py:1354); then the real steps run until done."""
        self.adapter.real_end = real_end
        self.adapter.recovers = []
        after = max([e['seq'] for e in self.store.events(limit=1)] or [0])
        self.clock.now = close
        self.fail(False, **stored)
        for _ in range(400):
            task = self.store.get('tasks', 't1')
            if task['state'] != 'uncertain':
                break
            self.clock.now = max(self.clock.now, task['reconcile_at'])  # the tick at which the next check is due
            self.step()
        else:
            raise AssertionError('never finished')
        finished = [e for e in self.store.events(after=after, limit=2000) if e['kind'] == 'task_finished']
        return self.store.get('tasks', 't1'), finished

    def done_lag(self, task, finished, real_end):
        """Seconds from the real end of the turn to task_finished; the task must be done exactly once."""
        self.assertEqual(task['state'], 'done')
        self.assertEqual([e['state'] for e in finished], ['done'])
        self.assertEqual(finished[0]['ts'], task['finished'])
        lag = finished[0]['ts'] - real_end
        self.assertGreaterEqual(lag, 0)
        return lag

    def gaps(self, close):
        """Planned delays: first check after the close, then start to start minus the check time."""
        starts = [round(s - close, 3) for s in self.adapter.recovers]
        return [starts[0]] + [round(b - a - RECOVER_S, 3) for a, b in zip(starts, starts[1:])]

    def test_case_a_done_within_one_poll_after_the_real_end(self):
        self.put_task(BASE + 356.94, first_output_at=BASE + 361.48, remote_done_at=BASE + 487.86)
        task, finished = self.until_done(CASE_A_CLOSE, CASE_A_END)
        lag = self.done_lag(task, finished, CASE_A_END)
        self.assertLessEqual(lag, ONE_POLL)
        gaps = self.gaps(CASE_A_CLOSE)
        self.assertEqual(gaps[:2], [5, 10])  # the first checks stay as fast as before
        self.assertTrue(all(g <= engine_mod.DONE_POLL_SECONDS for g in gaps))
        self.assertEqual(task['remote_done_at'], BASE + 487.86)
        self.assertNotIn('empty_reply_retry', [e['kind'] for e in self.store.events(limit=2000)])  # nothing resent
        print('\ncase a: task_finished %.2f s after the real end (%d checks)' % (lag, len(gaps)))

    def test_case_a_old_backoff_is_tens_of_seconds_late(self):
        self.store.put('settings', dict(id='app', done_poll_max_tasks=0))  # no fast-poll slot: the old backoff
        self.put_task(BASE + 356.94, first_output_at=BASE + 361.48, remote_done_at=BASE + 487.86)
        task, finished = self.until_done(CASE_A_CLOSE, CASE_A_END)
        lag = self.done_lag(task, finished, CASE_A_END)
        self.assertEqual(self.gaps(CASE_A_CLOSE), OLD_SEQUENCE[:6])
        self.assertGreater(lag, 2 * ONE_POLL)
        print('\ncase a, old backoff: task_finished %.2f s after the real end' % lag)

    def test_case_b_broken_stream_done_within_one_poll_for_every_end_time(self):
        ends = [CASE_B_OPEN + 4 * i for i in range(17)] + [CASE_B_CONFIRMED]
        lags = []
        for end in ends:
            self.put_task(BASE + 344.81)
            task, finished = self.until_done(CASE_B_CLOSE, end)
            lags.append(self.done_lag(task, finished, end))
            self.assertEqual(task['remote_done_at'], 0)  # never invented: the empty-reply retry stays off
            self.assertEqual(task['first_output_at'], 0)
            self.assertEqual(task['done_poll_since'], CASE_B_CLOSE)
        self.assertLessEqual(max(lags), ONE_POLL)
        self.assertNotIn('empty_reply_retry', [e['kind'] for e in self.store.events(limit=2000)])
        print('\ncase b: %d end times from 709.513 to 777.20, task_finished %.2f-%.2f s after the end'
              % (len(ends), min(lags), max(lags)))

    def test_case_b_old_backoff_is_tens_of_seconds_late(self):
        self.store.put('settings', dict(id='app', done_poll_max_tasks=0))
        lags = []
        for end in (CASE_B_OPEN, CASE_B_CONFIRMED):
            self.put_task(BASE + 344.81)
            task, finished = self.until_done(CASE_B_CLOSE, end)
            lags.append(self.done_lag(task, finished, end))
        self.assertGreater(min(lags), ONE_POLL)
        print('\ncase b, old backoff: task_finished %.2f s and %.2f s after the end' % tuple(lags))

    def test_turn_already_ended_at_remote_done_is_done_on_the_first_check(self):
        remote_done = BASE + 487.86
        self.put_task(BASE + 356.94, first_output_at=BASE + 361.48, remote_done_at=remote_done)
        task, finished = self.until_done(CASE_A_CLOSE, remote_done)
        lag = self.done_lag(task, finished, remote_done)
        self.assertEqual(len(self.adapter.recovers), 1)  # done on the first reconcile check
        self.assertEqual(self.gaps(CASE_A_CLOSE), [5])  # 5 s after the stream close, as before
        self.assertEqual(task['reconcile_checks'], 1)  # only the attempt's own check at the close failed
        self.assertLessEqual(lag, ONE_POLL)

    def test_approval_wait_keeps_old_backoff(self):
        self.put_task(BASE + 356.94, remote_done_at=BASE + 487.86)
        self.clock.now = BASE + 488.12
        delays = [self.fail(False, approval_seen={'key': 'x', 'at': BASE + 488})[0]]
        for _ in range(7):
            self.clock.now += delays[-1] + RECOVER_S
            delays.append(self.fail(True, awaiting_approval='nt_shell', approval_evidence=True,
                                    approval_seen={'key': 'x', 'at': BASE + 488})[0])
        self.assertEqual(delays, OLD_SEQUENCE)
        task = self.store.get('tasks', 't1')
        self.assertIn('Waiting for approval of nt_shell', task['error'])
        self.assertFalse(task['done_poll_fast'])

    def test_pending_tool_without_approval_evidence_keeps_old_backoff(self):
        self.put_task(BASE + 356.94, remote_done_at=BASE + 487.86)
        self.clock.now = BASE + 488.12
        delays = [self.fail(False, awaiting_approval='nt_shell')[0]]
        for _ in range(5):
            self.clock.now += delays[-1] + RECOVER_S
            delays.append(self.fail(True, awaiting_approval='nt_shell')[0])
        self.assertEqual(delays, OLD_SEQUENCE[:6])
        self.assertIn('A tool call in the coworker chat has not finished yet (nt_shell)',
                      self.store.get('tasks', 't1')['error'])

    def test_restart_recovery_uncertain_keeps_old_backoff(self):
        self.put_task(BASE + 356.94, state='uncertain')
        self.clock.now = BASE + 400
        delays = []
        for _ in range(8):
            delays.append(self.fail(True)[0])
            self.clock.now += delays[-1] + RECOVER_S
        self.assertEqual(delays, OLD_SEQUENCE)
        self.assertEqual(self.store.get('tasks', 't1')['done_poll_since'], 0)

    def test_cancelled_attempt_keeps_old_backoff(self):
        self.put_task(BASE + 356.94)
        cancel = threading.Event()
        cancel.set()
        self.clock.now = BASE + 488.12
        delays = [self.fail(False, cancel=cancel)[0]]
        for _ in range(4):
            self.clock.now += delays[-1] + RECOVER_S
            delays.append(self.fail(True)[0])
        self.assertEqual(delays, OLD_SEQUENCE[:5])

    def test_overdue_check_waits_300_s_and_stays_uncertain(self):
        self.put_task(BASE - 100000)
        self.clock.now = BASE + 488.12
        delay, task = self.fail(False)
        self.assertEqual(delay, 300)  # as on main (8ffd7af): the task limit only slows the checks down
        self.assertEqual(task['state'], 'uncertain')
        self.assertFalse(task.get('remote_unknown'))
        self.assertEqual((task['done_poll_since'], task['done_poll_fast']), (0, False))

    def test_slot_cap_keeps_requests_low(self):
        now = BASE + 488.12
        for i in range(engine_mod.DONE_POLL_MAX_TASKS):
            self.store.put('tasks', dict(id='o%d' % i, run_id='r1', role='executor', state='uncertain', deps=[], files=[],
                                         done_poll_fast=True, done_poll_since=now - 30, lease='x', worker_id='w9'))
        self.put_task(BASE + 356.94, remote_done_at=BASE + 487.86)
        self.clock.now = now
        delays = [self.fail(False)[0]]
        for _ in range(4):
            self.clock.now += delays[-1] + RECOVER_S
            delays.append(self.fail(True)[0])
        self.assertEqual(delays, OLD_SEQUENCE[:5])
        # Worst case of the fast poll: 6 tasks x 4 checks/min x 3 transcript reads, well inside 180/min.
        worst = engine_mod.DONE_POLL_MAX_TASKS * 60 / engine_mod.DONE_POLL_SECONDS * 3
        self.assertLessEqual(worst, HARD_LIMIT_PER_MINUTE / 2)

    def test_running_task_is_not_polled(self):
        task = self.put_task(BASE + 356.94, reconcile_at=0)
        self.clock.now = BASE + 500
        with self.engine.lock:
            self.engine._reconcile()
        self.assertEqual(self.adapter.recovers, [])
        self.assertEqual(self.store.get('tasks', 't1')['state'], 'running')
        self.assertEqual(self.store.get('tasks', 't1')['reconcile_at'], task['reconcile_at'])

    def test_delay_function_never_longer_than_old_backoff(self):
        for checks in range(1, 12):
            base = reconcile_backoff(checks)
            self.assertEqual(base, min(60, 5 * 2 ** min(checks - 1, 4)))
            self.assertEqual(reconcile_delay(checks, 1000.0), base)
            self.assertEqual(reconcile_delay(checks, 1000.0, 990.0, approval=True), base)
            self.assertEqual(reconcile_delay(checks, 1000.0, 990.0, slot_free=False), base)
            self.assertEqual(reconcile_delay(checks, 5000.0, 990.0), base)  # beyond the 1200 s window
            self.assertEqual(reconcile_delay(checks, 1000.0, 990.0), min(base, 15))
        self.assertEqual(reconcile_delay(5, 1000.0, 990.0, settings={'done_poll_seconds': 'bad'}), 15)
        self.assertEqual(reconcile_delay(5, 1000.0, 990.0, settings={'done_poll_seconds': 1}), 5)


if __name__ == '__main__':
    unittest.main()
