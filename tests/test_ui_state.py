"""The /api/state snapshot (server.py Application.snapshot) mirrors stored workers, runs and events.

No server is started: Application.snapshot is called on a small stand-in object that holds a
temporary Store; everything the snapshot needs besides the store is stubbed.
"""
import tempfile, threading, types, unittest
from unittest import mock

from notiontool import server
from notiontool.bridge import READ_ONLY_ROLES
from notiontool.store import Store

ACCOUNT = 'acc-1'


def stand_in(store):
    app = types.SimpleNamespace(store=store, port=8790, bind='127.0.0.1')
    app.mcp_servers = types.SimpleNamespace(public=lambda: [])
    app.mains = types.SimpleNamespace(public=lambda o: o)
    app.chat_list = lambda: []
    app.limits = lambda: {}
    app.safe_task = lambda t: t
    return app


class UiStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(self.temp.name)
        self.store.put('accounts', dict(id=ACCOUNT, state='ready', identity={'email': 'x'}))
        self.workers = [
            dict(id='w-main', account_id=ACCOUNT, purpose='main', state='busy', enabled=True, model='opus',
                 thread_id='th-main', current_task='t1', context_usage=0.42, last_error='', lease='L1', handoff='H1'),
            dict(id='w-co1', account_id=ACCOUNT, purpose='worker', state='ready', enabled=True, model='sonnet',
                 thread_id='th-co1', current_task='', context_usage=0.1, last_error='', lease='', handoff=''),
            dict(id='w-co2', account_id=ACCOUNT, purpose='worker', state='error', enabled=True, model='haiku',
                 thread_id='th-co2', current_task='t2', context_usage=0.0, last_error='Assignment cancelled'),
            dict(id='w-other', account_id='acc-2', purpose='worker', state='ready', enabled=True, model='opus', thread_id='th-x'),
        ]
        for worker in self.workers:
            self.store.put('workers', worker)
        self.runs = [
            dict(id='r1', title='First run', state='running', project_id='p1', context='secret context'),
            dict(id='r2', title='Second run', state='done', project_id='p2'),
        ]
        for run in self.runs:
            self.store.put('runs', run)
        self.seqs = [self.store.event('task_started', 'r1', task_id='t%d' % i, message='event %d' % i) for i in range(160)]
        patches = [mock.patch('notiontool.harness_account.selected_account', return_value=ACCOUNT),
                   mock.patch('notiontool.harness_account.main_chat', return_value=None),
                   mock.patch.object(server, 'account_setup', return_value={})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.snap = server.Application.snapshot(stand_in(self.store))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_workers_match_stored_objects(self):
        got = {w['id']: w for w in self.snap['workers']}
        self.assertEqual(list(got), ['w-main', 'w-co1', 'w-co2'])
        for stored in self.workers[:3]:
            expected = {k: v for k, v in stored.items() if k not in ('lease', 'handoff')}
            self.assertEqual(got[stored['id']], expected)
            self.assertEqual(got[stored['id']], {k: v for k, v in self.store.get('workers', stored['id']).items() if k not in ('lease', 'handoff')})
        self.assertNotIn('lease', got['w-main'])
        self.assertNotIn('handoff', got['w-main'])

    def test_worker_stats_follow_states(self):
        self.assertEqual(self.snap['stats'], dict(running=1, ready=1, recovering=0))

    def test_runs_match_stored_objects_without_context(self):
        self.assertEqual([r['id'] for r in self.snap['runs']], ['r1', 'r2'])
        for got, stored in zip(self.snap['runs'], self.runs):
            self.assertEqual(got, {k: v for k, v in stored.items() if k != 'context'})
        self.assertNotIn('context', self.snap['runs'][0])

    def test_latest_events_match_in_order_and_count(self):
        events = self.snap['events']
        self.assertEqual(len(events), 150)
        self.assertEqual([e['seq'] for e in events], self.seqs[-150:])
        self.assertEqual([e['task_id'] for e in events], ['t%d' % i for i in range(10, 160)])
        self.assertEqual(events, self.store.events())
        self.assertTrue(all(e['run_id'] == 'r1' and e['kind'] == 'task_started' for e in events))
        self.assertEqual(events[0]['project_id'], 'p1')

    def test_snapshot_reflects_a_new_event_and_worker_change(self):
        worker = self.store.get('workers', 'w-co1')
        worker.update(state='busy', current_task='t9')
        self.store.put('workers', worker)
        seq = self.store.event('task_started', 'r2', task_id='t9')
        snap = server.Application.snapshot(stand_in(self.store))
        self.assertEqual({w['id']: w['state'] for w in snap['workers']}['w-co1'], 'busy')
        self.assertEqual(snap['events'][-1]['seq'], seq)
        self.assertEqual(snap['events'][-1]['task_id'], 't9')
        self.assertEqual(len(snap['events']), 150)
        self.assertEqual(snap['stats']['running'], 2)


class RoleSettingsTests(unittest.TestCase):
    """P4b-1: role definitions in settings and /api/state; READ_ONLY_ROLES stay read-only (rule I7)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(self.temp.name)
        self.store.put('accounts', dict(id=ACCOUNT, state='ready'))
        patches = [mock.patch('notiontool.harness_account.selected_account', return_value=ACCOUNT),
                   mock.patch('notiontool.harness_account.main_chat', return_value=None),
                   mock.patch.object(server, 'account_setup', return_value={})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.app = stand_in(self.store)
        self.app.engine = types.SimpleNamespace(lock=threading.Lock())
        self.app.bridge = types.SimpleNamespace(close_local=lambda off: None)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def roles(self):
        state = server.Application.snapshot(self.app)['roles']
        return state, {r['name']: r for r in state['definitions']}

    def post(self, body):
        return server.Application.action(self.app, '/api/settings', body)

    def test_default_roles_listed(self):
        self.assertEqual((self.store.settings()['dynamic_roles'], self.store.settings()['role_definitions']), (False, []))
        state, roles = self.roles()
        self.assertFalse(state['dynamic'])
        self.assertEqual(state['error'], '')
        self.assertEqual(state['read_only_roles'], list(READ_ONLY_ROLES))
        for name in ('planner', 'executor', 'reviewer', 'integrator', 'tester') + tuple(READ_ONLY_ROLES):
            self.assertIn(name, roles)
        self.assertEqual(roles['executor']['tools'], 'read_write')
        self.assertEqual(roles['integrator']['tools'], 'read_write')
        for name in READ_ONLY_ROLES:
            self.assertEqual((roles[name]['tools'], roles[name]['protected']), ('read_only', True), name)
        self.assertFalse(any(r['custom'] for r in roles.values()))
        self.assertEqual(state['definitions'], server.default_roles())

    def test_custom_role_appears(self):
        self.assertEqual(self.post({'dynamic_roles': True, 'role_definitions': [
            {'name': 'translator', 'tools': 'read_only', 'description': ' Translates docs '},
            {'name': 'executor', 'tools': 'read_only'},
            {'name': 'reviewer', 'tools': 'read_only', 'description': 'Reads diffs'}]}), {'ok': True})
        stored = self.store.settings()
        self.assertTrue(stored['dynamic_roles'])
        self.assertEqual(stored['role_definitions'][0], dict(name='translator', tools='read_only', description='Translates docs'))
        state, roles = self.roles()
        self.assertTrue(state['dynamic'])
        self.assertEqual(roles['translator'], dict(name='translator', tools='read_only', description='Translates docs',
                                                   builtin=False, protected=False, custom=True, active=True))
        self.assertEqual(roles['executor']['tools'], 'read_only')  # a writable role may be made stricter
        self.assertEqual((roles['reviewer']['tools'], roles['reviewer']['protected']), ('read_only', True))
        self.assertIn('planner', roles)

    def test_read_only_role_cannot_be_made_writable(self):
        for name in READ_ONLY_ROLES:
            with self.subTest(role=name):
                with self.assertRaises(ValueError) as ctx:
                    self.post({'dynamic_roles': True, 'role_definitions': [{'name': name, 'tools': 'read_write'}]})
                self.assertIn('read-only', str(ctx.exception))
                self.assertIn('rule I7', str(ctx.exception))
                self.assertEqual(self.store.settings()['role_definitions'], [])
                self.assertFalse(self.store.settings()['dynamic_roles'])
        _, roles = self.roles()
        self.assertTrue(all(roles[name]['tools'] == 'read_only' for name in READ_ONLY_ROLES))

    def test_invalid_input_rejected(self):
        cases = {
            'not a list': ({'name': 'x1'}, 'must be a list'),
            'not an object': (['translator'], 'must be an object'),
            'bad name': ([{'name': 'Bad Name', 'tools': 'read_only'}], 'is invalid'),
            'missing name': ([{'tools': 'read_only'}], 'is invalid'),
            'bad tools': ([{'name': 'translator', 'tools': 'admin'}], "'read_only' or 'read_write'"),
            'duplicate': ([{'name': 'translator'}, {'name': 'translator'}], 'defined twice'),
            'unknown field': ([{'name': 'translator', 'shell': True}], 'unknown fields: shell'),
            'long description': ([{'name': 'translator', 'description': 'x' * 201}], 'at most 200'),
            'too many': ([{'name': 'role_%d' % i} for i in range(21)], 'at most 20'),
        }
        for label, (value, message) in cases.items():
            with self.subTest(label):
                with self.assertRaises(ValueError) as ctx:
                    self.post({'role_definitions': value})
                self.assertIn(message, str(ctx.exception))
                self.assertEqual(self.store.settings()['role_definitions'], [])

    def test_tampered_stored_value_falls_back_to_built_in_roles(self):
        settings = self.store.settings()
        settings.update(dynamic_roles=True, role_definitions=[{'name': 'reviewer', 'tools': 'read_write'}])
        self.store.put('settings', settings)
        state, roles = self.roles()
        self.assertIn('rule I7', state['error'])
        self.assertEqual(roles['reviewer']['tools'], 'read_only')
        self.assertEqual(state['definitions'], server.default_roles())


if __name__ == '__main__':
    unittest.main()
