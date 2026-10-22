"""Build 23/24 regressions: empty-reply proof, unknown session state, pending tool, ai_disabled cancel."""
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from notiontool import engine as engine_mod
from notiontool import turn_watch
from notiontool.engine import EMPTY_PROOF_STATES, Engine


class FakeStore:
    def __init__(self, **tables):
        self.tables = {name: dict(rows) for name, rows in tables.items()}
        self.events = []

    def get(self, table, key, default=None):
        return self.tables.get(table, {}).get(key, default)

    def put(self, table, row):
        self.tables.setdefault(table, {})[row['id']] = row

    def event(self, kind, *args, **kw):
        self.events.append(kind)


class EmptyProofTests(unittest.TestCase):
    def test_only_proven_states(self):
        self.assertEqual(tuple(EMPTY_PROOF_STATES), ('done_empty', 'done_error'))

    def test_unproven_states_never_retry(self):
        for exc in (None, SimpleNamespace(state=''), SimpleNamespace(state='uncertain'),
                    SimpleNamespace(state='running'), SimpleNamespace(state='unknown')):
            self.assertFalse(Engine._empty_remote_reply(SimpleNamespace(), {}, {}, {}, exc), exc)


class TurnWatchTests(unittest.TestCase):
    def test_unknown_session_is_never_interrupted(self):
        transcript = {'patches': [{'op': 'put', 'entity': {'id': 'u1', 'sequence': 1, 'type': 'user_message'}}]}
        with mock.patch.object(turn_watch, 'session_state', return_value='unknown'):
            self.assertIsNone(turn_watch.interrupted_turn(transcript, now=10_000.0))

    def test_pending_tool(self):
        call = {'id': 'c1', 'type': 'tool_use', 'tool_use_id': 'x'}
        result = {'id': 'r1', 'type': 'tool_result', 'tool_use_id': 'x'}
        with mock.patch.object(turn_watch, '_role', side_effect=lambda e: 'tool' if e else ''):
            self.assertTrue(turn_watch._pending_tool(call, [call]))
            self.assertFalse(turn_watch._pending_tool(call, [call, result]))
            self.assertFalse(turn_watch._pending_tool(result, [call, result]))


class CancelAiDisabledTests(unittest.TestCase):
    def make(self, account_state):
        store = FakeStore(
            runs={'r1': {'id': 'r1', 'state': 'executing'}},
            accounts={'acc': {'id': 'acc', 'state': account_state}},
            workers={'acc:w': {'id': 'acc:w', 'account_id': 'acc'}},
            tasks={'t1': {'id': 't1', 'state': 'uncertain', 'worker_id': 'acc:w', 'lease': 'L',
                          'remote_unknown': True, 'reconcile_at': 99.0},
                   't2': {'id': 't2', 'state': 'blocked', 'worker_id': 'acc:w'},
                   't3': {'id': 't3', 'state': 'queued'}})
        released = []
        fake = SimpleNamespace(lock=threading.Lock(), store=store, inflight={},
                               tasks=lambda run_id: list(store.tables['tasks'].values()),
                               _release=lambda wid, lease: released.append((wid, lease)))
        fake._worker_ai_disabled = lambda task: Engine._worker_ai_disabled(fake, task)
        return fake, store, released

    def test_ai_disabled_tasks_end_with_run(self):
        fake, store, released = self.make('ai_disabled')
        Engine.cancel(fake, 'r1')
        tasks = store.tables['tasks']
        self.assertEqual([tasks[k]['state'] for k in ('t1', 't2', 't3')], ['cancelled'] * 3)
        self.assertNotIn('remote_unknown', tasks['t1'])
        self.assertNotIn('reconcile_at', tasks['t1'])
        self.assertIn(('acc:w', 'L'), released)
        self.assertIn('run_cancelled', store.events)

    def test_healthy_account_keeps_reconciling(self):
        fake, store, released = self.make('ready')
        Engine.cancel(fake, 'r1')
        tasks = store.tables['tasks']
        self.assertEqual((tasks['t1']['state'], tasks['t2']['state']), ('uncertain', 'blocked'))
        self.assertTrue(tasks['t1']['remote_unknown'])
        self.assertEqual(released, [])


if __name__ == '__main__':
    unittest.main()
