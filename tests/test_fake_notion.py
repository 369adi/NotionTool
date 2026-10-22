import json
import os
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from notiontool import fake_notion
from notiontool.fake_notion import FakeClient, FakeState, install, resolve_state_path
from notiontool.notion import (NotionAdapter, ThreadMissing, catalog_entry, session_state, turn_state)
from notiontool.effort_policy import catalog_efforts
from notiontool.vendor.transport import NotionHttpError

STATE_ENV = 'NOTIONTOOL_FAKE_STATE'


def signed_in(state, key='ready'):
    client = FakeClient(key, state=state)
    client.identity()
    return client


def tid(state, kind='main', key='ready', index=0):
    return state.threads_of(key, kind)[index]['id']


class Adapter:
    """Just the real NotionAdapter helpers that rename/delete need (no store, no network)."""
    transaction = NotionAdapter.transaction
    thread_records = NotionAdapter.thread_records
    rename_thread = NotionAdapter.rename_thread
    delete_thread = NotionAdapter.delete_thread


class FakeNotionTestCase(unittest.TestCase):
    """Each test gets its own temp folder and NOTIONTOOL_FAKE_STATE pointing into it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {STATE_ENV: str(self.tmp / 'fake.json')})
        self._env.start()
        self.state = FakeState()

    def tearDown(self):
        self._env.stop()  # restores os.environ, also after os.environ.pop inside a test
        self._tmp.cleanup()


# --- shapes -----------------------------------------------------------------------------

class ShapeTests(FakeNotionTestCase):

    def test_seed_two_accounts_with_main_three_coworkers_two_private(self):
        for key in ('ready', 'noai'):
            kinds = [t['kind'] for t in self.state.threads_of(key)]
            self.assertEqual(kinds.count('main'), 1)
            self.assertEqual(kinds.count('coworker'), 3)
            self.assertEqual(kinds.count('private'), 2)
        self.assertEqual([t['title'] for t in self.state.threads_of('ready', 'coworker')],
                         ['Coworker 1', 'Coworker 2', 'Coworker 3'])

    def test_identity_shape_and_ai_access(self):
        ready, noai = signed_in(self.state, 'ready'), signed_in(self.state, 'noai')
        info = ready.info
        for key in ('user_id', 'email', 'user_name', 'client_version', 'space_id', 'spaces', 'onboarded', 'records'):
            self.assertIn(key, info)
        self.assertEqual(info['email'], 'main1@fake-notion.test')
        self.assertTrue(info['email'].endswith('.test'))
        self.assertEqual(uuid.UUID(info['space_id']).version, 8)
        self.assertEqual(info['spaces'], [{'id': info['space_id'], 'name': 'Fake Workspace One'}])
        self.assertEqual(NotionAdapter.workspace_ai_access(ready.info)['state'], 'enabled')
        self.assertEqual(NotionAdapter.workspace_ai_access(noai.info)['state'], 'disabled')
        self.assertEqual(ready.headers()['Origin'], 'https://fake.notion.invalid')

    def test_model_uses_real_labels_and_catalog_cache(self):
        client = signed_in(self.state)
        self.assertEqual(client.model(), 'albuquerque-quinn')
        self.assertEqual(client.model('sonnet'), 'achira-donut')
        supported, default = catalog_efforts(catalog_entry(client, 'albuquerque-quinn'))
        self.assertTrue(supported)
        self.assertEqual(default, 'medium')
        self.assertEqual(len(self.state.calls('getAvailableModels')), 1)  # second model() read the cache

    def test_threads_uses_removed_endpoint_fallback(self):
        client = signed_in(self.state)
        threads = client.threads()
        self.assertEqual(len(threads), 6)
        self.assertTrue(all(t['guessed'] is True for t in threads))
        self.assertEqual([str(uuid.UUID(t['id'])) for t in threads], [t['id'] for t in self.state.threads_of('ready')])
        self.assertTrue(self.state.calls('getInferenceTranscriptsForUser'))  # tried first, like live Notion

    def test_transcript_and_thread_record_shapes(self):
        client = signed_in(self.state)
        main = tid(self.state)
        transcript = client.transcript(main, limit=30)
        self.assertEqual(transcript['session'], {'status': 'idle', 'sequence': 3})
        kinds = [p['entity']['kind'] for p in transcript['patches'] if p['op'] == 'put']
        self.assertEqual(kinds, ['user_message', 'assistant_message', 'turn_completed'])
        self.assertEqual(session_state(transcript), 'finished')
        record = Adapter().thread_records(client, [main])[main]
        self.assertIs(record['alive'], True)
        self.assertEqual(record['data']['title'], 'Main')
        self.assertGreaterEqual(record['data']['last_turn_outcome']['completed_time'], record['latest_user_or_trigger_time'])
        message = client.call('syncRecordValuesSpaceInitial', {'requests': [
            {'pointer': {'table': 'thread_message', 'id': record['messages'][0], 'spaceId': client.info['space_id']},
             'version': -1}]})
        step = list(message['recordMap']['thread_message'].values())[0]['value']['value']['step']
        self.assertEqual(step['type'], 'user')

    def test_send_and_wait_deterministic_answer(self):
        client = signed_in(self.state)
        worker = tid(self.state, 'coworker')
        text, step = client.send(worker, 'Please do task A')
        self.assertTrue(text.rstrip().endswith('RESULT: DONE'))
        result = client.wait(worker, step)
        self.assertEqual(result['state'], 'done_reply')
        self.assertTrue(result['text'].endswith('RESULT: DONE'))
        self.assertEqual(result['text'], fake_notion.answer_for('Coworker 1', 'Please do task A', 2))
        record = Adapter().thread_records(client, [worker])[worker]
        self.assertGreaterEqual(record['last_turn_outcome']['completed_time'], record['latest_user_or_trigger_time'])
        self.assertEqual([c['step_id'] for c in self.state.calls('runInferenceTranscript')], [step])

    def test_create_thread_is_listed(self):
        client = signed_in(self.state)
        new = str(uuid.uuid4())
        text, _ = client.send(new, 'hello', create=True)
        self.assertTrue(text.endswith('RESULT: DONE'))
        self.assertIn(new, [t['id'] for t in client.threads()])
        self.assertEqual(self.state.thread(new)['title'], 'Welcome to Notion')

    def test_real_rename_and_delete_thread(self):
        client = signed_in(self.state)
        adapter = Adapter()
        private = tid(self.state, 'private')
        adapter.rename_thread(client, private, 'Renamed chat')
        self.assertEqual(adapter.thread_records(client, [private])[private]['data']['title'], 'Renamed chat')
        self.assertIs(adapter.delete_thread(client, private), True)
        self.assertNotIn(private, [t['id'] for t in client.threads()])
        with self.assertRaises(ThreadMissing):
            client.transcript(private)
        other = tid(self.state, 'private', index=1)
        self.assertIs(client.delete_thread(other), True)  # FakeClient helper sends the same requests

    def test_ai_disabled_account_cannot_send(self):
        client = signed_in(self.state, 'noai')
        with self.assertRaises(NotionHttpError) as err:
            client.send(tid(self.state, key='noai'), 'hi')
        self.assertEqual(err.exception.status, 403)

    def test_unknown_endpoint_is_never_a_silent_success(self):
        client = signed_in(self.state)
        with self.assertRaisesRegex(ValueError, 'fake: unhandled endpoint'):
            client.call('getSomethingNew', {})


# --- faults -------------------------------------------------------------------------------

class FaultTests(FakeNotionTestCase):

    def test_fault_too_large(self):
        client = signed_in(self.state)
        main = tid(self.state)
        self.state.set_fault(main, 'too_large', max_limit=10)
        with self.assertRaisesRegex(ValueError, 'Response too large'):
            client.call('getThreadTranscript', {'spaceId': client.info['space_id'], 'threadId': main,
                                                'direction': 'backward', 'limit': 100})
        transcript = client.transcript(main, limit=100)  # real retry: 100 -> 25 -> 6
        self.assertEqual(len(transcript['patches']), 3)
        self.assertEqual(len(self.state.calls('getThreadTranscript')), 4)

    def test_fault_hang(self):
        client = signed_in(self.state)
        worker = tid(self.state, 'coworker', index=1)
        self.state.set_fault(worker, 'hang')
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            client.send(worker, 'never ends', timeout=0.3)
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.3)
        self.assertLess(elapsed, 5)
        self.assertEqual(session_state(client.transcript(worker)), 'running')
        cancel = threading.Event()
        timer = threading.Timer(0.2, cancel.set)
        timer.start()
        self.addCleanup(timer.cancel)
        text, step = client.send(worker, 'cancel me', timeout=30, cancel=cancel)
        self.assertEqual(text, '')
        self.assertEqual(client.wait(worker, step, timeout=0.2)['state'], 'running')

    def test_fault_interrupted(self):
        client = signed_in(self.state)
        worker = tid(self.state, 'coworker', index=2)
        self.state.set_fault(worker, 'interrupted')
        with self.assertRaises(OSError):
            client.send(worker, 'stop halfway', step_id='step-cut')
        result = turn_state(client.transcript(worker), {'step_id': 'step-cut', 'before_seq': 0})
        self.assertTrue(result['text'])
        self.assertNotIn('RESULT: DONE', result['text'])
        self.assertFalse(any(e['kind'] == 'turn_completed' for e in result['own_turn']))
        self.state.set_fault(worker, 'interrupted', cut=False)
        text, _ = client.send(worker, 'stop halfway again')
        self.assertTrue(text)
        self.assertNotIn('RESULT: DONE', text)

    def test_fault_missing(self):
        client = signed_in(self.state)
        worker = tid(self.state, 'coworker')
        self.state.set_fault(worker, 'missing')
        with self.assertRaises(ThreadMissing):
            client.transcript(worker)
        with self.assertRaises(NotionHttpError) as err:
            client.send(worker, 'hi')
        self.assertEqual(err.exception.status, 404)
        self.assertNotIn(worker, [t['id'] for t in client.threads()])
        self.assertIs(Adapter().thread_records(client, [worker])[worker]['alive'], False)

    def test_fault_deleted_delay(self):
        now = [1000.0]
        state = FakeState(self.tmp / 'clock.json', clock=lambda: now[0])
        client = signed_in(state)
        worker = tid(state, 'coworker')
        state.set_fault(worker, 'deleted_delay', seconds=5)
        self.assertIn(worker, [t['id'] for t in client.threads()])  # still listed
        with self.assertRaises(ThreadMissing):
            client.transcript(worker)
        self.assertIs(Adapter().thread_records(client, [worker])[worker]['alive'], False)
        now[0] += 6
        self.assertNotIn(worker, [t['id'] for t in client.threads()])

    def test_faults_written_by_hand_in_the_state_file(self):
        path = self.tmp / 'hand.json'
        now = [50.0]
        state = FakeState(path, clock=lambda: now[0])
        worker = state.threads_of('ready', 'coworker')[0]['id']
        client = signed_in(FakeState(path, clock=lambda: now[0]))

        def write_faults(faults):
            data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else state.load()
            data['faults'][worker] = faults
            path.write_text(json.dumps(data), encoding='utf-8')

        # too_large set by hand: the thread is still alive, so the size limit is what fails.
        write_faults({'too_large': {'max_limit': 1}})
        with self.assertRaisesRegex(ValueError, 'Response too large'):
            client.call('getThreadTranscript', {'spaceId': client.info['space_id'], 'threadId': worker,
                                                'direction': 'backward', 'limit': 100})
        # deleted_delay added by hand next to it: listed until the delay (started at first read) ends.
        write_faults({'deleted_delay': {'seconds': 3}, 'too_large': {'max_limit': 1}})
        self.assertIn(worker, [t['id'] for t in client.threads()])
        now[0] += 4
        self.assertNotIn(worker, [t['id'] for t in client.threads()])
        with self.assertRaises(ValueError):
            state.set_fault(worker, 'no_such_fault')


# --- persistence and install ------------------------------------------------------------

class PersistenceAndInstallTests(FakeNotionTestCase):

    def test_state_persists_across_two_clients(self):
        path = self.tmp / 'persist.json'
        os.environ[STATE_ENV] = str(path)  # restored by tearDown
        first = signed_in(FakeState())
        worker = tid(FakeState(), 'coworker')
        _, step = first.send(worker, 'remember me')
        first.rename_thread(worker, 'Coworker 9')
        second = FakeClient('ready')  # new instance, new state object: reads the same file
        second.identity()
        self.assertTrue(path.exists())
        self.assertEqual(second.wait(worker, step)['state'], 'done_reply')
        self.assertEqual(second.thread_records([worker])[worker]['data']['title'], 'Coworker 9')

    def test_default_path_is_in_the_data_dir(self):
        os.environ.pop(STATE_ENV, None)
        self.assertEqual(resolve_state_path(data_dir=self.tmp), self.tmp / 'fake_notion.json')
        os.environ[STATE_ENV] = str(self.tmp / 'x.json')
        self.assertEqual(resolve_state_path(data_dir=self.tmp), self.tmp / 'x.json')

    def test_install_patches_only_client(self):
        os.environ.pop(STATE_ENV, None)
        directory = self.tmp

        class Store:
            accounts = {'acc1': {'id': 'acc1', 'email': 'MAIN1@fake-notion.test'},
                        'acc2': {'id': 'acc2', 'email': 'noai1@fake-notion.test'}}

            def __init__(self):
                self.directory = directory

            def get(self, table, ident, default=None):
                return self.accounts.get(ident, default) if table == 'accounts' else default

        class Dummy:
            def __init__(self):
                self.store = Store()
                self.other = 'kept'

        adapter = Dummy()
        before = dict(vars(adapter))
        state = install(adapter)
        self.assertEqual(state.path, self.tmp / 'fake_notion.json')
        self.assertEqual(set(vars(adapter)) - set(before), {'client'})
        self.assertEqual(adapter.other, 'kept')
        client = adapter.client('acc1')
        self.assertIsInstance(client, FakeClient)
        self.assertEqual(client.account_key, 'ready')
        self.assertEqual(client.identity()['email'], 'main1@fake-notion.test')
        self.assertEqual(adapter.client('acc2', anonymous=True).account_key, 'noai')
        self.assertIs(adapter.client('acc1').transport, client.transport)  # one transport per account
        with self.assertRaisesRegex(ValueError, 'Sign in'):
            adapter.client('unknown')


# --- review fixes: added faults -------------------------------------------------------------

class AddedFaultTests(FakeNotionTestCase):

    def transcript_call(self, client, thread):
        return client.call('getThreadTranscript', {'spaceId': client.info['space_id'], 'threadId': thread,
                                                   'direction': 'backward', 'limit': 30})

    def test_every_added_fault_can_be_set_and_cleared(self):
        worker = tid(self.state, 'coworker')
        added = ('rate_limit', 'busy', 'empty_stream', 'lost_stream', 'error_step', 'slow',
                 'chat_list_unavailable', 'welcome_not_listed')
        self.assertEqual(fake_notion.FAULTS[:5], ('too_large', 'hang', 'interrupted', 'missing', 'deleted_delay'))
        for name in added:
            self.assertIn(name, fake_notion.FAULTS)
            self.assertIn(name, fake_notion.__doc__)  # described in the file header
            self.state.set_fault(worker, name)
            self.assertIn(name, self.state.faults(worker))
            self.state.clear_fault(worker, name)
            self.assertEqual(self.state.faults(worker), {})
        self.state.set_fault('ready', 'chat_list_unavailable')  # account faults may target the account key
        self.state.clear_fault('ready')
        self.assertEqual(self.state.faults('ready'), {})
        with self.assertRaises(KeyError):
            self.state.set_fault('ready', 'busy')  # thread faults need a thread id

    def test_fault_rate_limit(self):
        from notiontool.engine import RateLimited
        client = signed_in(self.state)
        worker = tid(self.state, 'coworker')
        turns = self.state.thread(worker)['turns']
        self.state.set_fault(worker, 'rate_limit', retry_after=7, times=2)
        with self.assertRaises(RateLimited):
            self.transcript_call(client, worker)
        self.assertEqual(client.transport.retry_after, 7)
        with self.assertRaises(NotionHttpError) as err:
            client.send(worker, 'too fast')
        self.assertEqual(err.exception.status, 429)
        self.assertEqual(err.exception.retry_after, 7)
        self.assertEqual(self.state.thread(worker)['turns'], turns)  # nothing committed
        self.assertEqual(self.state.faults(worker), {})  # times=2 used up: cleared itself
        text, _ = client.send(worker, 'now it works')
        self.assertTrue(text.endswith('RESULT: DONE'))

    def test_fault_busy(self):
        client = signed_in(self.state)
        worker = tid(self.state, 'coworker')
        self.state.set_fault(worker, 'busy')
        transcript = client.transcript(worker)
        self.assertEqual(transcript['session']['status'], 'running')
        self.assertIs(transcript['session_pending'], True)
        self.assertEqual(session_state(transcript), 'running')
        record = Adapter().thread_records(client, [worker])[worker]
        self.assertGreater(record['latest_user_or_trigger_time'], record['data']['last_turn_outcome']['completed_time'])
        turns = self.state.thread(worker)['turns']
        with self.assertRaises(NotionHttpError) as err:
            client.send(worker, 'while busy')
        self.assertEqual(err.exception.status, 409)
        self.assertEqual(self.state.thread(worker)['turns'], turns)
        self.state.clear_fault(worker, 'busy')
        self.assertEqual(session_state(client.transcript(worker)), 'finished')

    def test_fault_empty_stream(self):
        client = signed_in(self.state)
        worker = tid(self.state, 'coworker')
        self.state.set_fault(worker, 'empty_stream')
        text, step = client.send(worker, 'answer silently')
        self.assertEqual(text, '')
        result = client.wait(worker, step)
        self.assertEqual(result['state'], 'done_reply')  # committed although the stream was empty
        self.assertTrue(result['text'].endswith('RESULT: DONE'))

    def test_fault_lost_stream(self):
        client = signed_in(self.state)
        worker = tid(self.state, 'coworker')
        self.state.set_fault(worker, 'lost_stream')
        with self.assertRaises(OSError) as err:
            client.send(worker, 'lost', step_id='step-lost')
        self.assertNotIsInstance(err.exception, NotionHttpError)
        entities = [p['entity'] for p in client.transcript(worker)['patches']]
        self.assertFalse(any(e.get('client_message_id') == 'step-lost' for e in entities))
        self.assertEqual([c['step_id'] for c in self.state.calls('runInferenceTranscript')], ['step-lost'])

    def test_fault_error_step(self):
        client = signed_in(self.state)
        worker = tid(self.state, 'coworker')
        self.state.set_fault(worker, 'error_step', message='model overloaded')
        text, step = client.send(worker, 'fail please')
        self.assertEqual(text, '')
        result = client.wait(worker, step)
        self.assertEqual(result['state'], 'done_error')
        self.assertTrue(any('error' in e['kind'] for e in result['own_turn']))

    def test_fault_slow(self):
        client = signed_in(self.state)
        worker = tid(self.state, 'coworker')
        self.state.set_fault(worker, 'slow', seconds=0.4)
        started = time.monotonic()
        text, _ = client.send(worker, 'take your time')
        self.assertGreaterEqual(time.monotonic() - started, 0.4)
        self.assertTrue(text.endswith('RESULT: DONE'))
        self.state.set_fault(worker, 'slow', seconds=5)
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            client.send(worker, 'too slow', step_id='step-slow', timeout=0.3)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(client.wait(worker, 'step-slow')['state'], 'done_reply')  # arrives late anyway

    def test_fault_chat_list_unavailable(self):
        from notiontool.notion import ChatListUnavailable
        client = signed_in(self.state)
        self.state.set_fault('ready', 'chat_list_unavailable')
        with self.assertRaises(ChatListUnavailable):
            client.threads()
        self.assertEqual(len(signed_in(self.state, 'noai').threads()), 6)  # other account unaffected
        self.state.clear_fault('ready', 'chat_list_unavailable')
        self.assertEqual(len(client.threads()), 6)
        self.state.set_fault(tid(self.state, 'private'), 'chat_list_unavailable')  # set on one of its chats
        with self.assertRaises(ChatListUnavailable):
            client.threads()

    def test_fault_welcome_not_listed(self):
        now = [100.0]
        state = FakeState(self.tmp / 'welcome.json', clock=lambda: now[0])
        client = signed_in(state)
        state.set_fault('ready', 'welcome_not_listed', seconds=5)
        new = str(uuid.uuid4())
        text, _ = client.send(new, 'hello', create=True)
        self.assertTrue(text.endswith('RESULT: DONE'))
        self.assertNotIn(new, [t['id'] for t in client.threads()])
        self.assertEqual(session_state(client.transcript(new)), 'finished')  # alive and readable
        now[0] += 6
        self.assertIn(new, [t['id'] for t in client.threads()])
        private = tid(state, 'private')
        state.set_fault(private, 'welcome_not_listed')  # no seconds: never listed
        now[0] += 1000
        self.assertNotIn(private, [t['id'] for t in client.threads()])
        state.clear_fault(private)
        self.assertIn(private, [t['id'] for t in client.threads()])


# --- review fixes: adapter fake, scenario seed, command line ---------------------------------

class AdapterSeedCliTests(FakeNotionTestCase):

    def setUp(self):
        super().setUp()
        self.stores = []

    def tearDown(self):
        for store in self.stores:  # close the SQLite files before the temporary folder is removed (Windows)
            store.close()
        super().tearDown()

    def store(self, name='app'):
        from notiontool.store import Store
        directory = self.tmp / name
        directory.mkdir(parents=True, exist_ok=True)
        store = Store(directory)
        self.stores.append(store)
        return store

    def adapter(self, store=None):
        adapter = fake_notion.FakeNotionAdapter(store or self.store(), state=self.state, force=True)
        adapter.refresh_on_login = False
        return adapter

    def test_adapter_guard_and_overrides(self):
        store = self.store()
        with mock.patch.dict(os.environ, {'NOTIONTOOL_TEST_INSTANCE': ''}):
            with self.assertRaises(fake_notion.SimulationRefused):
                fake_notion.FakeNotionAdapter(store, state=self.state)
        with mock.patch.dict(os.environ, {'NOTIONTOOL_TEST_INSTANCE': '1'}):
            adapter = fake_notion.FakeNotionAdapter(store, state=self.state)
        self.assertIsInstance(adapter, NotionAdapter)
        self.assertIs(adapter.simulated, True)
        overridden = {n for n in vars(fake_notion.FakeNotionAdapter)
                      if n in vars(NotionAdapter) and not n.startswith('__') and callable(vars(NotionAdapter)[n])}
        self.assertEqual(overridden, {'client', 'edge_transport', 'browser_login', 'adopt_session_cookies'})

    def test_adapter_login_client_and_edge_stub(self):
        adapter = self.adapter()
        signed = adapter.browser_login()
        self.assertEqual(signed['email'], 'main1@fake-notion.test')
        self.assertEqual(signed['state'], 'signed_in')
        client = adapter.client(signed['account_id'])
        self.assertIsInstance(client, FakeClient)
        self.assertEqual(client.account_key, 'ready')
        self.assertEqual(client.info['email'], 'main1@fake-notion.test')  # identity from the store record
        edge = adapter.edge_transport(signed['account_id'])
        self.assertIs(edge, client.transport)
        self.assertIs(adapter.edge_transports[signed['account_id']], edge)
        with self.assertRaisesRegex(RuntimeError, 'not available in simulation'):
            edge.browser
        edge.close()
        self.assertTrue(edge.closed)
        noai = adapter.adopt_session_cookies([{'name': 'token_v2', 'value': 'fake:noai'}])
        self.assertEqual(noai['email'], 'noai1@fake-notion.test')
        self.assertEqual(adapter.browser_login()['account_id'], signed['account_id'])  # same account again
        with self.assertRaises(ValueError):
            adapter.adopt_session_cookies([{'name': 'other', 'value': 'x'}])

    def test_unhandled_endpoint_logs_event_and_keeps_400(self):
        store = self.store()
        adapter = self.adapter(store)
        signed = adapter.browser_login()
        client = adapter.client(signed['account_id'])
        with self.assertRaisesRegex(ValueError, 'fake: unhandled endpoint getSomethingNew'):
            client.call('getSomethingNew', {})
        with self.assertRaises(NotionHttpError) as err:
            list(client.transport.stream('streamSomethingNew', {}))
        self.assertEqual(err.exception.status, 400)
        self.assertEqual([e['path'] for e in self.state.events('fake_unhandled_endpoint')],
                         ['getSomethingNew', 'streamSomethingNew'])
        logged = [e for e in store.events() if e['kind'] == 'fake_unhandled_endpoint']
        self.assertEqual([e['path'] for e in logged], ['getSomethingNew', 'streamSomethingNew'])

    def test_seed_scenario_writes_store_records(self):
        store = self.store()
        added = fake_notion.seed_scenario(store, self.state)
        self.assertEqual((added['accounts'], added['workers'], added['chats']), (2, 12, 2))
        self.assertEqual(store.get('accounts', 'ready')['state'], 'ready')
        self.assertTrue(store.get('accounts', 'ready')['model'])
        self.assertTrue(store.get('accounts', 'ready')['simulated'])
        self.assertEqual(store.get('accounts', 'noai')['state'], 'ai_disabled')
        workers = [w for w in store.all('workers') if w['account_id'] == 'ready']
        self.assertEqual(sorted(w['purpose'] for w in workers), ['main', 'private', 'private', 'worker', 'worker', 'worker'])
        coworker = store.get('workers', 'ready:' + tid(self.state, 'coworker'))
        self.assertEqual(coworker['label'], 'Coworker 1')
        self.assertEqual(store.get('chats', 'c_fake_ready')['main_worker'], 'ready:' + tid(self.state))
        again = fake_notion.seed_scenario(store, self.state)
        self.assertEqual((again['accounts'], again['workers'], again['chats']), (0, 0, 0))
        client = self.adapter(store).client('ready')  # seeded record works with the adapter
        self.assertEqual(len(client.threads()), 6)
        with self.assertRaises(ValueError):
            fake_notion.seed_scenario(store, self.state, name='unknown')

    def test_seed_scenario_writes_joined_workers_and_team_ledger(self):
        store = self.store()
        fake_notion.seed_scenario(store, self.state)
        for key in ('ready', 'noai'):
            workers = [w for w in store.all('workers') if w['account_id'] == key]
            coworkers = [w for w in workers if w['purpose'] == 'worker']
            self.assertEqual(len(coworkers), 3)
            for worker in coworkers:
                self.assertEqual((worker['join_state'], worker['enabled'], worker['pool_admitted']), ('joined', True, True))
            private = [w for w in workers if w['purpose'] == 'private']
            self.assertEqual(len(private), 2)
            for worker in private:
                self.assertFalse(worker['enabled'])
                self.assertNotIn('join_state', worker)
            ledger = store.get('accounts', key)['team_threads']
            expected = {'main': {'id': tid(self.state, 'main', key), 'pending': False}}
            for number in (1, 2, 3):
                expected['worker%d' % number] = {'id': tid(self.state, 'coworker', key, number - 1), 'pending': False}
            self.assertEqual(ledger, expected)  # same shape as notion.py ensure_team; no private chat in it
        before = store.get('accounts', 'ready')['team_threads']
        again = fake_notion.seed_scenario(store, self.state)
        self.assertEqual((again['accounts'], again['workers'], again['chats']), (0, 0, 0))
        self.assertEqual(store.get('accounts', 'ready')['team_threads'], before)

    def test_team_setup_after_seed_creates_no_welcome_chat(self):
        store = self.store()
        fake_notion.seed_scenario(store, self.state)
        adapter = self.adapter(store)
        adapter.welcome_gap_s = 0
        before = {key: sorted(t['id'] for t in self.state.threads_of(key)) for key in ('ready', 'noai')}
        welcome_before = [t for t in self.state.threads_of('ready') if t['title'] == fake_notion.WELCOME_TITLE]
        result = adapter.ensure_team('ready')
        self.assertEqual(result['team_size'], 3)
        after = {key: sorted(t['id'] for t in self.state.threads_of(key)) for key in ('ready', 'noai')}
        self.assertEqual(after, before)  # no thread was created in either simulated account
        welcome_after = [t for t in self.state.threads_of('ready') if t['title'] == fake_notion.WELCOME_TITLE]
        self.assertEqual(len(welcome_after), len(welcome_before))
        self.assertEqual(welcome_after, [])
        self.assertEqual(sorted(store.get('accounts', 'ready')['team_threads']), ['main', 'worker1', 'worker2', 'worker3'])

    def test_seed_sets_team_size_over_existing_settings_without_per_account(self):
        store = self.store()
        store.put('settings', dict(id='app', harness_account_id='ready', coworker_target=3, sample='keep'))
        fake_notion.seed_scenario(store, self.state)
        settings = store.get('settings', 'app')
        self.assertEqual(settings['per_account'], 3)
        self.assertEqual(settings['sample'], 'keep')  # existing values stay
        store.put('settings', dict(settings, per_account=5))
        fake_notion.seed_scenario(store, self.state)
        self.assertEqual(store.get('settings', 'app')['per_account'], 5)  # an explicit value is not overwritten
        store.put('settings', dict(settings, per_account=3))
        adapter = self.adapter(store)
        adapter.welcome_gap_s = 0
        before = {key: sorted(t['id'] for t in self.state.threads_of(key)) for key in ('ready', 'noai')}
        adapter.ensure_team('ready')
        after = {key: sorted(t['id'] for t in self.state.threads_of(key)) for key in ('ready', 'noai')}
        self.assertEqual(after, before)  # team setup found all roles filled: no new thread

    def run_main(self, *argv):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = fake_notion.main(['--state', str(self.state.path)] + list(argv))
        return code, (json.loads(out.getvalue()) if out.getvalue() else None), err.getvalue()

    def test_main_set_clear_show_reset(self):
        worker = tid(self.state, 'coworker')
        code, out, _ = self.run_main('set-fault', 'ready/Coworker 1', 'hang', 'seconds=5')
        self.assertEqual(code, 0)
        self.assertEqual(out, {'target': worker, 'faults': {'hang': {'seconds': 5}}})
        code, out, _ = self.run_main('show', 'ready/Coworker 1')
        self.assertEqual(code, 0)
        self.assertEqual(out['accounts']['ready']['threads'][0]['faults'], {'hang': {'seconds': 5}})
        code, out, _ = self.run_main('set-fault', worker, 'deleted_delay', 'seconds=60')
        self.assertEqual(sorted(out['faults']), ['deleted_delay', 'hang'])
        code, out, _ = self.run_main('clear-fault', worker, 'hang')
        self.assertEqual(sorted(out['faults']), ['deleted_delay'])
        code, out, _ = self.run_main('clear-fault', worker)
        self.assertEqual(out['faults'], {})
        code, _, err = self.run_main('set-fault', 'ready/No such chat', 'hang')
        self.assertEqual(code, 2)
        self.assertIn('no simulated thread', err)
        code, _, err = self.run_main('set-fault', worker, 'explode')
        self.assertEqual(code, 2)
        self.assertIn('unknown fault', err)
        self.state.set_fault(worker, 'missing')
        code, out, _ = self.run_main('reset')
        self.assertEqual((code, out['reset']), (0, True))
        self.assertEqual(self.state.faults(worker), {})
        code, out, _ = self.run_main('show')
        self.assertEqual(sorted(out['accounts']), ['noai', 'ready'])
        self.assertEqual(len(out['accounts']['ready']['threads']), 6)

    def test_main_seed(self):
        import contextlib
        import io
        data_dir = self.tmp / 'seeded'
        data_dir.mkdir()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = fake_notion.main(['--state', str(self.state.path), '--data-dir', str(data_dir), 'seed'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())['workers'], 12)
        self.assertEqual(self.store('seeded').get('accounts', 'noai')['state'], 'ai_disabled')
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(fake_notion.main(['--state', str(self.state.path), 'seed']), 2)  # needs --data-dir

    def test_module_entry_point(self):
        import subprocess
        import sys
        root = Path(__file__).resolve().parents[1]
        done = subprocess.run([sys.executable, '-m', 'notiontool.fake_notion', '--state', str(self.state.path), 'show'],
                              cwd=str(root), capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('ready', json.loads(done.stdout)['accounts'])


if __name__ == '__main__':
    unittest.main()
