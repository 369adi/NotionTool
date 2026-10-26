"""switch_chat_id: /api/main/switch/start finds the right Main chat (bug 2026-10-25: pool Mains were refused)."""
import tempfile
import unittest

from notiontool.operator_main import switch_chat_id
from tests.test_main_switch import FakeStore


def store_with(*mains, operator=None):
    store = FakeStore(tempfile.mkdtemp())
    for chat_id, worker_id, thread_id, purpose in mains:
        store.put('workers', dict(id=worker_id, purpose=purpose, thread_id=thread_id))
        store.put('chats', dict(id=chat_id, main_worker=worker_id, state='active', title=chat_id,
                                operator=purpose == 'operator'))
    return store


class SwitchChatIdTest(unittest.TestCase):
    def test_explicit_chat_id_wins(self):
        store = store_with(('c1', 'w1', 't1', 'main'), ('c2', 'w2', 't2', 'main'))
        self.assertEqual(switch_chat_id(store, {'chat_id': 'c2'}), 'c2')

    def test_thread_id_finds_rotated_pool_main(self):
        # worker id keeps the first thread, thread_id is the current one (the real case)
        store = store_with(('c1', 'a:old-thread', 'new-thread', 'main'), ('c2', 'w2', 't2', 'main'))
        self.assertEqual(switch_chat_id(store, {'thread_id': 'new-thread'}), 'c1')

    def test_worker_id(self):
        store = store_with(('c1', 'w1', 't1', 'main'), ('c2', 'w2', 't2', 'main'))
        self.assertEqual(switch_chat_id(store, {'worker_id': 'w2'}), 'c2')

    def test_unknown_thread_raises(self):
        store = store_with(('c1', 'w1', 't1', 'main'))
        with self.assertRaises(ValueError):
            switch_chat_id(store, {'thread_id': 'nope'})

    def test_operator_chat_is_default(self):
        store = store_with(('c1', 'w1', 't1', 'main'), ('op', 'w9', 't9', 'operator'))
        self.assertEqual(switch_chat_id(store, {}), 'op')

    def test_single_pool_main_is_default(self):
        store = store_with(('c1', 'w1', 't1', 'main'))
        self.assertEqual(switch_chat_id(store, {}), 'c1')

    def test_several_pool_mains_need_thread_id(self):
        store = store_with(('c1', 'w1', 't1', 'main'), ('c2', 'w2', 't2', 'main'))
        with self.assertRaises(ValueError) as caught:
            switch_chat_id(store, {})
        self.assertIn('thread t2', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
