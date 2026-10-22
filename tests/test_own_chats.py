"""Tests for notiontool.own_chats (fakes only, no live Notion, no restart)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from notiontool import own_chats as oc


class ValidateNameTests(unittest.TestCase):
    def reject(self, name, message, existing=()):
        with self.assertRaises(ValueError) as caught:
            oc.validate_name(name, existing)
        self.assertEqual(str(caught.exception), message)

    def test_trims_and_returns_clean_name(self):
        self.assertEqual(oc.validate_name('  Ideas  ', []), 'Ideas')

    def test_empty_and_blank_rejected(self):
        self.reject('', oc.MSG_EMPTY)
        self.reject('   ', oc.MSG_EMPTY)

    def test_non_text_rejected(self):
        self.reject(None, oc.MSG_TYPE)
        self.reject(42, oc.MSG_TYPE)

    def test_80_chars_ok_81_rejected(self):
        self.assertEqual(oc.validate_name('a' * 80), 'a' * 80)
        self.assertEqual(oc.validate_name(' ' + 'a' * 80 + ' '), 'a' * 80)
        self.reject('a' * 81, oc.MSG_LONG)

    def test_one_char_ok(self):
        self.assertEqual(oc.validate_name('x'), 'x')

    def test_control_characters_rejected(self):
        for name in ('Ide\tas', 'Ide\nas', 'A\x00B', 'A\x7fB', 'A\x85B'):
            self.reject(name, oc.MSG_CONTROL)

    def test_main_prefix_rejected(self):
        for name in ('Main', 'Main 2', 'Main Chat', '  Mainframe'):
            self.reject(name, oc.MSG_MAIN)
        self.assertEqual(oc.validate_name('My Main ideas'), 'My Main ideas')
        # With the Main tick a Main name is fine; the tick decides what the chat is.
        self.assertEqual(oc.validate_name('Main', [], allow_main=True), 'Main')
        self.assertEqual(oc.validate_name(' Main 3 ', [], allow_main=True), 'Main 3')

    def test_cowork_number_rejected(self):
        for name in ('Cowork 1', 'Cowork 12', ' Cowork 3 '):
            self.reject(name, oc.MSG_COWORK)
        self.assertEqual(oc.validate_name('Cowork notes'), 'Cowork notes')

    def test_reserved_pool_titles_rejected(self):
        for name in ('Welcome to Notion', 'welcome to notion', 'Cowork joined', 'Cowork (retired)'):
            self.reject(name, oc.MSG_RESERVED % name)

    def test_duplicate_case_insensitive_rejected(self):
        existing = ['NotionTool', ' Ideas ', None, 7]
        self.reject('ideas', oc.MSG_DUPLICATE % 'ideas', existing)
        self.reject('NOTIONTOOL', oc.MSG_DUPLICATE % 'NOTIONTOOL', existing)
        self.assertEqual(oc.validate_name('Ideas 2', existing), 'Ideas 2')


class PlanCreateTests(unittest.TestCase):
    def test_plan_shape(self):
        plan = oc.plan_create('acc1', '  Ideas ', 'req-1')
        self.assertEqual(plan, {
            'op': 'create_own_chat', 'id': 'own:req-1', 'account_id': 'acc1', 'name': 'Ideas',
            'request_id': 'req-1', 'purpose': 'user', 'owner': 'user', 'enabled': False,
            'steps': [{'step': 'create_welcome'}, {'step': 'rename_thread', 'title': 'Ideas'}],
        })
        self.assertEqual([s['step'] for s in plan['steps']], ['create_welcome', 'rename_thread'])

    def test_same_request_id_gives_identical_dict(self):
        first = oc.plan_create('acc1', 'Ideas', 'req-1')
        second = oc.plan_create('acc1', 'Ideas', 'req-1')
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        first['steps'].append('x')
        self.assertEqual(len(oc.plan_create('acc1', 'Ideas', 'req-1')['steps']), 2)
        self.assertNotEqual(oc.plan_create('acc1', 'Ideas', 'req-2')['id'], second['id'])

    def test_missing_fields_rejected(self):
        for account, request, message in (('', 'r', oc.MSG_ACCOUNT), (None, 'r', oc.MSG_ACCOUNT),
                                          ('a', '', oc.MSG_REQUEST), ('a', None, oc.MSG_REQUEST)):
            with self.assertRaises(ValueError) as caught:
                oc.plan_create(account, 'Ideas', request)
            self.assertEqual(str(caught.exception), message)

    def test_invalid_name_rejected(self):
        with self.assertRaises(ValueError) as caught:
            oc.plan_create('a', 'Main', 'r')
        self.assertEqual(str(caught.exception), oc.MSG_MAIN)


class IsUserChatTests(unittest.TestCase):
    def test_worker_records(self):
        self.assertTrue(oc.is_user_chat({'id': 'w1', 'purpose': 'user', 'enabled': False}))
        self.assertFalse(oc.is_user_chat({'id': 'w2', 'purpose': 'worker'}))
        self.assertFalse(oc.is_user_chat({'id': 'w3', 'purpose': 'main'}))

    def test_chat_records(self):
        self.assertTrue(oc.is_user_chat({'id': 'c1', 'owner': 'user'}))
        self.assertTrue(oc.is_user_chat({'id': 'c2', 'data': {'purpose': 'user'}}))
        self.assertTrue(oc.is_user_chat({'id': 'c3', 'data': {'owner': 'user'}}))
        self.assertFalse(oc.is_user_chat({'id': 'c4', 'title': 'Ideas'}))
        self.assertFalse(oc.is_user_chat(None))
        self.assertFalse(oc.is_user_chat('user'))


WORKERS = [
    {'id': 'w1', 'purpose': 'worker'},
    {'id': 'w2', 'purpose': 'worker', 'enabled': False},
    {'id': 'm1', 'purpose': 'main'},
    {'id': 'u1', 'purpose': 'user'},
    {'id': 'w3', 'purpose': 'worker'},
]


class NextCoworkTitleTests(unittest.TestCase):
    def test_with_cowork_names(self):
        self.assertIsNotNone(oc._cowork_module())
        self.assertEqual(oc.next_cowork_title([], None), 'Coworker 1')
        self.assertEqual(oc.next_cowork_title(WORKERS, {}), 'Coworker 4')
        self.assertEqual(oc.next_cowork_title(WORKERS, {'w1': 1, 'w2': 5, 'w3': 2}), 'Coworker 3')

    def test_without_cowork_names(self):
        with mock.patch.dict(sys.modules, {'notiontool.cowork_names': None}):
            self.assertIsNone(oc._cowork_module())
            self.assertEqual(oc.next_cowork_title([], None), 'Coworker 1')
            self.assertEqual(oc.next_cowork_title(WORKERS, {}), 'Coworker 4')
            self.assertEqual(oc.next_cowork_title(WORKERS, {'w1': 1, 'w2': 5, 'w3': 2}), 'Coworker 3')
            # w1 keeps 2, duplicate w3 gets 1, w2 gets 3; 'gone' is ignored -> next free is 4
            self.assertEqual(oc.next_cowork_title(WORKERS, {'w1': 2, 'w3': 2, 'gone': 1}), 'Coworker 4')

    def test_both_paths_agree(self):
        cases = [([], None), (WORKERS, {}), (WORKERS, {'w1': 3}), (WORKERS, {'w1': True, 'w2': 0, 'w3': 'x'})]
        with_module = [oc.next_cowork_title(w, s) for w, s in cases]
        with mock.patch.dict(sys.modules, {'notiontool.cowork_names': None}):
            without = [oc.next_cowork_title(w, s) for w, s in cases]
        self.assertEqual(with_module, without)


if __name__ == '__main__':
    unittest.main()
