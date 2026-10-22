"""B46: a coworker still working in Notion after the live stream ended is 'working', not hung."""
import threading
import time

from notiontool import notion as N
from notiontool import engine as E


class FakeStore:
    def __init__(self):
        self.lock = threading.RLock()
        self.data = {}

    def get(self, kind, ident, default=None):
        value = self.data.get((kind, ident))
        return dict(value) if value is not None else default

    def put(self, kind, doc):
        self.data[(kind, doc['id'])] = dict(doc)


def adapter_with(task):
    adapter = object.__new__(N.NotionAdapter)
    adapter.store = FakeStore()
    adapter.store.put('tasks', task)
    return adapter


def turn(n):
    return [{'kind': 'tool', 'sequence': 70 + i * 0.001} for i in range(n)]


def test_running_turn_with_new_steps_is_working():
    a = adapter_with({'id': 't1'})
    assert a.note_remote_progress({'id': 't1'}, turn(5), {'status': 'running'}, {}) is True
    saved = a.store.get('tasks', 't1')
    assert saved['remote_working'] is True and saved['remote_steps'] == 5 and saved['remote_progress_at']


def test_running_turn_without_new_step_for_long_is_not_working():
    a = adapter_with({'id': 't1', 'remote_steps': 5, 'remote_newest_seq': 70.004,
                      'remote_progress_at': time.time() - N.REMOTE_QUIET_SECONDS - 5})
    assert a.note_remote_progress({'id': 't1'}, turn(5), {'status': 'running'}, {}) is False
    assert a.store.get('tasks', 't1')['remote_working'] is False


def test_new_step_after_quiet_period_is_working_again():
    a = adapter_with({'id': 't1', 'remote_steps': 5, 'remote_newest_seq': 70.004,
                      'remote_progress_at': time.time() - N.REMOTE_QUIET_SECONDS - 5})
    assert a.note_remote_progress({'id': 't1'}, turn(6), {'status': 'running'}, {}) is True


def test_idle_session_is_never_working():
    a = adapter_with({'id': 't1'})
    assert a.note_remote_progress({'id': 't1'}, turn(3), {'status': 'idle'}, {}) is False


def test_empty_own_turn_is_not_working():
    a = adapter_with({'id': 't1'})
    assert a.note_remote_progress({'id': 't1'}, [], {'status': 'running'}, {}) is False


def test_working_text_and_constants():
    now = time.time()
    text = E.working_text({'remote_steps': 12, 'remote_progress_at': now - 30}, now)
    assert 'still working' in text and '12 steps' in text and 'nothing is resent' in text
    assert E.REMOTE_QUIET_MINUTES * 60 == N.REMOTE_QUIET_SECONDS
    assert E.REMOTE_WORKING_POLL_SECONDS <= 15
    assert 'remote_working' in E.REMOTE_LIVE_FIELDS


def test_transcript_body_cap_fits_long_coworker_chats():
    # A 10 MB cap cut long transcripts mid-JSON, so recover() could never see the finished turn.
    from notiontool.vendor import transport
    assert transport._MAX_BODY >= 32 * 1024 * 1024


def _adapter_for_fallback(native):
    a = adapter_with({'id': 't1'})
    a.lock = threading.RLock()
    a.transports = {'acc': native} if native is not None else {}
    a.events = []
    a.store.event = lambda kind, run, **kw: a.events.append(kind)
    return a


def test_failed_edge_read_falls_back_to_native_and_is_logged(monkeypatch):
    class FakeClient:
        def __init__(self, transport, identity):
            self.transport = transport

        def transcript(self, thread, limit=100):
            return {'patches': [], 'session': {'status': 'idle'}, 'thread': thread, 'limit': limit}

    monkeypatch.setattr(N, 'NotionClient', FakeClient)
    a = _adapter_for_fallback(object())
    got = a.recover_native({'account_id': 'acc'}, 'th', 12, {'id': 't1', 'run_id': 'r'}, OSError('Edge request timed out'))
    assert got['thread'] == 'th' and got['limit'] == 12
    assert a.events == ['recover_read_failed']


def test_fallback_failure_returns_none_and_is_logged(monkeypatch):
    class BrokenClient:
        def __init__(self, transport, identity):
            pass

        def transcript(self, thread, limit=100):
            raise OSError('offline')

    monkeypatch.setattr(N, 'NotionClient', BrokenClient)
    a = _adapter_for_fallback(object())
    assert a.recover_native({'account_id': 'acc'}, 'th', 12, {'id': 't1'}, OSError('x')) is None
    assert a.events == ['recover_read_failed', 'recover_native_failed']
    assert _adapter_for_fallback(None).recover_native({'account_id': 'acc'}, 'th', 12, {'id': 't1'}, OSError('x')) is None


def test_running_state_is_no_empty_proof():
    # 'running' must never count as proof of an empty finished turn (no resend).
    assert 'running' not in E.EMPTY_PROOF_STATES
