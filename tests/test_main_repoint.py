import tempfile
import unittest

from notiontool.store import Store
from notiontool.main_repoint import repoint

ACC = 'acc1'
OLD = ACC + ':old-thread'
NEW = ACC + ':new-thread'


class MainRepointTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.store.put('workers', dict(id=OLD, account_id=ACC, purpose='main', enabled=True, state='missing'))
        self.main = dict(id=NEW, account_id=ACC, purpose='main', enabled=True, state='ready')
        self.store.put('workers', self.main)

    def tearDown(self):
        close = getattr(self.store, 'close', None)
        if close:
            close()
        self.temp.cleanup()

    def test_chat_and_open_run_move_to_new_main(self):
        self.store.put('chats', dict(id='c1', title='Slot 2', main_worker=OLD))
        self.store.put('runs', dict(id='r1', state='planning', main_worker=OLD))
        self.assertEqual(repoint(self.store, ACC, self.main), {'chats': 1, 'runs': 1})
        self.assertEqual(self.store.get('chats', 'c1')['main_worker'], NEW)
        self.assertEqual(self.store.get('runs', 'r1')['main_worker'], NEW)

    def test_finished_runs_stay_unchanged(self):
        self.store.put('runs', dict(id='r2', state='done', main_worker=OLD))
        self.store.put('runs', dict(id='r3', state='cancelled', main_worker=OLD))
        self.assertEqual(repoint(self.store, ACC, self.main)['runs'], 0)
        self.assertEqual(self.store.get('runs', 'r2')['main_worker'], OLD)

    def test_healthy_main_of_other_chat_is_not_touched(self):
        other = dict(id=ACC + ':second', account_id=ACC, purpose='main', enabled=True, state='ready')
        self.store.put('workers', other)
        self.store.put('chats', dict(id='c2', title='Other', main_worker=other['id']))
        self.assertEqual(repoint(self.store, ACC, self.main)['chats'], 0)
        self.assertEqual(self.store.get('chats', 'c2')['main_worker'], other['id'])

    def test_other_account_is_not_touched(self):
        self.store.put('chats', dict(id='c3', title='hg', main_worker='acc2:dead'))
        self.store.put('runs', dict(id='r4', state='executing', main_worker='acc2:dead'))
        self.assertEqual(repoint(self.store, ACC, self.main), {'chats': 0, 'runs': 0})

    def test_only_one_chat_takes_the_new_main(self):
        self.store.put('chats', dict(id='c4', title='A', main_worker=OLD))
        self.store.put('chats', dict(id='c5', title='B', main_worker=OLD))
        self.assertEqual(repoint(self.store, ACC, self.main)['chats'], 1)
        owners = [c['id'] for c in self.store.all('chats') if c.get('main_worker') == NEW]
        self.assertEqual(len(owners), 1)

    def test_chat_already_on_new_main_blocks_second_owner(self):
        self.store.put('chats', dict(id='c6', title='Has it', main_worker=NEW))
        self.store.put('chats', dict(id='c7', title='Stale', main_worker=OLD))
        self.assertEqual(repoint(self.store, ACC, self.main)['chats'], 0)
        self.assertEqual(self.store.get('chats', 'c7')['main_worker'], OLD)

    def test_non_main_target_is_ignored(self):
        worker = dict(id=ACC + ':w', account_id=ACC, purpose='worker', enabled=True, state='ready')
        self.store.put('chats', dict(id='c8', title='S', main_worker=OLD))
        self.assertEqual(repoint(self.store, ACC, worker), {'chats': 0, 'runs': 0})


if __name__ == '__main__':
    unittest.main()
