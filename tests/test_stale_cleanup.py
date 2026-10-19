import json
import tempfile
import time
import unittest
from pathlib import Path

from notiontool import file_registry, stale_tasks
from notiontool.store import Store

NOW = 1_800_000_000.0
HOUR = 3600


class StoreCase(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent / '.test-data'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.store = Store(Path(self.temp.name) / 'data')

    def tearDown(self):
        time.sleep(.03)
        self.store.close()
        self.temp.cleanup()


class ExpiredClaimTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.project = Path(self.temp.name) / 'project'
        self.project.mkdir()
        self.old = str((self.project / 'engine.py').resolve())
        self.live = str((self.project / 'docs.md').resolve())
        self.old_key, self.live_key = file_registry.path_key(self.old), file_registry.path_key(self.live)
        self.store.put(file_registry.KIND, dict(id=self.old_key, path=self.old, owner='operator',
                                                since=NOW - 3 * HOUR, at=NOW - 3 * HOUR, expires=NOW - 2 * HOUR))
        self.store.put(file_registry.KIND, dict(id=self.live_key, path=self.live, owner='operator',
                                                since=NOW - 60, at=NOW - 60, expires=NOW + 600))

    def assert_only_live_left(self):
        self.assertIsNone(self.store.get(file_registry.KIND, self.old_key))
        live = self.store.get(file_registry.KIND, self.live_key)
        self.assertEqual(live['expires'], NOW + 600)
        self.assertEqual(live['since'], NOW - 60)

    def test_operator_claims_deletes_expired_and_keeps_live(self):
        claims = file_registry.operator_claims(self.store, NOW)
        self.assertEqual([c['id'] for c in claims], [self.live_key])
        self.assert_only_live_left()

    def test_in_use_deletes_expired(self):
        items = file_registry.in_use(self.store, now=NOW)
        self.assertEqual([i['path'] for i in items], [self.live])
        self.assert_only_live_left()

    def test_summary_deletes_expired(self):
        text = file_registry.summary(self.store, str(self.project), now=NOW)
        self.assertIn('docs.md (operator)', text)
        self.assertNotIn('engine.py', text)
        self.assert_only_live_left()

    def test_summary_is_empty_when_only_expired_claims_exist(self):
        self.store.delete(file_registry.KIND, self.live_key)
        self.assertEqual(file_registry.summary(self.store, str(self.project), now=NOW), '')
        self.assertEqual(self.store.all(file_registry.KIND), [])

    def test_operator_owner_ignores_expired(self):
        self.assertIsNone(file_registry.operator_owner(self.store, [self.old_key], NOW))
        self.assertEqual(file_registry.operator_owner(self.store, [self.live_key], NOW)['id'], self.live_key)
        self.assert_only_live_left()


class StaleContextTaskTests(StoreCase):
    def task(self, ident, worker_id, age, **extra):
        doc = dict(id=ident, run_id='__context__', role='context_compactor', title='Compress native context',
                   state='checkpoint', worker_id=worker_id, lease='', result='', created=NOW - age, sent_at=0)
        doc.update(extra)
        self.store.put('tasks', doc)

    def snapshot(self):
        return {kind: json.dumps(sorted(self.store.all(kind), key=lambda d: d['id']), sort_keys=True)
                for kind in ('tasks', 'workers')}

    def test_reports_missing_and_ready_unleased_workers_only(self):
        self.store.put('workers', dict(id='w-ready', state='ready', lease=''))
        self.store.put('workers', dict(id='w-other', state='ready', lease='other-lease'))
        self.store.put('workers', dict(id='w-held', state='ready', lease='L1'))
        self.store.put('workers', dict(id='w-busy', state='busy', lease=''))
        self.task('c-missing', 'w-gone', 54 * HOUR)
        self.task('c-ready', 'w-ready', 10 * HOUR)
        self.task('c-other', 'w-other', 8 * HOUR, lease='L9')
        self.task('c-held', 'w-held', 20 * HOUR, lease='L1')
        self.task('c-busy', 'w-busy', 20 * HOUR)
        self.task('c-young', 'w-ready', 2 * HOUR)
        self.task('c-done', 'w-ready', 30 * HOUR, state='done')
        self.task('t-normal', 'w-ready', 30 * HOUR, run_id='r1')
        before = self.snapshot()
        report = stale_tasks.stale_context_tasks(self.store, now=NOW)
        self.assertEqual([r['id'] for r in report], ['c-missing', 'c-ready', 'c-other'])
        first = report[0]
        self.assertEqual(first, dict(id='c-missing', state='checkpoint', title='Compress native context',
                                     worker_id='w-gone', age_seconds=54 * HOUR, worker_state='missing'))
        self.assertEqual(report[1]['worker_state'], 'ready')
        self.assertEqual(self.snapshot(), before)

    def test_age_prefers_sent_at_then_started_then_created(self):
        self.task('c-sent', 'w-gone', 50 * HOUR, sent_at=NOW - HOUR)
        self.task('c-started', 'w-gone', 50 * HOUR, started=NOW - 7 * HOUR)
        report = stale_tasks.stale_context_tasks(self.store, now=NOW)
        self.assertEqual([(r['id'], r['age_seconds']) for r in report], [('c-started', 7 * HOUR)])

    def test_threshold_and_missing_time(self):
        self.task('c-edge', 'w-gone', 6 * HOUR)
        self.task('c-notime', 'w-gone', 0, created=0)
        self.assertEqual(stale_tasks.stale_context_tasks(self.store, now=NOW), [])
        report = stale_tasks.stale_context_tasks(self.store, now=NOW, max_age=HOUR)
        self.assertEqual([r['id'] for r in report], ['c-edge'])


if __name__ == '__main__':
    unittest.main()
