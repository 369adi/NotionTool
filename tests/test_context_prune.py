"""Tests for context_archive.prune_ids and CoworkerUpkeep.prune_contexts (self-deleting chat histories)."""
import json
import tempfile
import threading
import unittest
from unittest import mock

from notiontool import context_archive as ca
from notiontool import coworker_upkeep as cu
from notiontool.store import Store

NOW = 1_000_000.0
KEEP = 6 * 3600
OLD = NOW - KEEP - 10
FRESH = NOW - 60


def rec(ident, updated=OLD, **kw):
    return dict(id=ident, updated=updated, **kw)


def worker(ident, thread=None, purpose='worker', enabled=True, state='ready', account='acc'):
    return dict(id=ident, account_id=account, thread_id=thread or ident, purpose=purpose, enabled=enabled, state=state)


class PruneIdsTests(unittest.TestCase):
    def prune(self, records, workers=(), chats=(), tasks=(), **kw):
        return ca.prune_ids(records, list(workers), list(chats), list(tasks), NOW, **kw)

    def test_live_worker_is_kept(self):
        self.assertEqual(self.prune([rec('acc:t1')], [worker('w1', 't1')]), [])

    def test_operator_and_main_workers_are_kept(self):
        workers = [worker('o', 'to', purpose='operator'), worker('m', 'tm', purpose='main')]
        self.assertEqual(self.prune([rec('acc:to'), rec('acc:tm')], workers), [])

    def test_main_chat_worker_is_kept_even_when_disabled(self):
        workers = [worker('w2', 't2', enabled=False, state='ready')]
        chats = [dict(id='c', main_worker='w2')]
        self.assertEqual(self.prune([rec('acc:t2')], workers, chats), [])

    def test_open_task_keeps_its_thread_and_worker(self):
        for state in ca.OPEN_TASK_STATES:
            with self.subTest(state=state):
                tasks = [dict(id='t', state=state, thread_id='t3')]
                self.assertEqual(self.prune([rec('acc:t3')], [], [], tasks), [])
        tasks = [dict(id='t', state='queued', worker_id='w5')]
        self.assertEqual(self.prune([rec('acc:t5')], [worker('w5', 't5', enabled=False)], [], tasks), [])

    def test_finished_task_does_not_keep_a_record(self):
        for state in ('done', 'failed', 'cancelled'):
            tasks = [dict(id='t', state=state, thread_id='t3', worker_id='w3')]
            self.assertEqual(self.prune([rec('acc:t3')], [worker('w3', 't3', enabled=False)], [], tasks), ['acc:t3'])

    def test_fresh_record_is_kept_even_without_owner(self):
        self.assertEqual(self.prune([rec('acc:x', FRESH)]), [])
        self.assertEqual(self.prune([rec('acc:x', NOW - KEEP + 1)]), [])
        self.assertEqual(self.prune([rec('acc:x', NOW - KEEP)]), ['acc:x'])

    def test_old_retired_missing_or_disabled_worker_is_deleted(self):
        workers = [worker('r', 'tr', enabled=False, state='retired'),
                   worker('m', 'tm', enabled=True, state='missing'),
                   worker('d', 'td', enabled=False, state='ready')]
        records = [rec('acc:tr'), rec('acc:tm'), rec('acc:td')]
        self.assertEqual(self.prune(records, workers), ['acc:td', 'acc:tm', 'acc:tr'])

    def test_fresh_record_of_retired_worker_is_kept(self):
        workers = [worker('r', 'tr', enabled=False, state='retired')]
        self.assertEqual(self.prune([rec('acc:tr', FRESH)], workers), [])

    def test_old_unknown_record_is_deleted(self):
        self.assertEqual(self.prune([rec('acc:ghost')]), ['acc:ghost'])

    def test_same_thread_in_other_account_is_not_protected(self):
        self.assertEqual(self.prune([rec('other:t1')], [worker('w1', 't1', account='acc')]), ['other:t1'])

    def test_missing_updated_counts_as_old_and_keep_s_is_configurable(self):
        self.assertEqual(self.prune([dict(id='acc:n')]), ['acc:n'])
        self.assertEqual(self.prune([rec('acc:x', NOW - 100)], keep_s=50), ['acc:x'])
        self.assertEqual(self.prune([rec('acc:x', NOW - 100)], keep_s=500), [])

    def test_dict_input_sorted_and_invalid_entries_skipped(self):
        records = {'b': rec('acc:b'), 'a': rec('acc:a'), 'bad': 'nope', 'none': dict(updated=1)}
        self.assertEqual(self.prune(records), ['acc:a', 'acc:b'])

    def test_unusable_now_deletes_nothing(self):
        self.assertEqual(ca.prune_ids([rec('acc:a')], [], [], [], None), [])


class FakeStore:
    def __init__(self, contexts=(), workers=(), chats=(), tasks=(), settings=None):
        self.data = dict(thread_context={r['id']: r for r in contexts}, workers={w['id']: w for w in workers},
                         chats={c['id']: c for c in chats}, tasks={t['id']: t for t in tasks})
        self.config = settings or {}
        self.events = []

    def settings(self):
        return dict(self.config)

    def all(self, kind):
        return [dict(v) for v in self.data[kind].values()]

    def delete(self, kind, ident):
        self.data[kind].pop(ident, None)

    def event(self, kind, **kw):
        self.events.append((kind, kw))


class Stop:
    calls = 0

    def wait(self, timeout=None):
        self.calls += 1
        return False


class Engine:
    def __init__(self):
        self.lock = threading.RLock()
        self.stop_event = Stop()


def blob(ident, updated=OLD, mb=1):
    return dict(id=ident, updated=updated, blob='x' * (mb * 1024 * 1024))


class PruneContextsTests(unittest.TestCase):
    def make(self, store, clock):
        return cu.CoworkerUpkeep(store, object(), Engine(), clock=lambda: clock[0])

    def test_deletes_unneeded_records_and_writes_one_event(self):
        store = FakeStore([blob('acc:t1'), blob('acc:old1'), blob('acc:old2'), blob('acc:new', FRESH)],
                          workers=[worker('w1', 't1')])
        up = self.make(store, [NOW])
        result = up.prune_contexts()
        self.assertEqual(sorted(store.data['thread_context']), ['acc:new', 'acc:t1'])
        self.assertEqual(result['count'], 2)
        self.assertEqual(len(store.events), 1)
        kind, fields = store.events[0]
        self.assertEqual(kind, 'thread_context_pruned')
        self.assertEqual(fields['count'], 2)
        self.assertAlmostEqual(fields['mb'], 2.0, places=1)

    def test_runs_at_most_once_per_hour(self):
        store = FakeStore([blob('acc:a')])
        clock = [NOW]
        up = self.make(store, clock)
        self.assertIsNotNone(up.prune_contexts())
        store.data['thread_context']['acc:b'] = blob('acc:b')
        clock[0] = NOW + cu.PRUNE_INTERVAL - 1
        self.assertIsNone(up.prune_contexts())
        self.assertIn('acc:b', store.data['thread_context'])
        clock[0] = NOW + cu.PRUNE_INTERVAL
        self.assertEqual(up.prune_contexts()['count'], 1)
        self.assertNotIn('acc:b', store.data['thread_context'])
        self.assertEqual([k for k, _ in store.events], ['thread_context_pruned'] * 2)

    def test_nothing_to_delete_writes_no_event(self):
        store = FakeStore([blob('acc:t1')], workers=[worker('w1', 't1')])
        self.assertEqual(self.make(store, [NOW]).prune_contexts()['count'], 0)
        self.assertEqual(store.events, [])

    def test_keep_hours_setting(self):
        two_hours_old = blob('acc:x', NOW - 2 * 3600, mb=0)
        default = FakeStore([dict(two_hours_old)])
        self.make(default, [NOW]).prune_contexts()
        self.assertIn('acc:x', default.data['thread_context'])
        short = FakeStore([dict(two_hours_old)], settings={'thread_context_keep_hours': 1})
        self.make(short, [NOW]).prune_contexts()
        self.assertNotIn('acc:x', short.data['thread_context'])
        bad = FakeStore([dict(two_hours_old)], settings={'thread_context_keep_hours': 'abc'})
        self.make(bad, [NOW]).prune_contexts()
        self.assertIn('acc:x', bad.data['thread_context'])

    def test_deletes_in_small_batches(self):
        store = FakeStore([blob('acc:%02d' % i, mb=0) for i in range(25)])
        up = self.make(store, [NOW])
        with mock.patch.object(cu, 'PRUNE_BATCH', 10):
            self.assertEqual(up.prune_contexts()['count'], 25)
        self.assertEqual(up.engine.stop_event.calls, 2)
        self.assertEqual(store.data['thread_context'], {})

    def test_loop_calls_prune_after_upkeep(self):
        store = FakeStore([blob('acc:a', mb=0)], settings={'coworker_upkeep': False})
        up = self.make(store, [NOW])
        steps = iter([False, True])
        up.engine.stop_event.wait = lambda timeout=None: next(steps)
        up.loop()
        self.assertEqual(store.data['thread_context'], {})


class VacuumTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(tmp.name)
        self.addCleanup(self.store.close)
        self.clock = [NOW]
        self.up = cu.CoworkerUpkeep(self.store, object(), Engine(), clock=lambda: self.clock[0])
        patcher = mock.patch.object(cu, 'VACUUM_MB', 1.0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fill(self, prefix, count=4):
        for i in range(count):
            self.store.put('thread_context', dict(id='%s%d' % (prefix, i), updated=0, blob='x' * 524288))

    def pruned_events(self):
        return [e for e in self.store.events(limit=500) if e['kind'] == 'thread_context_pruned']

    def test_real_store_prune_and_vacuum_once_per_day(self):
        self.fill('acc:a')
        result = self.up.prune_contexts()
        self.assertEqual(result['count'], 4)
        self.assertGreater(result['mb'], 1.9)
        self.assertTrue(result['vacuumed'])
        self.assertEqual(self.store.all('thread_context'), [])
        self.assertTrue(self.pruned_events()[-1]['vacuumed'])
        self.fill('acc:b')
        self.clock[0] += cu.PRUNE_INTERVAL
        self.assertFalse(self.up.prune_contexts()['vacuumed'])
        self.clock[0] = NOW + cu.VACUUM_INTERVAL
        self.fill('acc:c', 1)
        self.assertTrue(self.up.prune_contexts()['vacuumed'])

    def test_no_vacuum_while_a_task_runs(self):
        self.store.put('tasks', dict(id='t', state='running', thread_id='zzz', worker_id=''))
        self.fill('acc:a')
        self.assertFalse(self.up.prune_contexts()['vacuumed'])
        self.store.put('tasks', dict(id='t', state='done', thread_id='zzz', worker_id=''))
        self.clock[0] += cu.PRUNE_INTERVAL
        self.assertTrue(self.up.prune_contexts()['vacuumed'])

    def test_open_task_thread_survives_in_real_store(self):
        self.store.put('tasks', dict(id='t', state='queued', thread_id='keepme', worker_id=''))
        self.store.put('thread_context', dict(id='acc:keepme', updated=0))
        self.store.put('thread_context', dict(id='acc:gone', updated=0))
        self.up.prune_contexts()
        self.assertEqual([r['id'] for r in self.store.all('thread_context')], ['acc:keepme'])


if __name__ == '__main__':
    unittest.main()
