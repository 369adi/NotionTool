"""Unit tests for notiontool.main_handoff_ops with fake Notion and store objects; no network."""
import copy
import json
import os
import re
import shutil
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

from notiontool import main_handoff as mh
from notiontool import main_handoff_ops as ops_mod
from notiontool.main_handoff_ops import MainHandoffOps, run_rotation, sweep_archives

T0 = 1_000_000.0
ACCT = 'acct'
T_OLD = str(uuid.UUID(int=1))
NAMED = {str(uuid.UUID(int=10 + i)): name for i, name in enumerate(ops_mod.PROTECTED_TITLES)}
TOKEN_RE = re.compile(r'ack token alone on its own line:\n([0-9a-f]+)')


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


class FakeStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.lock = threading.RLock()
        self.data, self.events, self.counters = {}, [], {}

    def get(self, kind, ident, default=None):
        doc = self.data.get(kind, {}).get(str(ident))
        return copy.deepcopy(doc) if doc is not None else default

    def all(self, kind):
        return [copy.deepcopy(d) for d in self.data.get(kind, {}).values()]

    def put(self, kind, doc):
        self.data.setdefault(kind, {})[str(doc['id'])] = copy.deepcopy(doc)
        return doc

    def delete(self, kind, ident):
        self.data.get(kind, {}).pop(str(ident), None)

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

    def event(self, kind, run_id='', *, commit=True, **fields):
        self.events.append((kind, fields))

    def message_page(self, chat_id, limit=100):
        return {'messages': [m for m in self.all('messages') if m.get('chat_id') == chat_id][-limit:]}

    def settings(self):
        return {}


class FakeTransport:
    def __init__(self, client):
        self.client = client

    def stream(self, name, body, headers, timeout=None, cancel=None):
        c = self.client
        if body['kind'] == 'welcome':
            c.welcome_creations += 1
            c.threads_by_id[body['threadId']] = dict(title='Welcome to Notion', alive=True, entities=[])
        else:
            c.user_prompts.append((body['threadId'], body['prompt']))
            c.add(body['threadId'], 'user_message', body['prompt'])
            match = TOKEN_RE.search(body['prompt'])
            reply = ('Read the file.\n' + match.group(1)) if (c.ack and match) else 'I will start right away.'
            c.add(body['threadId'], 'assistant_message', reply)
        return iter(['{}'])


class FakeClient:
    def __init__(self):
        self.info = {'space_id': str(uuid.uuid4()), 'user_id': 'u1'}
        self.threads_by_id = {}
        self.welcome_creations = 0
        self.user_prompts = []
        self.ack = True
        self.seq = 0
        self.transport = FakeTransport(self)

    def add(self, tid, kind, text):
        self.seq += 1
        self.threads_by_id[tid]['entities'].append(dict(kind=kind, content=text, sequence=self.seq))

    def identity(self):
        return {'email': 'a@b.c'}

    def model(self, label=None):
        return 'model-1'

    def headers(self):
        return {}

    def threads(self):
        return [dict(id=t, title=d['title']) for t, d in self.threads_by_id.items() if d['alive']]

    def transcript(self, tid, limit=100):
        ents = self.threads_by_id[tid]['entities']
        return {'session': {'sequence': max([e['sequence'] for e in ents], default=0), 'status': 'idle'},
                'patches': [{'op': 'put', 'entity': dict(e)} for e in ents]}


class FakeAdapter:
    def __init__(self, store):
        self.store = store
        self.cl = FakeClient()
        self.renames, self.deletes = [], []

    def client(self, account_id):
        return self.cl

    def require_workspace_ai(self, account_id, identity):
        return True

    def new_id(self, space_id, tail='00aa'):
        return str(uuid.uuid4())

    def welcome_body(self, client, tid, model):
        return {'kind': 'welcome', 'threadId': tid}

    def body(self, client, tid, step_id, prompt, model, create=False, computer=False):
        return {'kind': 'user', 'threadId': tid, 'step': step_id, 'prompt': prompt}

    def ensure_thread_model(self, client, tid, model):
        return True

    def rename_thread(self, client, tid, title):
        self.renames.append((tid, title))
        self.cl.threads_by_id[tid]['title'] = title

    def delete_thread(self, client, tid):
        self.deletes.append(tid)
        self.cl.threads_by_id[tid]['alive'] = False
        return True

    def refresh(self, account_id):
        known = {w['thread_id'] for w in self.store.all('workers')}
        for t in self.cl.threads():
            if t['id'] not in known:
                self.store.put('workers', dict(id=account_id + ':' + t['id'], account_id=account_id,
                                               thread_id=t['id'], purpose='worker', enabled=False, state='ready'))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = FakeStore(self.tmp)
        self.adapter = FakeAdapter(self.store)
        self.clock = Clock()
        self.emitted = []
        cl = self.adapter.cl
        cl.threads_by_id[T_OLD] = dict(title='Main', alive=True, entities=[])
        for tid, name in NAMED.items():
            cl.threads_by_id[tid] = dict(title=name, alive=True, entities=[])
            self.store.put('workers', dict(id=ACCT + ':' + tid, account_id=ACCT, thread_id=tid,
                                           purpose='worker', enabled=True, state='ready'))
        self.store.put('accounts', dict(id=ACCT, email='a@b.c', state='ready',
                                        team_threads={'main': {'id': T_OLD, 'pending': False}}))
        self.store.put('workers', dict(id=ACCT + ':' + T_OLD, account_id=ACCT, thread_id=T_OLD,
                                       purpose='main', enabled=True, state='ready'))
        self.store.put('chats', dict(id='c_main', title='Main', main_worker=ACCT + ':' + T_OLD))
        self.store.put('messages', dict(id='m1', chat_id='c_main', role='user', text='Please finish task X'))
        self.store.put('messages', dict(id='m2', chat_id='c_main', role='assistant', text='Working on X'))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def emit(self, kind, message='', **fields):
        self.emitted.append((kind, message, fields))

    def rotate(self):
        return run_rotation(self.store, self.adapter, ACCT, self.emit, clock=self.clock)

    def ops(self):
        return MainHandoffOps(self.store, self.adapter, ACCT, old_chat_id=T_OLD, main_chat_id='c_main',
                              clock=self.clock)

    def handoff(self):
        docs = self.store.all('main_handoffs')
        self.assertEqual(len(docs), 1)
        return docs[0]

    def title(self, tid):
        return self.adapter.cl.threads_by_id[tid]['title']

    def assert_named_untouched(self):
        for tid, name in NAMED.items():
            self.assertEqual(self.title(tid), name)
            self.assertTrue(self.adapter.cl.threads_by_id[tid]['alive'])
            self.assertNotIn(tid, self.adapter.deletes)
            self.assertNotIn(tid, [r[0] for r in self.adapter.renames])


class FullRunTest(Base):
    def test_full_run_reaches_done_and_repoints_after_ack(self):
        result = self.rotate()
        self.assertEqual(result['state'], 'done')
        doc = self.handoff()
        new = doc['new_chat_id']
        self.assertEqual(self.title(new), 'Main')
        self.assertTrue(self.title(T_OLD).startswith('Main (Archive '))
        self.assertEqual(self.store.get('chats', 'c_main')['main_worker'], ACCT + ':' + new)
        self.assertEqual(self.store.get('accounts', ACCT)['team_threads']['main']['id'], new)
        self.assertEqual(self.store.get('workers', ACCT + ':' + T_OLD)['purpose'], 'archived_main')
        self.assertEqual(self.adapter.cl.welcome_creations, 1)
        self.assertEqual(len(self.adapter.cl.user_prompts), 1)
        prompt = self.adapter.cl.user_prompts[0][1]
        self.assertIn('Please finish task X', prompt)
        ops = self.ops()
        with open(os.path.join(ops.folder(), ops.file_id() + '.json'), encoding='utf-8') as fh:
            record = json.load(fh)
        self.assertIsNotNone(record['acked_at'])
        self.assertEqual(record['handoff']['messages'][0]['text'], 'Please finish task X')
        self.assertEqual(self.adapter.deletes, [])
        self.assert_named_untouched()


class AckGateTest(Base):
    def test_no_repoint_without_ack_then_ack_later(self):
        self.adapter.cl.ack = False
        result = self.rotate()
        self.assertEqual(result['state'], 'prompt_sent')
        self.assertFalse(result['acked'])
        self.assertEqual(self.store.get('chats', 'c_main')['main_worker'], ACCT + ':' + T_OLD)
        self.assertEqual(self.store.get('accounts', ACCT)['team_threads']['main']['id'], T_OLD)
        self.assertEqual(self.title(T_OLD), 'Main')
        kinds = [e[0] for e in self.emitted]
        self.assertIn('main_handoff_ack_missing', kinds)
        self.assertNotIn('main_handoff_old_archived', kinds)
        # A second run without ack still keeps the old Main and sends nothing new.
        self.assertEqual(self.rotate()['state'], 'prompt_sent')
        self.assertEqual(len(self.adapter.cl.user_prompts), 1)
        self.assertEqual(self.store.get('chats', 'c_main')['main_worker'], ACCT + ':' + T_OLD)
        # The ack arrives later in the new chat.
        doc = self.handoff()
        token = self.store.get(ops_mod.PROMPTS, doc['id'] + ':prompt')['token']
        self.adapter.cl.add(doc['new_chat_id'], 'assistant_message', token)
        self.assertEqual(self.rotate()['state'], 'done')
        self.assertEqual(self.store.get('chats', 'c_main')['main_worker'], ACCT + ':' + doc['new_chat_id'])
        self.assertEqual(len(self.adapter.cl.user_prompts), 1)
        self.assertEqual(self.adapter.cl.welcome_creations, 1)

    def test_wrong_token_is_no_ack(self):
        self.adapter.cl.ack = False
        self.rotate()
        doc = self.handoff()
        self.adapter.cl.add(doc['new_chat_id'], 'assistant_message', 'deadbeefX not the token')
        self.assertFalse(self.ops().confirm_ack(self.handoff()))
        self.assertEqual(self.store.get('chats', 'c_main')['main_worker'], ACCT + ':' + T_OLD)


class NamedChatTest(Base):
    def test_ops_refuse_rename_of_named_chats(self):
        ops = self.ops()
        for tid in NAMED:
            self.assertFalse(ops.rename_chat(tid, 'Main'))
            self.assertFalse(ops.rename_chat(tid, mh.archive_title(T0)))
        self.assertFalse(ops.rename_chat(T_OLD, 'Roblox'))
        self.assert_named_untouched()

    def test_named_chat_never_deleted_even_when_due(self):
        tid = next(t for t, n in NAMED.items() if n == 'Roblox')
        self.store.put('main_handoffs', dict(id='mh_x', account_id=ACCT, old_chat_id=tid, state='done',
                                             archived_at=T0 - 99999, created=T0 - 99999))
        self.store.put(ops_mod.PROMPTS, dict(id='mh_x:prompt', chat_id='x', acked=True, token='t'))
        result = sweep_archives(self.store, self.adapter, clock=self.clock)
        self.assertEqual(result['deleted'], 0)
        self.assertEqual(result['errors'], 1)
        with self.assertRaises(ValueError):
            self.ops().delete_chat(tid)
        self.assert_named_untouched()

    def test_rotation_skips_named_main(self):
        self.adapter.cl.threads_by_id[T_OLD]['title'] = 'Studio432'
        result = self.rotate()
        self.assertEqual(result['state'], 'skipped')
        self.assertEqual(self.store.all('main_handoffs'), [])
        self.assertEqual(self.adapter.cl.welcome_creations, 0)

    def test_full_run_and_sweep_leave_named_chats(self):
        self.rotate()
        self.clock.t = T0 + mh.DELETE_DELAY + 1
        sweep_archives(self.store, self.adapter, clock=self.clock)
        self.assertEqual(self.adapter.deletes, [T_OLD])
        self.assert_named_untouched()


class IdempotencyTest(Base):
    def test_repeat_create_welcome_makes_no_second_chat(self):
        ops = self.ops()
        first = ops.create_welcome(ACCT, 'rid:create')
        second = ops.create_welcome(ACCT, 'rid:create')
        self.assertEqual(first['status'], 'ok')
        self.assertEqual(first['chat_id'], second['chat_id'])
        # Even without our own registry the Mains operation returns the same chat.
        self.store.delete(ops_mod.WELCOMES, 'rid:create')
        third = self.ops().create_welcome(ACCT, 'rid:create')
        self.assertEqual(third['chat_id'], first['chat_id'])
        self.assertEqual(self.adapter.cl.welcome_creations, 1)

    def test_rerun_while_active_creates_no_second_welcome(self):
        self.adapter.cl.ack = False
        self.rotate()
        self.rotate()
        self.rotate()
        self.assertEqual(self.adapter.cl.welcome_creations, 1)
        self.assertEqual(len(self.store.all('main_handoffs')), 1)

    def test_no_duplicate_prompt(self):
        ops = self.ops()
        new = ops.create_welcome(ACCT, 'rid:create')['chat_id']
        ops.summarize({'messages': []})
        self.assertTrue(ops.send_prompt(new, 'hello', 'fp1'))
        self.assertFalse(ops.send_prompt(new, 'hello', 'fp1'))
        self.assertTrue(ops.has_prompt(new, 'fp1'))
        self.assertEqual(len(self.adapter.cl.user_prompts), 1)

    def test_summarize_reuses_unacked_file(self):
        ops = self.ops()
        first = ops.summarize({'messages': []})
        second = self.ops().summarize({'messages': [{'role': 'user', 'text': 'other'}]})
        self.assertEqual(first, second)


class RestartTest(Base):
    def test_restart_between_states(self):
        machine = mh.MainHandoff(self.store, self.ops())
        doc = machine.start(ACCT, T_OLD)
        for expected in ('summary_requested', 'summary_done', 'chat_pending', 'chat_created'):
            self.assertEqual(machine.step(doc['id'])['state'], expected)
        # Process restarts: fresh objects, only the store survives.
        result = self.rotate()
        self.assertEqual(result['state'], 'done')
        self.assertEqual(self.adapter.cl.welcome_creations, 1)
        self.assertEqual(len(self.adapter.cl.user_prompts), 1)

    def test_restart_after_prompt_attempt_does_not_resend(self):
        machine = mh.MainHandoff(self.store, self.ops())
        doc = machine.start(ACCT, T_OLD)
        for _ in range(4):
            machine.step(doc['id'])
        doc = self.store.get('main_handoffs', doc['id'])
        self.assertEqual(doc['state'], 'chat_created')
        machine.step(doc['id'])  # renamed_main
        ops = self.ops()
        ops.send_prompt(doc['new_chat_id'], doc['summary'], doc['id'] + ':prompt')  # sent, then crash before the move
        doc = self.store.get('main_handoffs', doc['id'])
        doc['prompt_attempted'] = True
        self.store.put('main_handoffs', doc)
        self.assertEqual(self.rotate()['state'], 'done')
        self.assertEqual(len(self.adapter.cl.user_prompts), 1)


class EventsTest(Base):
    def test_run_rotation_emits_every_state(self):
        self.rotate()
        states = [k[len('main_handoff_'):] for k, _m, _f in self.emitted
                  if k.startswith('main_handoff_') and k[len('main_handoff_'):] in mh.STATES]
        self.assertEqual(states, ['planned', 'summary_requested', 'summary_done', 'chat_pending',
                                  'chat_created', 'renamed_main', 'prompt_sent', 'old_archived', 'done'])
        for kind, message, fields in self.emitted:
            if kind.startswith('main_handoff_'):
                self.assertEqual(fields.get('account_id'), ACCT)
                self.assertIsInstance(message, str)
        self.assertIn('main_handoff_acked', [k for k, _m, _f in self.emitted])
        # emit carries the events; nothing is logged twice into the store.
        self.assertFalse([k for k, _f in self.store.events if k.startswith('main_handoff_')])

    def test_run_rotation_without_main(self):
        self.store.delete('accounts', ACCT)
        self.store.put('workers', dict(id=ACCT + ':' + T_OLD, account_id=ACCT, thread_id=T_OLD,
                                       purpose='worker', enabled=False, state='ready'))
        self.assertEqual(self.rotate()['state'], 'skipped')


class SweepTest(Base):
    def test_sweep_deletes_only_after_delay(self):
        self.assertEqual(self.rotate()['state'], 'done')
        self.clock.t = T0 + mh.DELETE_DELAY - 1
        self.assertEqual(sweep_archives(self.store, self.adapter, clock=self.clock)['deleted'], 0)
        self.assertTrue(self.adapter.cl.threads_by_id[T_OLD]['alive'])
        with self.assertRaises(ValueError):
            self.ops().delete_chat(T_OLD)
        self.assertEqual(self.adapter.deletes, [])
        self.clock.t = T0 + mh.DELETE_DELAY
        self.assertEqual(sweep_archives(self.store, self.adapter, clock=self.clock)['deleted'], 1)
        self.assertEqual(self.adapter.deletes, [T_OLD])
        self.assertIn('main_archive_deleted', [k for k, _f in self.store.events])
        self.clock.t += 10000
        sweep_archives(self.store, self.adapter, clock=self.clock)
        self.assertEqual(self.adapter.deletes, [T_OLD])

    def test_sweep_keeps_archive_renamed_by_user(self):
        self.rotate()
        self.adapter.cl.threads_by_id[T_OLD]['title'] = 'My notes'
        self.clock.t = T0 + mh.DELETE_DELAY + 5
        result = sweep_archives(self.store, self.adapter, clock=self.clock)
        self.assertEqual(result['deleted'], 0)
        self.assertEqual(self.adapter.deletes, [])

    def test_sweep_ignores_unfinished_handoff(self):
        self.adapter.cl.ack = False
        self.rotate()
        self.clock.t = T0 + 10 * mh.DELETE_DELAY
        self.assertEqual(sweep_archives(self.store, self.adapter, clock=self.clock)['checked'], 0)
        self.assertEqual(self.adapter.deletes, [])


if __name__ == '__main__':
    unittest.main()
