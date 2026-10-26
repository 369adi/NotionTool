import os, sys, types, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from notiontool.chat_pool import pool_decision, pick_main, script_mode_failure, name_coworkers, COWORK_TITLE, WELCOME_TITLE
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
    def test_marker_joined_is_coworker_without_rename(self):
        # I41: pool_decision never renames; the naming pass migrates 'Cowork joined' to 'Coworker N'.
        ch, rn, ev = pool_decision({'label': COWORK_TITLE, 'enabled': True}, DONE)
        self.assertTrue(ch['enabled']); self.assertEqual(ch['join_state'], 'joined'); self.assertIsNone(rn)
        ch, rn, ev = pool_decision({'label': COWORK_TITLE, 'state': 'busy', 'enabled': True}, DONE)
        self.assertTrue(ch['enabled']); self.assertIsNone(rn)
    def test_marker_not_joined_not_renamed(self):
        ch, rn, ev = pool_decision({'label': COWORK_TITLE, 'state': 'ready'}, FRESH)
        self.assertFalse(ch['enabled']); self.assertIsNone(rn)
    def test_welcome_joined_without_rename(self):
        ch, rn, ev = pool_decision({'label': WELCOME_TITLE, 'join_state': 'pending'}, FRESH)
        self.assertFalse(ch['enabled']); self.assertIsNone(rn)
        ch, rn, ev = pool_decision({'label': WELCOME_TITLE, 'join_state': 'pending', 'enabled': True}, DONE)
        self.assertTrue(ch['enabled']); self.assertIsNone(rn); self.assertEqual(ev, 'coworker_joined')
        ch, rn, ev = pool_decision({'label': WELCOME_TITLE, 'join_state': 'joined', 'state': 'busy', 'enabled': True}, DONE)
        self.assertTrue(ch['enabled']); self.assertIsNone(rn); self.assertIsNone(ev)
    def test_any_unrenamed_welcome_chat_is_coworker(self):
        ch, rn, ev = pool_decision({'label': WELCOME_TITLE, 'enabled': True}, DONE)
        self.assertTrue(ch['enabled']); self.assertIsNone(rn)
        titled = {'alive': True, 'data': {'user_set_title': True, 'turn_outcomes': {'a': {'status': 'completed'}}}}
        self.assertFalse(pool_decision({'label': WELCOME_TITLE, 'enabled': True}, titled)[0]['enabled'])
        self.assertFalse(pool_decision({'label': WELCOME_TITLE, 'enabled': True}, {'alive': False})[0]['enabled'])
    def test_harness_title_is_not_a_user_rename(self):
        own = {'alive': True, 'data': {'title': 'Coworker 2', 'user_set_title': True, 'turn_outcomes': {'a': {'status': 'completed'}}}}
        ch, rn, ev = pool_decision({'label': 'Coworker 2', 'harness_title': 'Coworker 2', 'enabled': True, 'join_state': 'joined'}, own)
        self.assertTrue(ch['enabled']); self.assertIsNone(rn)
    def test_disabled_welcome_chat_is_never_enabled_automatically(self):
        for worker in ({'label': WELCOME_TITLE}, {'label': WELCOME_TITLE, 'enabled': False}, {'label': COWORK_TITLE, 'join_state': 'pending'}):
            self.assertFalse(pool_decision(worker, DONE)[0]['enabled'], worker)
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
        self.assertTrue(w(4)['enabled'])
        self.assertNotIn((T(1), WELCOME_TITLE), self.client.renamed)  # no two-step rename back to Welcome any more
        self.assertNotIn(COWORK_TITLE, [title for _, title in self.client.renamed])
        self.assertEqual(res['coworkers'], 2)
        self.assertEqual(self.store.get('accounts', 'r')['team_threads']['main']['id'], T(5))
        chat = self.a.ensure_main_chat('r')
        self.assertEqual(chat['main_worker'], 'r:' + T(5))
        self.assertEqual(self.a.ensure_main_chat('r')['id'], chat['id'])  # loaded, not duplicated
        self.assertEqual(len(self.store.all('chats')), 1)
        self.assertFalse(account_setup(self.store.get('accounts', 'r'), self.store.all('workers'))['chat_ready'])

    def test_naming_pass_after_join(self):
        self.a.apply_chat_pool('r', self.client)
        res = name_coworkers(self.a, self.client, 'r', None, now=1000.0)
        w = lambda i: self.store.get('workers', 'r:' + T(i))
        # Legacy 'Cowork joined' migrated, joined Welcome named; not-joined, NotionTool and Main untouched.
        self.assertEqual(sorted(self.client.renamed), sorted([(T(1), 'Coworker 1'), (T(4), 'Coworker 3')]))
        self.assertEqual((w(1)['label'], w(1)['harness_title']), ('Coworker 1', 'Coworker 1'))
        self.assertEqual(w(4)['label'], 'Coworker 3')
        self.assertEqual((w(2)['label'], w(3)['label'], w(5)['label']), ('Cowork joined', 'NotionTool', 'Main'))
        self.assertEqual(self.store.get('accounts', 'r')['cowork_numbers'], res['numbers'])
        self.assertIn('coworker_renamed', self.store.events)
        # Next pass: nothing left to do (no loop).
        self.client.records.update({T(1): dict(DONE, data=dict(DONE['data'], title='Coworker 1', user_set_title=True)),
                                    T(4): dict(DONE, data=dict(DONE['data'], title='Coworker 3', user_set_title=True))})
        self.client.renamed.clear()
        name_coworkers(self.a, self.client, 'r', None, now=1001.0)
        self.assertEqual(self.client.renamed, [])
        self.assertTrue(w(1)['enabled'])

    def test_rename_failure_retries_later(self):
        self.a.apply_chat_pool('r', self.client)
        def boom(c, tid, title): raise RuntimeError('offline')
        self.a.rename_thread = boom
        name_coworkers(self.a, self.client, 'r', None, now=1000.0)
        self.assertIn('coworker_rename_failed', self.store.events)
        self.assertEqual(self.store.get('workers', 'r:' + T(1))['rename_retry_at'], 1900.0)
        calls = []
        self.a.rename_thread = lambda c, tid, title: calls.append(tid)
        name_coworkers(self.a, self.client, 'r', None, now=1500.0)
        self.assertEqual(calls, [])  # still backing off
        name_coworkers(self.a, self.client, 'r', None, now=1901.0)
        self.assertEqual(sorted(calls), sorted([T(1), T(4)]))

    def test_budget_paused_no_calls(self):
        self.a.apply_chat_pool('r', self.client)
        self.a.network_budget = types.SimpleNamespace(status=lambda: {'paused': True})
        res = name_coworkers(self.a, self.client, 'r', None, now=1000.0)
        self.assertEqual(self.client.renamed, []); self.assertEqual(res['renamed'], [])

    def test_flag_bridge(self):
        self.a.note_worker_mcp_failure({'account_id':'r','label':'Cowork joined'})
        acc = self.store.get('accounts', 'r')
        self.assertTrue(acc['bridge_connected']); self.assertEqual(acc['bridge_checked_at'], 0)
        self.assertIn('worker_mcp_unavailable', self.store.events)

class RotationKeepsNumber(unittest.TestCase):
    def test_replacement_in_worker3_becomes_coworker_3(self):
        store = Store()
        new_raw = T(13).replace('-', '')
        store.put('accounts', {'id': 'r', 'slot': 1, 'state': 'ready', 'team_threads': {
            'worker1': {'id': T(11), 'pending': False}, 'worker3': {'id': new_raw, 'pending': False}}})
        store.put('workers', dict(id='r:' + T(11), account_id='r', thread_id=T(11), purpose='worker', enabled=True, state='ready',
                                  lease='', join_state='joined', label='Coworker 1', harness_title='Coworker 1'))
        store.put('workers', dict(id='r:' + T(12), account_id='r', thread_id=T(12), purpose='retired_worker', retired=True,
                                  enabled=False, state='ready', lease='', join_state='joined', label='Coworker 3', retired_role='worker3'))
        # After the rotation the replacement joined (it was admitted as a pool chat with the Welcome title).
        store.put('workers', dict(id='r:' + T(13), account_id='r', thread_id=T(13), purpose='worker', enabled=True, state='ready',
                                  lease='', join_state='joined', pool_admitted=True, label=WELCOME_TITLE))
        client = Client([], {T(11): dict(DONE, data=dict(DONE['data'], title='Coworker 1', user_set_title=True)),
                             T(12): DONE, T(13): DONE})
        a = adapter(store, client)
        name_coworkers(a, client, 'r', None, now=5.0)
        self.assertEqual(client.renamed, [(T(13), 'Coworker 3')])
        self.assertEqual(store.get('workers', 'r:' + T(12))['label'], 'Coworker 3')  # retired chat not renamed

    def test_context_switch_duplicate_record_names_new_chat_once(self):
        # Live bug 24 Oct: a context switch moved worker r:T21 to the new Welcome chat T22, and a second
        # record r:T22 for the same chat existed. The chat was renamed twice per pass ('Coworker 4', then
        # 'Coworker 9') and the working record kept the label 'Welcome to Notion'.
        store = Store()
        store.put('accounts', {'id': 'r', 'slot': 1, 'state': 'ready', 'team_threads': {
            'worker4': {'id': T(22), 'pending': False}}})
        store.put('workers', dict(id='r:' + T(21), account_id='r', thread_id=T(22), purpose='worker', enabled=True,
                                  state='ready', lease='', join_state='joined', label=WELCOME_TITLE, harness_title='Coworker 2'))
        store.put('workers', dict(id='r:' + T(22), account_id='r', thread_id=T(22), purpose='worker', enabled=False,
                                  state='ready', lease='', join_state='pending', label=WELCOME_TITLE))
        client = Client([], {T(22): DONE})
        a = adapter(store, client)
        res = name_coworkers(a, client, 'r', None, now=5.0)
        self.assertEqual(client.renamed, [(T(22), 'Coworker 4')])
        self.assertEqual(store.get('workers', 'r:' + T(21))['label'], 'Coworker 4')
        self.assertEqual(store.get('workers', 'r:' + T(22))['label'], 'Coworker 4')
        self.assertEqual(res['numbers'], {'r:' + T(21): 4})
        # Next pass: the title is right, nothing is renamed again.
        client.records[T(22)] = dict(DONE, data=dict(DONE['data'], title='Coworker 4', user_set_title=True))
        client.renamed.clear()
        name_coworkers(a, client, 'r', None, now=6.0)
        self.assertEqual(client.renamed, [])

if __name__ == '__main__':
    unittest.main(verbosity=1)
