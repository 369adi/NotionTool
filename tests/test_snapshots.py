"""Tests for notiontool.snapshots; temporary folders only, never the real work folder."""
import hashlib
import os
import shutil
import tempfile
import unittest
from unittest import mock

from notiontool import snapshot_diff
from notiontool import snapshots as snap


def sha(data):
    return hashlib.sha256(data).hexdigest()


class SnapshotTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.join(self._tmp.name, 'project')
        os.makedirs(self.root)
        self.store = os.path.join(self._tmp.name, 'store')
        self._ns = 1000000000
        patcher = mock.patch.object(snap, '_clock', self._tick)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _tick(self):
        self._ns += 1000
        return self._ns

    def path(self, rel):
        return os.path.join(self.root, *rel.split('/'))

    def write(self, rel, data):
        full = self.path(rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'wb') as handle:
            handle.write(data)
        return full

    def read(self, rel):
        with open(self.path(rel), 'rb') as handle:
            return handle.read()

    def take(self, task_id, paths):
        return snap.take(task_id, paths, self.root, store_dir=self.store)

    def object_dirs(self):
        folder = os.path.join(self.store, snap.OBJECTS)
        return sorted(name for name in os.listdir(folder) if not name.startswith('.'))


class TakeTests(SnapshotTestCase):
    def test_take_records_hash_size_and_mtime(self):
        full = self.write('a.py', b'print(1)\n')
        stat = os.stat(full)
        snapshot = self.take('t1', ['a.py'])
        entry = snapshot['files']['a.py']
        self.assertEqual(entry['sha256'], sha(b'print(1)\n'))
        self.assertEqual(entry['size'], 9)
        self.assertEqual(entry['mtime_ns'], stat.st_mtime_ns)
        self.assertAlmostEqual(entry['mtime'], stat.st_mtime, places=3)
        self.assertEqual(snapshot['task_id'], 't1')
        self.assertEqual(snapshot['missing'], [])
        self.assertEqual(snap.load('t1', self.root, self.store), snapshot)

    def test_identical_contents_stored_once(self):
        self.write('a.py', b'same')
        self.write('pkg/c.py', b'same')
        self.write('d.py', b'other')
        self.take('t1', ['a.py', 'pkg/c.py', 'd.py'])
        self.assertEqual(self.object_dirs(), sorted([sha(b'same'), sha(b'other')]))
        self.take('t2', ['a.py', 'd.py'])
        self.assertEqual(self.object_dirs(), sorted([sha(b'same'), sha(b'other')]))

    def test_object_folder_named_by_sha_keeps_copy(self):
        self.write('a.py', b'v1')
        snapshot = self.take('t1', ['a.py'])
        self.write('a.py', b'v2')
        stored = snap.object_path(self.store, sha(b'v1'))
        self.assertEqual(os.path.basename(os.path.dirname(stored)), sha(b'v1'))
        with open(stored, 'rb') as handle:
            self.assertEqual(handle.read(), b'v1')
        self.assertEqual(snap.stored_bytes(snapshot, 'a.py'), b'v1')

    def test_missing_and_duplicate_paths(self):
        self.write('a.py', b'x')
        snapshot = self.take('t1', ['./a.py', 'a.py', 'gone.py', 'gone.py'])
        self.assertEqual(list(snapshot['files']), ['a.py'])
        self.assertEqual(snapshot['missing'], ['gone.py'])

    def test_backslash_and_absolute_paths_are_normalized(self):
        self.write('pkg/m.py', b'x')
        snapshot = self.take('t1', ['pkg\\m.py', self.path('pkg/m.py'), 'pkg//m.py'])
        self.assertEqual(list(snapshot['files']), ['pkg/m.py'])

    def test_path_outside_root_is_rejected(self):
        outside = os.path.join(self._tmp.name, 'outside.py')
        with open(outside, 'wb') as handle:
            handle.write(b'x')
        with self.assertRaises(ValueError):
            self.take('t1', ['../outside.py'])
        with self.assertRaises(ValueError):
            self.take('t1', [outside])

    def test_default_store_dir_is_inside_given_root(self):
        self.write('a.py', b'x')
        snapshot = snap.take('t1', ['a.py'], self.root)
        expected = os.path.join(self.root, 'work', 'snapshots')
        self.assertEqual(os.path.normcase(snapshot['store_dir']), os.path.normcase(os.path.abspath(expected)))
        self.assertTrue(os.path.isfile(os.path.join(expected, 'snapshots', 't1.json')))

    def test_task_id_is_sanitized_and_empty_rejected(self):
        self.assertEqual(snap.safe_task_id('run 1/a'), 'run_1_a')
        for bad in ('', '...', None):
            with self.assertRaises(ValueError):
                snap.safe_task_id(bad)
        with self.assertRaises(ValueError):
            self.take('', [])


class CompareTests(SnapshotTestCase):
    def test_compare_categories(self):
        self.write('keep.py', b'k')
        self.write('edit.py', b'e1')
        self.write('gone.py', b'g')
        before = self.take('t1', ['keep.py', 'edit.py', 'gone.py'])
        self.write('edit.py', b'e2 changed')
        os.unlink(self.path('gone.py'))
        self.write('new.py', b'n')
        result = snap.compare(before, ['new.py', 'never.py'])
        self.assertEqual(result, dict(changed=['edit.py'], added=['new.py'], removed=['gone.py'], unchanged=['keep.py']))

    def test_missing_at_take_then_created_is_added(self):
        before = self.take('t1', ['later.py'])
        self.write('later.py', b'now')
        self.assertEqual(snap.compare(before, ['later.py'])['added'], ['later.py'])

    def test_compare_result_feeds_snapshot_diff_summary(self):
        self.write('edit.py', b'1')
        before = self.take('t1', ['edit.py'])
        self.write('edit.py', b'2')
        text = snapshot_diff.summary(snap.compare(before, []))
        self.assertIn('Snapshot comparison: 1 changed, 0 added, 0 removed, 0 unchanged.', text)
        self.assertIn('- edit.py', text)

    def test_diff_uses_snapshot_diff(self):
        self.write('n.py', b'one\ntwo\n')
        self.write('same.py', b'a\n')
        self.write('le.py', b'a\nb\n')
        before = self.take('t1', ['n.py', 'same.py', 'le.py'])
        self.write('n.py', b'one\n2\n')
        self.write('le.py', b'a\r\nb\r\n')
        lines = snap.diff(before, 'n.py').split('\n')
        self.assertEqual(lines[0], '--- a/n.py')
        self.assertIn('+2', lines)
        self.assertEqual(snap.diff(before, 'same.py'), 'same.py: no changes')
        self.assertEqual(snap.diff(before, 'le.py'), 'le.py: line endings changed LF -> CRLF')


class OutsideClaimTests(unittest.TestCase):
    def test_paths_are_normalized(self):
        changed = ['a.py', '.\\pkg\\b.py', 'pkg//c.py', 'docs/x.md']
        self.assertEqual(snap.outside_claim(changed, ['./a.py', 'pkg/b.py']), ['docs/x.md', 'pkg/c.py'])

    def test_claimed_folder_covers_files_below(self):
        changed = ['pkg/b.py', 'pkg/sub/c.py', 'pkgx/y.py']
        self.assertEqual(snap.outside_claim(changed, ['pkg/']), ['pkgx/y.py'])

    def test_accepts_compare_result(self):
        result = dict(changed=['a.py'], added=['b.py'], removed=['c.py'], unchanged=['d.py'])
        self.assertEqual(snap.outside_claim(result, ['a.py']), ['b.py', 'c.py'])

    def test_empty_claim_reports_every_change_once(self):
        self.assertEqual(snap.outside_claim(['b.py', 'a.py', './a.py'], []), ['a.py', 'b.py'])


class RestoreTests(SnapshotTestCase):
    def restore(self, task_id, path):
        return snap.restore(task_id, path, root=self.root, store_dir=self.store)

    def test_backs_up_current_file_then_restores(self):
        self.write('a.py', b'v1')
        entry = self.take('t1', ['a.py'])['files']['a.py']
        self.write('a.py', b'v2 changed')
        result = self.restore('t1', 'a.py')
        self.assertEqual(self.read('a.py'), b'v1')
        self.assertEqual(result['path'], 'a.py')
        self.assertEqual(result['sha256'], sha(b'v1'))
        with open(result['backup'], 'rb') as handle:
            self.assertEqual(handle.read(), b'v2 changed')
        self.assertTrue(os.path.normcase(result['backup']).startswith(
            os.path.normcase(os.path.join(self.store, snap.BACKUPS, 't1'))))
        self.assertEqual(os.stat(self.path('a.py')).st_mtime_ns, entry['mtime_ns'])

    def test_restores_deleted_file_without_backup(self):
        self.write('pkg/m.py', b'module')
        self.take('t1', ['pkg/m.py'])
        shutil.rmtree(self.path('pkg'))
        result = self.restore('t1', 'pkg\\m.py')
        self.assertIsNone(result['backup'])
        self.assertEqual(self.read('pkg/m.py'), b'module')

    def test_unknown_task_or_path_raises(self):
        self.write('a.py', b'x')
        self.take('t1', ['a.py'])
        with self.assertRaises(KeyError):
            self.restore('t1', 'other.py')
        with self.assertRaises(KeyError):
            self.restore('nope', 'a.py')

    def test_damaged_copy_leaves_file_alone(self):
        self.write('a.py', b'v1')
        self.take('t1', ['a.py'])
        with open(snap.object_path(self.store, sha(b'v1')), 'wb') as handle:
            handle.write(b'bad')
        self.write('a.py', b'current')
        with self.assertRaises(ValueError):
            self.restore('t1', 'a.py')
        self.assertEqual(self.read('a.py'), b'current')
        self.assertFalse(os.path.exists(os.path.join(self.store, snap.BACKUPS)))

    def test_two_restores_keep_two_backups(self):
        self.write('a.py', b'v1')
        self.take('t1', ['a.py'])
        self.write('a.py', b'v2')
        first = self.restore('t1', 'a.py')['backup']
        self.write('a.py', b'v3')
        second = self.restore('t1', 'a.py')['backup']
        self.assertNotEqual(first, second)
        with open(first, 'rb') as handle:
            self.assertEqual(handle.read(), b'v2')
        with open(second, 'rb') as handle:
            self.assertEqual(handle.read(), b'v3')


class PruneTests(SnapshotTestCase):
    def prune(self, **kwargs):
        return snap.prune(root=self.root, store_dir=self.store, **kwargs)

    def test_keep_last_removes_oldest_and_unused_contents(self):
        self.write('s.py', b'shared')
        for number in (1, 2, 3):
            self.write('f%d.py' % number, b'only-%d' % number)
            self.take('t%d' % number, ['f%d.py' % number, 's.py'])
        result = self.prune(keep_last=2)
        self.assertEqual(result['removed_snapshots'], ['t1'])
        self.assertEqual(result['kept_snapshots'], 2)
        self.assertEqual(result['removed_objects'], 1)
        self.assertEqual(self.object_dirs(), sorted([sha(b'shared'), sha(b'only-2'), sha(b'only-3')]))
        with self.assertRaises(KeyError):
            snap.load('t1', self.root, self.store)
        self.assertEqual(snap.load('t3', self.root, self.store)['task_id'], 't3')

    def test_max_mb_drops_oldest_until_under_limit(self):
        self.write('big1.bin', b'1' * 2000)
        self.take('t1', ['big1.bin'])
        self.write('big2.bin', b'2' * 2000)
        self.take('t2', ['big2.bin'])
        self.write('small.txt', b'0123456789')
        self.take('t3', ['small.txt'])
        result = self.prune(keep_last=200, max_mb=3000 / (1024 * 1024))
        self.assertEqual(result['removed_snapshots'], ['t1'])
        self.assertEqual(result['bytes_kept'], 2010)
        self.assertNotIn(sha(b'1' * 2000), self.object_dirs())

    def test_nothing_to_prune(self):
        empty = self.prune()
        self.assertEqual(empty, dict(removed_snapshots=[], kept_snapshots=0, removed_objects=0, bytes_kept=0))
        self.write('a.py', b'x')
        self.take('t1', ['a.py'])
        result = self.prune()
        self.assertEqual(result['removed_snapshots'], [])
        self.assertEqual(self.object_dirs(), [sha(b'x')])


if __name__ == '__main__':
    unittest.main()
