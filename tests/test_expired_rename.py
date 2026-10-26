"""main_handoff_ops.sweep_expired: old Mains and user-named chats become '<title> (expired)'."""
import copy
import unittest

from notiontool import main_handoff_ops as ops
from notiontool.team_rotation import TeamRotator

HOUR = 3600
NOW = 1_792_939_122.0  # 'Main 20' (created 1792769922) is 47 h old here
BUG_CHAT = 'c_create_main_desk-b641f426e0a74d84'
T20, T_CUR, T_RBX, T_CW, T_GONE = 't-main20', 't-current', 't-rbx', 't-coworker', 't-gone'


class FakeStore:
    def __init__(self):
        self.data, self.events = {}, []

    def get(self, kind, ident, default=None):
        doc = self.data.get(kind, {}).get(ident)
        return copy.deepcopy(doc) if doc is not None else default

    def all(self, kind):
        return [copy.deepcopy(d) for d in self.data.get(kind, {}).values()]

    def put(self, kind, doc):
        self.data.setdefault(kind, {})[doc['id']] = copy.deepcopy(doc)
        return doc

    def event(self, kind, run_id='', **fields):
        self.events.append(dict(fields, kind=kind))

    def settings(self):
        return {}


class FakeClient:
    def __init__(self, titles):
        self.info = {'space_id': 'space'}
        self.titles = titles

    def threads(self):
        return [{'id': t, 'title': title} for t, title in self.titles.items()]


class FakeAdapter:
    network_budget = None

    def __init__(self, titles, created):
        self.titles, self.created, self.renamed = titles, created, []
        self.fail_rename = False

    def client(self, account_id):
        return FakeClient(self.titles)

    def thread_records(self, client, ids):
        return {t: {'id': t, 'created_time': int(self.created[t] * 1000)} for t in ids if t in self.created}

    def rename_thread(self, client, tid, title):
        if self.fail_rename:
            raise OSError('Notion unreachable')
        self.renamed.append((tid, title))
        self.titles[tid] = title


def build():
    store = FakeStore()
    store.put('accounts', dict(id='acc', state='ready'))
    # The current active Main: 5 h old, must stay untouched.
    store.put('workers', dict(id='w_cur', account_id='acc', thread_id=T_CUR, purpose='main', enabled=True,
                              state='ready', created_at=NOW - 5 * HOUR))
    store.put('chats', dict(id='c_cur', title='Main 21', main_worker='w_cur', created=NOW - 5 * HOUR, state='active'))
    # The bug record: pool Main whose worker record is gone (worker_state 'missing'), 47 h old.
    store.put('operations', dict(id='create_main:desk-b641f426e0a74d84', account_id='acc', thread_id=T20))
    store.put('chats', dict(id=BUG_CHAT, title='Main 20', main_worker='w_gone', created=1792769922,
                            main_number=20, state='active'))
    # A user-named chat, 30 h old.
    store.put('workers', dict(id='w_rbx', account_id='acc', thread_id=T_RBX, purpose='private', enabled=False,
                              state='ready', created_at=NOW - 30 * HOUR))
    store.put('chats', dict(id='c_app_rbx', title='RBX', main_worker='w_rbx', account_id='acc', user_named=True,
                            created=NOW - 30 * HOUR, state='active'))
    # An old Coworker: left to the retire/delete flow.
    store.put('workers', dict(id='w_cw', account_id='acc', thread_id=T_CW, purpose='worker', enabled=True,
                              state='ready', created_at=NOW - 40 * HOUR))
    store.put('chats', dict(id='c_cw', title='Coworker 3', main_worker='w_cw', created=NOW - 40 * HOUR,
                            state='active'))
    titles = {T20: 'Main 20', T_CUR: 'Main 21', T_RBX: 'RBX', T_CW: 'Coworker 3'}
    created = {T20: 1792769922, T_CUR: NOW - 5 * HOUR, T_RBX: NOW - 30 * HOUR, T_CW: NOW - 40 * HOUR}
    return store, FakeAdapter(titles, created)


class ExpiredRenameTest(unittest.TestCase):
    def test_missing_worker_main_is_expired(self):
        store, adapter = build()
        result = ops.sweep_expired(store, adapter, now=NOW)
        chat = store.get('chats', BUG_CHAT)
        self.assertEqual(chat['title'], 'Main 20 (expired)')
        self.assertEqual(chat['state'], 'expired')
        self.assertTrue(chat['expired_thread_done'])
        self.assertIn((T20, 'Main 20 (expired)'), adapter.renamed)
        self.assertEqual(result['renamed'], 2)
        self.assertIn('chat_expired', [e['kind'] for e in store.events])

    def test_user_named_chat_keeps_its_name(self):
        store, adapter = build()
        ops.sweep_expired(store, adapter, now=NOW)
        chat = store.get('chats', 'c_app_rbx')
        self.assertEqual(chat['title'], 'RBX (expired)')
        self.assertEqual(chat['state'], 'expired')
        self.assertTrue(chat['user_named'])
        self.assertEqual(adapter.titles[T_RBX], 'RBX (expired)')

    def test_current_main_and_coworker_untouched(self):
        store, adapter = build()
        before_cur, before_cw = store.get('chats', 'c_cur'), store.get('chats', 'c_cw')
        ops.sweep_expired(store, adapter, now=NOW)
        self.assertEqual(store.get('chats', 'c_cur'), before_cur)
        self.assertEqual(store.get('chats', 'c_cw'), before_cw)
        self.assertEqual(adapter.titles[T_CUR], 'Main 21')
        self.assertEqual(adapter.titles[T_CW], 'Coworker 3')
        self.assertNotIn(T_CUR, [t for t, _ in adapter.renamed])

    def test_second_sweep_changes_nothing(self):
        store, adapter = build()
        ops.sweep_expired(store, adapter, now=NOW)
        data, renamed, events = copy.deepcopy(store.data), list(adapter.renamed), len(store.events)
        again = ops.sweep_expired(store, adapter, now=NOW + HOUR)
        self.assertEqual(again['renamed'], 0)
        self.assertEqual(again['threads_renamed'], 0)
        self.assertEqual(store.data, data)
        self.assertEqual(adapter.renamed, renamed)
        self.assertEqual(len(store.events), events)
        self.assertNotIn('(expired) (expired)', ' '.join(c['title'] for c in store.all('chats')))

    def test_young_chat_is_not_expired(self):
        store, adapter = build()
        ops.sweep_expired(store, adapter, now=1792769922 + 21 * HOUR)
        self.assertEqual(store.get('chats', BUG_CHAT)['state'], 'active')
        self.assertEqual(store.get('chats', BUG_CHAT)['title'], 'Main 20')

    def test_failed_thread_rename_is_retried(self):
        store, adapter = build()
        adapter.fail_rename = True
        ops.sweep_expired(store, adapter, now=NOW)
        chat = store.get('chats', BUG_CHAT)
        self.assertEqual((chat['title'], chat['state'], chat['expired_thread_done']), ('Main 20 (expired)', 'expired', False))
        adapter.fail_rename = False
        ops.sweep_expired(store, adapter, now=NOW + 300)
        self.assertTrue(store.get('chats', BUG_CHAT)['expired_thread_done'])
        self.assertEqual(adapter.titles[T20], 'Main 20 (expired)')

    def test_gone_thread_expires_only_the_record(self):
        store, adapter = build()
        del adapter.titles[T20]
        ops.sweep_expired(store, adapter, now=NOW)
        chat = store.get('chats', BUG_CHAT)
        self.assertEqual((chat['title'], chat['state']), ('Main 20 (expired)', 'expired'))
        self.assertNotIn(T20, [t for t, _ in adapter.renamed])

    def test_expired_title_is_idempotent(self):
        self.assertEqual(ops.expired_title('Main 20'), 'Main 20 (expired)')
        self.assertEqual(ops.expired_title('RBX (expired)'), 'RBX (expired)')
        self.assertEqual(ops.expired_title('Main (expired, 2026-10-24 10:00)'), 'Main (expired, 2026-10-24 10:00)')

    def test_team_rotation_cycle_runs_the_sweep(self):
        store, adapter = build()
        rotator = TeamRotator(store, adapter, start_job=lambda *a, **k: None, clock=lambda: NOW)
        self.assertEqual(rotator.expired_pass(), 2)
        self.assertEqual(store.get('chats', BUG_CHAT)['title'], 'Main 20 (expired)')


if __name__ == '__main__':
    unittest.main()
