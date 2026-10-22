"""Automatic Coworker team growth (team_grow.py). Fake clock, fake adapter and a synchronous job runner:
nothing is sent to Notion."""
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from notiontool.engine import Engine
from notiontool.server import Application
from notiontool.store import Store
from notiontool.team_grow import BACKOFF_S, CHECK_S, TeamGrower, pending_roles, usable_count


class Clock:
    def __init__(self): self.now = 1000.0
    def __call__(self): return self.now


class FakeBudget:
    def __init__(self): self.info = dict(paused=False, cooldown_seconds=0)
    def status(self): return dict(self.info)


class FakeAdapter:
    def __init__(self, store):
        self.store = store; self.calls = []; self.fail = None; self.network_budget = FakeBudget()
    def ensure_team(self, ident, create_main=False):
        self.calls.append(ident)
        if self.fail:
            raise ValueError(self.fail)
        self.main_flags = getattr(self, 'main_flags', []) + [create_main]
        n = len([w for w in self.store.all('workers') if w['account_id'] == ident and w.get('purpose') == 'worker'])
        for i in range(n, 3):
            self.store.put('workers', dict(id='w%d' % i, account_id=ident, purpose='worker', join_state='joined', enabled=True, state='ready', thread_id='t%d' % i))
        return {'account_id': ident}


class Jobs:
    """Like Application.job: one job per account; run=False keeps the job open (still running)."""
    def __init__(self): self.run = True; self.open = []; self.busy = set(); self.titles = []
    def __call__(self, title, fn, ident):
        if ident in self.busy:
            raise ValueError('An operation is already running for this account')
        self.titles.append(title)
        if self.run:
            try: fn()
            except Exception: pass
        else:
            self.open.append(fn)
        return {'job_id': 'j'}


def worker(ident, **extra):
    doc = dict(id=ident, account_id='a1', purpose='worker', join_state='joined', enabled=True, state='ready', thread_id='t-' + ident)
    doc.update(extra)
    return doc


class TeamGrowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.store.put('settings', dict(id='app', per_account=3, max_workers=40))
        self.store.put('accounts', dict(id='a1', state='ready', slot=1, team_threads={'main': {'id': 'm', 'pending': False}}))
        self.store.put('workers', dict(id='m', account_id='a1', purpose='main', join_state='joined', enabled=True, state='ready', thread_id='m'))
        self.adapter = FakeAdapter(self.store)
        self.clock = Clock()
        self.jobs = Jobs()
        self.grower = TeamGrower(self.store, self.adapter, self.jobs, clock=self.clock)

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def kinds(self):
        return [e.get('kind') or e.get('type') for e in self.store.events()]

    def test_usable_counts_only_joined_enabled_live_welcome_coworkers(self):
        workers = [worker('a'), worker('b', enabled=False), worker('c', join_state='pending'), worker('d', state='missing'),
                   worker('e', state='retired'), worker('f', retired=True), worker('g', purpose='main'), worker('h', account_id='a2')]
        self.assertEqual(usable_count(workers, 'a1'), 1)

    def test_startup_check_grows_a_short_team_and_logs_done(self):
        self.store.put('workers', worker('w0'))
        self.assertEqual(self.grower.maybe_check(), ['a1'])
        self.assertEqual(self.adapter.calls, ['a1'])
        self.assertEqual(self.jobs.titles, ['Grow Welcome Coworkers to 3'])
        self.assertIn('team_grow_started', self.kinds()); self.assertIn('team_grow_done', self.kinds())
        self.assertEqual(self.grower.public(), dict(account='a1', target=3, usable=3, growing=False, last_error='', waiting='', retry_at=0))

    def test_missing_main_is_created_even_with_a_full_team(self):
        self.store.delete('workers', 'm')
        for i in range(3):
            self.store.put('workers', worker('w%d' % i))
        self.assertEqual(self.grower.check(), ['a1'])
        self.assertEqual(self.jobs.titles, ['Create the Main and 3 Welcome Coworkers'])
        self.assertEqual(self.adapter.main_flags, [True])

    def test_full_team_or_not_ready_account_starts_nothing(self):
        for i in range(3):
            self.store.put('workers', worker('w%d' % i))
        self.store.put('accounts', dict(id='a2', state='signin', team_threads={}))
        self.assertEqual(self.grower.check(), [])
        self.assertEqual(self.adapter.calls, [])

    def test_checks_again_only_every_five_minutes(self):
        self.adapter.fail = None; self.jobs.run = False
        self.grower.maybe_check(); self.jobs.open.clear(); self.grower.growing.clear()
        self.clock.now += CHECK_S - 1
        self.assertEqual(self.grower.maybe_check(), [])
        self.clock.now += 1
        self.assertEqual(self.grower.maybe_check(), ['a1'])

    def test_one_job_per_account_at_a_time(self):
        self.jobs.run = False
        self.assertEqual(self.grower.check(), ['a1'])
        self.assertTrue(self.grower.public()['growing'])
        self.assertEqual(self.grower.check(), [])
        self.jobs.open.pop()()
        self.assertFalse(self.grower.public()['growing'])

    def test_busy_account_operation_defers_without_failure(self):
        self.jobs.busy.add('a1')
        self.assertEqual(self.grower.check(), [])
        state = self.grower.public()
        self.assertFalse(state['growing']); self.assertEqual(state['last_error'], '')
        self.jobs.busy.clear()
        self.assertEqual(self.grower.check(), ['a1'])

    def test_failure_backs_off_fifteen_minutes(self):
        self.adapter.fail = 'Notion said no'
        self.grower.check()
        self.assertIn('team_grow_failed', self.kinds())
        state = self.grower.public()
        self.assertIn('Notion said no', state['last_error']); self.assertFalse(state['growing'])
        self.clock.now += CHECK_S
        self.assertEqual(self.grower.check(), [])
        self.clock.now += BACKOFF_S - CHECK_S
        self.adapter.fail = None
        self.assertEqual(self.grower.check(), ['a1'])
        self.assertEqual(self.grower.public()['last_error'], '')

    def test_pending_ledger_blocks_growth_i5(self):
        account = self.store.get('accounts', 'a1')
        account['team_threads']['worker1'] = {'id': 'x', 'pending': True}
        self.store.put('accounts', account)
        self.assertEqual(pending_roles(account), ['worker1'])
        self.assertEqual(self.grower.check(), [])
        self.assertEqual(self.adapter.calls, [])
        self.assertIn('unconfirmed', self.grower.public()['waiting'])

    def test_network_budget_pause_and_cooldown_block_growth(self):
        self.adapter.network_budget.info['paused'] = True
        self.assertEqual(self.grower.check(), [])
        self.adapter.network_budget.info.update(paused=False, cooldown_seconds=30)
        self.assertEqual(self.grower.check(), [])
        self.adapter.network_budget.info['cooldown_seconds'] = 0
        self.assertEqual(self.grower.check(), ['a1'])

    def test_open_context_rotation_blocks_growth(self):
        self.store.put('workers', worker('w0'))
        self.store.put('rotations', dict(id='w0', state='submitted'))
        self.assertEqual(self.grower.check(), [])
        self.assertIn('rotation', self.grower.public()['waiting'])
        self.store.delete('rotations', 'w0')
        self.assertEqual(self.grower.check(), ['a1'])

    def test_engine_loop_calls_the_grower_and_survives_errors(self):
        stop = SimpleNamespace(calls=0)
        def wait(_):
            stop.calls += 1
            return stop.calls > 2
        grower = Mock(); grower.maybe_check.side_effect = [RuntimeError('x'), []]
        # The loop now checks stop_event.is_set() and waits on wake_event (0.25 s); each wait counts one round.
        fake = SimpleNamespace(stop_event=SimpleNamespace(wait=wait, is_set=lambda: stop.calls >= 3),
                               wake_event=SimpleNamespace(wait=wait, clear=lambda: None),
                               tick=lambda: None, team_grower=grower, store=Mock())
        Engine._loop(fake)
        self.assertEqual(grower.maybe_check.call_count, 2)
        # Name and text: the bare type name hid the missing chat_pool.name_coworkers (live 22 Oct).
        fake.store.event.assert_called_once_with('team_grow_check_failed', message='RuntimeError: x')

    def test_state_team_from_application(self):
        self.assertIsNone(Application.team_state(SimpleNamespace()))
        self.assertEqual(Application.team_state(SimpleNamespace(team_grower=self.grower), 'a1')['target'], 3)
        broken = SimpleNamespace(team_grower=SimpleNamespace(public=Mock(side_effect=RuntimeError)))
        self.assertIsNone(Application.team_state(broken))


if __name__ == '__main__':
    unittest.main()
