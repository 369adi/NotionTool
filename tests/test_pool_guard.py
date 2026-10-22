import itertools
import tempfile
import unittest

from notiontool.chat_pool import WELCOME_TITLE, pick_main, pool_decision
from notiontool.harness_account import main_chat
from notiontool.store import Store

DONE = {'alive': True, 'data': {'turn_outcomes': {'a': {'status': 'completed'}}}}
ACC = 'acc1'


class FixedPool(unittest.TestCase):
    def test_disabled_welcome_chat_stays_disabled(self):
        for state in ('', 'pending', 'joined'):
            ch, rn, ev = pool_decision({'label': WELCOME_TITLE, 'enabled': False, 'join_state': state}, DONE)
            self.assertFalse(ch['enabled']); self.assertIsNone(rn); self.assertIsNone(ev)
        ch, _, _ = pool_decision({'label': 'Cowork joined'}, DONE)
        self.assertFalse(ch['enabled'])

    def test_enabled_chat_keeps_working(self):
        ch, rn, ev = pool_decision({'label': WELCOME_TITLE, 'enabled': True, 'join_state': 'pending'}, DONE)
        self.assertEqual((ch['enabled'], ch['join_state'], ev), (True, 'joined', 'coworker_joined'))
        ch, rn, _ = pool_decision({'label': 'Cowork joined', 'enabled': True}, DONE)
        # Rule I41: pool_decision never renames; name_coworkers does the naming.
        self.assertTrue(ch['enabled']); self.assertIsNone(rn)
        ch, _, ev = pool_decision({'label': 'Mein Chat', 'enabled': True, 'join_state': 'joined'}, DONE)
        self.assertEqual((ch['enabled'], ch['join_state'], ev), (False, 'released', 'coworker_released'))

    def test_external_and_operator_untouched(self):
        for purpose in ('external', 'operator'):
            for enabled in (True, False):
                w = {'label': WELCOME_TITLE, 'purpose': purpose, 'enabled': enabled, 'join_state': 'joined'}
                self.assertEqual(pool_decision(w, DONE), ({}, None, None))


def W(tid, **kw):
    return dict(dict(id=ACC + ':' + tid, account_id=ACC, thread_id=tid, purpose='main', state='ready',
                     enabled=True, label='Main'), **kw)


class SingleMain(unittest.TestCase):
    def test_two_mains_deterministic_in_any_order(self):
        ws = [W('aaa', last_used=5), W('bbb', last_used=5), W('ccc', last_used=1)]
        for perm in itertools.permutations(ws):
            self.assertEqual(pick_main(list(perm))['thread_id'], 'bbb')
        ws = [W('aaa', created=9), W('zzz', created=2)]
        for perm in itertools.permutations(ws):
            self.assertEqual(pick_main(list(perm))['thread_id'], 'aaa')

    def test_ledger_match_wins(self):
        ws = [W('aaa', last_used=1), W('bbb', last_used=9)]
        for perm in itertools.permutations(ws):
            self.assertEqual(pick_main(list(perm), 'aaa')['thread_id'], 'aaa')

    def test_released_missing_operator_never_picked(self):
        ws = [W('r', join_state='released', last_used=9), W('m', state='missing', last_used=9),
              W('o', purpose='operator', last_used=9), W('e', purpose='external', last_used=9), W('ok', last_used=1)]
        for ledger in ('', 'r', 'm', 'o', 'e'):
            self.assertEqual(pick_main(ws, ledger)['thread_id'], 'ok')
        self.assertIsNone(pick_main(ws[:4], 'r'))


class MainChatAgrees(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = s = Store(self.temp.name)
        s.put('accounts', dict(id=ACC, state='ready', team_threads={}))
        for w in (W('aaa', last_used=1), W('bbb', last_used=7), W('rel', join_state='released', last_used=99),
                  W('op', purpose='operator', last_used=99)):
            s.put('workers', w)
        s.put('chats', dict(id='c-a', main_worker=ACC + ':aaa', state='active'))
        s.put('chats', dict(id='c-b', main_worker=ACC + ':bbb', state='active'))
        s.put('chats', dict(id='c-rel', main_worker=ACC + ':rel', state='active'))
        s.put('chats', dict(id='c-op', main_worker=ACC + ':op', state='active', operator=True))

    def tearDown(self):
        close = getattr(self.store, 'close', None)
        if close:
            close()
        self.temp.cleanup()

    def check(self, ledger):
        acc = self.store.get('accounts', ACC)
        acc['team_threads'] = {'main': {'id': ledger, 'pending': False}} if ledger else {}
        self.store.put('accounts', acc)
        workers = [w for w in self.store.all('workers') if w['account_id'] == ACC]
        chat = main_chat(self.store, ACC)
        self.assertEqual(chat['main_worker'], pick_main(workers, ledger)['id'])
        self.assertNotIn(chat['id'], ('c-rel', 'c-op'))
        return chat['id']

    def test_main_chat_equals_pick_main(self):
        self.assertEqual(self.check(''), 'c-b')
        self.assertEqual(self.check('aaa'), 'c-a')
        self.assertEqual(self.check('rel'), 'c-b')
        self.assertEqual(self.check('op'), 'c-b')

    def test_released_chat_never_returned(self):
        self.store.put('chats', dict(self.store.get('chats', 'c-b'), state='released'))
        self.assertIsNone(main_chat(self.store, ACC))  # picked Main has no live chat: nothing else is substituted


if __name__ == '__main__':
    unittest.main()
