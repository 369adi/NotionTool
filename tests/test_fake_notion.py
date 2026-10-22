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


if __name__ == '__main__':
    unittest.main()
