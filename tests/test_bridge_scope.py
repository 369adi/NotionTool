"""Coworker write scope: a task's files list and read-only roles (fixed and custom)."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from notiontool.bridge import Bridge
from notiontool.engine import Engine
from notiontool.store import Store
import tests.test_engine as base


class BridgeScopeTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent / '.test-data'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.project = (Path(self.temp.name) / 'project').resolve()
        (self.project / 'docs').mkdir(parents=True)
        self.store = Store(Path(self.temp.name) / 'data')
        self.engine = Engine(self.store, base.FakeAdapter())
        self.store.put('runs', dict(id='r1', chat_id='c', folder_id='', goal='Docs', state='executing',
                                    project_path=str(self.project)))
        tasks = (('a1', 'one', 'executor', ['docs/A.md']),
                 ('a2', 'two', 'executor', []),
                 ('a3', 'three', 'auditor', ['docs/C.md']),
                 ('a4', 'four', 'reviewer', ['docs/D.md']),
                 ('a5', 'five', 'helper', ['docs/E.md']))
        for ident, lease, role, files in tasks:
            self.store.put('tasks', dict(id=ident, run_id='r1', state='running', lease=lease, files=files,
                                         role=role, title=ident, worker_id='w-' + ident))
        settings = dict(self.store.settings(), role_definitions=[
            dict(name='auditor', tools='read_only', description='Custom read-only role'),
            dict(name='helper', tools='read_write', description='Custom writable role')])
        for patcher in (mock.patch.object(self.store, 'settings', return_value=settings),
                        mock.patch('notiontool.devtools._backup', return_value='')):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.bridge = Bridge(self.store, self.engine)
        self.other = self.project / 'docs' / 'Other.md'
        self.other.write_bytes(b'keep\n')

    def tearDown(self):
        self.engine.close()
        time.sleep(.03)
        self.store.close()
        self.temp.cleanup()

    def write(self, token, path, text='new\n'):
        return self.bridge.call('write_file', {'path': str(path), 'text': text}, token)

    def op_write(self, token, path, text='new\n'):
        return self.bridge.operator_call(None, 'nt_write_file', dict(path=str(path), content=text, assignment_token=token))

    def test_allowed_path_is_written(self):
        self.write('one', 'docs/A.md', 'relative\n')
        self.assertEqual((self.project / 'docs' / 'A.md').read_bytes(), b'relative\n')

    def test_absolute_and_relative_forms_match(self):
        target = self.project / 'docs' / 'A.md'
        self.write('one', target, 'absolute\n')
        self.assertEqual(target.read_bytes(), b'absolute\n')
        self.write('one', 'docs/../docs/A.md', 'dotted\n')
        self.assertEqual(target.read_bytes(), b'dotted\n')
        self.op_write('one', target, 'operator tool\n')
        self.assertEqual(target.read_bytes(), b'operator tool\n')
        # An absolute entry in the files list matches a relative write as well.
        self.store.put('tasks', dict(self.store.get('tasks', 'a1'), files=[str(target)], assigned_files=[str(target)]))
        self.write('one', 'docs/A.md', 'abs entry\n')
        self.assertEqual(target.read_bytes(), b'abs entry\n')

    def test_path_outside_the_files_list_is_refused(self):
        attempts = (
            lambda: self.write('one', 'docs/Other.md'),
            lambda: self.write('one', self.other),
            lambda: self.write('one', 'docs/New.md'),
            lambda: self.op_write('one', self.other),
            lambda: self.bridge.operator_call(None, 'nt_edit_file', dict(path=str(self.other), old_text='keep',
                                                                        new_text='x', assignment_token='one')),
        )
        for attempt in attempts:
            with self.assertRaisesRegex(ValueError, "not in this task's files list.*no file was changed"):
                attempt()
        self.assertEqual(self.other.read_bytes(), b'keep\n')
        self.assertFalse((self.project / 'docs' / 'New.md').exists())

    def test_task_without_files_list_keeps_todays_behaviour(self):
        self.write('two', 'docs/Other.md', 'free\n')
        self.op_write('two', self.project / 'docs' / 'New.md', 'free too\n')
        self.assertEqual(self.other.read_bytes(), b'free\n')
        self.assertEqual((self.project / 'docs' / 'New.md').read_bytes(), b'free too\n')
        self.assertEqual(self.store.get('tasks', 'a2')['assigned_files'], [])  # claims did not narrow it

    def test_custom_read_only_role_cannot_write(self):
        self.assertTrue(self.bridge.role_is_read_only('auditor'))
        self.assertFalse(self.bridge.role_is_read_only('helper'))
        self.assertFalse(self.bridge.role_is_read_only('executor'))
        target = self.project / 'docs' / 'C.md'
        attempts = (
            lambda: self.write('three', 'docs/C.md'),
            lambda: self.op_write('three', target),
            lambda: self.bridge.operator_call(None, 'nt_edit_file', dict(path=str(target), old_text='a',
                                                                        new_text='b', assignment_token='three')),
            lambda: self.bridge.call('run_shell', {'command': 'Set-Content docs/C.md x'}, 'three'),
        )
        for attempt in attempts:
            with self.assertRaisesRegex(ValueError, 'read-only PC access'):
                attempt()
        self.assertFalse(target.exists())

    def test_fixed_read_only_role_still_refused(self):
        self.assertTrue(self.bridge.role_is_read_only('reviewer'))
        with self.assertRaisesRegex(ValueError, 'read-only PC access'):
            self.op_write('four', self.project / 'docs' / 'D.md')
        self.assertFalse((self.project / 'docs' / 'D.md').exists())

    def test_custom_writable_role_writes_its_own_file(self):
        self.write('five', 'docs/E.md', 'helper\n')
        self.assertEqual((self.project / 'docs' / 'E.md').read_bytes(), b'helper\n')


if __name__ == '__main__':
    unittest.main()
