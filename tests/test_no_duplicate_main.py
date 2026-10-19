"""No duplicate Main from team setup; Main switch only on real need; old Main renamed 'Main (old, ...)'.

Fake Notion clients and fake switch ops only: no network, no chats, no restart.
"""
import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from notiontool.store import Store
from notiontool.notion import NotionAdapter
from notiontool import main_switch as ms

SPACE = '440869f1-58c1-81c8-b3d1-0003ec9a0dfc'
T_OP = '3fea1b13-e54d-805c-bc47-00a9af92c91f'
T_OLD = '3fda1b13-e54d-80f7-88fc-00a9b1c2e514'


class FakeClient:
    def __init__(self):
        self.info = dict(space_id=SPACE, user_id='user', email='test@example.test', user_name='Test',
                         spaces=[{'id': SPACE, 'name': 'Tests'}], records={})
        self.created, self.actions, self.titles = [], [], {}
        self.transport = self

    def identity(self):
        return self.info

    def model(self, selected='opus'):
        return 'verified-opus-codename'

    def headers(self):
        return {}

    def threads(self):
        return [dict(id=ident, title=title) for ident, title in self.titles.items()]

    def stream(self, path, body, headers, timeout):
        self.created.append(body)
        self.titles[body['threadId']] = 'Welcome to Notion'
        yield json.dumps({'type': 'patch-start', 'data': {'s': []}})

    def call(self, path, body, timeout=45):
        if path == 'saveTransactionsFanout':
            tx = body['transactions'][0]
            self.actions.append((tx['debug']['userAction'], tx['operations']))
            for op in tx['operations']:
                if op['pointer']['table'] == 'thread':
                    self.titles[op['pointer']['id']] = op['args']
            return {}
        if path == 'syncRecordValuesSpaceInitial':
            done = {'alive': True, 'data': {'turn_outcomes': {'t': {'status': 'completed'}}}}
            return {'recordMap': {'thread': {r['pointer']['id']: {'value': {'value': done}} for r in body['requests']}}}
        raise AssertionError(path)


class TeamSetupTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).parent / '.test-data'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.store = Store(self.temp.name)
        self.adapter = NotionAdapter(self.store)
        self.client = FakeClient()
        self.adapter.client = lambda *args, **kwargs: self.client
        self.store.put('accounts', dict(id='a', slot=1, email='test@example.test', label='Slot 1', state='ready'))
        self.store.put('settings', dict(self.store.settings(), per_account=5))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def add_operator(self, purpose='operator'):
        self.client.titles[T_OP] = 'Main'
        self.store.put('workers', dict(id='a:' + T_OP, account_id='a', thread_id=T_OP, purpose=purpose, label='Main',
                                       enabled=False, state='ready', join_state='joined'))
        self.store.put('chats', dict(id='c_op', operator=True, state='active', main_worker='a:' + T_OP, title='Operator Main'))

    def mains(self):
        return [w for w in self.store.all('workers') if w.get('purpose') == 'main']

    def onboarding_writes(self):
        return [a for a, _ in self.client.actions if a == 'newUserHomeActions.savePostOnboardingAgentConfig']

    def test_operator_main_blocks_new_main_and_fills_coworkers(self):
        self.add_operator()
        # The bug: ledger Main points at an old chat that is gone now.
        account = self.store.get('accounts', 'a')
        account['team_threads'] = {'main': {'id': T_OLD, 'pending': False}}
        self.store.put('accounts', account)
        result = self.adapter.ensure_team('a')
        self.assertEqual(len(self.client.created), 5)  # five Coworkers, no Main
        self.assertEqual(self.mains(), [])
        self.assertEqual(self.onboarding_writes(), [])
        self.assertEqual(self.store.get('accounts', 'a')['team_threads']['main'], {'id': T_OP, 'pending': False})
        self.assertEqual(self.client.titles[T_OP], 'Main')
        self.assertEqual(result['team_size'], 5)
        self.adapter.ensure_team('a')  # idempotent: still nothing created
        self.assertEqual(len(self.client.created), 5)

    def test_main_on_operator_thread_counts_as_operator_main(self):
        self.add_operator(purpose='main')
        self.assertEqual(self.adapter.operator_main('a', {T_OP})['thread_id'], T_OP)
        self.assertIsNone(self.adapter.operator_main('a', set()))  # not reachable

    def test_spare_main_is_never_an_operator_main(self):
        self.store.put('workers', dict(id='a:spare', account_id='a', thread_id='spare', purpose='spare_main',
                                       label='Spare Main (unused)', enabled=False, state='ready'))
        self.assertIsNone(self.adapter.operator_main('a', {'spare'}))

    def test_no_main_and_no_flag_creates_no_main(self):
        self.adapter.ensure_team('a')
        self.assertEqual(len(self.client.created), 5)
        self.assertEqual(self.mains(), [])
        self.assertEqual(self.onboarding_writes(), [])

    def test_create_main_flag_creates_exactly_one_main(self):
        self.adapter.ensure_team('a', create_main=True)
        self.assertEqual(len(self.client.created), 6)
        self.assertEqual(len(self.mains()), 1)
        self.assertEqual(len(self.onboarding_writes()), 1)
        self.adapter.ensure_team('a', create_main=True)
        self.assertEqual(len(self.client.created), 6)


def _switch_fakes():
    path = Path(__file__).with_name('test_main_switch.py')
    spec = importlib.util.spec_from_file_location('_nt_main_switch_fakes', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


F = _switch_fakes()


class SwitchRuleTests(F.Base):
    def test_old_title_format(self):
        ts = time.mktime((2026, 10, 19, 14, 5, 0, 0, 0, -1))
        self.assertEqual(ms._archive_title(ts), 'Main (old, 2026-10-19 14:05)')

    def test_need_reasons_tokens_turns_and_lag(self):
        settings = dict(main_context_ask_tokens=100, main_context_ask_turns=10, main_context_lag_ms=5000)
        self.assertEqual(ms.need_reasons(dict(estimated_tokens=5, turns=1, lag_ms=100), settings), [])
        self.assertEqual(ms.need_reasons(dict(estimated_tokens=5, turns=1, lag_ms=6000), settings), ['lag'])
        self.assertEqual(ms.need_reasons(dict(estimated_tokens=200, turns=20), settings), ['tokens', 'turns'])

    def test_check_asks_on_measured_lag(self):
        self.store._settings.update(main_context_lag_ms=5000)
        worker = self.store.get('workers', 'w_op')
        worker['context_usage'] = dict(estimated_tokens=10, turns=1, lag_ms=9000)
        self.store.put('workers', worker)
        ms.check(self.store, worker, worker['context_usage'], self.store.settings())
        self.assertTrue(self.store.get('workers', 'w_op')['context_ask']['ask'])

    def test_start_never_from_team_setup(self):
        out = ms.start(self.store, 'c_op', F.FakeDelegate(), 'q1', now=F.T0, trigger='team_setup')
        self.assertFalse(out['ok'])
        self.assertIsNone(self.store.get('main_switches', 'q1'))

    def test_start_refused_when_measured_and_not_needed(self):
        worker = self.store.get('workers', 'w_op')
        worker['context_usage'] = dict(estimated_tokens=10, turns=1, lag_ms=10)
        self.store.put('workers', worker)
        delegate = F.FakeDelegate()
        self.assertFalse(ms.start(self.store, 'c_op', delegate, 'q1', now=F.T0)['ok'])
        self.assertEqual(delegate.bodies, [])

    def test_switch_renames_new_main_and_old_main(self):
        worker = self.store.get('workers', 'w_op')
        worker['context_ask'] = dict(ask=True, reasons=['tokens'])
        self.store.put('workers', worker)
        record = self.started()
        self.run_done()
        self.write_brief()
        ops = F.FakeOps(os.path.join(self.data, 'handoffs'), ack=True)
        out = ms.advance(self.store, record, ops, now=F.T0)
        self.assertEqual(out['state'], 'switched')
        self.assertEqual(ops.renamed[0], ('thread-new', 'Main'))
        self.assertEqual(ops.renamed[1][0], 'thread-old')
        self.assertRegex(ops.renamed[1][1], r'^Main \(old, \d{4}-\d{2}-\d{2} \d{2}:\d{2}\)$')
        self.assertEqual(ops.deleted, [])


if __name__ == '__main__':
    unittest.main()
