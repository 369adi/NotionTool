"""Safe stream end: empty streams reconcile with the task's own chat, no wrong-chat fallback, 404 means missing. Fakes only, no network."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from notiontool.engine import PendingRemote
from notiontool.notion import NotionAdapter, NotionClient, ThreadMissing
from notiontool.store import Store
from notiontool.vendor.transport import NotionStreamError

SPACE = '440869f1-58c1-81c8-b3d1-0003ec9a0dfc'
T1 = '11111111-1111-4111-8111-111111111111'
T2 = '22222222-2222-4222-8222-222222222222'
STEP = 'step-0001'
PROMPT = 'Edit the files and report'


def user(seq, step=STEP, text=PROMPT):
    return {'op': 'put', 'entity': {'id': step, 'kind': 'user_message', 'sequence': seq,
                                    'content': [{'type': 'text', 'content': text}]}}


def reply(seq, text):
    return {'op': 'put', 'entity': {'id': 'r%d' % seq, 'kind': 'assistant_message', 'sequence': seq,
                                    'content': [{'type': 'text', 'content': text}]}}


def done(seq):
    return {'op': 'put', 'entity': {'id': 'd%d' % seq, 'kind': 'turn_completed', 'sequence': seq}}


def head():
    return {'patches': [], 'session': {'status': 'idle', 'sequence': 5}}


def answered(text='files edited'):
    return {'patches': [user(6), reply(7, text), done(8)], 'session': {'status': 'idle'}}


class FakeClient:
    """Fake client and transport: transcripts are served in order, the stream is empty unless lines are given."""
    def __init__(self, transcripts, lines=None):
        self.info = dict(space_id=SPACE, user_id='user', email='test@example.test')
        self.transcripts, self.lines = list(transcripts), lines
        self.read, self.sent, self.transport = [], [], self

    def headers(self):
        return {}

    def model(self, selected='opus'):
        return 'verified-opus-codename'

    def transcript(self, thread, limit=100):
        self.read.append(thread)
        return self.transcripts.pop(0) if len(self.transcripts) > 1 else self.transcripts[0]

    def stream(self, path, body, headers=None, timeout=60, cancel=None):
        self.sent.append(path)
        for line in self.lines or []:
            yield line
        if self.lines is None:
            raise NotionStreamError('Notion returned an empty inference stream; reconcile the submitted request')

    def call(self, path, body, timeout=45):
        raise AssertionError('unexpected call ' + path)


class StatusTransport:
    def __init__(self, status, text=''):
        self.status, self.text, self.calls = status, text, []

    def request(self, path, body, headers=None, timeout=45):
        self.calls.append((path, body))
        return self.status, self.text

    def stream(self, *args, **kwargs):
        raise AssertionError('recovery must never send')


class Base(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).parent / '.test-data'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.store = Store(self.temp.name)
        self.store.put('accounts', dict(id='a', slot=1, email='test@example.test', label='Slot 1', state='ready', model='m'))
        self.worker = dict(id='a:' + T1, account_id='a', thread_id=T1, purpose='worker', enabled=True,
                           state='ready', lease='', last_used=0, label='Worker')
        self.store.put('workers', dict(self.worker))
        self.store.put('tasks', dict(id='task1', run_id='run1', attempts=1))
        for target, fake in (('notiontool.chat_context.ChatContext.rotate', lambda *a, **k: a[3]),
                             ('notiontool.chat_context.ChatContext.observe', lambda *a, **k: None)):
            patcher = mock.patch(target, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def adapter(self, fake):
        adapter = NotionAdapter(self.store)
        adapter.client = lambda *a, **k: fake
        adapter.verify_response_model = lambda *a, **k: None
        adapter.thread_busy = lambda *a, **k: False
        adapter.ensure_thread_model = lambda *a, **k: None
        adapter.new_id = lambda *a, **k: STEP
        adapter.body = lambda *a, **k: {'threadId': T1}
        adapter.confirm_turn_error = lambda *a, **k: self.fail('no turn error expected')
        return adapter

    def run_task(self, fake):
        task = dict(self.store.get('tasks', 'task1'))
        return self.adapter(fake).run(dict(self.worker), task, PROMPT, threading.Event(), lambda text: None)


class EmptyStreamTests(Base):
    def test_empty_stream_with_completed_reply_is_accepted(self):
        fake = FakeClient([head(), answered()])
        self.assertEqual(str(self.run_task(fake)), 'files edited')
        self.assertEqual(fake.sent, ['runInferenceTranscript'])
        self.assertEqual(set(fake.read), {T1})

    def test_blank_lines_only_reconcile_and_accept(self):
        fake = FakeClient([head(), answered('done')], lines=['', '   '])
        self.assertEqual(str(self.run_task(fake)), 'done')
        self.assertEqual(fake.sent, ['runInferenceTranscript'])

    def test_empty_stream_without_our_message_stays_unconfirmed(self):
        fake = FakeClient([head(), {'patches': [], 'session': {'status': 'idle'}}])
        with self.assertRaises(PendingRemote):
            self.run_task(fake)
        self.assertEqual(fake.sent, ['runInferenceTranscript'])

    def test_empty_stream_without_reply_stays_unconfirmed(self):
        fake = FakeClient([head(), {'patches': [user(6)], 'session': {'status': 'running'}}])
        with self.assertRaises(PendingRemote):
            self.run_task(fake)
        self.assertEqual(fake.sent, ['runInferenceTranscript'])

    def test_old_reply_before_before_seq_is_not_accepted(self):
        old = {'patches': [user(3), reply(4, 'old answer'), done(5)], 'session': {'status': 'idle'}}
        fake = FakeClient([head(), old])
        with self.assertRaises(PendingRemote):
            self.run_task(fake)
        self.assertEqual(fake.sent, ['runInferenceTranscript'])


class OwnThreadTests(Base):
    def test_no_fallback_when_task_thread_is_unknown(self):
        fake = FakeClient([answered()])
        with self.assertRaises(PendingRemote):
            self.adapter(fake).recover(dict(self.worker, thread_id=T2), dict(id='task1', step_id=STEP, before_seq=5), None)
        self.assertEqual(fake.read, [])
        self.assertEqual(fake.sent, [])

    def test_reads_only_the_task_thread(self):
        fake = FakeClient([answered('own chat')])
        task = dict(id='task1', thread_id=T1, step_id=STEP, before_seq=5)
        result = self.adapter(fake).recover(dict(self.worker, thread_id=T2), task, None)
        self.assertEqual(str(result), 'own chat')
        self.assertEqual(fake.read, [T1])


class MissingThreadTests(Base):
    def recover_with(self, transport):
        adapter = self.adapter(None)
        adapter.client = lambda *a, **k: NotionClient(transport, {'space_id': SPACE})
        return adapter.recover(dict(self.worker), dict(id='task1', thread_id=T1, step_id=STEP, before_seq=5), None)

    def test_404_on_own_transcript_raises_thread_missing(self):
        transport = StatusTransport(404, 'Not found')
        with self.assertRaises(ThreadMissing) as caught:
            self.recover_with(transport)
        self.assertEqual(caught.exception.thread_id, T1)
        self.assertEqual([path for path, _ in transport.calls], ['getThreadTranscript'])

    def test_other_transcript_errors_stay_unconfirmed(self):
        with self.assertRaises(PendingRemote):
            self.recover_with(StatusTransport(500, 'boom'))


if __name__ == '__main__':
    unittest.main()
