"""Coworker tools work inside the task's git worktree; reviews see the reviewed task's copy."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from notiontool.bridge import Bridge
from notiontool.engine import Engine
from notiontool.store import Store
import tests.test_engine as base


class BridgeWorktreeTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent / '.test-data'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.project = (Path(self.temp.name) / 'project').resolve()
        (self.project / 'pkg').mkdir(parents=True)
        (self.project / 'pkg' / 'mod.py').write_bytes(b'main\n')
        self.copy = self.project / 'work' / 'worktrees' / 'tb1'
        (self.copy / 'pkg').mkdir(parents=True)
        (self.copy / 'pkg' / 'mod.py').write_bytes(b'copy\n')
        self.store = Store(Path(self.temp.name) / 'data')
        self.engine = Engine(self.store, base.FakeAdapter())
        self.store.put('runs', dict(id='r1', chat_id='c', folder_id='', goal='Code', state='executing',
                                    project_path=str(self.project)))
        self.store.put('tasks', dict(id='b1', run_id='r1', state='running', lease='build', role='executor',
                                     title='b1', worker_id='w-b1', patch_worktree=str(self.copy),
                                     files=['pkg/mod.py', str(self.project / 'pkg' / 'abs.py')]))
        self.store.put('tasks', dict(id='v1', run_id='r1', state='running', lease='review', role='reviewer',
                                     title='v1', worker_id='w-v1', review_of='b1', files=[]))
        for patcher in (mock.patch('notiontool.devtools._backup', return_value=''),):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.bridge = Bridge(self.store, self.engine)

    def tearDown(self):
        self.engine.close()
        time.sleep(.03)
        self.store.close()
        self.temp.cleanup()

    def write(self, token, path, text='new\n'):
        return self.bridge.call('write_file', {'path': str(path), 'text': text}, token)

    def test_relative_write_lands_in_the_copy(self):
        self.write('build', 'pkg/mod.py', 'rel\n')
        self.assertEqual((self.copy / 'pkg' / 'mod.py').read_bytes(), b'rel\n')
        self.assertEqual((self.project / 'pkg' / 'mod.py').read_bytes(), b'main\n')

    def test_absolute_copy_path_matches_relative_entry(self):
        self.write('build', self.copy / 'pkg' / 'mod.py', 'abs copy\n')
        self.assertEqual((self.copy / 'pkg' / 'mod.py').read_bytes(), b'abs copy\n')

    def test_absolute_main_path_maps_into_the_copy(self):
        self.write('build', self.project / 'pkg' / 'abs.py', 'abs main\n')
        self.assertEqual((self.copy / 'pkg' / 'abs.py').read_bytes(), b'abs main\n')
        self.assertFalse((self.project / 'pkg' / 'abs.py').exists())

    def test_unlisted_file_in_the_copy_is_allowed(self):
        # Owner rule 2026-10-22: the own copy is merged and tested at the end, so any file in it is writable.
        self.write('build', 'pkg/other.py', 'other\n')
        self.assertEqual((self.copy / 'pkg' / 'other.py').read_bytes(), b'other\n')
        self.assertFalse((self.project / 'pkg' / 'other.py').exists())

    def test_reviewer_reads_the_reviewed_copy(self):
        text = self.bridge.call('read_file', {'path': 'pkg/mod.py'}, 'review')
        self.assertIn('copy', str(text))
        with self.assertRaisesRegex(ValueError, 'read-only PC access'):
            self.write('review', 'pkg/mod.py')

    def test_task_without_copy_uses_project(self):
        task = dict(self.store.get('tasks', 'b1'), patch_worktree=None)
        self.store.put('tasks', task)
        self.write('build', 'pkg/mod.py', 'main write\n')
        self.assertEqual((self.project / 'pkg' / 'mod.py').read_bytes(), b'main write\n')


    def op_write(self, token, path, text='new\n'):
        return self.bridge.operator_call(None, 'nt_write_file', dict(path=str(path), content=text, assignment_token=token))

    def test_operator_write_tool_lands_in_the_copy(self):
        self.op_write('build', 'pkg/mod.py', 'op rel\n')
        self.assertEqual((self.copy / 'pkg' / 'mod.py').read_bytes(), b'op rel\n')
        self.op_write('build', self.project / 'pkg' / 'abs.py', 'op abs\n')
        self.assertEqual((self.copy / 'pkg' / 'abs.py').read_bytes(), b'op abs\n')
        self.assertEqual((self.project / 'pkg' / 'mod.py').read_bytes(), b'main\n')
        self.assertFalse((self.project / 'pkg' / 'abs.py').exists())
        self.op_write('build', 'pkg/other.py', 'op other\n')
        self.assertEqual((self.copy / 'pkg' / 'other.py').read_bytes(), b'op other\n')
        self.assertFalse((self.project / 'pkg' / 'other.py').exists())

if __name__ == '__main__':
    unittest.main()
