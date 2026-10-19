"""Multi-Main harness requirements (mocked Notion; no live requests)."""
import json, tempfile, threading, time, unittest, sys
from pathlib import Path
from unittest.mock import patch

from notiontool.engine import Engine
from notiontool.store import Store, redact
from notiontool.mains import Mains, migrate
from notiontool.allocation import allocation
from notiontool.chat_context import cleanup_blocker
from tests.test_engine import FakeAdapter


class FakeTransport:
    def __init__(self, client): self.client, self.posts = client, 0
    def stream(self, method, body, headers, timeout=0):
        assert method == 'runInferenceTranscript'
        self.posts += 1
        if self.client.drop_creation: raise OSError('timeout')
        self.client.thread_ids.append(body['threadId']); yield b'{}'
        if self.client.timeout_after_accept: raise OSError('timeout after accept')


class FakeClient:
    def __init__(self):
        self.info = {'space_id': 'space'}; self.thread_ids = ['main']; self.drop_creation = False
        self.timeout_after_accept = False; self.transport = FakeTransport(self)
    def threads(self): return [{'id': t} for t in self.thread_ids]
    def model(self): return 'model'
    def headers(self): return {}
    def identity(self): return {}
    def transcript(self, tid): return []


class FakeNotion(FakeAdapter):
    def __init__(self, store):
        super().__init__(); self.store, self.fake = store, FakeClient(); self.counter = 0; self.renamed = []
    def client(self, account_id): return self.fake
    def require_workspace_ai(self, *a): pass
    def new_id(self, space, tail):
        self.counter += 1; return f'thread-{self.counter}'
    def welcome_body(self, client, tid, model): return {'threadId': tid}
    def ensure_thread_model(self, *a): pass
    def rename_thread(self, client, tid, title): self.renamed.append((tid, title))
    def refresh(self, account_id):  # mirrors NotionAdapter.refresh: new threads become disabled workers
        known = {w['thread_id'] for w in self.store.all('workers')}
        for tid in self.fake.thread_ids:
            if tid not in known:
                self.store.put('workers', dict(id='w-' + tid, account_id='a', thread_id=tid, purpose='worker',
                               enabled=False, state='ready', lease='', label=tid, last_used=0))


class App:
    def __init__(self, store, adapter, engine): self.store, self.adapter, self.engine = store, adapter, engine


class MultiMainTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent / '.test-data'; root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.store = Store(self.temp.name)
        self.store.put('accounts', dict(id='a', slot=1, email='t@example.test', state='ready', cooldown_until=0))
        self.store.put('settings', dict(self.store.settings(), harness_account_id='a', max_workers=3, per_account=3))
        self.adapter = FakeNotion(self.store)
        self.store.put('workers', dict(id='main', account_id='a', thread_id='main', purpose='main', enabled=True,
                                       state='ready', lease='', label='main', last_used=0))
        for n in range(3):
            self.store.put('workers', dict(id=f'w{n}', account_id='a', thread_id=f'w{n}', purpose='worker', enabled=True,
                                           state='ready', lease='', label=f'w{n}', last_used=0))
        self.store.put('chats', dict(id='c', title='Account 1 · Main Chat', main_worker='main', folder_id='',
                                     project_path=self.temp.name, created=1))
        migrate(self.store)
        self.engine = Engine(self.store, self.adapter)
        self.mains = Mains(App(self.store, self.adapter, self.engine))
    def tearDown(self):
        self.adapter.gate.set(); self.engine.close(); time.sleep(.03); self.store.close(); self.temp.cleanup()
    def pump(self, predicate, timeout=15):  # returns as soon as predicate holds; 4 s was too tight under full-suite load
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.engine.tick()
            if predicate(): return
            time.sleep(.015)
        self.fail('state not reached: ' + str([(r['state'], r.get('error')) for r in self.store.all('runs')]))
    def create(self, request):
        op, _ = self.mains.request_create('a', request); return self.mains.run_create(op['id'])

    # 2, 3, 4
    def test_create_main_uses_existing_welcome_creation_and_creates_exactly_one(self):
        op = self.create('click-1')
        self.assertEqual(op['state'], 'done'); self.assertEqual(op['title'], 'Main 2')
        self.assertEqual(self.adapter.fake.transport.posts, 1)
        again, new = self.mains.request_create('a', 'click-1'); self.mains.run_create(again['id'])
        self.assertFalse(new); self.assertEqual(self.adapter.fake.transport.posts, 1)
        mains = [c for c in self.store.all('chats')]
        self.assertEqual(sorted(c['main_number'] for c in mains), [1, 2])
        self.assertIn(('thread-1', 'Main 2'), self.adapter.renamed)
        worker = self.store.get('workers', 'w-thread-1')
        self.assertEqual((worker['purpose'], worker['enabled']), ('main', True))
    def test_main_numbers_are_unique_under_concurrency_and_survive_restart(self):
        results = []
        threads = [threading.Thread(target=lambda i=i: results.append(self.mains.request_create('a', f'r{i}')[0]['number'])) for i in range(20)]
        [t.start() for t in threads]; [t.join() for t in threads]
        self.assertEqual(sorted(results), list(range(2, 22)))
        self.store.close(); self.store = Store(self.temp.name); migrate(self.store)
        self.assertEqual(self.store.next_counter('main'), 22)
    def test_migration_numbers_existing_main_and_keeps_backup(self):
        chat = self.store.get('chats', 'c')
        self.assertEqual((chat['main_number'], chat['title']), (1, 'Main 1'))
        self.assertTrue(list((Path(self.temp.name) / 'backups').glob('notiontool-schema1-*.db')))
        self.assertFalse(migrate(self.store))  # idempotent
    # 19
    def test_timeout_after_submission_never_resends_creation(self):
        self.adapter.fake.drop_creation = True
        op = self.create('t-1'); self.assertEqual(op['state'], 'unconfirmed')
        self.adapter.fake.drop_creation = False
        op = self.mains.run_create(op['id']); self.assertEqual(op['state'], 'unconfirmed')
        self.assertEqual(self.adapter.fake.transport.posts, 1)
        self.adapter.fake.thread_ids.append(op['thread_id'])  # Notion actually accepted it
        self.assertEqual(self.mains.run_create(op['id'])['state'], 'done')
        self.assertEqual(self.adapter.fake.transport.posts, 1)
    def test_resent_message_with_same_request_id_is_one_run(self):
        first = self.engine.submit('c', 'Hallo!', 'msg-1'); second = self.engine.submit('c', 'Hallo!', 'msg-1')
        self.assertEqual(first['id'], second['id']); self.assertTrue(second['duplicate'])
        self.assertEqual(len([m for m in self.store.all('messages') if m['role'] == 'user']), 1)
    # 5
    def test_simple_questions_get_no_coworker(self):
        for text in ('What is the capital of France?', 'Calculate 17 * 23', 'Hallo!'):
            self.assertEqual(allocation(text, 3)['mode'], 'direct')
        run = self.engine.submit('c', 'What is the capital of France?')
        self.pump(lambda: self.store.get('runs', run['id'])['state'] == 'done')
        self.assertEqual([r for _, r in self.adapter.roles], ['integrator'])
    # 6, 8: five tasks, three coworkers -> waves, never above capacity
    def test_parallel_work_runs_in_waves_within_capacity(self):
        self.adapter.count = 5
        run = self.engine.submit('c', 'Build and verify five independent modules with separate checks for each one')
        self.pump(lambda: self.store.get('runs', run['id'])['state'] in ('done', 'blocked', 'failed'), 8)
        self.assertLessEqual(self.adapter.peak, 4)  # 3 coworkers + main
        self.assertGreaterEqual(self.adapter.peak, 2)
    # several Mains run independently
    def test_two_mains_accept_work_independently(self):
        op = self.create('m2')
        self.engine.submit(op['chat_id'], 'Hallo!'); self.engine.submit('c', 'Hallo!')
        self.pump(lambda: all(r['state'] == 'done' for r in self.store.all('runs')) and len(self.store.all('runs')) == 2)
        self.assertEqual({r['chat_id'] for r in self.store.all('runs')}, {op['chat_id'], 'c'})
    # 17
    def test_coworker_cleanup_only_after_secured_handoff(self):
        w = self.store.get('workers', 'w0')
        self.store.put('tasks', dict(id='old', run_id='r', state='done', result='', finished=1, worker_id='w0'))
        self.assertIn('not stored', cleanup_blocker(self.store, w, {'id': 'new'}))
        self.store.put('tasks', dict(id='old', run_id='r', state='blocked', remote_unknown=True, worker_id='w0'))
        self.assertIn('not confirmed', cleanup_blocker(self.store, w, {'id': 'new'}))
        self.store.put('tasks', dict(id='old', run_id='r', state='done', result='{"ok":1}', finished=1, worker_id='w0'))
        self.assertEqual(cleanup_blocker(self.store, w, {'id': 'new'}), '')
    def test_secrets_never_reach_event_log(self):
        self.store.event('x', message='Authorization: Bearer abc.def.ghi123 token_v2=xyz', api_key='k')
        text = json.dumps(self.store.events(0, 50))
        for secret in ('abc.def.ghi123', 'xyz', '"k"'): self.assertNotIn(secret, text)
    # 21
    def test_message_marker_changes_only_with_new_messages(self):
        a = self.store.message_marker('c'); self.assertEqual(a, self.store.message_marker('c'))
        self.store.put('messages', dict(id='m1', chat_id='c', role='user', text='x', created=1))
        self.assertNotEqual(a, self.store.message_marker('c'))


@unittest.skipUnless(sys.platform == 'win32', 'Host API test needs the Windows DPAPI session store')
class HostApiTests(unittest.TestCase):
    # 20
    def test_bearer_required_and_stream_is_event_stream(self):
        import urllib.request, urllib.error
        from notiontool.server import Application
        temp = tempfile.TemporaryDirectory(); app = Application(temp.name)
        try:
            app.launch(0); base = f'http://127.0.0.1:{app.port}'
            with self.assertRaises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(base + '/api/health', timeout=5)
            self.assertEqual(err.exception.code, 401)
            bad = urllib.request.Request(base + '/api/health', headers={'Authorization': 'Bearer wrong'})
            with self.assertRaises(urllib.error.HTTPError): urllib.request.urlopen(bad, timeout=5)
            good = {'Authorization': 'Bearer ' + app.host_api_key}
            body = json.loads(urllib.request.urlopen(urllib.request.Request(base + '/api/health', headers=good), timeout=5).read())
            self.assertTrue(body['ok']); self.assertEqual(body['bind'], '127.0.0.1')
            app.store.event('probe', message='hello')
            stream = urllib.request.urlopen(urllib.request.Request(base + '/api/stream?after=0&timeout=5', headers=good), timeout=10)
            self.assertIn('text/event-stream', stream.headers['Content-Type'])
            self.assertIn('event: probe', stream.read().decode())
        finally:
            app.engine.stop_event.set(); app.http.shutdown(); app.close(); app.store.close(); temp.cleanup()


if __name__ == '__main__':
    unittest.main()
