import sys, types, unittest
sys.path.insert(0, r'C:\Users\liket\Downloads\NotionTool-harness\NotionTool')
from notiontool.chat_pool import pool_decision, pick_main, script_mode_failure, COWORK_TITLE, WELCOME_TITLE
from notiontool import notion as N
from notiontool.setup_status import account_setup

DONE = {'alive': True, 'data': {'turn_outcomes': {'a': {'status': 'completed'}}}}
FRESH = {'alive': True, 'data': {}}

class Store:
    def __init__(s):
        s.t = {'accounts': {}, 'workers': {}, 'chats': {}}; s.events = []
    def all(s, k): return list(s.t.setdefault(k, {}).values())
    def get(s, k, i, d=None): return s.t.setdefault(k, {}).get(i, d)
    def put(s, k, v): s.t.setdefault(k, {})[v['id']] = v
    def event(s, kind, *a, **kw): s.events.append(kind)

class Client:
    def __init__(s, threads, records):
        s.threads_list = threads; s.records = records; s.renamed = []
        s.info = {'space_id': '8d7d3dc4-6a0c-8164-897f-00036215c141', 'user_id': 'u', 'records': {}}
    def threads(s): return s.threads_list
    def call(s, method, body):
        return {'recordMap': {'thread': {r['pointer']['id']: {'value': {'value': s.records.get(r['pointer']['id'], {})}} for r in body['requests']}}}

def adapter(store, client):
    a = N.NotionAdapter.__new__(N.NotionAdapter)
    a.store = store
    a.rename_thread = lambda c, tid, title: client.renamed.append((tid, title))
    return a

T = lambda i: '3fbd3dc4-6a0c-80aa-b071-00a9e12fce%02d' % i

class PoolRules(unittest.TestCase):
    def test_marker_joined_is_coworker_and_unnamed_again(self):
        ch, rn, ev = pool_decision({'label': COWORK_TITLE}, DONE)
        self.assertTrue(ch['enabled']); self.assertEqual(ch['join_state'], 'joined'); self.assertEqual(rn, WELCOME_TITLE)
        ch, rn, ev = pool_decision({'label': COWORK_TITLE, 'state': 'busy'}, DONE)
        self.assertTrue(ch['enabled']); self.assertIsNone(rn)
    def test_marker_not_joined_is_unnamed_again(self):
        ch, rn, ev = pool_decision({'label': COWORK_TITLE, 'state': 'ready'}, FRESH)
        self.assertFalse(ch['enabled']); self.assertEqual(rn, WELCOME_TITLE)
    def test_welcome_joined_without_rename(self):
        ch, rn, ev = pool_decision({'label': WELCOME_TITLE, 'join_state': 'pending'}, FRESH)
        self.assertFalse(ch['enabled']); self.assertIsNone(rn)
        ch, rn, ev = pool_decision({'label': WELCOME_TITLE, 'join_state': 'pending'}, DONE)
        self.assertTrue(ch['enabled']); self.assertIsNone(rn); self.assertEqual(ev, 'coworker_joined')
        ch, rn, ev = pool_decision({'label': WELCOME_TITLE, 'join_state': 'joined', 'state': 'busy'}, DONE)
        self.assertTrue(ch['enabled']); self.assertIsNone(rn); self.assertIsNone(ev)
    def test_any_unrenamed_welcome_chat_is_coworker(self):
        ch, rn, ev = pool_decision({'label': WELCOME_TITLE}, DONE)
        self.assertTrue(ch['enabled']); self.assertIsNone(rn)
        titled = {'alive': True, 'data': {'user_set_title': True, 'turn_outcomes': {'a': {'status': 'completed'}}}}
        self.assertFalse(pool_decision({'label': WELCOME_TITLE}, titled)[0]['enabled'])
        self.assertFalse(pool_decision({'label': WELCOME_TITLE}, {'alive': False})[0]['enabled'])
    def test_user_chat_never_used(self):
        for label in ('NotionTool', 'Sonstiges', 'Studio432'):
            self.assertFalse(pool_decision({'label': label, 'enabled': True}, None)[0]['enabled'])
    def test_renamed_coworker_released(self):
        ch, rn, ev = pool_decision({'label': 'Mein Chat', 'join_state': 'joined'}, DONE)
        self.assertEqual(ch['join_state'], 'released'); self.assertFalse(ch['enabled'])
    def test_busy_never_renamed(self):
        self.assertIsNone(pool_decision({'label': COWORK_TITLE, 'state': 'busy'}, FRESH)[1])
    def test_main_both_titles(self):
        self.assertEqual(pick_main([{'label': 'Main Chat', 'thread_id': 'a'}])['thread_id'], 'a')
        self.assertEqual(pick_main([{'label': 'Main Chat', 'thread_id': 'a'}, {'label': 'Main', 'thread_id': 'b'}])['thread_id'], 'b')
        self.assertIsNone(pick_main([{'label': 'Welcome to Notion', 'thread_id': 'c'}]))
    def test_script_mode(self):
        self.assertTrue(script_mode_failure([{'type': 'tool-result', 'v': 'Connection NotionTool is not available in script mode'}]))
        self.assertFalse(script_mode_failure([{'type': 'user', 'v': 'Connection NotionTool is not available in script mode'}]))

class AdapterPool(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.store.put('accounts', {'id': 'r', 'slot': 1, 'label': 'Slot 2', 'team_threads': None, 'state': 'ready', 'bridge_connected': True})
        rows = [(T(1), 'Cowork joined', None), (T(2), 'Cowork joined', None), (T(3), 'NotionTool', None),
                (T(4), 'Welcome to Notion', 'pending'), (T(5), 'Main', None)]
        for tid, label, js in rows:
            w = dict(id='r:' + tid, account_id='r', thread_id=tid, purpose='worker', enabled=True, welcome=True, state='ready', lease='', label=label)
            if js: w['join_state'] = js
            self.store.put('workers', w)
        self.client = Client([], {T(1): DONE, T(2): FRESH, T(4): DONE})
        self.a = adapter(self.store, self.client)

    def test_apply(self):
        res = self.a.apply_chat_pool('r', self.client)
        w = lambda i: self.store.get('workers', 'r:' + T(i))
        self.assertEqual(w(5)['purpose'], 'main')
        self.assertTrue(w(1)['enabled']); self.assertFalse(w(2)['enabled']); self.assertFalse(w(3)['enabled'])
        self.assertTrue(w(4)['enabled']); self.assertEqual(w(4)['label'], WELCOME_TITLE)
        self.assertIn((T(1), WELCOME_TITLE), self.client.renamed); self.assertIn((T(2), WELCOME_TITLE), self.client.renamed)
        self.assertNotIn(COWORK_TITLE, [title for _, title in self.client.renamed])
        self.assertEqual(res['coworkers'], 2)
        self.assertEqual(self.store.get('accounts', 'r')['team_threads']['main']['id'], T(5))
        chat = self.a.ensure_main_chat('r')
        self.assertEqual(chat['main_worker'], 'r:' + T(5))
        self.assertEqual(self.a.ensure_main_chat('r')['id'], chat['id'])  # loaded, not duplicated
        self.assertEqual(len(self.store.all('chats')), 1)
        self.assertFalse(account_setup(self.store.get('accounts', 'r'), self.store.all('workers'))['chat_ready'])

    def test_flag_bridge(self):
        self.a.note_worker_mcp_failure({'account_id':'r','label':'Cowork joined'})
        acc = self.store.get('accounts', 'r')
        self.assertTrue(acc['bridge_connected']); self.assertEqual(acc['bridge_checked_at'], 0)
        self.assertIn('worker_mcp_unavailable', self.store.events)

if __name__ == '__main__':
    unittest.main(verbosity=1)
