import tempfile, unittest
from pathlib import Path
from notiontool.store import Store
from notiontool.chat_context import ChatContext

OLD = '3fbd3dc4-6a0c-80aa-b071-00a9e12fce0b'
NEW = '3fbd3dc4-6a0c-80bb-8001-00a9aaaaaaaa'

class Adapter:
    def __init__(s, listed=True, deleter=False):
        s.listed = listed; s.renamed = []; s.deleted = []
        if deleter: s.delete_thread = lambda c, t: s.deleted.append(t)
    def new_id(s, space, tail): return NEW
    def create_welcome(s, client, tid, model): return s.listed, True
    def rename_thread(s, c, tid, title): s.renamed.append((tid, title))

class Client:
    info = {'space_id': '8d7d3dc4-6a0c-8164-897f-00036215c141'}

class FreshWelcome(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).parent / '.test-data'; root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root); self.store = Store(self.temp.name)
        self.store.put('accounts', dict(id='a', slot=1, model='m'))
        self.w = dict(id='a:' + OLD, account_id='a', thread_id=OLD, purpose='worker', enabled=True, generation=3, label='Welcome to Notion')
        self.store.put('workers', self.w)
        self.pending = dict(id=self.w['id'], handoff='STAND X', old_thread=OLD)
        self.store.put('rotations', self.pending)
    def tearDown(self): self.store.close(); self.temp.cleanup()
    def kinds(self): return [e.get('kind') or e.get('type') for e in self.store.all('events')] if hasattr(self.store, 'all') else []

    def test_moves_worker_to_new_welcome_and_retires_old(self):
        a = Adapter()
        w = ChatContext(self.store).fresh_welcome(a, Client(), dict(self.w), {'id': 't', 'run_id': 'r'}, self.pending, {})
        self.assertEqual(w['thread_id'], NEW); self.assertEqual(w['id'], 'a:' + OLD)
        self.assertEqual(w['handoff'], 'STAND X'); self.assertFalse(w['handoff_delivered']); self.assertEqual(w['generation'], 4)
        self.assertEqual(w['label'], 'Welcome to Notion'); self.assertEqual(w['join_state'], 'joined')
        g = self.store.get('thread_generations', OLD + ':4')
        self.assertEqual(g['successor'], NEW); self.assertEqual(g['mode'], 'new_welcome')
        self.assertIsNone(self.store.get('rotations', self.w['id']))
        self.assertEqual(a.renamed, [(OLD, 'Cowork (retired)')])

    def test_verified_delete_is_used_when_available(self):
        a = Adapter(deleter=True)
        ChatContext(self.store).fresh_welcome(a, Client(), dict(self.w), {'id': 't'}, self.pending, {})
        self.assertEqual(a.deleted, [OLD]); self.assertEqual(a.renamed, [])

    def test_not_listed_keeps_same_chat(self):
        a = Adapter(listed=False)
        self.assertIsNone(ChatContext(self.store).fresh_welcome(a, Client(), dict(self.w), {'id': 't'}, self.pending, {}))
        self.assertEqual(self.store.get('workers', self.w['id'])['thread_id'], OLD)
        self.assertIsNotNone(self.store.get('rotations', self.w['id']))

class DeleteThread(unittest.TestCase):
    def test_delete_sets_alive_false_and_verifies(self):
        from notiontool.notion import NotionAdapter
        a = NotionAdapter.__new__(NotionAdapter); sent = []
        class C:
            info = {'space_id': 's'}
            def call(c, path, body, timeout=45):
                if path == 'saveTransactionsFanout':
                    sent.append(body['transactions'][0]['operations'][0]); return {}
                return {'recordMap': {'thread': {OLD: {'value': {'value': {'alive': not sent}}}}}}
        self.assertTrue(a.delete_thread(C(), OLD))
        self.assertEqual(sent[0]['args'], {'alive': False}); self.assertEqual(sent[0]['command'], 'update')

if __name__ == '__main__':
    unittest.main()
