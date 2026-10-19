"""retired_chats.delete_retired with a fake store and a fake adapter (no network)."""
import copy
import threading
import unittest

from notiontool import retired_chats


class FakeStore:
    def __init__(self, data):
        self.data = copy.deepcopy(data)
        self.events = []
        self.puts = 0
        self.lock = threading.RLock()

    def get(self, kind, ident, default=None):
        doc = self.data.get(kind, {}).get(ident)
        return copy.deepcopy(doc) if doc is not None else default

    def all(self, kind):
        return [copy.deepcopy(d) for d in self.data.get(kind, {}).values()]

    def put(self, kind, doc):
        self.puts += 1
        self.data.setdefault(kind, {})[doc['id']] = copy.deepcopy(doc)
        return doc

    def event(self, kind, run_id='', **fields):
        self.events.append((kind, fields))


class FakeAdapter:
    def __init__(self, records, fail=()):
        self.records = records
        self.fail = set(fail)
        self.calls = []

    def client(self, account_id):
        self.calls.append(('client', account_id))
        return 'client-' + account_id

    def thread_records(self, client, ids):
        self.calls.append(('records', client, tuple(ids)))
        return {t: copy.deepcopy(self.records[t]) for t in ids if t in self.records}

    def delete_thread(self, client, tid):
        self.calls.append(('delete', client, tid))
        if tid in self.fail:
            raise ValueError('Notion did not confirm the deletion of chat ' + tid)
        self.records[tid]['alive'] = False
        return True


def rec(title='', alive=True, user_set=False):
    data = {'title': title}
    if user_set:
        data['user_set_title'] = True
    return {'alive': alive, 'data': data}


def setup(retired, records, account_state='ready', workers_extra=(), chats=(), fail=()):
    workers = {'w1': dict(id='w1', account_id='a1', thread_id='cur1', purpose='worker', retired_threads=list(retired))}
    for w in workers_extra:
        workers[w['id']] = w
    store = FakeStore({'workers': workers,
                       'accounts': {'a1': dict(id='a1', state=account_state), 'a2': dict(id='a2', state='ready')},
                       'chats': {c['id']: c for c in chats}})
    return store, FakeAdapter(records, fail)


def ids(items):
    return [i['thread_id'] for i in items]


class DeleteRetiredTest(unittest.TestCase):
    def test_deletes_our_retired_chat(self):
        store, adapter = setup(['t1', 't2'], {'t1': rec('Cowork (retired)'), 't2': rec('Welcome to Notion')})
        out = retired_chats.delete_retired(store, adapter)
        self.assertEqual(ids(out['deleted']), ['t1', 't2'])
        self.assertEqual(out['skipped'], [])
        self.assertEqual(out['failed'], [])
        self.assertEqual(store.get('workers', 'w1')['retired_threads'], [])
        self.assertEqual([e[0] for e in store.events], ['coworker_old_chat_deleted'] * 2)
        self.assertEqual(store.events[0][1]['worker_id'], 'w1')
        self.assertEqual(store.events[0][1]['thread_id'], 't1')
        self.assertIn(('delete', 'client-a1', 't1'), adapter.calls)

    def test_empty_and_cowork_titles_allowed(self):
        store, adapter = setup(['t1', 't2'], {'t1': rec(''), 't2': rec('Cowork 3')})
        out = retired_chats.delete_retired(store, adapter)
        self.assertEqual(ids(out['deleted']), ['t1', 't2'])

    def test_skips_current_thread(self):
        other = dict(id='w2', account_id='a1', thread_id='t1', purpose='worker', retired_threads=[])
        store, adapter = setup(['t1'], {'t1': rec('Cowork 1')}, workers_extra=[other])
        out = retired_chats.delete_retired(store, adapter)
        self.assertEqual(out['deleted'], [])
        self.assertEqual(out['skipped'], [dict(thread_id='t1', reason='current thread of a worker')])
        self.assertFalse([c for c in adapter.calls if c[0] == 'delete'])
        self.assertEqual(store.get('workers', 'w1')['retired_threads'], ['t1'])

    def test_skips_main_and_operator_chat(self):
        main = dict(id='wm', account_id='a1', thread_id='mcur', purpose='main', retired_threads=['tm'])
        op = dict(id='wo', account_id='a1', thread_id='ocur', purpose='worker', retired_threads=['to'])
        chats = [dict(id='c1', main_worker='wm'), dict(id='c2', operator=True, worker_id='wo'),
                 dict(id='c3', operator=True, thread_id='tx')]
        store, adapter = setup(['tx'], {t: rec('Cowork 1') for t in ('tm', 'to', 'tx')},
                               workers_extra=[main, op], chats=chats)
        out = retired_chats.delete_retired(store, adapter)
        self.assertEqual(out['deleted'], [])
        self.assertEqual(sorted(ids(out['skipped'])), ['tm', 'to', 'tx'])
        self.assertTrue(all(s['reason'] == 'belongs to a Main or operator chat' for s in out['skipped']))
        self.assertFalse([c for c in adapter.calls if c[0] == 'delete'])

    def test_skips_protected_and_user_named_titles(self):
        records = {'p1': rec('NotionTool'), 'p2': rec('Main 2'), 'p3': rec('Welcome to Notion', user_set=True),
                   'p4': rec('My sales notes'), 'p5': rec('roblox')}
        store, adapter = setup(list(records), records)
        out = retired_chats.delete_retired(store, adapter)
        self.assertEqual(out['deleted'], [])
        reasons = {s['thread_id']: s['reason'] for s in out['skipped']}
        self.assertTrue(reasons['p1'].startswith('protected title'))
        self.assertTrue(reasons['p2'].startswith('protected title'))
        self.assertTrue(reasons['p3'].startswith('title set by the user'))
        self.assertTrue(reasons['p4'].startswith('named chat'))
        self.assertTrue(reasons['p5'].startswith('protected title'))
        self.assertFalse([c for c in adapter.calls if c[0] == 'delete'])
        self.assertEqual(store.get('workers', 'w1')['retired_threads'], list(records))

    def test_skips_not_ready_account(self):
        store, adapter = setup(['t1'], {'t1': rec('Cowork 1')}, account_state='auth_failed')
        out = retired_chats.delete_retired(store, adapter)
        self.assertEqual(ids(out['skipped']), ['t1'])
        self.assertIn('not ready', out['skipped'][0]['reason'])
        self.assertEqual(adapter.calls, [])

    def test_already_gone(self):
        store, adapter = setup(['t1'], {'t1': rec('Cowork 1', alive=False)})
        out = retired_chats.delete_retired(store, adapter)
        self.assertEqual(out['deleted'][0]['note'], 'already gone')
        self.assertEqual(store.get('workers', 'w1')['retired_threads'], [])
        self.assertFalse([c for c in adapter.calls if c[0] == 'delete'])
        self.assertEqual(store.events[0][0], 'coworker_old_chat_deleted')

    def test_failure_kept(self):
        store, adapter = setup(['t1', 't2'], {'t1': rec('Cowork 1'), 't2': rec('Cowork 2')}, fail=['t1'])
        out = retired_chats.delete_retired(store, adapter)
        self.assertEqual(ids(out['failed']), ['t1'])
        self.assertIn('did not confirm', out['failed'][0]['error'])
        self.assertEqual(ids(out['deleted']), ['t2'])
        self.assertEqual(store.get('workers', 'w1')['retired_threads'], ['t1'])
        self.assertEqual([e[0] for e in store.events], ['coworker_old_chat_delete_failed', 'coworker_old_chat_deleted'])

    def test_records_error_does_not_raise(self):
        store, adapter = setup(['t1'], {'t1': rec('Cowork 1')})
        adapter.thread_records = lambda client, ids: (_ for _ in ()).throw(RuntimeError('boom'))
        out = retired_chats.delete_retired(store, adapter)
        self.assertEqual(ids(out['failed']), ['t1'])

    def test_dry_run_changes_nothing(self):
        store, adapter = setup(['t1', 't2', 't3'], {'t1': rec('Cowork 1'), 't2': rec('Cowork 2', alive=False),
                                                  't3': rec('Sonstiges')})
        before = copy.deepcopy(store.data)
        out = retired_chats.delete_retired(store, adapter, dry_run=True)
        self.assertEqual(ids(out['deleted']), ['t1', 't2'])
        self.assertEqual(ids(out['skipped']), ['t3'])
        self.assertEqual(store.data, before)
        self.assertEqual(store.puts, 0)
        self.assertEqual(store.events, [])
        self.assertFalse([c for c in adapter.calls if c[0] == 'delete'])

    def test_limit(self):
        store, adapter = setup(['t1', 't2'], {'t1': rec('Cowork 1'), 't2': rec('Cowork 2')})
        out = retired_chats.delete_retired(store, adapter, limit=1)
        self.assertEqual(ids(out['deleted']), ['t1'])
        self.assertEqual(out['skipped'], [dict(thread_id='t2', reason='limit reached')])

    def test_accounts_handled_one_after_another(self):
        w2 = dict(id='w2', account_id='a2', thread_id='cur2', purpose='worker', retired_threads=['u1'])
        store, adapter = setup(['t1'], {'t1': rec('Cowork 1'), 'u1': rec('Cowork 2')}, workers_extra=[w2])
        out = retired_chats.delete_retired(store, adapter)
        self.assertEqual(ids(out['deleted']), ['t1', 'u1'])
        order = [c[0] + ':' + (c[1] if c[0] == 'client' else c[1][7:]) for c in adapter.calls]
        self.assertEqual(order, ['client:a1', 'records:a1', 'delete:a1', 'client:a2', 'records:a2', 'delete:a2'])


if __name__ == '__main__':
    unittest.main()
