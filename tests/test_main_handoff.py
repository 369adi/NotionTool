"""Tests for notiontool.main_handoff (Main handoff into a fresh Welcome chat).

Fakes only: no network, no real store file, no sleep. The fake clock is ops.now().

Assumed T1 contract. If the real module differs, adjust only the calls marked
CONTRACT below, not the behaviour being tested.
  should_rotate(main, settings) -> bool
      main = dict(idle, turn_running, turns_since_main, compress_requested)
  build_handoff_sources(store, account_id, main_chat_id, settings) -> dict
      key 'messages': newest Main messages, oldest first, total text <= main_handoff_chars
  archive_title(ts) -> 'Main (Archive YYYY-MM-DD HH:MM)' in local time
  due_for_delete(handoff, now) -> bool, True once now >= archived_at + 3600
  MainHandoff(store, ops)
      start(account_id, main_chat_id) -> handoff dict with 'id', or None
      step(handoff_id) -> handoff dict (store kind 'main_handoffs')
      archived_chat_ids(account_id) -> iterable of chat ids
  ops: now(), summarize(sources), create_welcome(account_id, request_id),
       list_chats(account_id), rename_chat(chat_id, title),
       send_prompt(chat_id, text, fingerprint), has_prompt(chat_id, fingerprint),
       last_user_prompt(chat_id), delete_chat (callable or None)
"""
import copy
import time
import unittest

from notiontool import main_handoff as mh

ACCOUNT = 'acc1'
OLD = 'c-main'
USER_CHAT = 'c-user'
STATES = ('planned', 'summary_requested', 'summary_done', 'chat_pending', 'chat_created',
          'renamed_main', 'prompt_sent', 'old_archived', 'done')


class Crash(Exception):
    pass


class FakeStore:
    """In-memory copy of the store.py methods; docs are deep-copied like JSON rows."""

    def __init__(self, settings=None):
        self.objects = {}
        self.events_log = []
        self.messages = []
        self.counters = {}
        self._settings = dict(id='app', main_rotate_turns=60, main_handoff_chars=12000)
        self._settings.update(settings or {})

    def get(self, kind, ident, default=None):
        doc = self.objects.get((kind, str(ident)))
        return copy.deepcopy(doc) if doc is not None else default

    def all(self, kind):
        return [copy.deepcopy(doc) for (k, _), doc in self.objects.items() if k == kind]

    def put(self, kind, doc):
        self.objects[(kind, str(doc['id']))] = copy.deepcopy(doc)
        return doc

    def delete(self, kind, ident):
        self.objects.pop((kind, str(ident)), None)

    def batch(self, records, deletes=()):
        for kind, doc in records:
            self.put(kind, doc)
        for kind, ident in deletes:
            self.delete(kind, ident)

    def next_counter(self, name, records_fn=None):
        self.counters[name] = self.counters.get(name, 0) + 1
        for kind, doc in (records_fn(self.counters[name]) if records_fn else ()):
            self.put(kind, doc)
        return self.counters[name]

    def counter(self, name):
        return self.counters.get(name, 0)

    def event(self, kind, run_id='', *, commit=True, **fields):
        self.events_log.append(dict(kind=kind, run_id=run_id, **copy.deepcopy(fields)))
        return len(self.events_log)

    def events(self, after=0, limit=150):
        rows = [dict(seq=i + 1, **e) for i, e in enumerate(self.events_log)]
        return [r for r in rows if r['seq'] > after][-limit:]

    def message(self, chat_id, role, text, run_id=''):
        doc = dict(id='m%d' % len(self.messages), chat_id=chat_id, role=role, text=text,
                   run_id=run_id, created=float(len(self.messages)))
        self.messages.append(doc)
        return doc

    def message_page(self, chat_id, limit=100):
        rows = [m for m in self.messages if m['chat_id'] == chat_id]
        return dict(messages=copy.deepcopy(rows[-limit:]), has_more=len(rows) > limit)

    def settings(self):
        return dict(self._settings)

    def count(self, kind):
        return sum(1 for e in self.events_log if e['kind'] == kind)


class FakeOps:
    """Records every call. create_welcome returns ok, unclear, or raises."""

    def __init__(self, delete=False):
        self.clock = 1000000.0
        self.calls = []
        self.create_mode = 'ok'
        self.crash_after_send = False
        self.delivered = {}
        self.deleted = []
        self.created = 0
        self.chats = [dict(id=OLD, title='Main'), dict(id=USER_CHAT, title='My research')]
        self.delete_chat = self._delete if delete else None

    def count(self, name):
        return sum(1 for c in self.calls if c[0] == name)

    def now(self):
        return self.clock

    def summarize(self, sources):
        self.calls.append(('summarize',))
        return 'Handoff summary.'

    def create_welcome(self, account_id, request_id=None):
        self.calls.append(('create_welcome', account_id, request_id))
        self.created += 1
        chat_id = 'c-new%d' % self.created
        self.chats.append(dict(id=chat_id, title='Welcome', created=self.clock, request_id=request_id))
        if self.create_mode == 'raise':
            raise Crash('crash after the chat was created')
        if self.create_mode == 'unclear':
            return dict(status='unclear')
        return dict(status='ok', chat_id=chat_id)

    def list_chats(self, account_id):
        self.calls.append(('list_chats', account_id))
        return copy.deepcopy(self.chats)

    def rename_chat(self, chat_id, title):
        self.calls.append(('rename_chat', chat_id, title))
        for chat in self.chats:
            if chat['id'] == chat_id:
                chat['title'] = title

    def send_prompt(self, chat_id, text, fingerprint):
        self.calls.append(('send_prompt', chat_id, fingerprint))
        self.delivered.setdefault((chat_id, fingerprint), text)
        if self.crash_after_send:
            self.crash_after_send = False
            raise Crash('crash after send_prompt')

    def has_prompt(self, chat_id, fingerprint):
        return (chat_id, fingerprint) in self.delivered

    def last_user_prompt(self, chat_id):
        return 'Please continue Phase 7.'

    def _delete(self, chat_id):
        self.calls.append(('delete_chat', chat_id))
        self.deleted.append(chat_id)
        self.chats = [c for c in self.chats if c['id'] != chat_id]


def make(delete=False, **settings):
    store = FakeStore(settings)
    ops = FakeOps(delete=delete)
    store.put('chats', dict(id=OLD, account_id=ACCOUNT, title='Main', role='main'))
    store.put('chats', dict(id=USER_CHAT, account_id=ACCOUNT, title='My research',
                            role='coworker', user_named=True))
    for i in range(5):
        store.message(OLD, 'user' if i % 2 == 0 else 'assistant', 'message %d' % i)
    return store, ops, mh.MainHandoff(store, ops)  # CONTRACT


def drive(handoff, hid, steps=30):
    for _ in range(steps):
        try:
            handoff.step(hid)  # CONTRACT
        except Crash:
            return 'crashed'
    return 'ok'


def doc(store, hid):
    return store.get('main_handoffs', hid)


def main(**fields):
    base = dict(idle=True, turn_running=False, turns_since_main=0, compress_requested=False)
    base.update(fields)
    return base


SETTINGS = dict(main_rotate_turns=60, main_handoff_chars=12000)


class DecisionTests(unittest.TestCase):
    def test_should_rotate_false_while_turn_running(self):
        self.assertFalse(mh.should_rotate(main(idle=False, turn_running=True, turns_since_main=100,
                                               compress_requested=True), SETTINGS))

    def test_should_rotate_false_when_not_idle(self):
        self.assertFalse(mh.should_rotate(main(idle=False, turns_since_main=100), SETTINGS))

    def test_should_rotate_true_at_main_rotate_turns(self):
        self.assertTrue(mh.should_rotate(main(turns_since_main=60), SETTINGS))

    def test_should_rotate_true_on_compress_requested(self):
        self.assertTrue(mh.should_rotate(main(turns_since_main=0, compress_requested=True), SETTINGS))

    def test_should_rotate_false_below_threshold(self):
        self.assertFalse(mh.should_rotate(main(turns_since_main=59), SETTINGS))

    def test_build_handoff_sources_char_limit_keeps_newest(self):
        store = FakeStore()
        for i in range(30):
            store.message(OLD, 'user', ('%02d' % i) * 500)
        sources = mh.build_handoff_sources(store, ACCOUNT, OLD, store.settings())  # CONTRACT
        texts = [m['text'] for m in sources['messages']]
        self.assertTrue(texts)
        self.assertLessEqual(sum(len(t) for t in texts), 12000)
        self.assertEqual(texts[-1], '29' * 500)
        self.assertNotIn('00' * 500, texts)
        self.assertEqual(texts, sorted(texts))

    def test_archive_title_exact_format(self):
        ts = time.mktime((2026, 10, 16, 19, 5, 0, 0, 0, -1))
        self.assertEqual(mh.archive_title(ts), 'Main (Archive 2026-10-16 19:05)')

    def test_due_for_delete_boundary(self):
        handoff = dict(archived_at=1000.0, delete_after=4600.0)
        self.assertFalse(mh.due_for_delete(handoff, 1000.0 + 3599))
        self.assertTrue(mh.due_for_delete(handoff, 1000.0 + 3600))


class FlowTests(unittest.TestCase):
    def test_second_start_same_account_returns_none(self):
        store, ops, handoff = make()
        self.assertIsNotNone(handoff.start(ACCOUNT, OLD))
        self.assertIsNone(handoff.start(ACCOUNT, OLD))
        self.assertIsNone(mh.MainHandoff(store, ops).start(ACCOUNT, OLD))

    def test_restart_in_chat_pending_creates_no_second_chat(self):
        store, ops, handoff = make()
        hid = handoff.start(ACCOUNT, OLD)['id']
        ops.create_mode = 'raise'
        drive(handoff, hid)
        self.assertEqual(doc(store, hid)['state'], 'chat_pending')
        ops.create_mode = 'ok'
        drive(mh.MainHandoff(store, ops), hid)
        self.assertEqual(ops.count('create_welcome'), 1)
        self.assertIn(('rename_chat', 'c-new1', 'Main'), ops.calls)

    def test_unclear_create_is_reconciled_via_list_chats(self):
        store, ops, handoff = make()
        ops.create_mode = 'unclear'
        hid = handoff.start(ACCOUNT, OLD)['id']
        drive(handoff, hid)
        self.assertEqual(ops.count('create_welcome'), 1)
        self.assertGreaterEqual(ops.count('list_chats'), 1)
        self.assertIn(('rename_chat', 'c-new1', 'Main'), ops.calls)

    def test_prompt_sent_exactly_once_across_crash(self):
        store, ops, handoff = make()
        hid = handoff.start(ACCOUNT, OLD)['id']
        ops.crash_after_send = True
        self.assertEqual(drive(handoff, hid), 'crashed')
        drive(mh.MainHandoff(store, ops), hid)
        sends = [c for c in ops.calls if c[0] == 'send_prompt']
        self.assertEqual(len(sends), 1)
        self.assertEqual(len(ops.delivered), 1)
        self.assertIn('Please continue Phase 7.', list(ops.delivered.values())[0])

    def test_old_main_renamed_and_archived(self):
        store, ops, handoff = make()
        hid = handoff.start(ACCOUNT, OLD)['id']
        drive(handoff, hid)
        record = doc(store, hid)
        self.assertEqual(record['state'], 'done')
        self.assertIn(('rename_chat', OLD, mh.archive_title(record['archived_at'])), ops.calls)
        self.assertIn(OLD, list(handoff.archived_chat_ids(ACCOUNT)))
        self.assertIn(OLD, list(mh.MainHandoff(store, ops).archived_chat_ids(ACCOUNT)))

    def test_no_delete_before_3600s(self):
        store, ops, handoff = make(delete=True)
        hid = handoff.start(ACCOUNT, OLD)['id']
        drive(handoff, hid)
        ops.clock = doc(store, hid)['archived_at'] + 3599
        drive(handoff, hid)
        self.assertEqual(ops.deleted, [])
        self.assertEqual(store.count('main_archive_delete_unavailable'), 0)

    def test_delete_op_called_once_after_3600s(self):
        store, ops, handoff = make(delete=True)
        hid = handoff.start(ACCOUNT, OLD)['id']
        drive(handoff, hid)
        ops.clock = doc(store, hid)['archived_at'] + 3600
        drive(handoff, hid)
        drive(mh.MainHandoff(store, ops), hid)
        self.assertEqual(ops.deleted, [OLD])
        self.assertEqual(ops.count('delete_chat'), 1)

    def test_without_delete_op_one_unavailable_event_and_chat_kept(self):
        store, ops, handoff = make(delete=False)
        hid = handoff.start(ACCOUNT, OLD)['id']
        drive(handoff, hid)
        ops.clock = doc(store, hid)['archived_at'] + 7200
        drive(handoff, hid)
        drive(mh.MainHandoff(store, ops), hid)
        self.assertEqual(store.count('main_archive_delete_unavailable'), 1)
        self.assertIn(OLD, [c['id'] for c in ops.chats])
        self.assertEqual(ops.deleted, [])

    def test_user_named_chat_never_touched(self):
        store, ops, handoff = make(delete=True)
        hid = handoff.start(ACCOUNT, OLD)['id']
        drive(handoff, hid)
        ops.clock = doc(store, hid)['archived_at'] + 3600
        drive(handoff, hid)
        touched = [c for c in ops.calls if c[0] in ('rename_chat', 'delete_chat') and c[1] == USER_CHAT]
        self.assertEqual(touched, [])
        self.assertIn(dict(id=USER_CHAT, title='My research'), ops.chats)

    def test_one_event_per_transition(self):
        store, ops, handoff = make()
        hid = handoff.start(ACCOUNT, OLD)['id']
        drive(handoff, hid)
        drive(handoff, hid)
        for state in STATES:
            self.assertEqual(store.count('main_handoff_' + state), 1, state)
        self.assertEqual(store.count('main_handoff_failed'), 0)


if __name__ == '__main__':
    unittest.main()
