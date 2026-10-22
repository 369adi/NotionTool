"""Main switch for a pool Main (mode 'pool'): every Main has every Main feature (bug 2026-10-22)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_main_handoff_ops as fakes  # noqa: E402
import test_main_switch_operator as base  # noqa: E402

from notiontool import main_switch as ms  # noqa: E402
from notiontool.main_handoff_ops import switch_ops  # noqa: E402

ACCT, T_OLD, MAIN_WORKER, T0 = base.ACCT, base.T_OLD, base.MAIN_WORKER, base.T0


class PoolSwitchTest(base.OperatorSwitchBase):
    def setUp(self):
        super().setUp()
        self.store.settings = lambda: {'main_context_policy': 'ask'}
        chat = self.store.get('chats', 'c_main')
        chat.update(state='active', project_path=self.tmp)
        self.store.put('chats', chat)

    def start(self, rid='psw1'):
        return ms.start(self.store, 'c_main', lambda body: {}, rid, now=T0,
                        brief_text='## Goal\nKeep going\n\n## Next steps\n- go on\n')

    def test_start_accepts_pool_main(self):
        result = self.start()
        self.assertTrue(result['ok'], result)
        self.assertEqual((result['state'], result['mode']), ('brief_ready', 'pool'))
        self.assertEqual(result['operator_thread'], T_OLD)

    def test_unknown_chat_still_refused(self):
        result = ms.start(self.store, 'nope', lambda body: {}, 'psw2', now=T0, brief_text='## Goal\nx\n')
        self.assertFalse(result['ok'])

    def test_full_pool_switch_creates_welcome_and_repoints(self):
        self.start()
        record = self.store.get(ms.KIND, 'psw1')
        ops = switch_ops(self.store, self.adapter, record, clock=self.clock)
        self.assertTrue(ops.supports_operator and ops.pool)
        result = ms.advance(self.store, record, ops, now=T0)
        self.assertEqual(result['state'], 'switched', result)
        new = result['new_chat_id']
        self.assertEqual(self.adapter.cl.welcome_creations, 1)
        self.assertEqual(self.title(new), 'Main')
        new_worker = self.store.get('workers', ACCT + ':' + new)
        self.assertEqual((new_worker['purpose'], new_worker['enabled']), ('main', True))
        old = self.store.get('workers', MAIN_WORKER)
        self.assertEqual((old['purpose'], old['enabled']), ('archived_main', False))
        chat = self.store.get('chats', 'c_main')
        self.assertEqual(chat['main_worker'], ACCT + ':' + new)
        self.assertEqual(chat['predecessor_worker'], MAIN_WORKER)
        self.assertEqual(self.store.get('accounts', ACCT)['team_threads']['main']['id'], new)
        # The operator Main is untouched; the old pool Main gets the archive title, never deleted.
        self.assertEqual(self.store.get('workers', base.OP_WORKER)['purpose'], 'operator')
        self.assertRegex(self.title(T_OLD), r'^Main \(old, Archive ')
        self.assertEqual(self.adapter.deletes, [])

    def test_no_ack_keeps_old_pool_main(self):
        self.adapter.cl.ack = False
        self.start()
        record = self.store.get(ms.KIND, 'psw1')
        result = ms.advance(self.store, record, switch_ops(self.store, self.adapter, record, clock=self.clock), now=T0)
        self.assertEqual(result['state'], 'prompt_sent')
        self.assertEqual(self.store.get('chats', 'c_main')['main_worker'], MAIN_WORKER)
        self.assertEqual(self.store.get('workers', MAIN_WORKER)['purpose'], 'main')


if __name__ == '__main__':
    unittest.main()
