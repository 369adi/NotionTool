"""Unit tests for notiontool.main_switch with fake store, delegate and ops; no network, no chats."""
import copy
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from notiontool import main_switch as ms

T0 = 1_000_000.0


class FakeStore:
    def __init__(self, directory, policy='ask'):
        self.directory = Path(directory)
        self.data, self.events, self.puts = {}, [], []
        self._settings = dict(main_context_policy=policy)

    def settings(self):
        return dict(self._settings)

    def get(self, kind, ident, default=None):
        doc = self.data.get(kind, {}).get(str(ident))
        return copy.deepcopy(doc) if doc is not None else default

    def put(self, kind, doc):
        self.puts.append((kind, doc.get('id')))
        self.data.setdefault(kind, {})[str(doc['id'])] = copy.deepcopy(doc)

    def all(self, kind):
        return [copy.deepcopy(d) for d in self.data.get(kind, {}).values()]

    def event(self, kind, run_id='', **fields):
        self.events.append((kind, run_id, fields))


class FakeDelegate:
    def __init__(self):
        self.bodies = []

    def __call__(self, body):
        self.bodies.append(copy.deepcopy(body))
        return dict(ok=True, run_id='r1', state='executing')


class FakeOps:
    supports_operator = True

    def __init__(self, folder, ack=True):
        self._folder, self.ack = folder, ack
        self.created, self.sent, self.acks, self.renamed, self.deleted = [], [], [], [], []

    def now(self):
        return T0

    def folder(self):
        return self._folder

    def file_id(self):
        return 'main_acct'

    def create_welcome(self, account_id, request_id):
        self.created.append((account_id, request_id))
        return dict(status='ok', chat_id='thread-new', created=True)

    def send_prompt(self, chat, text, fingerprint):
        self.sent.append((chat, text, fingerprint))
        return True

    def confirm_ack(self, handoff):
        self.acks.append(dict(handoff))
        return self.ack

    def rename_chat(self, chat_id, title):
        self.renamed.append((chat_id, title))
        return True

    def delete_chat(self, chat_id):
        self.deleted.append(chat_id)


class GapOps(FakeOps):
    supports_operator = False


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.data = os.path.join(self.root, 'data')
        os.makedirs(self.data)
        self.store = FakeStore(self.data)
        self.store.put('workers', dict(id='w_op', purpose='operator', thread_id='thread-old', account_id='acct'))
        self.store.put('chats', dict(id='c_op', operator=True, main_worker='w_op', state='active',
                                     project_path=self.root))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def started(self):
        return ms.start(self.store, 'c_op', FakeDelegate(), 'q1', now=T0)

    def write_brief(self, text='## Goal\nx\n'):
        path = ms.brief_path(self.store, 'q1')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as handle:
            handle.write(text.encode('utf-8'))

    def run_done(self, state='done'):
        self.store.put('runs', dict(id='r1', state=state))


class CheckTests(Base):
    def test_thresholds_and_on_change_write(self):
        worker = self.store.get('workers', 'w_op')
        settings = dict(main_context_ask_tokens=100, main_context_ask_turns=10)
        self.assertIsNone(ms.check(self.store, worker, dict(estimated_tokens=99, turns=9), settings))
        before = len(self.store.puts)
        notice = ms.check(self.store, worker, dict(estimated_tokens=100, turns=1), settings)
        self.assertTrue(notice['ask'])
        self.assertEqual(notice['text'], ms.ASK_TEXT)
        self.assertEqual(notice['usage']['estimated_tokens'], 100)
        self.assertEqual(len(self.store.puts), before + 1)
        self.assertTrue(self.store.get('workers', 'w_op')['context_ask']['ask'])
        ms.check(self.store, worker, dict(estimated_tokens=150, turns=2), settings)
        self.assertEqual(len(self.store.puts), before + 1)  # unchanged notice: no write
        self.assertTrue(ms.check(self.store, worker, dict(estimated_tokens=0, turns=10), settings)['ask'])
        self.assertEqual(len(self.store.puts), before + 2)  # reason changed to turns
        self.assertIsNone(ms.check(self.store, worker, dict(estimated_tokens=0, turns=0), settings))
        self.assertIsNone(self.store.get('workers', 'w_op')['context_ask'])

    def test_defaults_and_non_operator(self):
        worker = self.store.get('workers', 'w_op')
        self.assertIsNone(ms.check(self.store, worker, dict(estimated_tokens=499999, turns=299), {}))
        self.assertTrue(ms.check(self.store, worker, dict(estimated_tokens=1300000, turns=0), {})['ask'])
        coworker = dict(id='w2', purpose='worker')
        self.assertIsNone(ms.check(self.store, coworker, dict(estimated_tokens=10**9, turns=10**6), {}))
        main = dict(id='w3', purpose='main')
        self.assertIsNone(ms.check(self.store, main, dict(estimated_tokens=10**9, turns=10**6), {}))


class StartTests(Base):
    def test_body_shape_and_record(self):
        delegate = FakeDelegate()
        out = ms.start(self.store, 'c_op', delegate, 'q1', now=T0)
        self.assertTrue(out['ok'])
        self.assertEqual(len(delegate.bodies), 1)
        body = delegate.bodies[0]
        self.assertEqual(set(body), {'chat_id', 'goal', 'request_id', 'tasks'})
        self.assertEqual(body['chat_id'], 'c_op')
        self.assertEqual(len(body['tasks']), 1)
        task = body['tasks'][0]
        self.assertEqual(set(task), {'id', 'title', 'prompt', 'acceptance', 'files'})
        self.assertEqual(task['files'], ['data/handoffs/main-brief-q1.md'])
        for word in ('main-brief-q1.md', 'LF', '8000', 'Fixed user rules', 'Next steps', 'Key ids',
                     'project-memory/NotionTool.md', 'work/audit/*.md'):
            self.assertIn(word, task['prompt'])
        record = self.store.get(ms.KIND, 'q1')
        self.assertEqual(record['state'], 'brief_requested')
        self.assertEqual(record['run_id'], 'r1')
        self.assertEqual(record['operator_chat_id'], 'c_op')
        self.assertTrue(record['brief_path'].endswith(os.path.join('handoffs', 'main-brief-q1.md')))

    def test_idempotent_per_request_id(self):
        delegate = FakeDelegate()
        ms.start(self.store, 'c_op', delegate, 'q1', now=T0)
        again = ms.start(self.store, 'c_op', delegate, 'q1', now=T0 + 5)
        self.assertTrue(again['duplicate'])
        self.assertEqual(len(delegate.bodies), 1)
        self.assertEqual(len(self.store.all(ms.KIND)), 1)

    def test_policy_gate(self):
        for policy in ('hint', ''):
            self.store._settings['main_context_policy'] = policy
            delegate = FakeDelegate()
            out = ms.start(self.store, 'c_op', delegate, 'q1', now=T0)
            self.assertFalse(out['ok'])
            self.assertEqual(delegate.bodies, [])
        self.store._settings['main_context_policy'] = 'auto'
        self.assertTrue(ms.start(self.store, 'c_op', FakeDelegate(), 'q1', now=T0)['ok'])

    def test_rejects_non_operator_and_bad_id(self):
        self.store.put('chats', dict(id='c_main', main_worker='w_op', state='active'))
        delegate = FakeDelegate()
        self.assertFalse(ms.start(self.store, 'c_main', delegate, 'q1')['ok'])
        self.assertFalse(ms.start(self.store, 'c_op', delegate, '../x')['ok'])
        self.assertEqual(delegate.bodies, [])

    def test_delegate_error_returns_dict(self):
        def boom(body):
            raise ValueError('No enabled Coworker in this account')
        out = ms.start(self.store, 'c_op', boom, 'q1')
        self.assertFalse(out['ok'])
        self.assertIsNone(self.store.get(ms.KIND, 'q1'))


class AdvanceTests(Base):
    def test_no_chat_before_brief_exists(self):
        record = self.started()
        ops = FakeOps(os.path.join(self.data, 'handoffs'))
        self.store.put('runs', dict(id='r1', state='executing'))
        self.assertEqual(ms.advance(self.store, record, ops, now=T0)['state'], 'brief_requested')
        self.assertEqual(ops.created, [])
        self.run_done()
        out = ms.advance(self.store, record, ops, now=T0)
        self.assertEqual(out['state'], 'failed')
        self.assertIn('missing', out['reason'])
        self.assertEqual(ops.created, [])

    def test_full_switch_with_ack(self):
        record = self.started()
        self.run_done()
        self.write_brief()
        ops = FakeOps(os.path.join(self.data, 'handoffs'), ack=True)
        out = ms.advance(self.store, record, ops, now=T0)
        self.assertEqual(out['state'], 'switched')
        self.assertEqual(ops.created, [('acct', 'main-switch-q1')])
        chat, text, fingerprint = ops.sent[0]
        self.assertEqual(chat, 'thread-new')
        self.assertEqual(fingerprint, 'switch_q1:prompt')
        self.assertIn('ack token', text)
        self.assertEqual(ops.acks[0]['id'], 'switch_q1')
        self.assertEqual(ops.acks[0]['old_chat_id'], 'thread-old')
        self.assertEqual(ops.acks[0]['new_chat_id'], 'thread-new')
        self.assertEqual(ops.renamed[0], ('thread-new', 'Main'))
        self.assertEqual(ops.renamed[1][0], 'thread-old')
        self.assertRegex(ops.renamed[1][1], r'^Main \(expired, \d{4}-\d{2}-\d{2} \d{2}:\d{2}\)$')
        self.assertEqual(ops.deleted, [])
        self.assertEqual(ms.advance(self.store, out, ops, now=T0)['state'], 'switched')
        self.assertEqual(len(ops.created), 1)

    def test_no_ack_stays_prompt_sent(self):
        record = self.started()
        self.run_done()
        self.write_brief()
        ops = FakeOps(os.path.join(self.data, 'handoffs'), ack=False)
        out = ms.advance(self.store, record, ops, now=T0)
        self.assertEqual(out['state'], 'prompt_sent')
        self.assertEqual(ops.renamed, [('thread-new', 'Main')])
        out = ms.advance(self.store, out, ops, now=T0 + 60)
        self.assertEqual(out['state'], 'prompt_sent')
        self.assertEqual(len(ops.created), 1)
        self.assertEqual(len(ops.sent), 1)
        ops.ack = True
        self.assertEqual(ms.advance(self.store, out, ops, now=T0 + 120)['state'], 'switched')

    def test_ops_gap_stops_at_brief_ready(self):
        record = self.started()
        self.run_done()
        self.write_brief()
        ops = GapOps(os.path.join(self.data, 'handoffs'))
        out = ms.advance(self.store, record, ops, now=T0)
        self.assertEqual(out['state'], 'brief_ready')
        self.assertEqual(out['reason'], ms.OPS_GAP_REASON)
        self.assertEqual(ops.created, [])

    def test_failed_run_and_bad_brief(self):
        record = self.started()
        self.run_done('cancelled')
        self.assertEqual(ms.advance(self.store, record, FakeOps(self.data), now=T0)['state'], 'failed')
        self.store.data[ms.KIND].clear()
        record = ms.start(self.store, 'c_op', FakeDelegate(), 'q1', now=T0)
        self.run_done()
        self.write_brief('x\r\ny\r\n')
        out = ms.advance(self.store, record, FakeOps(self.data), now=T0)
        self.assertEqual(out['state'], 'failed')
        self.assertIn('CR', out['reason'])

    def test_never_raises(self):
        record = self.started()
        self.run_done()
        self.write_brief()

        class Broken(FakeOps):
            def create_welcome(self, account_id, request_id):
                raise OSError('network down')
        out = ms.advance(self.store, record, Broken(self.data), now=T0)
        self.assertEqual(out['state'], 'brief_ready')
        self.assertIn('network down', out['error'])


class FileTests(unittest.TestCase):
    def test_module_is_lf_and_english(self):
        raw = Path(ms.__file__).read_bytes()
        self.assertNotIn(b'\r', raw)
        text = raw.decode('utf-8').lower()
        for word in ('bitte', 'nicht ', 'wechseln', 'soll', 'ueber'):
            self.assertNotIn(word, text)


if __name__ == '__main__':
    unittest.main()
