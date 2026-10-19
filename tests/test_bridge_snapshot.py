import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from notiontool import bridge as bridge_module
from notiontool import snapshots
from notiontool.bridge import Bridge
from notiontool.engine import Engine
from notiontool.store import Store
import tests.test_engine as base


class BridgeSnapshotTests(unittest.TestCase):
    """O5: a coworker write or edit of an existing project file stores the old bytes first."""

    def setUp(self):
        root = Path(__file__).resolve().parent / '.test-data'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.project = Path(self.temp.name) / 'project'
        (self.project / 'docs').mkdir(parents=True)
        self.store = Store(Path(self.temp.name) / 'data')
        self.engine = Engine(self.store, base.FakeAdapter())
        self.store.put('runs', dict(id='r1', chat_id='c', folder_id='', goal='Docs', state='executing',
                                    project_path=str(self.project)))
        for ident, lease, title, files in (('a1', 'one', 'Docs', ['docs/A.md']), ('a2', 'two', 'Router', [])):
            self.store.put('tasks', dict(id=ident, run_id='r1', state='running', lease=lease, files=files,
                                         role='executor', title=title, worker_id='w-' + ident))
        self.bridge = Bridge(self.store, self.engine)
        self.target = self.project / 'docs' / 'A.md'
        self.snap_dir = Path(self.store.directory).resolve() / bridge_module.SNAPSHOT_DIR
        patcher = mock.patch('notiontool.devtools._backup', return_value='')
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.engine.close()
        time.sleep(.03)
        self.store.close()
        self.temp.cleanup()

    def write(self, content='x', token='one'):
        return self.bridge.operator_call(None, 'nt_write_file',
                                         dict(path=str(self.target), content=content, assignment_token=token))

    def saved(self):
        folder = self.snap_dir / snapshots.SNAPSHOTS
        if not folder.is_dir():
            return []
        items = [json.loads(p.read_text(encoding='utf-8')) for p in folder.glob('*.json')]
        return sorted(items, key=lambda item: item['created_ns'])

    def kinds(self):
        return [row[0] for row in self.store.db.execute('select kind from events').fetchall()]

    def test_existing_crlf_file_is_snapshotted_byte_exact(self):
        old = b'line one\r\nline two\r\n'
        self.target.write_bytes(old)
        self.write('new')
        self.assertEqual(self.target.read_bytes(), b'new')
        saved = self.saved()
        self.assertEqual(len(saved), 1)
        self.assertEqual(list(saved[0]['files']), ['docs/A.md'])
        self.assertEqual(snapshots.stored_bytes(saved[0], 'docs/A.md'), old)
        self.assertNotIn('snapshot_failed', self.kinds())

    def test_edit_of_existing_file_is_snapshotted(self):
        self.target.write_bytes(b'old\n')
        self.bridge.operator_call(None, 'nt_edit_file', dict(path=str(self.target), old_text='old', new_text='new',
                                                             assignment_token='one'))
        self.assertEqual(self.target.read_bytes(), b'new\n')
        saved = self.saved()
        self.assertEqual(len(saved), 1)
        self.assertEqual(snapshots.stored_bytes(saved[0], 'docs/A.md'), b'old\n')

    def test_worker_write_file_tool_is_snapshotted(self):
        self.target.write_bytes(b'before\n')
        self.bridge.call('write_file', {'path': 'docs/A.md', 'text': 'after'}, 'one')
        self.assertEqual(self.target.read_bytes(), b'after')
        saved = self.saved()
        self.assertEqual(len(saved), 1)
        self.assertEqual(snapshots.stored_bytes(saved[0], 'docs/A.md'), b'before\n')

    def test_new_file_gets_no_snapshot(self):
        self.write('fresh')
        self.assertEqual(self.target.read_bytes(), b'fresh')
        self.assertEqual(self.saved(), [])

    def test_snapshots_live_in_the_data_folder_not_the_project(self):
        self.target.write_bytes(b'old')
        self.write('new')
        self.assertTrue(self.snap_dir.is_dir())
        self.assertFalse(self.snap_dir.is_relative_to(self.project.resolve()))
        self.assertEqual(sorted(p.name for p in self.project.rglob('*')), ['A.md', 'docs'])

    def test_snapshot_failure_still_writes_and_logs_an_event(self):
        self.target.write_bytes(b'old')
        with mock.patch('notiontool.snapshots.take', side_effect=OSError('disk full')):
            result = self.write('new')
        self.assertEqual(self.target.read_bytes(), b'new')
        self.assertTrue(result)
        rows = [json.loads(doc) for kind, doc in self.store.db.execute('select kind, doc from events').fetchall()
                if kind == 'snapshot_failed']
        self.assertEqual(len(rows), 1)
        self.assertIn('disk full', json.dumps(rows[0]))

    def test_prune_keeps_the_limit(self):
        self.target.write_bytes(b'v0')
        with mock.patch.object(bridge_module, 'SNAPSHOT_KEEP', 2):
            for number in range(1, 5):
                self.write('v%d' % number)
        saved = self.saved()
        self.assertEqual(len(saved), 2)
        self.assertEqual([snapshots.stored_bytes(s, 'docs/A.md') for s in saved], [b'v2', b'v3'])
        objects = [p for p in (self.snap_dir / snapshots.OBJECTS).iterdir() if not p.name.startswith('.')]
        self.assertEqual(len(objects), 2)

    def test_lock_still_refuses_a_foreign_owner_without_snapshot(self):
        self.target.write_bytes(b'old')
        with self.assertRaisesRegex(ValueError, 'Another active task owns this file'):
            self.write('stolen', token='two')
        self.assertEqual(self.target.read_bytes(), b'old')
        self.assertEqual(self.saved(), [])

    def test_operator_without_token_is_still_refused(self):
        self.target.write_bytes(b'old')
        with self.assertRaisesRegex(ValueError, 'coworker task is editing'):
            self.bridge.operator_call(None, 'nt_write_file', dict(path=str(self.target), content='x'))
        self.assertEqual(self.target.read_bytes(), b'old')
        self.assertEqual(self.saved(), [])

    def test_project_elsewhere_keeps_tools_unchanged(self):
        self.target.write_bytes(b'def needle():\n    pass\n')
        self.write('def needle():\n    return 1\n')
        self.assertIsNone(self.bridge.hidden_area(self.project))
        self.assertEqual([e['name'] for e in self.bridge.call('list_files', {'path': 'docs'}, 'one')], ['A.md'])
        with mock.patch('notiontool.devtools.ROOT', self.project.resolve()):
            hits = self.bridge.operator_call(None, 'nt_search_code', dict(pattern='needle', path='.'))['results']
            listing = self.bridge.operator_call(None, 'nt_list_dir', dict(path='.'))['entries']
            (self.project / 'docs' / 'B.js').write_bytes(b'function needle() {}\n')
            outline = self.bridge.operator_call(None, 'nt_file_outline', dict(path='docs/B.js'))['outline']
        self.assertEqual(len(hits), 1)
        self.assertEqual(outline, ['1: needle'])
        self.assertEqual(listing, ['docs/'])


class DataFolderGuardTests(unittest.TestCase):
    """NotionTool working on itself: the data folder (with the snapshots) sits inside the project."""

    def setUp(self):
        root = Path(__file__).resolve().parent / '.test-data'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.project = (Path(self.temp.name) / 'project').resolve()
        (self.project / 'docs').mkdir(parents=True)
        # Not named 'data', so devtools.SKIP cannot hide it by name: only the new guard does.
        self.store = Store(self.project / 'state')
        self.engine = Engine(self.store, base.FakeAdapter())
        self.store.put('runs', dict(id='r1', chat_id='c', folder_id='', goal='Docs', state='executing',
                                    project_path=str(self.project)))
        self.store.put('tasks', dict(id='a1', run_id='r1', state='running', lease='one', files=['docs/A.md'],
                                     role='executor', title='Docs', worker_id='w-a1'))
        self.bridge = Bridge(self.store, self.engine)
        self.target = self.project / 'docs' / 'A.md'
        for patcher in (mock.patch('notiontool.devtools._backup', return_value=''),
                        mock.patch('notiontool.devtools.ROOT', self.project)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.target.write_bytes(b'def needle():\n    pass\n')
        self.op('nt_write_file', path=str(self.target), content='def needle():\n    return 1\n', assignment_token='one')
        self.snap_dir = Path(self.store.directory).resolve() / bridge_module.SNAPSHOT_DIR
        self.snap_files = sorted(p for p in self.snap_dir.rglob('*') if p.is_file())

    def tearDown(self):
        self.engine.close()
        time.sleep(.03)
        self.store.close()
        self.temp.cleanup()

    def op(self, name, **args):
        return self.bridge.operator_call(None, name, args)

    def test_snapshots_really_land_inside_the_project(self):
        self.assertTrue(self.snap_files)
        self.assertTrue(all(p.is_relative_to(self.project) for p in self.snap_files))
        from notiontool import devtools
        raw = devtools.call('nt_search_code', dict(pattern='needle', path='.'))['results']
        self.assertTrue(any('state' in hit for hit in raw))  # without the guard the old bytes are found

    def test_search_hides_the_data_folder(self):
        hits = self.op('nt_search_code', pattern='needle', path='.')['results']
        self.assertEqual(len(hits), 1)
        self.assertIn('A.md:1: def needle():', hits[0])
        self.assertFalse(any('state' in hit for hit in hits))
        with self.assertRaisesRegex(ValueError, 'inside the NotionTool data folder'):
            self.op('nt_search_code', pattern='needle', path='state')

    def test_listing_hides_the_data_folder(self):
        self.assertEqual(self.op('nt_list_dir', path='.')['entries'], ['docs/'])
        recursive = self.op('nt_list_dir', path='.', recursive=True)['entries']
        self.assertEqual([e.replace('\\', '/') for e in recursive], ['docs/A.md'])
        self.assertEqual([e['name'] for e in self.bridge.call('list_files', {'path': '.'}, 'one')], ['docs'])
        for name, args in (('nt_list_dir', dict(path='state/snapshots')),):
            with self.assertRaisesRegex(ValueError, 'Listing refused'):
                self.op(name, **args)
        with self.assertRaisesRegex(ValueError, 'Listing refused'):
            self.bridge.call('list_files', {'path': 'STATE'}, 'one')  # same case-insensitive key as path_key

    def test_outline_refuses_snapshot_files(self):
        with self.assertRaisesRegex(ValueError, 'Outline refused'):
            self.op('nt_file_outline', path=str(self.snap_files[0]))
        (self.project / 'docs' / 'B.js').write_bytes(b'function needle() {}\n')
        self.assertEqual(self.op('nt_file_outline', path='docs/B.js')['outline'], ['1: needle'])

    def test_snapshot_files_are_not_writable(self):
        target = self.snap_files[0]
        before = target.read_bytes()
        rel = target.relative_to(self.project).as_posix()
        attempts = (
            lambda: self.op('nt_write_file', path=str(target), content='x', assignment_token='one'),
            lambda: self.op('nt_write_file', path=str(target), content='x'),
            lambda: self.op('nt_edit_file', path=str(target), old_text='a', new_text='b', count=1),
            lambda: self.bridge.call('write_file', {'path': rel, 'text': 'x'}, 'one'),
            lambda: self.bridge.call('write_file', {'path': 'state/new.txt', 'text': 'x'}, 'one'),
        )
        for attempt in attempts:
            with self.assertRaisesRegex(ValueError, 'inside the NotionTool data folder'):
                attempt()
        self.assertEqual(target.read_bytes(), before)
        self.assertFalse((self.project / 'state' / 'new.txt').exists())

    def test_normal_project_files_stay_writable(self):
        self.op('nt_write_file', path=str(self.target), content='def needle():\n    return 2\n', assignment_token='one')
        self.bridge.call('write_file', {'path': 'docs/A.md', 'text': 'def needle():\n    return 3\n'}, 'one')
        self.assertEqual(self.target.read_bytes(), b'def needle():\n    return 3\n')


if __name__ == '__main__':
    unittest.main()
