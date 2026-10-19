import unittest
import sys, types, unittest
sys.path.insert(0, r'C:\Users\liket\Downloads\NotionTool-harness\NotionTool')
from notiontool.chat_pool import pool_decision, pick_main, script_mode_failure, COWORK_TITLE, WELCOME_TITLE
from notiontool import notion as N
from notiontool.setup_status import account_setup

DONE = {'alive': True, 'data': {'turn_outcomes': {'a': {'status': 'completed'}}}}
FRESH = {'alive': True, 'data': {}}

class PoolAdmission(unittest.TestCase):
    def test_admitted_welcome_chat_is_enabled_once_joined(self):
        ch, rn, ev = pool_decision({'label': WELCOME_TITLE, 'join_state': 'pending', 'pool_admitted': True}, FRESH)
        self.assertFalse(ch['enabled']); self.assertNotIn('pool_admitted', ch)
        ch, rn, ev = pool_decision({'label': WELCOME_TITLE, 'join_state': 'pending', 'pool_admitted': True}, DONE)
        self.assertTrue(ch['enabled']); self.assertFalse(ch['pool_admitted']); self.assertEqual(ev, 'coworker_joined')

    def test_failed_read_keeps_pool_chat_admitted(self):
        worker = {'label': WELCOME_TITLE, 'join_state': 'joined', 'enabled': True}
        ch, rn, ev = pool_decision(worker, None)
        self.assertFalse(ch['enabled']); self.assertTrue(ch['pool_admitted'])
        worker.update(ch)
        ch, rn, ev = pool_decision(worker, DONE)
        self.assertTrue(ch['enabled']); self.assertFalse(ch['pool_admitted'])

    def test_release_ends_admission(self):
        ch, rn, ev = pool_decision({'label': 'NotionTool', 'join_state': 'joined', 'enabled': True, 'pool_admitted': True}, DONE)
        self.assertFalse(ch['enabled']); self.assertEqual(ch['join_state'], 'released'); self.assertFalse(ch['pool_admitted'])

    def test_unknown_chat_is_never_admitted(self):
        for worker in ({'label': WELCOME_TITLE}, {'label': WELCOME_TITLE, 'join_state': 'pending'}, {'label': COWORK_TITLE}):
            ch = pool_decision(worker, DONE)[0]
            self.assertFalse(ch['enabled'], worker); self.assertNotIn('pool_admitted', ch)


if __name__ == '__main__':
    unittest.main()
