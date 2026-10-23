import re
import unittest
from pathlib import Path

from notiontool import bridge, engine, file_leases, task_states as ts

ENGINE_SOURCE = Path(engine.__file__).read_text(encoding='utf-8')


class FakeStore:
    def __init__(self):
        self.events = []

    def event(self, kind, run_id='', *, commit=True, **fields):
        self.events.append(dict(kind=kind, run_id=run_id, commit=commit, **fields))


class RaisingStore:
    def event(self, *args, **kwargs):
        raise RuntimeError('store is down')


def task(state='running', **extra):
    return dict(id='t1', run_id='r1', state=state, **extra)


class TableTests(unittest.TestCase):
    def test_every_state_has_a_row_and_every_target_is_declared(self):
        self.assertEqual(set(ts.TRANSITIONS), set(ts.TASK_STATES))
        self.assertEqual(len(ts.TASK_STATES), len(set(ts.TASK_STATES)))
        for old, targets in ts.TRANSITIONS.items():
            self.assertTrue(targets <= set(ts.TASK_STATES), old)

    def test_states_the_engine_and_leases_use_are_declared(self):
        used = set(re.findall(r"set_state\(self\.store, task, '([a-z]+)'", ENGINE_SOURCE))
        used |= set(re.findall(r"else '([a-z]+)'", ENGINE_SOURCE)) & set(ts.TASK_STATES)
        self.assertTrue(used)
        self.assertTrue(used <= set(ts.TASK_STATES), used - set(ts.TASK_STATES))
        self.assertTrue(set(ts.RUN_LIVE_STATES) <= set(ts.TASK_STATES))
        self.assertTrue(ts.LEASE_LIVE_STATES <= set(ts.TASK_STATES))

    def test_known_moves(self):
        for old, new in [('queued', 'running'), ('running', 'done'), ('running', 'uncertain'), ('uncertain', 'done'),
                         ('retry', 'running'), ('blocked', 'queued'), ('failed', 'queued'), ('running', 'cancelling'),
                         ('cancelling', 'cancelled'), ('failed', 'skipped'), ('done', 'retry')]:
            self.assertTrue(ts.is_legal(old, new), (old, new))

    def test_unknown_or_dead_end_moves_are_illegal(self):
        for old, new in [('done', 'running'), ('cancelled', 'running'), ('skipped', 'queued'), ('queued', 'done'),
                         ('running', 'nonsense'), ('running', None)]:
            self.assertFalse(ts.is_legal(old, new), (old, new))

    def test_first_state_and_repeat_are_legal(self):
        self.assertTrue(ts.is_legal(None, 'queued'))
        self.assertTrue(ts.is_legal('', 'queued'))
        self.assertTrue(ts.is_legal('uncertain', 'uncertain'))
        self.assertTrue(ts.is_legal('cancelled', 'cancelled'))


class SetStateTests(unittest.TestCase):
    def test_legal_move_writes_and_logs_nothing(self):
        store, t = FakeStore(), task('running')
        self.assertTrue(ts.set_state(store, t, 'done', 'finished'))
        self.assertEqual(t['state'], 'done')
        self.assertEqual(store.events, [])

    def test_illegal_move_logs_once_and_still_goes_through(self):
        store, t = FakeStore(), task('done')
        self.assertFalse(ts.set_state(store, t, 'running', 'test move'))
        self.assertEqual(t['state'], 'running')
        self.assertEqual(len(store.events), 1)
        event = store.events[0]
        self.assertEqual(event['kind'], 'illegal_transition')
        self.assertEqual(event['run_id'], 'r1')
        self.assertEqual(event['task_id'], 't1')
        self.assertEqual(event['old'], 'done')
        self.assertEqual(event['new'], 'running')
        self.assertIn('test_task_states.py', event['caller'])
        self.assertIn('test_illegal_move_logs_once_and_still_goes_through', event['caller'])
        self.assertFalse(event['commit'])

    def test_explicit_caller_is_used(self):
        store, t = FakeStore(), task('cancelled')
        ts.set_state(store, t, 'running', caller='x.py:y')
        self.assertEqual(store.events[0]['caller'], 'x.py:y')

    def test_extra_fields_are_applied_also_on_an_illegal_move(self):
        store, t = FakeStore(), task('cancelled')
        ts.set_state(store, t, 'retry', 'r', next_at=5, error='')
        self.assertEqual((t['state'], t['next_at'], t['error']), ('retry', 5, ''))

    def test_never_raises(self):
        t = task('done')
        self.assertFalse(ts.set_state(RaisingStore(), t, 'running'))
        self.assertEqual(t['state'], 'running')
        t = task('done')
        ts.set_state(object(), t, 'running')  # a store without event()
        self.assertEqual(t['state'], 'running')
        t = {'id': 'x'}  # no state yet
        self.assertTrue(ts.set_state(FakeStore(), t, 'queued'))
        self.assertEqual(t['state'], 'queued')
        t = task('running')
        ts.set_state(FakeStore(), t, 'not-a-state')  # unknown target: logged, written
        self.assertEqual(t['state'], 'not-a-state')


class LiveSetTests(unittest.TestCase):
    def test_run_live_states_are_unchanged_for_engine_and_bridge(self):
        self.assertEqual(ts.RUN_LIVE_STATES, ('running', 'uncertain'))
        self.assertEqual(engine.LIVE_TASK_STATES, ('running', 'uncertain'))
        self.assertEqual(bridge.LIVE_TASK_STATES, ('running', 'uncertain'))
        self.assertIs(engine.LIVE_TASK_STATES, ts.RUN_LIVE_STATES)
        self.assertIs(bridge.LIVE_TASK_STATES, ts.RUN_LIVE_STATES)

    def test_lease_live_states_are_unchanged_for_file_leases(self):
        self.assertEqual(ts.LEASE_LIVE_STATES, frozenset({'running', 'cancelling', 'uncertain', 'checkpoint', 'preparing'}))
        self.assertIs(file_leases.LIVE_TASK_STATES, ts.LEASE_LIVE_STATES)

    def test_the_two_sets_stay_different(self):
        self.assertNotEqual(set(ts.RUN_LIVE_STATES), set(ts.LEASE_LIVE_STATES))
        self.assertTrue(set(ts.RUN_LIVE_STATES) < ts.LEASE_LIVE_STATES)


class EngineSourceTests(unittest.TestCase):
    def test_no_direct_task_state_write_is_left_in_engine(self):
        self.assertEqual(re.findall(r"task\.update\(\s*state=", ENGINE_SOURCE), [])
        self.assertEqual(re.findall(r"\btask\[['\"]state['\"]\]\s*=(?!=)", ENGINE_SOURCE), [])
        self.assertGreaterEqual(ENGINE_SOURCE.count('set_state(self.store, task,'), 35)


if __name__ == '__main__':
    unittest.main()
