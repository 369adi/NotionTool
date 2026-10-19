"""Chat list guard: guessed ids, unreadable chat list, text-hash reply matching. Fakes only, no network."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from notiontool.engine import PendingRemote
from notiontool.mains import Mains
from notiontool.notion import ChatListUnavailable, NotionAdapter, NotionClient
from notiontool.store import Store

SPACE = '440869f1-58c1-81c8-b3d1-0003ec9a0dfc'
T1 = '11111111-1111-4111-8111-111111111111'
T2 = '22222222-2222-4222-8222-222222222222'


class Transport:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def request(self, path, body, headers=None, timeout=45):
        self.calls.append(path)
        return self.responses.get(path, (500, 'boom'))


def space_record(thread_ids):
    return json.dumps({'recordMap': {'space': {SPACE: {'value': {'value': {'id': SPACE, 'data': {'thread_ids': thread_ids}}}}}}})


def client(responses):
    return NotionClient(Transport(responses), {'space_id': SPACE})


class FakeClient:
    def __init__(self, threads=None, transcript=None):
        self.info = dict(space_id=SPACE, user_id='user', email='test@example.test', user_name='Test', records={})
        self.listed, self.data, self.created, self.transport = threads, transcript, [], self

    def identity(self):
        return self.info

    def model(self, selected='opus'):
        return 'verified-opus-codename'

    def headers(self):
        return {}

    def threads(self):
        if self.listed is None:
            raise ChatListUnavailable('Notion chat list unavailable: test')
        return list(self.listed)

    def transcript(self, thread, limit=100):
        return self.data

    def stream(self, path, body, headers, timeout, cancel=None):
        self.created.append(body)
        return iter(())

    def call(self, path, body, timeout=45):
        raise AssertionError('unexpected call ' + path)


class Base(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).parent / '.test-data'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.store = Store(self.temp.name)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def adapter(self, fake):
        adapter = NotionAdapter(self.store)
        adapter.client = lambda *args, **kwargs: fake
        adapter.apply_chat_pool = lambda *args, **kwargs: {}
        adapter.welcome_body = lambda *args, **kwargs: {}
        adapter.verify_response_model = lambda *args, **kwargs: None
        self.store.put('accounts', dict(id='a', slot=1, email='test@example.test', label='Slot 1', state='ready'))
        return adapter

    def own_worker(self, label='Existing worker'):
        worker = dict(id='a:' + T1, account_id='a', thread_id=T1, purpose='worker', enabled=True,
                      state='ready', lease='', last_used=0, label=label)
        self.store.put('workers', worker)
        return worker


class ThreadsFallbackTests(unittest.TestCase):
    def test_space_record_entries_are_marked_guessed(self):
        c = client({'getInferenceTranscriptsForUser': (400, 'gone'),
                    'syncRecordValuesSpaceInitial': (200, space_record([T1, T2]))})
        self.assertEqual(c.threads(), [{'id': T1, 'guessed': True}, {'id': T2, 'guessed': True}])

    def test_confirmed_list_is_not_guessed(self):
        c = client({'getInferenceTranscriptsForUser': (200, json.dumps({'transcripts': [{'id': T1, 'title': 'x'}]}))})
        self.assertEqual(c.threads(), [{'id': T1, 'title': 'x'}])

    def test_unreadable_list_raises_instead_of_empty(self):
        with self.assertRaises(ChatListUnavailable):
            client({'getInferenceTranscriptsForUser': (400, 'gone')}).threads()

    def test_missing_space_record_raises(self):
        c = client({'getInferenceTranscriptsForUser': (400, 'gone'),
                    'syncRecordValuesSpaceInitial': (200, json.dumps({'recordMap': {}}))})
        with self.assertRaises(ChatListUnavailable):
            c.threads()

    def test_chat_list_unavailable_is_a_value_error(self):
        self.assertTrue(issubclass(ChatListUnavailable, ValueError))


class RefreshGuardTests(Base):
    def test_guessed_entries_never_create_workers(self):
        self.own_worker('Mine')
        adapter = self.adapter(FakeClient(threads=[{'id': T1, 'guessed': True}, {'id': T2, 'guessed': True}]))
        adapter.refresh('a')
        workers = {w['thread_id']: w for w in self.store.all('workers')}
        self.assertEqual(set(workers), {T1})
        self.assertEqual(workers[T1]['label'], 'Mine')
        self.assertEqual(workers[T1]['state'], 'ready')

    def test_confirmed_entries_still_create_workers(self):
        adapter = self.adapter(FakeClient(threads=[{'id': T2, 'title': 'Welcome to Notion'}]))
        adapter.refresh('a')
        self.assertEqual([w['thread_id'] for w in self.store.all('workers')], [T2])

    def test_unreadable_list_raises_and_keeps_workers(self):
        self.own_worker()
        adapter = self.adapter(FakeClient(threads=None))
        with self.assertRaises(ChatListUnavailable):
            adapter.refresh('a')
        self.assertEqual(self.store.get('workers', 'a:' + T1)['state'], 'ready')


class NoChatCreationTests(Base):
    def test_ensure_main_creates_nothing(self):
        fake = FakeClient(threads=None)
        adapter = self.adapter(fake)
        with self.assertRaises(ChatListUnavailable):
            adapter.ensure_main('a')
        self.assertEqual(fake.created, [])
        self.assertEqual(self.store.all('chats'), [])

    def test_ensure_team_creates_nothing(self):
        fake = FakeClient(threads=None)
        adapter = self.adapter(fake)
        with self.assertRaises(ChatListUnavailable):
            adapter.ensure_team('a')
        self.assertEqual(fake.created, [])

    def test_create_welcome_reports_unconfirmed(self):
        fake = FakeClient(threads=None)
        adapter = self.adapter(fake)
        listed, _clean = adapter.create_welcome(fake, T1, 'm')
        self.assertFalse(listed)
        self.assertEqual(len(fake.created), 1)  # the one requested POST only, no fallback

    def test_mains_thread_lookup_propagates(self):
        with self.assertRaises(ChatListUnavailable):
            Mains._threads(FakeClient(threads=None))


def user(seq, text, ident=None):
    return {'op': 'put', 'entity': {'id': ident or 'u%d' % seq, 'kind': 'user_message', 'sequence': seq,
                                    'content': [{'type': 'text', 'content': text}]}}


def reply(seq, text):
    return {'op': 'put', 'entity': {'id': 'r%d' % seq, 'kind': 'assistant_message', 'sequence': seq,
                                    'content': [{'type': 'text', 'content': text}]}}


def done(seq):
    return {'op': 'put', 'entity': {'id': 'd%d' % seq, 'kind': 'turn_completed', 'sequence': seq}}


class TextHashMatchTests(Base):
    PROMPT = 'Do the same thing again'

    def recover(self, patches):
        fake = FakeClient(threads=[], transcript={'patches': patches, 'session': {'status': 'idle'}})
        adapter = self.adapter(fake)
        task = dict(id='task1', thread_id=T1, step_id='not-found', before_seq=5,
                    request_fingerprint=hashlib.sha256(self.PROMPT.encode()).hexdigest())
        return adapter.recover(dict(id='a:' + T1, account_id='a', thread_id=T1), task, None)

    def test_single_match_after_before_seq_is_accepted(self):
        result = self.recover([user(6, self.PROMPT), reply(7, 'answer'), done(8)])
        self.assertEqual(str(result), 'answer')

    def test_earlier_duplicate_prompt_stays_unconfirmed(self):
        with self.assertRaises(PendingRemote):
            self.recover([user(2, self.PROMPT), reply(3, 'old answer'), done(4),
                          user(6, self.PROMPT), reply(7, 'answer'), done(8)])

    def test_only_earlier_match_stays_unconfirmed(self):
        with self.assertRaises(PendingRemote):
            self.recover([user(2, self.PROMPT), reply(3, 'old answer'), done(4)])

    def test_two_matches_after_before_seq_stay_unconfirmed(self):
        with self.assertRaises(PendingRemote):
            self.recover([user(6, self.PROMPT), reply(7, 'a'), done(8),
                          user(9, self.PROMPT), reply(10, 'b'), done(11)])


if __name__ == '__main__':
    unittest.main()
