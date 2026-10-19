import tempfile
import unittest
from pathlib import Path

from notiontool.store import Store
from notiontool.chat_release import release_chat
from notiontool.chat_pool import pick_main
from notiontool.harness_account import main_chat

ACC = 'acc1'
USER = ACC + ':user-thread'
MAIN = ACC + ':main-thread'
ROOT = Path(__file__).resolve().parents[1] / 'notiontool'


class ChatReleaseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = s = Store(self.temp.name)
        s.put('accounts', dict(id=ACC, state='ready', team_threads={'main': {'id': 'user-thread', 'pending': False}}))
        s.put('workers', dict(id=USER, account_id=ACC, thread_id='user-thread', purpose='main', enabled=True,
                              state='recovering', lease='L1', label='Welcome to Notion', welcome=True))
        s.put('workers', dict(id=MAIN, account_id=ACC, thread_id='main-thread', purpose='main', enabled=True,
                              state='ready', lease='', label='Welcome to Notion', welcome=True))
        s.put('chats', dict(id='c3', title='Main 3', main_worker=USER, state='active'))
        s.put('chats', dict(id='c4', title='Main 4', main_worker=MAIN, state='active'))
        s.put('runs', dict(id='r1', chat_id='c3', state='cancelled', main_worker=USER))
        s.put('tasks', dict(id='t1', run_id='r1', worker_id=USER, state='blocked', remote_unknown=True, lease='L1'))

    def tearDown(self):
        close = getattr(self.store, 'close', None)
        if close:
            close()
        self.temp.cleanup()

    def workers(self):
        return [w for w in self.store.all('workers') if w['account_id'] == ACC]

    def test_release_detaches_chat_and_frees_the_thread(self):
        result = release_chat(self.store, 'c3')
        self.assertEqual((result['worker_id'], result['closed_tasks'], result['already']), (USER, 1, False))
        chat = self.store.get('chats', 'c3')
        self.assertEqual((chat['state'], chat['main_worker'], chat['released_worker']), ('released', '', USER))
        worker = self.store.get('workers', USER)
        self.assertEqual((worker['purpose'], worker['enabled'], worker['lease'], worker['state']),
                         ('external', False, '', 'ready'))
        task = self.store.get('tasks', 't1')
        self.assertEqual((task['state'], task['remote_unknown'], task['lease']), ('cancelled', False, ''))
        self.assertNotIn('main', self.store.get('accounts', ACC)['team_threads'])
        self.assertEqual(self.store.get('chats', 'c4')['main_worker'], MAIN)

    def test_released_thread_is_never_picked_again(self):
        # Before the release the user's thread wins both lookups: that is the bug being fixed.
        self.assertEqual(main_chat(self.store, ACC)['id'], 'c3')
        self.assertEqual(pick_main(self.workers(), 'user-thread')['id'], USER)
        release_chat(self.store, 'c3')
        self.assertEqual(main_chat(self.store, ACC)['id'], 'c4')
        self.assertEqual(pick_main(self.workers(), 'user-thread')['id'], MAIN)
        self.assertEqual(pick_main(self.workers(), '')['id'], MAIN)

    def test_open_run_of_the_chat_blocks_release(self):
        self.store.put('runs', dict(id='r2', chat_id='c3', state='executing', main_worker=USER))
        with self.assertRaisesRegex(ValueError, 'Cancel the open runs'):
            release_chat(self.store, 'c3')
        self.assertEqual(self.store.get('chats', 'c3')['state'], 'active')
        self.assertEqual(self.store.get('workers', USER)['purpose'], 'main')

    def test_open_run_elsewhere_on_the_thread_blocks_release(self):
        self.store.put('runs', dict(id='r3', chat_id='c4', state='planning', main_worker=MAIN))
        self.store.put('tasks', dict(id='t2', run_id='r3', worker_id=USER, state='running', lease='L2'))
        with self.assertRaisesRegex(ValueError, 'open run still uses'):
            release_chat(self.store, 'c3')
        self.assertEqual(self.store.get('tasks', 't1')['state'], 'blocked')

    def test_second_release_is_a_no_op(self):
        release_chat(self.store, 'c3')
        again = release_chat(self.store, 'c3')
        self.assertEqual((again['already'], again['closed_tasks'], again['worker_id']), (True, 0, USER))

    def test_unknown_chat_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'Chat not found'):
            release_chat(self.store, 'nope')

    def test_routes_and_submit_guard_released_threads(self):
        # Source-level check: the HTTP routes are not exercised here.
        server = (ROOT / 'server.py').read_text(encoding='utf-8')
        self.assertIn("path=='/api/chat/release'", server)
        self.assertEqual(server.count("if worker['purpose'] in ('external','operator'):"), 2)
        engine = (ROOT / 'engine.py').read_text(encoding='utf-8')
        self.assertIn("if chat.get('state') == 'released':", engine)


if __name__ == '__main__':
    unittest.main()
