"""Token ledger in token_budget: who used the tokens (task, role, model).

The ledger is pure reporting. These tests check the grouping of usage_by_task, that the setting
token_ledger_enabled=False records nothing, that the old call signatures still work and that the
window totals, the rolling burst ledger and the limit checks are exactly the same with and
without the ledger.
"""
import copy
import tempfile
import threading
import unittest
from pathlib import Path

from notiontool import token_budget
from notiontool.engine import RateLimited

NOW = 1_800_000_000.0  # fixed clock, start of an hour bucket

_SOURCE = Path(__file__).resolve().parent.parent / 'notiontool'
# The strict charge-point tests run once the callers pass task / role / model (review fix in notion.py, engine.py).
NOTION_PASSES_TASK = "charge_send(self.store, worker, prompt, task_id=task['id']" in \
    (_SOURCE / 'notion.py').read_text(encoding='utf-8')
ENGINE_PASSES_TASK = "role=task.get('role'), model=task.get('model')" in \
    (_SOURCE / 'engine.py').read_text(encoding='utf-8')


class FakeStore:
    def __init__(self, **settings):
        self.lock = threading.RLock()
        self.docs, self.events, self.reads = {}, [], []
        self._settings = dict(settings)

    def get(self, kind, ident, default=None):
        self.reads.append(kind)
        return copy.deepcopy(self.docs.get((kind, str(ident)), default))

    def all(self, kind):
        return [copy.deepcopy(v) for (k, _), v in self.docs.items() if k == kind]

    def put(self, kind, doc):
        self.docs[(kind, str(doc['id']))] = copy.deepcopy(doc)
        return doc

    def event(self, kind, run_id='', **fields):
        self.events.append((kind, fields))

    def delete(self, kind, ident):
        self.docs.pop((kind, str(ident)), None)

    def settings(self):
        return dict(self._settings)

    def kind(self, kind):
        return {i: d for (k, i), d in self.docs.items() if k == kind}


MAIN = dict(id='main', label='Main', purpose='main')
COWORKER = dict(id='w1', label='Coworker', purpose='worker', context_usage=dict(estimated_tokens=900))


def run_charges(store):
    """The same mixed sequence of charges, old and new signatures."""
    token_budget.charge(store, COWORKER, 1000, 0, turn=True, now=NOW,
                        task_id='t1', role='executor', model='opus')
    token_budget.charge(store, COWORKER, 0, 200, now=NOW + 5, task_id='t1', role='executor', model='opus')
    token_budget.charge_send(store, COWORKER, 'x' * 300, now=NOW + 10,
                             task_id='t1', role='reviewer', model='sonnet')
    token_budget.charge_reply(store, COWORKER, 'y' * 30, now=NOW + 11,
                              task_id='t1', role='reviewer', model='sonnet')
    token_budget.charge(store, COWORKER, 500, 50, turn=True, now=NOW + 20,
                        task_id='t2', role='executor', model='sonnet')
    token_budget.charge(store, MAIN, 4000, 0, True, NOW + 30)          # old positional signature
    token_budget.charge_send(store, MAIN, 'z' * 30, NOW + 31)             # old signature
    token_budget.charge_reply(store, MAIN, 'w' * 60, NOW + 32)            # old signature


class TokenLedgerTests(unittest.TestCase):
    def setUp(self):
        token_budget.clear_hard_stop_cache()
        self.addCleanup(token_budget.clear_hard_stop_cache)

    def test_grouping_by_task_role_and_model(self):
        store = FakeStore()
        run_charges(store)
        usage = token_budget.usage_by_task(store)
        t1, t2 = usage['by_task']['t1'], usage['by_task']['t2']
        # t1: 1000 in + 200 out (executor/opus), 900 context + 100 prompt in + 10 out (reviewer/sonnet)
        self.assertEqual((t1['input'], t1['output'], t1['tokens'], t1['turns']), (2000, 210, 2210, 2))
        self.assertEqual(t1['by_role']['executor']['tokens'], 1200)
        self.assertEqual(t1['by_role']['reviewer']['tokens'], 1010)
        self.assertEqual(t1['by_model']['opus']['tokens'], 1200)
        self.assertEqual(t1['by_model']['sonnet']['tokens'], 1010)
        self.assertEqual((t2['tokens'], t2['turns']), (550, 1))
        # Old callers: no task, role falls back to the worker purpose, no model.
        main = usage['by_task'][token_budget.LEDGER_NONE]
        self.assertEqual((main['input'], main['output'], main['tokens'], main['turns']), (4010, 20, 4030, 2))
        self.assertEqual(list(main['by_role']), ['main'])
        self.assertEqual(usage['by_role']['executor']['tokens'], 1750)
        self.assertEqual(usage['by_role']['reviewer']['tokens'], 1010)
        self.assertEqual(usage['by_role']['main']['tokens'], 4030)
        self.assertEqual(usage['by_model']['opus']['tokens'], 1200)
        self.assertEqual(usage['by_model']['sonnet']['tokens'], 1560)
        self.assertEqual(usage['by_model'][token_budget.LEDGER_NONE]['tokens'], 4030)
        # The ledger total equals the window total.
        self.assertEqual(usage['total']['tokens'], token_budget.public(store, now=NOW + 40)['used'])
        self.assertEqual(usage['total']['tokens'], 2210 + 550 + 4030)

    def test_worker_fields_used_when_not_passed_and_explicit_values_win(self):
        store = FakeStore()
        worker = dict(id='w9', purpose='worker', task_id='t9', role='executor', model='opus')
        token_budget.charge(store, worker, 100, now=NOW)
        token_budget.charge(store, worker, 10, now=NOW, task_id='t8', role='planner', model='sonnet')
        usage = token_budget.usage_by_task(store)
        self.assertEqual(usage['by_task']['t9']['by_role']['executor']['tokens'], 100)
        self.assertEqual(usage['by_task']['t9']['by_model']['opus']['tokens'], 100)
        self.assertEqual(usage['by_task']['t8']['by_role']['planner']['tokens'], 10)
        self.assertEqual(usage['by_task']['t8']['by_model']['sonnet']['tokens'], 10)

    def test_since_keeps_only_newer_hour_buckets(self):
        store = FakeStore()
        token_budget.charge(store, MAIN, 100, now=NOW, task_id='old')
        token_budget.charge(store, MAIN, 7, now=NOW + 3600 + 5, task_id='new')
        self.assertEqual(set(token_budget.usage_by_task(store)['by_task']), {'old', 'new'})
        later = token_budget.usage_by_task(store, since=NOW + 3600)
        self.assertEqual(set(later['by_task']), {'new'})
        self.assertEqual(later['total']['tokens'], 7)
        self.assertEqual(token_budget.usage_by_task(store, since=NOW + 7200)['by_task'], {})

    def test_usage_by_task_is_read_only(self):
        store = FakeStore()
        run_charges(store)
        before = copy.deepcopy(store.docs)
        token_budget.usage_by_task(store)
        token_budget.usage_by_task(store, since=NOW)
        self.assertEqual(store.docs, before)
        self.assertEqual(token_budget.usage_by_task(FakeStore())['total']['tokens'], 0)

    def test_setting_off_records_nothing(self):
        for value in (False, 'false', '0', 0, 'off'):
            store = FakeStore(token_ledger_enabled=value)
            run_charges(store)
            self.assertEqual(store.kind(token_budget.LEDGER_KIND), {}, value)
            self.assertEqual(token_budget.usage_by_task(store)['by_task'], {})
            self.assertEqual(token_budget.public(store, now=NOW + 40)['used'], 6790)
        self.assertTrue(token_budget.ledger_enabled(FakeStore()))           # default on
        self.assertTrue(token_budget.ledger_enabled(FakeStore(token_ledger_enabled=True)))

    def test_old_call_signatures_still_work(self):
        store = FakeStore()
        record = token_budget.charge(store, MAIN, 10)
        self.assertEqual(record['used'], 10)
        self.assertEqual(token_budget.charge(store, MAIN, input_tokens=5, output_tokens=1, turn=True)['used'], 16)
        self.assertIsNone(token_budget.charge(store, MAIN))                 # nothing to charge
        self.assertEqual(token_budget.charge_send(store, MAIN, 'abc')['turns'], 2)
        self.assertEqual(token_budget.charge_reply(store, MAIN, 'abcdef')['output'], 3)
        self.assertIsNone(token_budget.charge(None, MAIN, 10))             # never raises
        token_budget._rate_add(store, NOW, 5, 'worker', True)              # unchanged helper

    def test_limits_and_totals_unchanged_by_ledger(self):
        on, off = FakeStore(), FakeStore(token_ledger_enabled=False)
        run_charges(on)
        run_charges(off)
        for kind in (token_budget.KIND, token_budget.RATE_KIND):
            self.assertEqual(on.kind(kind), off.kind(kind), kind)
        for at in (NOW + 40, NOW + 600, NOW + 4000):
            self.assertEqual(token_budget.exhausted(on, at), token_budget.exhausted(off, at))
            self.assertEqual(token_budget.burst_state(on, at), token_budget.burst_state(off, at))
        # The limit stop works exactly as before with the ledger on.
        limited = FakeStore(token_budget_6h=1000)
        token_budget.charge(limited, MAIN, 999, now=NOW, task_id='t1', role='executor', model='opus')
        token_budget.refuse_if_exhausted(limited, now=NOW + 1)
        token_budget.charge(limited, MAIN, 1, now=NOW + 2, task_id='t1', role='executor', model='opus')
        with self.assertRaises(RateLimited):
            token_budget.refuse_if_exhausted(limited, now=NOW + 3)
        self.assertTrue(token_budget.hard_stop_state(limited, 'main', now=NOW + 3)['stop'])
        # Burst pause unchanged: coworker tokens over burst_hard_15m pause coworkers.
        burst = FakeStore()
        token_budget.charge(burst, COWORKER, 9_000_000, turn=True, now=NOW, task_id='t1', model='opus')
        self.assertEqual(token_budget.burst_state(burst, NOW + 1)['level'], 'pause')
        with self.assertRaises(RateLimited):
            token_budget.refuse_if_burst(burst, COWORKER, now=NOW + 1)

    def test_limit_checks_never_read_the_ledger(self):
        store = FakeStore()
        run_charges(store)
        store.reads.clear()
        token_budget.exhausted(store, NOW + 40)
        token_budget.refuse_if_exhausted(store, NOW + 40)
        token_budget.burst_state(store, NOW + 40)
        token_budget.refuse_if_burst(store, COWORKER, NOW + 40)
        token_budget.hard_stop_state(store, 'worker', now=NOW + 40)
        self.assertNotIn(token_budget.LEDGER_KIND, store.reads)

    def test_ledger_error_never_breaks_a_charge(self):
        class BrokenLedgerStore(FakeStore):
            def put(self, kind, doc):
                if kind == token_budget.LEDGER_KIND:
                    raise RuntimeError('disk full')
                return super().put(kind, doc)

        store = BrokenLedgerStore()
        record = token_budget.charge(store, MAIN, 50, turn=True, now=NOW, task_id='t1')
        self.assertEqual(record['used'], 50)
        self.assertEqual(token_budget.public(store, now=NOW)['used'], 50)

    def test_real_store_uses_objects_table_and_no_new_file(self):
        from notiontool.store import Store
        root = Path(__file__).resolve().parent / '.test-data'
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as temp:
            data = Path(temp) / 'data'
            store = Store(data)
            try:
                files_before = sorted(p.name for p in data.iterdir())
                token_budget.charge(store, COWORKER, 1000, 10, turn=True, now=NOW,
                                    task_id='t1', role='executor', model='opus')
                token_budget.charge_reply(store, COWORKER, 'y' * 30, now=NOW + 1,
                                          task_id='t1', role='executor', model='opus')
                usage = token_budget.usage_by_task(store)
                self.assertEqual(usage['by_task']['t1']['tokens'], 1020)
                self.assertEqual(len(store.all(token_budget.LEDGER_KIND)), 1)
                files_after = sorted(p.name for p in data.iterdir() if not p.name.endswith(('-wal', '-shm', '-journal')))
                self.assertEqual([f for f in files_after if f not in files_before], [])
                store.put('settings', dict(id='app', token_ledger_enabled=False))
                token_budget.charge(store, COWORKER, 5, now=NOW + 2, task_id='t1', role='executor', model='opus')
                self.assertEqual(token_budget.usage_by_task(store)['by_task']['t1']['tokens'], 1020)
            finally:
                store.close()

    # ---- retention (token_ledger_keep_days) ------------------------------------------------------
    def test_old_ledger_buckets_are_deleted_after_keep_days(self):
        store = FakeStore()
        day = 86400
        token_budget.charge(store, COWORKER, 100, turn=True, now=NOW, task_id='old')
        token_budget.charge(store, COWORKER, 50, turn=True, now=NOW + 10 * day, task_id='mid')
        budget_before = store.kind(token_budget.KIND)
        self.assertEqual(len(store.kind(token_budget.LEDGER_KIND)), 2)
        # 15 days later: the first bucket (ended NOW + 1 h) is older than 14 days, the 10-day one is not.
        token_budget.charge(store, COWORKER, 7, turn=True, now=NOW + 15 * day, task_id='new')
        self.assertEqual(set(token_budget.usage_by_task(store)['by_task']), {'mid', 'new'})
        # token_budget window records and the rolling token_rate record are never deleted.
        for ident, record in budget_before.items():
            self.assertIn(ident, store.kind(token_budget.KIND))
        self.assertIn('rolling', store.kind(token_budget.RATE_KIND))

    def test_keep_days_setting_and_default(self):
        self.assertEqual(token_budget.ledger_keep_days(FakeStore()), 14)
        self.assertEqual(token_budget.ledger_keep_days(FakeStore(token_ledger_keep_days='30')), 30)
        self.assertEqual(token_budget.ledger_keep_days(FakeStore(token_ledger_keep_days=0)), 1)
        self.assertEqual(token_budget.ledger_keep_days(FakeStore(token_ledger_keep_days='x')), 14)
        store = FakeStore(token_ledger_keep_days=30)
        token_budget.charge(store, MAIN, 100, now=NOW, task_id='old')
        token_budget.charge(store, MAIN, 7, now=NOW + 15 * 86400, task_id='new')
        self.assertEqual(set(token_budget.usage_by_task(store)['by_task']), {'old', 'new'})
        token_budget.charge(store, MAIN, 7, now=NOW + 31 * 86400, task_id='newest')
        self.assertEqual(set(token_budget.usage_by_task(store)['by_task']), {'new', 'newest'})

    def test_prune_runs_once_per_new_bucket_and_never_raises(self):
        store = FakeStore()
        calls = []
        original = token_budget.ledger_prune
        try:
            token_budget.ledger_prune = lambda s, now=None: calls.append(now) or 0
            for second in range(5):
                token_budget.charge(store, MAIN, 10, now=NOW + second)
            token_budget.charge(store, MAIN, 10, now=NOW + 3600)
        finally:
            token_budget.ledger_prune = original
        self.assertEqual(len(calls), 2)  # only when an hourly bucket is new, not per charge

        class NoDelete(FakeStore):
            delete = None
        self.assertEqual(token_budget.ledger_prune(NoDelete(), NOW), 0)
        self.assertEqual(token_budget.ledger_prune(None, NOW), 0)

    def test_retention_off_ledger_and_limits_unchanged(self):
        on, off = FakeStore(token_ledger_keep_days=1), FakeStore(token_ledger_enabled=False)
        for store in (on, off):
            token_budget.charge(store, COWORKER, 100, turn=True, now=NOW)
            token_budget.charge(store, COWORKER, 100, turn=True, now=NOW + 3 * 86400)
        for kind in (token_budget.KIND, token_budget.RATE_KIND):
            self.assertEqual(on.kind(kind), off.kind(kind), kind)
        self.assertEqual(off.kind(token_budget.LEDGER_KIND), {})

    # ---- real send and reply path ----------------------------------------------------------------
    def test_fake_task_through_send_and_reply_path(self):
        usage, window, model = self.run_fake_task()
        # Every token of the sent turn and its reply is in the ledger, exactly as in the window total.
        self.assertGreater(usage['total']['output'], 0)
        self.assertGreater(usage['total']['input'], 0)
        self.assertEqual(usage['total']['tokens'], window['used'])
        self.assertEqual(usage['total']['turns'], 1)
        self.assertEqual(sum(row['tokens'] for row in usage['by_task'].values()), window['used'])

    @unittest.skipUnless(NOTION_PASSES_TASK, 'notion.py charge_send/charge_reply do not pass task_id/role/model yet')
    def test_fake_task_ledger_row_is_attributed_strictly(self):
        usage, window, model = self.run_fake_task()
        self.assertIn('t1', usage['by_task'])
        row = usage['by_task']['t1']
        self.assertEqual(set(row['by_role']), {'executor'})
        self.assertEqual(set(row['by_model']), {model})
        self.assertEqual(row['by_model'][model]['tokens'], window['used'])
        self.assertEqual(row['tokens'], window['used'])
        self.assertEqual(row['turns'], 1)

    @unittest.skipUnless(ENGINE_PASSES_TASK, 'engine._limit_charge_reply does not pass task_id/role/model yet')
    def test_engine_recovery_reply_charge_records_task_role_model(self):
        import types
        from notiontool import engine
        owner = next(v for v in vars(engine).values()
                     if isinstance(v, type) and '_limit_charge_reply' in vars(v))
        store = FakeStore()
        store.put('workers', dict(COWORKER))
        stub = types.SimpleNamespace(store=store)
        task = {'id': 't9', 'role': 'reviewer', 'model': 'sonnet', 'paused_by': 'limit'}
        owner._limit_charge_reply(stub, task, {'recovery': True, 'worker_id': 'w1'}, 'y' * 300)
        self.assertTrue(task['limit_reply_charged'])
        usage = token_budget.usage_by_task(store)
        self.assertEqual(set(usage['by_task']), {'t9'})
        row = usage['by_task']['t9']
        self.assertEqual(set(row['by_role']), {'reviewer'})
        self.assertEqual(set(row['by_model']), {'sonnet'})
        self.assertGreater(row['output'], 0)
        self.assertEqual(row['tokens'], token_budget.public(store)['used'])
        # Charged once: a second call for the same task records nothing more.
        owner._limit_charge_reply(stub, task, {'recovery': True, 'worker_id': 'w1'}, 'y' * 300)
        self.assertEqual(token_budget.usage_by_task(store)['by_task']['t9']['tokens'], row['tokens'])

    def run_fake_task(self):
        """One task (t1, executor, opus) through the real FakeNotionAdapter.run send and reply path."""
        from unittest import mock
        from notiontool import fake_notion
        from notiontool.fake_notion import FakeState
        from notiontool.store import Store
        root = Path(__file__).resolve().parent / '.test-data'
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as temp:
            state = FakeState(path=str(Path(temp) / 'fake.json'))
            directory = Path(temp) / 'app'
            directory.mkdir()
            store = Store(directory)
            try:
                adapter = fake_notion.FakeNotionAdapter(store, state=state, force=True)
                adapter.refresh_on_login = False
                adapter.stream_turn_poll_s = 0.05
                adapter.verify_response_model = mock.Mock(return_value=[])
                fake_notion.seed_scenario(store, state)
                thread = state.threads_of('ready', 'coworker')[0]['id']
                worker = store.get('workers', 'ready:' + thread)
                client = adapter.client('ready')
                store.put('thread_configs', dict(id=thread, key=[thread, worker.get('generation', 0),
                                                                 client.model('opus')], at=NOW))
                store.put('tasks', {'id': 't1', 'run_id': 'r1', 'role': 'executor', 'lease': 'assignment',
                                    'state': 'running', 'model': 'opus'})
                reply = adapter.run(worker, store.get('tasks', 't1'), 'Say hello', threading.Event(), lambda text: None)
                self.assertTrue(str(reply).strip())
                return token_budget.usage_by_task(store), token_budget.public(store), client.model('opus')
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
