"""I33: the desktop app shows ALL Mains (also user-renamed ones like 'Roblox', several at once)."""
import tempfile
import unittest

from notiontool import desktop_shell as ds
from tests.test_desktop_shell import FakeServer


def seed(store):
    store.put('workers', dict(id='acc:op', account_id='acc', purpose='operator', thread_id='t-op', label='Main',
                              main_number=7))
    store.put('chats', dict(id='c-op', account_id='acc', main_worker='acc:op', operator=True, title='Operator Main'))
    # User took over a Main and renamed it 'Roblox': purpose external, no chats record, disabled.
    store.put('workers', dict(id='acc:roblox', account_id='acc', purpose='external', thread_id='t-roblox',
                              label='Roblox', enabled=False, welcome=True, main_number=3,
                              released_chat='c-rel', released_at=1.0))
    store.put('workers', dict(id='acc:w1', account_id='acc', purpose='worker', thread_id='t-w1',
                              label='Welcome to Notion', retired_threads=['t-old']))
    store.put('workers', dict(id='acc:arch', account_id='acc', purpose='archived_operator', thread_id='t-arch',
                              label='Main', main_number=5))


class DesktopMainsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = FakeServer()
        seed(self.server.store)

    def tearDown(self):
        self.tmp.cleanup()

    def state(self):
        return ds.desktop_state(self.server, ds.load_app_settings(self.tmp.name), now=100.0)

    def threads(self, state):
        return [m['thread_id'] for m in state['mains']]

    def test_renamed_main_shown(self):
        state = self.state()
        roblox = next(m for m in state['mains'] if m['thread_id'] == 't-roblox')
        self.assertEqual(roblox['title'], 'Roblox')
        self.assertNotIn('t-roblox', state['hidden_threads'])

    def test_two_mains_shown(self):
        store = self.server.store
        store.put('workers', dict(id='acc:m2', account_id='acc', purpose='main', thread_id='t-m2',
                                  label='Welcome to Notion', main_number=4))
        state = self.state()
        self.assertEqual(sorted(self.threads(state)), ['t-m2', 't-op', 't-roblox'])
        m2 = next(m for m in state['mains'] if m['thread_id'] == 't-m2')
        self.assertEqual(m2['title'], 'Main 4')
        self.assertTrue(all(m['role'] == 'main' for m in state['mains']))

    def test_coworker_hidden(self):
        state = self.state()
        self.assertNotIn('t-w1', self.threads(state))
        self.assertIn('t-w1', state['hidden_threads'])
        self.assertIn('t-old', state['hidden_threads'])

    def test_archived_and_retired_main_hidden(self):
        store = self.server.store
        store.put('workers', dict(id='acc:ret', account_id='acc', purpose='main', thread_id='t-old'))
        store.put('chats', dict(id='c-arch', account_id='acc', main_worker='acc:arch', title='Old'))
        state = self.state()
        self.assertNotIn('t-arch', self.threads(state))
        self.assertNotIn('t-old', self.threads(state))

    def test_duplicate_thread_listed_once(self):
        store = self.server.store
        store.put('chats', dict(id='c-op2', account_id='acc', main_worker='acc:op', title='Operator Main'))
        state = self.state()
        self.assertEqual(self.threads(state).count('t-op'), 1)
        op = next(m for m in state['mains'] if m['thread_id'] == 't-op')
        self.assertTrue(op['operator'])
        self.assertEqual(op['chat_id'], 'c-op')

    def test_other_account_main_not_listed(self):
        self.server.store.put('workers', dict(id='other:m', account_id='other', purpose='main', thread_id='t-x'))
        self.assertNotIn('t-x', self.threads(self.state()))


if __name__ == '__main__':
    unittest.main()
