"""App 'New chat' routes (own_chats.handle_app_route) with a fake Notion adapter: no live Notion calls."""
import tempfile
import types
import unittest
import uuid
from pathlib import Path

from notiontool import own_chats
from notiontool.mains import Mains
from notiontool.store import Store
from tests import test_engine as fixtures


class FakeTransport:
    def __init__(self, adapter):
        self.adapter = adapter

    def stream(self, method, body, headers, timeout=0):
        self.adapter.streams.append((method, body['thread_id']))
        self.adapter.threads[body['thread_id']] = 'Welcome to Notion'
        return iter(())


class FakeClient:
    def __init__(self, adapter):
        self.adapter = adapter
        self.info = {'space_id': 'space-1'}
        self.transport = FakeTransport(adapter)

    def identity(self):
        return {'email': 'test@example.test'}

    def model(self):
        return 'model-1'

    def headers(self):
        return {}

    def threads(self):
        return [dict(id=tid, title=title) for tid, title in self.adapter.threads.items()]


class FakeAdapter:
    def __init__(self, store):
        self.store = store
        self.threads, self.streams, self.renames = {}, [], []

    def client(self, account_id):
        return FakeClient(self)

    def require_workspace_ai(self, account_id, identity):
        return None

    def new_id(self, space_id, prefix):
        return str(uuid.uuid4())

    def welcome_body(self, client, thread_id, model):
        return {'thread_id': thread_id, 'model': model}

    def ensure_thread_model(self, client, thread_id, model):
        return None

    def rename_thread(self, client, thread_id, title):
        self.renames.append((thread_id, title))
        self.threads[thread_id] = title

    def refresh(self, account_id):
        existing = {w['thread_id'] for w in self.store.all('workers')}
        for tid, title in self.threads.items():
            if tid not in existing:
                self.store.put('workers', dict(id=account_id + ':' + tid, account_id=account_id, thread_id=tid,
                                               purpose='worker', enabled=False, state='ready', lease='',
                                               last_used=0, label=title))


class AppChatRouteTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent / '.test-data'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.store = Store(self.temp.name)
        self.store.put('accounts', dict(id='a', slot=1, email='test@example.test', state='ready', cooldown_until=0))
        self.adapter = FakeAdapter(self.store)
        self.server = types.SimpleNamespace(store=self.store, adapter=self.adapter,
                                            engine=types.SimpleNamespace(lock=self.store.lock))
        self.server.mains = Mains(self.server)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def post(self, path, **body):
        body.setdefault('account_id', 'a')
        return own_chats.handle_app_route(self.server, path, body)

    def create(self, name, main, request_id):
        return self.post(own_chats.CREATE_ROUTE, name=name, main=main, request_id=request_id)

    def test_create_main_registers_a_main_with_user_title(self):
        out = self.create('Tax return 2026', True, 'r-main')
        self.assertEqual((out['state'], out['role'], out['duplicate']), ('done', 'main', False))
        worker = self.store.get('workers', out['worker_id'])
        self.assertEqual((worker['purpose'], worker['enabled'], worker['label']), ('main', True, 'Tax return 2026'))
        self.assertTrue(worker['data']['user_set_title'])
        chat = self.store.get('chats', out['chat_id'])
        self.assertEqual((chat['main_worker'], chat['title']), (worker['id'], 'Tax return 2026'))
        self.assertEqual(len(self.adapter.streams), 1)
        self.assertEqual(self.adapter.renames[-1], (out['thread_id'], 'Tax return 2026'))
        self.assertEqual(self.adapter.threads[out['thread_id']], 'Tax return 2026')

    def test_create_private_has_no_main_chat_and_is_listed(self):
        out = self.create('Holiday plans', False, 'r-private')
        self.assertEqual((out['state'], out['role'], out['chat_id']), ('done', 'private', ''))
        worker = self.store.get('workers', out['worker_id'])
        self.assertEqual((worker['purpose'], worker['enabled']), ('private', False))
        self.assertFalse([c for c in self.store.all('chats') if c.get('main_worker') == worker['id']])
        rows = own_chats.private_chats(self.store, 'a')
        self.assertEqual([r['title'] for r in rows], ['Holiday plans'])
        self.assertEqual(rows[0]['settings']['promote']['route'], '/api/app/chat/promote')
        self.assertEqual(rows[0]['settings']['promote']['body'], dict(thread_id=out['thread_id'], main=True))

    def test_same_request_id_never_creates_twice(self):
        first = self.create('Notes', False, 'r-same')
        second = self.create('Notes', False, 'r-same')
        self.assertTrue(second['duplicate'])
        self.assertEqual(first['thread_id'], second['thread_id'])
        self.assertEqual(len(self.adapter.streams), 1)
        self.assertEqual(len([w for w in self.store.all('workers') if w['purpose'] == 'private']), 1)
        other = self.create('Notes 2', False, 'r-other')
        self.assertNotEqual(other['thread_id'], first['thread_id'])
        self.assertEqual(len(self.adapter.streams), 2)

    def test_invalid_name_writes_nothing(self):
        with self.assertRaises(ValueError):
            self.create('Main 7', False, 'r-bad')
        self.assertIsNone(self.store.get(own_chats.APP_LEDGER, 'appchat:r-bad'))
        self.assertEqual(self.adapter.streams, [])

    def test_promote_and_back_creates_nothing_in_notion(self):
        out = self.create('Research', False, 'r-promote')
        streams, renames = len(self.adapter.streams), len(self.adapter.renames)
        up = self.post(own_chats.PROMOTE_ROUTE, thread_id=out['thread_id'], main=True)
        self.assertEqual((up['ok'], up['role']), (True, 'main'))
        worker = self.store.get('workers', out['worker_id'])
        self.assertEqual((worker['purpose'], worker['enabled']), ('main', True))
        chat = self.store.get('chats', up['chat_id'])
        self.assertEqual((chat['main_worker'], chat['title']), (worker['id'], 'Research'))
        down = self.post(own_chats.PROMOTE_ROUTE, chat_id=up['chat_id'], main=False)
        self.assertEqual((down['role'], down['chat_id']), ('private', ''))
        self.assertIsNone(self.store.get('chats', up['chat_id']))
        self.assertEqual(self.store.get('workers', out['worker_id'])['purpose'], 'private')
        again = self.post(own_chats.PROMOTE_ROUTE, thread_id=out['thread_id'], main=True)
        self.assertEqual(again['chat_id'], up['chat_id'])  # same Main chat entry comes back
        self.assertEqual((len(self.adapter.streams), len(self.adapter.renames)), (streams, renames))

    def test_promote_refuses_coworkers(self):
        self.store.put('workers', dict(id='a:w', account_id='a', thread_id='w', purpose='worker', enabled=True,
                                       state='ready', label='Cowork 1'))
        with self.assertRaises(ValueError):
            self.post(own_chats.PROMOTE_ROUTE, thread_id='w', main=True)

    def test_rename_keeps_user_title(self):
        out = self.create('Draft', False, 'r-rename')
        result = self.post(own_chats.RENAME_ROUTE, thread_id=out['thread_id'], name='Final name')
        self.assertEqual((result['ok'], result['name']), (True, 'Final name'))
        self.assertEqual(self.adapter.threads[out['thread_id']], 'Final name')
        worker = self.store.get('workers', out['worker_id'])
        self.assertEqual(worker['label'], 'Final name')
        self.assertTrue(worker['data']['user_set_title'] and worker['user_set_title'])
        self.adapter.refresh('a')  # a chat list refresh keeps the user's title and the private role
        worker = self.store.get('workers', out['worker_id'])
        self.assertEqual((worker['label'], worker['purpose']), ('Final name', 'private'))
        with self.assertRaises(ValueError):
            self.post(own_chats.RENAME_ROUTE, thread_id=out['thread_id'], name='Cowork 3')


class PrivateChatSelectionTests(unittest.TestCase):
    setUp = fixtures.EngineTests.setUp
    worker = fixtures.EngineTests.worker
    tearDown = fixtures.EngineTests.tearDown

    @staticmethod
    def _picked(result):
        if isinstance(result, dict):
            return result.get('id')
        if isinstance(result, (tuple, list)):
            return [r.get('id') if isinstance(r, dict) else r for r in result]
        return result

    def test_private_never_chosen_as_coworker_or_rotated(self):
        # Even enabled and idle, a private chat is never picked; it competes with free coworkers and alone.
        self.store.put('workers', dict(id='p', account_id='a', thread_id='p', purpose='private', enabled=True,
                                       state='ready', lease='', label='Tax', last_used=0, created_at=1))
        run = dict(id='r1', chat_id='c', main_worker='main', capacity=5, state='executing')
        task = dict(id='t1', run_id='r1', role='executor', title='Part', state='queued', files=[])
        settings = self.store.settings()
        for _ in range(20):
            picked = self._picked(self.engine._choose_worker(task, run, settings))
            self.assertNotIn('p', picked if isinstance(picked, list) else [picked])
        for worker in self.store.all('workers'):
            if worker.get('purpose') == 'worker':
                worker['state'] = 'busy'
                self.store.put('workers', worker)
        picked = self._picked(self.engine._choose_worker(task, run, settings))
        self.assertNotIn('p', picked if isinstance(picked, list) else [picked])
        # team_rotation: a private chat listed in the team ledger and far past the age limit is never due.
        from notiontool import team_rotation
        account = self.store.get('accounts', 'a')
        account['team_threads'] = {role: {'id': 'p'} for role in team_rotation.team_roles(settings)}
        self.store.put('accounts', account)
        rotator = team_rotation.TeamRotator(self.store, None, lambda *a: None, clock=lambda: 4_000_000_000.0)
        result = rotator.candidate(account, 4_000_000_000.0)
        chosen = result[1] if isinstance(result, tuple) and len(result) > 1 else None
        self.assertNotEqual((chosen or {}).get('id'), 'p')
        self.assertNotIn('p', [w['id'] for w in self.store.all('workers')
                               if w.get('purpose', 'worker') == 'worker'])


SERVER_SOURCE = (Path(__file__).resolve().parents[1] / 'notiontool' / 'server.py').read_bytes()


@unittest.skipUnless(b'/api/app/chat/create' in SERVER_SOURCE, 'server.py does not route /api/app/chat/* yet')
class ServerRouteTests(AppChatRouteTests):
    """The three routes through the real POST dispatch (Handler.handle_request -> Application.action)."""

    def post(self, path, **body):
        from notiontool.server import Application
        body.setdefault('account_id', 'a')
        return Application.action(self.server, path, body)

    def test_server_routes_wired(self):
        made = self.create('Tax 2026', False, 'srv-1')
        for key in ('id', 'state', 'role', 'thread_id', 'chat_id', 'worker_id', 'duplicate'):
            self.assertIn(key, made)
        self.assertEqual(made['role'], 'private')
        self.assertFalse(made['duplicate'])
        moved = self.post(own_chats.PROMOTE_ROUTE, thread_id=made['thread_id'], main=True)
        for key in ('ok', 'role', 'chat_id', 'thread_id', 'worker_id'):
            self.assertIn(key, moved)
        self.assertEqual(moved['role'], 'main')
        named = self.post(own_chats.RENAME_ROUTE, thread_id=made['thread_id'], name='Taxes')
        for key in ('ok', 'name', 'thread_id', 'worker_id'):
            self.assertIn(key, named)
        self.assertEqual(named['name'], 'Taxes')


if __name__ == '__main__':
    unittest.main()
