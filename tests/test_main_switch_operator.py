"""Operator mode of MainHandoffOps (main_switch.py): fake store and Notion adapter; no network, no chats."""
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_main_handoff_ops as fakes  # noqa: E402  (FakeStore, FakeAdapter, Clock)

from notiontool import main_switch as ms  # noqa: E402
from notiontool import operator_main  # noqa: E402
from notiontool.main_handoff_ops import (CREATED_CHAT_PREFIX, MainHandoffOps, run_rotation,  # noqa: E402
                                         switch_ops)

ACCT = fakes.ACCT
T_OLD = fakes.T_OLD
T_OP = str(uuid.UUID(int=99))
OP_WORKER = ACCT + ':' + T_OP
MAIN_WORKER = ACCT + ':' + T_OLD
T0 = fakes.T0


class OperatorSwitchBase(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent / '.test-data'
        root.mkdir(exist_ok=True)
        self.tmp = tempfile.mkdtemp(dir=root)
        self.store = fakes.FakeStore(self.tmp)
        self.adapter = fakes.FakeAdapter(self.store)
        self.clock = fakes.Clock()
        cl = self.adapter.cl
        cl.threads_by_id[T_OLD] = dict(title='Main', alive=True, entities=[])
        cl.threads_by_id[T_OP] = dict(title='Operator Main', alive=True, entities=[])
        self.store.put('accounts', dict(id=ACCT, email='a@b.c', state='ready',
                                        team_threads={'main': {'id': T_OLD, 'pending': False}}))
        self.store.put('workers', dict(id=MAIN_WORKER, account_id=ACCT, thread_id=T_OLD, purpose='main',
                                       enabled=True, state='ready'))
        self.store.put('workers', dict(id=OP_WORKER, account_id=ACCT, thread_id=T_OP, purpose='operator',
                                       enabled=False, state='ready'))
        self.store.put('chats', dict(id='c_main', title='Main', main_worker=MAIN_WORKER))
        self.store.put('chats', dict(id='c_op', title='Operator Main', main_worker=OP_WORKER, operator=True,
                                     state='active', project_path=self.tmp))
        self.store.put('messages', dict(id='m1', chat_id='c_main', role='user', text='Please finish task X'))
        self.brief = ms.brief_path(self.store, 'sw1')
        os.makedirs(os.path.dirname(self.brief), exist_ok=True)
        with open(self.brief, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write('## Goal\nKeep going\n')
        self.record = dict(id='sw1', request_id='sw1', state='brief_ready', run_id='r1', operator_chat_id='c_op',
                           operator_worker_id=OP_WORKER, operator_thread=T_OP, account_id=ACCT,
                           brief_path=self.brief, handoff_id='switch_sw1', new_chat_id='', reason='', error='',
                           created=T0, updated=T0)
        self.store.put(ms.KIND, self.record)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ops(self):
        return switch_ops(self.store, self.adapter, self.store.get(ms.KIND, 'sw1'), clock=self.clock)

    def advance(self):
        return ms.advance(self.store, self.store.get(ms.KIND, 'sw1'), self.ops(), now=T0)

    def title(self, tid):
        return self.adapter.cl.threads_by_id[tid]['title']


class OperatorModeTest(OperatorSwitchBase):
    def test_flags_and_separate_file_ids(self):
        normal = MainHandoffOps(self.store, self.adapter, ACCT)
        self.assertFalse(getattr(normal, 'supports_operator', False))
        self.assertEqual(normal.file_id(), 'main_' + ACCT)
        ops = self.ops()
        self.assertTrue(ops.supports_operator)
        self.assertEqual(ops.file_id(), 'switch_sw1')

    def test_full_operator_switch_repoints_operator_chat(self):
        result = self.advance()
        self.assertEqual(result['state'], 'switched', result)
        self.assertTrue(result['archive_renamed'])
        new = result['new_chat_id']
        new_worker = self.store.get('workers', ACCT + ':' + new)
        self.assertEqual((new_worker['purpose'], new_worker['enabled'], new_worker['state']),
                         ('operator', False, 'ready'))
        old = self.store.get('workers', OP_WORKER)
        self.assertEqual((old['purpose'], old['enabled']), ('archived_operator', False))
        chat = operator_main.operator_chat(self.store)
        self.assertEqual(chat['id'], 'c_op')
        self.assertEqual(chat['main_worker'], ACCT + ':' + new)
        self.assertTrue(chat['operator'])
        self.assertNotEqual(chat.get('state'), 'released')
        self.assertEqual(chat['predecessor_worker'], OP_WORKER)
        self.assertEqual(operator_main.operator_chat(self.store, ACCT)['id'], 'c_op')
        # No pool Main was created; the pool Main and its ledger are untouched.
        mains = [w['id'] for w in self.store.all('workers') if w.get('purpose') == 'main' or w.get('enabled')]
        self.assertEqual(mains, [MAIN_WORKER])
        self.assertFalse([c for c in self.store.all('chats') if str(c['id']).startswith(CREATED_CHAT_PREFIX)])
        self.assertEqual(self.store.get('chats', 'c_main')['main_worker'], MAIN_WORKER)
        self.assertEqual(self.store.get('accounts', ACCT)['team_threads']['main']['id'], T_OLD)
        [op] = self.store.all('operations')
        self.assertEqual((op['purpose'], op['chat_id']), ('operator_candidate', ''))
        # Old operator chat renamed 'Main (old, YYYY-MM-DD HH:MM)', never deleted.
        self.assertRegex(self.title(T_OP), r'^Main \(old, \d{4}-\d{2}-\d{2} \d{2}:\d{2}\)$')
        self.assertEqual(self.adapter.deletes, [])
        self.assertEqual(self.adapter.cl.welcome_creations, 1)
        # First message: who the chat is, brief file, project memory, check word.
        [(chat_id, prompt)] = self.adapter.cl.user_prompts
        self.assertEqual(chat_id, new)
        self.assertIn('You are the new Main (operator) of NotionTool', prompt)
        self.assertIn(self.brief, prompt)
        self.assertIn('project-memory\\NotionTool.md', prompt)
        self.assertIn('check word', prompt)
        token = fakes.TOKEN_RE.search(prompt).group(1)
        saved = self.store.get('main_handoff_prompts', 'switch_sw1:prompt')
        self.assertEqual(saved['token'], token)
        self.assertTrue(saved['acked'] and saved['first_task_done'])
        folder = self.ops().folder()
        self.assertTrue(os.path.isfile(os.path.join(folder, 'switch_sw1.json')))
        self.assertFalse(os.path.exists(os.path.join(folder, 'main_' + ACCT + '.json')))

    def test_no_ack_keeps_old_operator_and_no_pool_main(self):
        self.adapter.cl.ack = False
        result = self.advance()
        self.assertEqual(result['state'], 'prompt_sent')
        new_worker = self.store.get('workers', ACCT + ':' + result['new_chat_id'])
        self.assertEqual((new_worker['purpose'], new_worker['enabled']), ('operator_candidate', False))
        self.assertEqual(operator_main.operator_chat(self.store)['main_worker'], OP_WORKER)
        self.assertEqual(self.store.get('workers', OP_WORKER)['purpose'], 'operator')
        self.assertEqual(self.title(T_OP), 'Operator Main')

    def test_rename_rules_for_operator_thread(self):
        ops = self.ops()
        self.assertFalse(ops.rename_chat(T_OP, 'Something else'))
        normal = MainHandoffOps(self.store, self.adapter, ACCT, clock=self.clock)
        self.assertFalse(normal.rename_chat(T_OP, 'Main (Archive 1)'))
        self.assertEqual(self.title(T_OP), 'Operator Main')
        self.assertTrue(ops.rename_chat(T_OP, 'Main (Archive 1)'))
        self.assertEqual(self.title(T_OP), 'Main (Archive 1)')
        self.assertEqual(self.adapter.deletes, [])

    def test_normal_rotation_unchanged_next_to_operator(self):
        result = run_rotation(self.store, self.adapter, ACCT, lambda *a, **k: None, clock=self.clock)
        self.assertEqual(result['state'], 'done')
        new = result['new_chat_id']
        worker = self.store.get('workers', ACCT + ':' + new)
        self.assertEqual((worker['purpose'], worker['enabled']), ('main', True))
        self.assertEqual(self.store.get('workers', MAIN_WORKER)['purpose'], 'archived_main')
        self.assertEqual(self.store.get('chats', 'c_main')['main_worker'], ACCT + ':' + new)
        self.assertEqual(self.store.get('accounts', ACCT)['team_threads']['main']['id'], new)
        [op] = self.store.all('operations')
        self.assertNotIn('purpose', op)
        self.assertEqual(operator_main.operator_chat(self.store)['main_worker'], OP_WORKER)
        folder = MainHandoffOps(self.store, self.adapter, ACCT).folder()
        self.assertTrue(os.path.isfile(os.path.join(folder, 'main_' + ACCT + '.json')))
        self.assertFalse(os.path.exists(os.path.join(folder, 'switch_sw1.json')))


if __name__ == '__main__':
    unittest.main()
