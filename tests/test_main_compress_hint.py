import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from notiontool.store import Store
from notiontool.chat_context import ChatContext
from notiontool.engine import RateLimited


class MainCompressHintTests(unittest.TestCase):
    """The Main is the operator's live chat: no automatic checkpoint turn is injected there."""

    def setUp(self):
        base = Path(__file__).parent / '.test-data'
        base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.store = Store(self.temp.name)
        self.store.put('accounts', dict(id='a', team_threads={'main': {'id': 'old'}}))
        self.store.put('runs', dict(id='r', chat_id='c', goal='Keep every constraint', project_path='C:/p'))
        settings = self.store.settings(); settings['context_turn_limit'] = 1
        self.store.put('settings', settings)
        self.store.put('tasks', dict(id='previous', state='done', thread_id='old', sent_at=1,
                                     submitted_prompt='Previous instructions', raw_response='Done'))
        self.task = dict(id='t', run_id='r', worker_id='w', state='running')
        self.store.put('tasks', self.task)
        self.stream = Mock(return_value=iter([]))
        self.client = SimpleNamespace(info={'space_id': 'space'}, model=lambda: 'opus', threads=lambda: [],
                                      transport=SimpleNamespace(stream=self.stream), headers=lambda: {})
        self.adapter = SimpleNamespace(store=self.store, new_id=lambda *a: 'new', semantic_context=True,
                                       welcome_body=lambda *a: {'createThread': True}, ensure_thread_model=Mock(),
                                       body=Mock(return_value={}), recover=Mock(return_value='{}'))
        self.idle = {'session': {'status': 'idle'}, 'patches': []}

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def worker(self, purpose, **extra):
        w = dict(id='w', account_id='a', thread_id='old', purpose=purpose, enabled=True, state='busy', lease='l')
        w.update(extra)
        self.store.put('workers', w)
        return w

    def events(self, kind):
        return [e for e in self.store.all('events') if e.get('kind', e.get('type')) == kind] if hasattr(self.store, 'all') else []

    def test_main_over_limit_gets_hint_and_no_checkpoint_turn(self):
        w = self.worker('main')
        with self.assertRaises(RateLimited) as caught:
            ChatContext(self.store).rotate(self.adapter, self.client, w, self.task, self.idle, threading.Event())
        self.assertEqual(caught.exception.scope, 'thread')
        self.assertTrue(caught.exception.free)
        self.stream.assert_not_called()
        self.adapter.body.assert_not_called()
        saved = self.store.get('workers', 'w')
        self.assertIn('Compress', saved['context_hint'])
        self.assertEqual(saved.get('generation', 0), 0)
        self.assertIsNone(self.store.get('rotations', 'w'))

    def test_hint_is_idempotent_on_repeat(self):
        w = self.worker('main')
        for _ in range(3):
            with self.assertRaises(RateLimited):
                ChatContext(self.store).rotate(self.adapter, self.client, self.store.get('workers', 'w'),
                                               self.task, self.idle, threading.Event())
        self.stream.assert_not_called()
        self.assertEqual(self.store.get('workers', 'w').get('generation', 0), 0)

    def test_explicit_compress_button_still_rotates_main(self):
        self.adapter.semantic_context = False
        w = self.worker('main', compress_requested=True)
        updated = ChatContext(self.store).rotate(self.adapter, self.client, w, self.task, self.idle, threading.Event())
        self.assertEqual(updated['generation'], 1)
        self.assertFalse(updated['compress_requested'])

    def test_coworker_rotation_unchanged(self):
        self.adapter.semantic_context = False
        w = self.worker('worker')
        updated = ChatContext(self.store).rotate(self.adapter, self.client, w, self.task, self.idle, threading.Event())
        self.assertEqual(updated['generation'], 1)
        self.assertNotIn('context_hint', updated)

    def test_auto_policy_restores_inline_main_rotation(self):
        self.adapter.semantic_context = False
        settings = self.store.settings(); settings['main_context_policy'] = 'auto'
        self.store.put('settings', settings)
        w = self.worker('main')
        updated = ChatContext(self.store).rotate(self.adapter, self.client, w, self.task, self.idle, threading.Event())
        self.assertEqual(updated['generation'], 1)


if __name__ == '__main__':
    unittest.main()
