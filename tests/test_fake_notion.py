import json
import threading
import time
import uuid

import pytest

from notiontool import fake_notion
from notiontool.fake_notion import FakeClient, FakeState, install, resolve_state_path
from notiontool.notion import (NotionAdapter, ThreadMissing, catalog_entry, session_state, turn_state)
from notiontool.effort_policy import catalog_efforts
from notiontool.vendor.transport import NotionHttpError


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv('NOTIONTOOL_FAKE_STATE', str(tmp_path / 'fake.json'))
    return FakeState()


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


# --- shapes -----------------------------------------------------------------------------

def test_seed_two_accounts_with_main_three_coworkers_two_private(state):
    for key in ('ready', 'noai'):
        kinds = [t['kind'] for t in state.threads_of(key)]
        assert kinds.count('main') == 1 and kinds.count('coworker') == 3 and kinds.count('private') == 2
    assert [t['title'] for t in state.threads_of('ready', 'coworker')] == ['Coworker 1', 'Coworker 2', 'Coworker 3']


def test_identity_shape_and_ai_access(state):
    ready, noai = signed_in(state, 'ready'), signed_in(state, 'noai')
    info = ready.info
    for key in ('user_id', 'email', 'user_name', 'client_version', 'space_id', 'spaces', 'onboarded', 'records'):
        assert key in info
    assert info['email'] == 'main1@fake-notion.test' and info['email'].endswith('.test')
    assert uuid.UUID(info['space_id']).version == 8
    assert info['spaces'] == [{'id': info['space_id'], 'name': 'Fake Workspace One'}]
    assert NotionAdapter.workspace_ai_access(ready.info)['state'] == 'enabled'
    assert NotionAdapter.workspace_ai_access(noai.info)['state'] == 'disabled'
    assert ready.headers()['Origin'] == 'https://fake.notion.invalid'


def test_model_uses_real_labels_and_catalog_cache(state):
    client = signed_in(state)
    assert client.model() == 'albuquerque-quinn'
    assert client.model('sonnet') == 'achira-donut'
    supported, default = catalog_efforts(catalog_entry(client, 'albuquerque-quinn'))
    assert supported and default == 'medium'
    assert len(state.calls('getAvailableModels')) == 1  # second model() read the cached catalog


def test_threads_uses_removed_endpoint_fallback(state):
    client = signed_in(state)
    threads = client.threads()
    assert len(threads) == 6 and all(t['guessed'] is True for t in threads)
    assert [str(uuid.UUID(t['id'])) for t in threads] == [t['id'] for t in state.threads_of('ready')]
    assert state.calls('getInferenceTranscriptsForUser')  # tried first, like live Notion


def test_transcript_and_thread_record_shapes(state):
    client = signed_in(state)
    main = tid(state)
    transcript = client.transcript(main, limit=30)
    assert transcript['session'] == {'status': 'idle', 'sequence': 3}
    kinds = [p['entity']['kind'] for p in transcript['patches'] if p['op'] == 'put']
    assert kinds == ['user_message', 'assistant_message', 'turn_completed']
    assert session_state(transcript) == 'finished'
    record = Adapter().thread_records(client, [main])[main]
    assert record['alive'] is True and record['data']['title'] == 'Main'
    assert record['data']['last_turn_outcome']['completed_time'] >= record['latest_user_or_trigger_time']
    message = client.call('syncRecordValuesSpaceInitial', {'requests': [
        {'pointer': {'table': 'thread_message', 'id': record['messages'][0], 'spaceId': client.info['space_id']}, 'version': -1}]})
    step = list(message['recordMap']['thread_message'].values())[0]['value']['value']['step']
    assert step['type'] == 'user'


def test_send_and_wait_deterministic_answer(state):
    client = signed_in(state)
    worker = tid(state, 'coworker')
    text, step = client.send(worker, 'Please do task A')
    assert text.rstrip().endswith('RESULT: DONE')
    result = client.wait(worker, step)
    assert result['state'] == 'done_reply' and result['text'].endswith('RESULT: DONE')
    assert result['text'] == fake_notion.answer_for('Coworker 1', 'Please do task A', 2)
    record = Adapter().thread_records(client, [worker])[worker]
    assert record['last_turn_outcome']['completed_time'] >= record['latest_user_or_trigger_time']
    assert [c['step_id'] for c in state.calls('runInferenceTranscript')] == [step]


def test_create_thread_is_listed(state):
    client = signed_in(state)
    new = str(uuid.uuid4())
    text, _ = client.send(new, 'hello', create=True)
    assert text.endswith('RESULT: DONE')
    assert new in [t['id'] for t in client.threads()]
    assert state.thread(new)['title'] == 'Welcome to Notion'


def test_real_rename_and_delete_thread(state):
    client = signed_in(state)
    adapter = Adapter()
    private = tid(state, 'private')
    adapter.rename_thread(client, private, 'Renamed chat')
    assert adapter.thread_records(client, [private])[private]['data']['title'] == 'Renamed chat'
    assert adapter.delete_thread(client, private) is True
    assert private not in [t['id'] for t in client.threads()]
    with pytest.raises(ThreadMissing):
        client.transcript(private)
    other = tid(state, 'private', index=1)
    assert client.delete_thread(other) is True  # FakeClient helper sends the same requests


def test_ai_disabled_account_cannot_send(state):
    client = signed_in(state, 'noai')
    with pytest.raises(NotionHttpError) as err:
        client.send(tid(state, key='noai'), 'hi')
    assert err.value.status == 403


def test_unknown_endpoint_is_never_a_silent_success(state):
    client = signed_in(state)
    with pytest.raises(ValueError, match='fake: unhandled endpoint'):
        client.call('getSomethingNew', {})


# --- faults -------------------------------------------------------------------------------

def test_fault_too_large(state):
    client = signed_in(state)
    main = tid(state)
    state.set_fault(main, 'too_large', max_limit=10)
    with pytest.raises(ValueError, match='Response too large'):
        client.call('getThreadTranscript', {'spaceId': client.info['space_id'], 'threadId': main,
                                            'direction': 'backward', 'limit': 100})
    transcript = client.transcript(main, limit=100)  # real retry: 100 -> 25 -> 6
    assert len(transcript['patches']) == 3
    limits = [c for c in state.calls('getThreadTranscript')]
    assert len(limits) == 4


def test_fault_hang(state):
    client = signed_in(state)
    worker = tid(state, 'coworker', index=1)
    state.set_fault(worker, 'hang')
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        client.send(worker, 'never ends', timeout=0.3)
    assert 0.3 <= time.monotonic() - started < 5
    assert session_state(client.transcript(worker)) == 'running'
    cancel = threading.Event()
    threading.Timer(0.2, cancel.set).start()
    text, step = client.send(worker, 'cancel me', timeout=30, cancel=cancel)
    assert text == '' and client.wait(worker, step, timeout=0.2)['state'] == 'running'


def test_fault_interrupted(state):
    client = signed_in(state)
    worker = tid(state, 'coworker', index=2)
    state.set_fault(worker, 'interrupted')
    with pytest.raises(OSError):
        client.send(worker, 'stop halfway', step_id='step-cut')
    result = turn_state(client.transcript(worker), {'step_id': 'step-cut', 'before_seq': 0})
    assert result['text'] and 'RESULT: DONE' not in result['text']
    assert not any(e['kind'] == 'turn_completed' for e in result['own_turn'])
    state.set_fault(worker, 'interrupted', cut=False)
    text, _ = client.send(worker, 'stop halfway again')
    assert text and 'RESULT: DONE' not in text


def test_fault_missing(state):
    client = signed_in(state)
    worker = tid(state, 'coworker')
    state.set_fault(worker, 'missing')
    with pytest.raises(ThreadMissing):
        client.transcript(worker)
    with pytest.raises(NotionHttpError) as err:
        client.send(worker, 'hi')
    assert err.value.status == 404
    assert worker not in [t['id'] for t in client.threads()]
    assert Adapter().thread_records(client, [worker])[worker]['alive'] is False


def test_fault_deleted_delay(tmp_path):
    now = [1000.0]
    state = FakeState(tmp_path / 'clock.json', clock=lambda: now[0])
    client = signed_in(state)
    worker = tid(state, 'coworker')
    state.set_fault(worker, 'deleted_delay', seconds=5)
    assert worker in [t['id'] for t in client.threads()]  # still listed
    with pytest.raises(ThreadMissing):
        client.transcript(worker)
    assert Adapter().thread_records(client, [worker])[worker]['alive'] is False
    now[0] += 6
    assert worker not in [t['id'] for t in client.threads()]


def test_faults_written_by_hand_in_the_state_file(tmp_path):
    path = tmp_path / 'hand.json'
    now = [50.0]
    state = FakeState(path, clock=lambda: now[0])
    worker = state.threads_of('ready', 'coworker')[0]['id']
    data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else state.load()
    data['faults'][worker] = {'deleted_delay': {'seconds': 3}, 'too_large': {'max_limit': 1}}
    path.write_text(json.dumps(data), encoding='utf-8')
    client = signed_in(FakeState(path, clock=lambda: now[0]))
    assert worker in [t['id'] for t in client.threads()]  # delay starts at the first read
    now[0] += 4
    assert worker not in [t['id'] for t in client.threads()]
    with pytest.raises(ValueError):
        state.set_fault(worker, 'no_such_fault')


# --- persistence and install ------------------------------------------------------------

def test_state_persists_across_two_clients(tmp_path, monkeypatch):
    path = tmp_path / 'persist.json'
    monkeypatch.setenv('NOTIONTOOL_FAKE_STATE', str(path))
    first = signed_in(FakeState())
    worker = tid(FakeState(), 'coworker')
    _, step = first.send(worker, 'remember me')
    first.rename_thread(worker, 'Coworker 9')
    second = FakeClient('ready')  # new instance, new state object: reads the same file
    second.identity()
    assert path.exists()
    assert second.wait(worker, step)['state'] == 'done_reply'
    assert second.thread_records([worker])[worker]['data']['title'] == 'Coworker 9'


def test_default_path_is_in_the_data_dir(tmp_path, monkeypatch):
    monkeypatch.delenv('NOTIONTOOL_FAKE_STATE', raising=False)
    assert resolve_state_path(data_dir=tmp_path) == tmp_path / 'fake_notion.json'
    monkeypatch.setenv('NOTIONTOOL_FAKE_STATE', str(tmp_path / 'x.json'))
    assert resolve_state_path(data_dir=tmp_path) == tmp_path / 'x.json'


def test_install_patches_only_client(tmp_path, monkeypatch):
    monkeypatch.delenv('NOTIONTOOL_FAKE_STATE', raising=False)

    class Store:
        directory = tmp_path
        accounts = {'acc1': {'id': 'acc1', 'email': 'MAIN1@fake-notion.test'},
                    'acc2': {'id': 'acc2', 'email': 'noai1@fake-notion.test'}}

        def get(self, table, ident, default=None):
            return self.accounts.get(ident, default) if table == 'accounts' else default

    class Dummy:
        def __init__(self):
            self.store = Store()
            self.other = 'kept'

    adapter = Dummy()
    before = {k: v for k, v in vars(adapter).items()}
    state = install(adapter)
    assert state.path == tmp_path / 'fake_notion.json'
    assert set(vars(adapter)) - set(before) == {'client'} and adapter.other == 'kept'
    client = adapter.client('acc1')
    assert isinstance(client, FakeClient) and client.account_key == 'ready'
    assert client.identity()['email'] == 'main1@fake-notion.test'
    assert adapter.client('acc2', anonymous=True).account_key == 'noai'
    assert adapter.client('acc1').transport is client.transport  # one transport per account
    with pytest.raises(ValueError, match='Sign in'):
        adapter.client('unknown')
