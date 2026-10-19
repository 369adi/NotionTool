"""A requested fresh context moves a coworker exactly once, and only when nothing is unresolved."""
import tempfile
import threading
import unittest
from pathlib import Path

from notiontool.chat_context import ChatContext
from notiontool.store import Store

OLD = '3fbd3dc4-6a0c-80aa-b071-00a9e12fce0b'
NEW = '3fbd3dc4-6a0c-80bb-8001-00a9aaaaaaaa'
IDLE = {'session': {'status': 'idle', 'sequence': 3}, 'patches': []}


class Adapter:
    def __init__(self):
        self.created, self.renamed = [], []

    def new_id(self, space, tail):
        return NEW

    def create_welcome(self, client, tid, model):
        self.created.append(tid)
        return True, True

    def rename_thread(self, client, tid, title):
        self.renamed.append((tid, title))


class Client:
    info = {'space_id': '8d7d3dc4-6a0c-8164-897f-00036215c141'}


class CoworkerFreshRequestTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent / '.test-data'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root, ignore_cleanup_errors=True)
        self.store = Store(self.temp.name)
        self.store.put('accounts', dict(id='a', slot=1, model='m'))
        self.worker = dict(id='a:' + OLD, account_id='a', thread_id=OLD, purpose='worker', enabled=True, state='ready',
                           lease='', generation=3, label='Welcome to Notion', compress_requested=True,
                           compress_reason='Fresh context requested at about 950000 tokens')
        self.store.put('workers', self.worker)
        self.adapter = Adapter()
        self.task = {'id': 't-new', 'run_id': 'r'}

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def rotate(self):
        worker = self.store.get('workers', self.worker['id'])
        return ChatContext(self.store).rotate(self.adapter, Client(), worker, self.task, IDLE, threading.Event())

    def events_text(self):
        return ' '.join(str(event) for event in self.store.events(limit=100))

    def test_request_moves_the_coworker_to_a_new_welcome_chat_once(self):
        worker = self.rotate()
        self.assertEqual(self.adapter.created, [NEW])
        self.assertEqual((worker['thread_id'], worker['generation'], worker['label']), (NEW, 4, 'Welcome to Notion'))
        stored = self.store.get('workers', self.worker['id'])
        self.assertFalse(stored['compress_requested'])
        self.assertEqual(stored['compress_reason'], '')
        self.assertFalse(stored['handoff_delivered'])
        self.assertIn(OLD, stored['retired_threads'])
        self.assertEqual(self.store.get('thread_generations', OLD + ':4')['mode'], 'new_welcome')
        self.assertIsNone(self.store.get('rotations', self.worker['id']))
        self.assertEqual(self.adapter.renamed, [(OLD, 'Cowork (retired)')])
        self.assertIn('context_rotated', self.events_text())
        # The request is used up: the next task in the new chat does not move the coworker again.
        self.task = {'id': 't-next', 'run_id': 'r'}
        self.assertEqual(self.rotate()['thread_id'], NEW)
        self.assertEqual(self.adapter.created, [NEW])

    def test_without_a_request_a_small_context_stays(self):
        worker = dict(self.worker, compress_requested=False, compress_reason='')
        self.store.put('workers', worker)
        self.assertEqual(self.rotate()['thread_id'], OLD)
        self.assertEqual(self.adapter.created, [])

    def test_unresolved_assignment_blocks_the_move(self):
        self.store.put('tasks', dict(id='t-old', run_id='r0', worker_id=self.worker['id'], state='uncertain'))
        with self.assertRaisesRegex(ValueError, 'unresolved assignment'):
            self.rotate()
        self.assertEqual(self.adapter.created, [])
        stored = self.store.get('workers', self.worker['id'])
        self.assertEqual(stored['thread_id'], OLD)
        self.assertTrue(stored['compress_requested'])

    def test_same_thread_mode_creates_no_chat(self):
        settings = self.store.settings()
        settings['coworker_rotate_mode'] = 'same_thread'
        self.store.put('settings', settings)
        self.assertEqual(self.store.settings()['coworker_rotate_mode'], 'same_thread')
        worker = self.rotate()
        self.assertEqual(self.adapter.created, [])
        self.assertEqual((worker['thread_id'], worker['generation']), (OLD, 4))
        self.assertFalse(self.store.get('workers', self.worker['id'])['compress_requested'])


if __name__ == '__main__':
    unittest.main()
