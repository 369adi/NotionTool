"""Coworker team size follows the settings (per_account, capped by max_workers). Fake client only:
nothing is sent to Notion and no real thread is created."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from notiontool.store import Store
from notiontool.notion import NotionAdapter
from notiontool.server import Application
from notiontool.team_size import coworker_capacity, ledger_roles, team_accounts, team_roles, team_size

SPACE = '440869f1-58c1-81c8-b3d1-0003ec9a0dfc'


class FakeClient:
    def __init__(self):
        self.info = dict(space_id=SPACE, user_id='user', email='test@example.test', user_name='Test',
                         spaces=[{'id': SPACE, 'name': 'Tests'}], records={})
        self.created = []; self.titles = {}; self.transport = self
    def identity(self): return self.info
    def model(self, selected='opus'): return 'verified-opus-codename'
    def headers(self): return {}
    def threads(self): return [dict(id=ident, title=title) for ident, title in self.titles.items()]
    def stream(self, path, body, headers, timeout):
        self.created.append(body)
        self.titles[body['threadId']] = 'Welcome to Notion'
        yield json.dumps({'type': 'patch-start', 'data': {'s': []}})
    def call(self, path, body, timeout=45):
        if path == 'saveTransactionsFanout':
            for op in body['transactions'][0]['operations']:
                if op['pointer']['table'] == 'thread': self.titles[op['pointer']['id']] = op['args']
            return {}
        if path == 'syncRecordValuesSpaceInitial':
            done = {'alive': True, 'data': {'turn_outcomes': {'t': {'status': 'completed'}}}}
            return {'recordMap': {'thread': {r['pointer']['id']: {'value': {'value': done}} for r in body['requests']}}}
        raise AssertionError(path)


class TeamSizeRules(unittest.TestCase):
    def test_size_comes_from_per_account_capped_by_max_workers(self):
        self.assertEqual(team_size({'max_workers': 40, 'per_account': 10}), 10)  # the live store values
        self.assertEqual(team_size({'max_workers': 3, 'per_account': 10}), 3)
        self.assertEqual(team_size({'max_workers': 40, 'per_account': 0}), 1)
        self.assertEqual(team_size({'max_workers': 99, 'per_account': 99}), 40)
        self.assertEqual(team_size({'per_account': 'x'}), 10)
        self.assertEqual(team_size({}), 10)
        self.assertEqual(team_roles({'per_account': 3}), ['worker1', 'worker2', 'worker3'])

    def test_ledger_roles_are_ordered_and_include_roles_above_n(self):
        ledger = {'main': {}, 'worker10': {}, 'worker2': {}, 'worker1': {}, 'workerX': {}}
        self.assertEqual(ledger_roles(ledger), ['worker1', 'worker2', 'worker10'])
        accounts = [dict(id='a', state='ready', team_threads={'main': {}, 'worker1': {}}),
                    dict(id='b', state='ready', team_threads={'main': {}}), dict(id='c', state='ready'),
                    dict(id='d', state='expired', team_threads={'worker1': {}})]
        self.assertEqual(team_accounts(accounts), ['a'])

    def test_capacity_counts_at_most_n_per_account(self):
        workers = [dict(account_id='a')] * 12 + [dict(account_id='b')] * 3
        self.assertEqual(coworker_capacity({'max_workers': 40, 'per_account': 10}, workers), 13)
        self.assertEqual(coworker_capacity({'max_workers': 40, 'per_account': 5}, workers), 8)
        self.assertEqual(coworker_capacity({'max_workers': 6, 'per_account': 10}, workers), 6)
        self.assertEqual(coworker_capacity({'max_workers': 40, 'per_account': 10}, []), 0)

    def test_delegation_paths_use_team_size(self):
        root = Path(__file__).resolve().parent.parent / 'notiontool'
        engine = (root / 'engine.py').read_text(encoding='utf-8')
        operator = (root / 'operator_main.py').read_text(encoding='utf-8')
        server = (root / 'server.py').read_bytes().decode('utf-8')
        notion = (root / 'notion.py').read_bytes().decode('utf-8')
        self.assertIn('capacity = coworker_capacity(self.store.settings(), workers)', engine)
        self.assertIn('team_size(store.settings()), len(coworkers)', operator)
        self.assertIn('roles=team_roles(self.store.settings())', notion)
        self.assertNotIn("'worker4','worker5'", notion)
        self.assertNotIn("'worker4', 'worker5'", server)
        self.assertIn('Application.grow_teams(self, old_team)', server)


class TeamSizeEnsureTeam(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).parent / '.test-data'; root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.store = Store(self.temp.name)
        self.adapter = NotionAdapter(self.store)
        self.client = FakeClient()
        self.adapter.client = lambda *args, **kwargs: self.client
        self.store.put('accounts', dict(id='a', slot=1, email='test@example.test', label='Slot 1', state='ready'))
    def tearDown(self):
        self.store.close(); self.temp.cleanup()
    def size(self, n, max_workers=40):
        settings = self.store.settings(); settings.update(per_account=n, max_workers=max_workers)
        self.store.put('settings', settings)
    def ledger(self):
        return self.store.get('accounts', 'a').get('team_threads') or {}

    def test_ten_coworkers_when_the_setting_is_ten(self):
        self.size(10)
        result = self.adapter.ensure_team('a', create_main=True)
        self.assertEqual(len(self.client.created), 11)  # one Main + ten Welcome Coworkers
        self.assertEqual(ledger_roles(self.ledger()), ['worker%d' % i for i in range(1, 11)])
        self.assertEqual(result['team_size'], 10)
        self.assertEqual(result['welcome_chats'], 11)
        self.adapter.ensure_team('a', create_main=True)
        self.assertEqual(len(self.client.created), 11)  # idempotent, nothing created twice

    def test_growing_adds_only_the_missing_roles(self):
        self.size(5)
        self.adapter.ensure_team('a', create_main=True)
        first = dict(self.ledger())
        self.size(7)
        self.adapter.ensure_team('a', create_main=True)
        self.assertEqual(len(self.client.created), 8)
        for role in ledger_roles(first):
            self.assertEqual(self.ledger()[role]['id'], first[role]['id'])
        self.assertEqual(ledger_roles(self.ledger()), ['worker%d' % i for i in range(1, 8)])

    def test_lowering_keeps_every_chat(self):
        self.size(6)
        self.adapter.ensure_team('a', create_main=True)
        workers = len(self.store.all('workers')); threads = dict(self.client.titles)
        self.size(2)
        result = self.adapter.ensure_team('a', create_main=True)
        self.assertEqual(len(self.client.created), 7)  # nothing new
        self.assertEqual(len(self.store.all('workers')), workers)  # nothing deleted
        self.assertEqual(set(self.client.titles), set(threads))
        self.assertEqual(ledger_roles(self.ledger()), ['worker%d' % i for i in range(1, 7)])  # extra roles stay recorded
        self.assertEqual(result['team_size'], 2)
        self.size(4)
        self.adapter.ensure_team('a', create_main=True)
        self.assertEqual(len(self.client.created), 7)  # the kept chats fill the roles again

    def test_spare_joined_pool_chat_fills_a_new_role_first(self):
        self.size(2)
        self.adapter.ensure_team('a', create_main=True)
        spare = self.adapter.new_id(SPACE, '00a9')
        self.client.titles[spare] = 'Welcome to Notion'
        self.adapter.refresh('a')
        record = next(w for w in self.store.all('workers') if w['thread_id'] == spare)
        record.update(purpose='worker', join_state='joined', enabled=True, state='ready')
        self.store.put('workers', record)
        self.size(3)
        self.adapter.ensure_team('a', create_main=True)
        self.assertEqual(len(self.client.created), 3)  # no new Welcome chat
        self.assertEqual(self.ledger()['worker3']['id'], spare)

    def test_unconfirmed_new_role_is_never_resent(self):
        self.size(2)
        self.adapter.ensure_team('a', create_main=True)
        account = self.store.get('accounts', 'a')
        account['team_threads']['worker3'] = {'id': self.adapter.new_id(SPACE, '00a9'), 'pending': True}
        self.store.put('accounts', account)
        self.size(3)
        with self.assertRaisesRegex(ValueError, 'unconfirmed'):
            self.adapter.ensure_team('a', create_main=True)
        self.assertEqual(len(self.client.created), 3)
        self.assertTrue(self.ledger()['worker3']['pending'])


class TeamSizeSettingsTrigger(unittest.TestCase):
    def fake(self, size, accounts):
        settings = dict(id='app', max_workers=40, per_account=size)
        store = SimpleNamespace(settings=lambda: settings, all=lambda kind: accounts, event=Mock())
        app = SimpleNamespace(store=store, adapter=Mock(), job=Mock(return_value={'job_id': 'j1'}))
        return app

    def test_larger_size_reconciles_managed_teams(self):
        app = self.fake(10, [dict(id='a', state='ready', team_threads={'main': {}, 'worker1': {}}),
                             dict(id='b', state='ready', team_threads={'main': {}})])
        self.assertEqual(Application.grow_teams(app, 5), ['j1'])
        app.job.assert_called_once()
        title, fn, ident = app.job.call_args[0]
        self.assertEqual((title, ident), ('Grow Welcome Coworkers to 10', 'a'))
        app.adapter.ensure_team.assert_not_called()  # runs only inside the job
        fn()
        app.adapter.ensure_team.assert_called_once_with('a')

    def test_same_or_smaller_size_does_nothing(self):
        app = self.fake(5, [dict(id='a', state='ready', team_threads={'worker1': {}})])
        self.assertEqual(Application.grow_teams(app, 5), [])
        self.assertEqual(Application.grow_teams(app, 10), [])
        app.job.assert_not_called()

    def test_busy_account_defers_with_an_event(self):
        app = self.fake(10, [dict(id='a', state='ready', team_threads={'worker1': {}})])
        app.job.side_effect = ValueError('An operation is already running for this account')
        self.assertEqual(Application.grow_teams(app, 5), [])
        app.store.event.assert_called_once()


if __name__ == '__main__':
    unittest.main()