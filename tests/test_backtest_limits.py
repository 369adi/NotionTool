"""Backtest of the request cap, the coworker throttle, the coworker pause and the limit stop.

Runs ONLY against the simulated Notion (notiontool.fake_notion) with a store in a temp folder.
Every test starts behind a safety guard: the real transports (native HTTP, Edge), the real
adapter client, urllib and sockets raise AssertionError('real Notion touched ...'), sqlite may
only open files inside the temp folder (so data/notiontool.db is never opened), and nothing may
connect to any port (8790 included). The limit code under test is the real code:
network_budget.NetworkBudget, token_budget.burst_state/refuse_if_burst and NotionAdapter.run.
"""
import os
import shutil
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from notiontool import fake_notion, token_budget
from notiontool.edge_transport import EdgeTransport
from notiontool.engine import PendingRemote, RateLimited
from notiontool.fake_notion import FakeNotionAdapter, FakeState, FakeTransport, seed_scenario
from notiontool.network_budget import HARD_LIMIT_PER_MINUTE, WINDOW_SECONDS, NetworkBudget, clamp_limit
from notiontool.notion import NotionAdapter, NotionClient
from notiontool.store import Store
from notiontool.vendor.transport import NativeNotionTransport

ROOT = Path(__file__).resolve().parents[1]
REAL_DBS = {os.path.normcase(os.path.realpath(ROOT / 'data' / 'notiontool.db'))}
if ROOT.parent.name == 'worktrees':  # <project>/work/worktrees/<task>: also guard the main project's DB
    REAL_DBS.add(os.path.normcase(os.path.realpath(ROOT.parents[2] / 'data' / 'notiontool.db')))
FAKE_ACCOUNT_KEYS = {account[0] for account in fake_notion.ACCOUNTS}
APP_PORT = 8790
LIMITS = token_budget.BURST_DEFAULTS


class FakeClock:
    """Simulated time for NetworkBudget: sleep() only moves the clock."""

    def __init__(self, start):
        self.t = float(start)

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += max(float(seconds), 0.001)


class BacktestLimits(unittest.TestCase):

    # ---- safety guard -------------------------------------------------------------------------
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='nt_backtest_'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.tmp_real = os.path.normcase(os.path.realpath(self.tmp))
        self.touched = []
        self.guard_patches()
        state_path = self.tmp / 'fake.json'
        env = mock.patch.dict(os.environ, {'NOTIONTOOL_TEST_INSTANCE': '1', 'NOTIONTOOL_FAKE_STATE': str(state_path)})
        env.start()
        self.addCleanup(env.stop)
        self.state = FakeState(path=str(state_path))
        self.store = Store(self.tmp / 'app')
        self.addCleanup(self.store.close)
        self.adapter = FakeNotionAdapter(self.store, state=self.state, force=True)
        self.adapter.refresh_on_login = False
        self.adapter.stream_turn_poll_s = 0.05
        # The fake cannot answer the model checks (see the result file); only these two are replaced.
        self.adapter.verify_response_model = mock.Mock(return_value=[])
        self.adapter.ensure_thread_model = mock.Mock(return_value=None)
        seed_scenario(self.store, self.state)
        self.coworker = self.store.get('workers', 'ready:' + self.state.threads_of('ready', 'coworker')[0]['id'])
        self.main = self.store.get('workers', 'ready:' + self.state.threads_of('ready', 'main')[0]['id'])
        self.assertEqual(self.coworker['purpose'], 'worker')
        self.assertEqual(self.main['purpose'], 'main')
        self.assert_only_fake_accounts()

    def tearDown(self):
        # Runs before the cleanups: the guards are still active and the temp store is still open.
        self.assert_nothing_real_touched()

    def guard_patches(self):
        def refuse(name):
            def guard(*args, **kwargs):
                self.touched.append(name)
                raise AssertionError('real Notion touched: ' + name)
            return guard

        def refuse_socket(name):
            def guard(*args, **kwargs):
                address = args[1] if len(args) > 1 and isinstance(args[0], socket.socket) else (args[0] if args else None)
                port = address[1] if isinstance(address, tuple) and len(address) > 1 else None
                self.touched.append('%s %r%s' % (name, address, ' (NotionTool app port)' if port == APP_PORT else ''))
                raise AssertionError('real network touched: %s %r' % (name, address))
            return guard

        original_call = NotionClient.call

        def guarded_call(client, *args, **kwargs):
            if not isinstance(client.transport, FakeTransport):
                self.touched.append('NotionClient.call on ' + type(client.transport).__name__)
                raise AssertionError('real Notion touched: NotionClient.call')
            return original_call(client, *args, **kwargs)

        original_connect = sqlite3.connect

        def guarded_connect(database, *args, **kwargs):
            if str(database) != ':memory:':
                path = os.path.normcase(os.path.realpath(str(database)))
                if path in REAL_DBS or not path.startswith(self.tmp_real):
                    self.touched.append('sqlite ' + path)
                    raise AssertionError('real DB touched: ' + path)
            return original_connect(database, *args, **kwargs)

        patches = [
            mock.patch.object(NativeNotionTransport, 'request', refuse('NativeNotionTransport.request')),
            mock.patch.object(NativeNotionTransport, 'stream', refuse('NativeNotionTransport.stream')),
            mock.patch.object(NativeNotionTransport, '_dispatch', refuse('NativeNotionTransport._dispatch')),
            mock.patch.object(EdgeTransport, 'request', refuse('EdgeTransport.request')),
            mock.patch.object(EdgeTransport, 'stream', refuse('EdgeTransport.stream')),
            mock.patch.object(NotionAdapter, 'client', refuse('NotionAdapter.client (real)')),
            mock.patch.object(NotionAdapter, 'edge_transport', refuse('NotionAdapter.edge_transport (real)')),
            mock.patch.object(NotionClient, 'call', guarded_call),
            mock.patch.object(urllib.request, 'urlopen', refuse('urllib.request.urlopen')),
            mock.patch.object(urllib.request.OpenerDirector, 'open', refuse('urllib OpenerDirector.open')),
            mock.patch.object(socket, 'create_connection', refuse_socket('socket.create_connection')),
            mock.patch.object(socket.socket, 'connect', refuse_socket('socket.connect')),
            mock.patch.object(socket.socket, 'connect_ex', refuse_socket('socket.connect_ex')),
            mock.patch.object(sqlite3, 'connect', guarded_connect),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def assert_nothing_real_touched(self):
        self.assertEqual(self.touched, [], 'a real transport, socket or DB was used')
        self.assert_only_fake_accounts()

    def assert_only_fake_accounts(self):
        # data/notiontool.db may not be opened, so its ids are not read: instead every account the
        # temp store and the fake know must be one of the simulated account keys.
        self.assertTrue(os.path.normcase(os.path.realpath(self.store.directory)).startswith(self.tmp_real))
        accounts = {a['id'] for a in self.store.all('accounts')}
        self.assertTrue(accounts and accounts <= FAKE_ACCOUNT_KEYS, accounts)
        self.assertTrue({w.get('account_id') for w in self.store.all('workers')} <= FAKE_ACCOUNT_KEYS)
        self.assertTrue(set(self.state.load()['accounts']) <= FAKE_ACCOUNT_KEYS)
        self.assertTrue(all(a.get('simulated') for a in self.store.all('accounts')))

    # ---- helpers ------------------------------------------------------------------------------
    def sends(self):
        return len(self.state.calls('runInferenceTranscript'))

    def events(self, kind):
        return [e for e in self.store.events(limit=5000) if e['kind'] == kind]

    def turn(self, worker, task_id):
        """One turn through the real NotionAdapter.run on the fake: (outcome, detail, sends made)."""
        task = dict(id=task_id, run_id='r1', role='executor', lease='assignment', state='running')
        self.store.put('tasks', task)
        before = self.sends()
        try:
            outcome = ('reply', self.adapter.run(worker, task, 'Say hello', threading.Event(), lambda t: None))
        except RateLimited as exc:
            outcome = ('refused', exc)
        except PendingRemote:
            outcome = ('pending', None)
        return outcome[0], outcome[1], self.sends() - before

    def charge(self, worker, tokens, at):
        token_budget.charge(self.store, worker, input_tokens=tokens, now=at)

    def assert_refused_without_send(self, worker, task_id, level):
        events_before = len(self.events('token_burst_' + level))
        outcome, exc, sent = self.turn(worker, task_id)
        self.assertEqual(outcome, 'refused')
        self.assertEqual(sent, 0, 'a coworker turn reached the fake while ' + level)
        self.assertTrue(exc.free)
        self.assertIn('Coworkers slowed down', str(exc))
        self.assertEqual(len(self.events('token_burst_' + level)), events_before + 1)
        self.assertEqual(self.store.get('tasks', task_id).get('sent_at'), None)

    def assert_sent(self, worker, task_id):
        outcome, detail, sent = self.turn(worker, task_id)
        self.assertNotEqual(outcome, 'refused', detail)
        self.assertEqual(sent, 1)
        return outcome, detail

    # ---- 1) request cap -----------------------------------------------------------------------
    def test_1_request_cap_180_per_sliding_minute(self):
        clock = FakeClock(100_000.0)
        budget = NetworkBudget(self.store, clock=clock.now, sleep=clock.sleep)
        self.assertEqual(HARD_LIMIT_PER_MINUTE, 180)
        self.assertEqual(budget.limit_per_minute, 180)
        flood_at = clock.t
        starts = []
        for _ in range(400):  # 400 requests asked for at once
            ticket = budget.acquire(priority='rest', source='fake-flood')
            starts.append(clock.t)
            budget.release(ticket, 'ok', latency=0.05)
        self.assertEqual(len(starts), 400)
        low, peak = 0, 0
        for i, start in enumerate(starts):  # the budget's own window: starts newer than now - 60 s
            while start - starts[low] >= WINDOW_SECONDS:
                low += 1
            peak = max(peak, i - low + 1)
        self.assertEqual(peak, 180, 'more than 180 starts inside one sliding 60 s window')
        self.assertEqual(sum(1 for s in starts if s < flood_at + WINDOW_SECONDS), 180)  # the rest waited
        self.assertGreaterEqual(starts[180] - starts[0], WINDOW_SECONDS)
        self.assertGreaterEqual(starts[360] - starts[180], WINDOW_SECONDS)
        self.assertGreaterEqual(budget.window_waits, 2)
        held = self.events('request_window_wait')
        self.assertTrue(held and all(e['limit'] == 180 and e['seconds'] > 0 for e in held))
        # Saved settings may only lower the cap, never raise it.
        self.assertEqual(clamp_limit(1000), 180)
        self.store.put('settings', dict(self.store.get('settings', 'network_budget', {}) or {}, id='network_budget',
                                        limit_per_minute=500))
        self.assertEqual(NetworkBudget(self.store, clock=clock.now, sleep=clock.sleep).limit_per_minute, 180)

    # ---- 2) throttle --------------------------------------------------------------------------
    def test_2_throttle_spaces_coworker_turns(self):
        spacing = LIMITS['burst_spacing_s']
        t0 = 2_000_000.0
        self.charge(self.coworker, LIMITS['burst_soft_15m'] + 200_000, t0 - 30)
        self.assertEqual(token_budget.burst_state(self.store, t0)['level'], 'throttle')
        allowed, refused = [], 0
        for second in range(0, 121):  # two simulated minutes, a coworker turn asked for every second
            now = t0 + second
            self.assertIsNone(token_budget.refuse_if_burst(self.store, self.main, now=now))  # Main never slowed
            try:
                token_budget.refuse_if_burst(self.store, self.coworker, now=now)
            except RateLimited as exc:
                refused += 1
                self.assertTrue(exc.free)
                continue
            allowed.append(now)
            token_budget.charge_send(self.store, self.coworker, 'turn %d' % second, now=now)
        gaps = [b - a for a, b in zip(allowed, allowed[1:])]
        self.assertEqual(len(allowed), 7, allowed)
        self.assertTrue(gaps and min(gaps) >= spacing, gaps)
        self.assertEqual(refused, 121 - len(allowed))
        self.assertEqual(len(self.events('token_burst_throttle')), refused)
        # The same throttle on the real send path: right after a coworker turn nothing is sent.
        now = time.time()
        self.charge(self.coworker, LIMITS['burst_soft_15m'] + 200_000, now - 30)
        token_budget.charge_send(self.store, self.coworker, 'just sent', now=now)
        self.assert_refused_without_send(self.coworker, 't_throttled', 'throttle')
        self.assert_sent(self.main, 't_main')

    # ---- 3) pause -----------------------------------------------------------------------------
    def test_3a_pause_over_hard_15m(self):
        self.charge(self.coworker, LIMITS['burst_hard_15m'] + 100_000, time.time() - 60)
        self.assertEqual(token_budget.burst_state(self.store)['level'], 'pause')
        self.assert_refused_without_send(self.coworker, 't_cw1', 'pause')
        self.assert_refused_without_send(self.coworker, 't_cw2', 'pause')
        self.assert_sent(self.main, 't_main')  # the Main is still allowed

    def test_3b_pause_over_hard_60m(self):
        now = time.time()
        for minutes in (20, 30, 40, 50):  # 18.4M in 60 min, nothing in the last 15 min
            self.charge(self.coworker, 4_600_000, now - minutes * 60)
        state = token_budget.burst_state(self.store)
        self.assertEqual((state['level'], state['tokens_15m']), ('pause', 0))
        self.assertGreaterEqual(state['tokens_60m'], LIMITS['burst_hard_60m'])
        self.assert_refused_without_send(self.coworker, 't_cw', 'pause')
        self.assert_sent(self.main, 't_main')

    # ---- 4) recovery --------------------------------------------------------------------------
    def test_4_turns_allowed_again_after_the_rolling_window(self):
        t0 = 3_000_000.0
        self.charge(self.coworker, LIMITS['burst_hard_15m'] + 100_000, t0)
        level = lambda now: token_budget.burst_state(self.store, now)['level']
        self.assertEqual([level(t0 + 1), level(t0 + 899)], ['pause', 'pause'])
        with self.assertRaises(RateLimited):
            token_budget.refuse_if_burst(self.store, self.coworker, now=t0 + 899)
        self.assertEqual(level(t0 + 901), 'ok')  # 15 min passed, 8.1M < 18M in 60 min
        self.assertIsNone(token_budget.refuse_if_burst(self.store, self.coworker, now=t0 + 901))
        # 60-minute pause: clears only when the hour has rolled past.
        for _ in range(4):
            self.charge(self.coworker, 4_600_000, t0 + 1000)
        self.assertEqual([level(t0 + 1001), level(t0 + 1000 + 901), level(t0 + 1000 + 3599)],
                         ['pause', 'pause', 'pause'])
        self.assertEqual(level(t0 + 1000 + 3601), 'ok')
        self.assertIsNone(token_budget.refuse_if_burst(self.store, self.coworker, now=t0 + 1000 + 3601))
        # On the real send path: tokens that are older than 15 min no longer stop a coworker turn.
        self.charge(self.coworker, LIMITS['burst_hard_15m'] + 100_000, time.time() - 950)
        self.assertEqual(token_budget.burst_state(self.store)['level'], 'ok')
        self.assert_sent(self.coworker, 't_cw_back')

    # ---- 5) limit stop of running coworker turns ----------------------------------------------
    def test_5_limit_stop_cancels_running_coworker_turns(self):
        source = (ROOT / 'notiontool' / 'engine.py').read_text(encoding='utf-8')
        if "'limit_stop'" not in source and '"limit_stop"' not in source:
            self.skipTest('limit_stop not built yet')
        from notiontool.engine import Engine
        engine = Engine(self.store, self.adapter)
        if not hasattr(engine, '_limit_stop'):
            self.skipTest('limit_stop exists but Engine._limit_stop (design interface) is missing')
        self.state.set_fault(self.coworker['thread_id'], 'hang', seconds=30)
        cancels, results = {}, {}
        for worker, task_id in ((self.coworker, 't_cw'), (self.main, 't_main')):
            task = dict(id=task_id, run_id='r1', role='executor', lease='assignment', state='running')
            self.store.put('tasks', task)
            cancels[task_id] = cancel = threading.Event()

            def go(worker=worker, task=task, cancel=cancel):
                try:
                    results[task['id']] = ('reply', self.adapter.run(worker, task, 'Say hello', cancel, lambda t: None))
                except Exception as exc:  # noqa: BLE001 - the outcome is asserted below
                    results[task['id']] = (type(exc).__name__, exc)
            thread = threading.Thread(target=go, daemon=True)
            thread.start()
            engine.inflight[object()] = dict(task_id=task_id, lease='assignment', worker_id=worker['id'],
                                             cancel=cancel, recovery=False)
        deadline = time.time() + 5
        while self.sends() < 2 and time.time() < deadline:
            time.sleep(0.02)
        self.charge(self.coworker, LIMITS['burst_hard_15m'] + 100_000, time.time())
        started = time.time()
        stopped = engine._limit_stop(engine.clock())
        self.assertIn('t_cw', stopped)
        self.assertTrue(cancels['t_cw'].is_set())
        self.assertFalse(cancels['t_main'].is_set())
        while 't_cw' not in results and time.time() - started < 2:
            time.sleep(0.02)
        self.assertIn('t_cw', results, 'the running coworker turn did not stop at once')
        self.assertEqual(results['t_cw'][0], 'PendingRemote')
        self.assertEqual(len(self.events('limit_stop')), 1)


if __name__ == '__main__':
    unittest.main()
