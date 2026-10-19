import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from notiontool import file_registry
from notiontool.bridge import Bridge
from notiontool.engine import Engine
from notiontool.store import Store
import tests.test_engine as base


class OwnTaskLockTests(unittest.TestCase):
    """A task's file lock blocks other tasks and the operator, never the task itself."""

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
        patcher = mock.patch('notiontool.devtools._backup', return_value='')
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.engine.close()
        time.sleep(.03)
        self.store.close()
        self.temp.cleanup()

    def write(self, path=None, **extra):
        return self.bridge.operator_call(None, 'nt_write_file',
                                         dict(path=str(path or self.target), content='x', **extra))

    def test_reproduce_own_file_reported_as_held_by_a_coworker_task(self):
        # The refusal the coworker saw: its own task (Docs, a1) is named as the lock holder.
        with self.assertRaisesRegex(ValueError, r'A coworker task is editing this file right now \(Docs, a1\)'):
            self.write()
        self.assertFalse(self.target.exists())

    def test_owning_task_may_write_its_own_locked_file(self):
        self.write(assignment_token='one')
        self.assertEqual(self.target.read_text(), 'x')
        self.assertEqual(self.store.get('tasks', 'a1')['files'], ['docs/A.md'])
        self.assertEqual(file_registry.operator_claims(self.store), [])

    def test_owning_task_may_edit_its_own_locked_file(self):
        self.target.write_text('old', encoding='utf-8')
        self.bridge.operator_call(None, 'nt_edit_file', dict(path=str(self.target), old_text='old', new_text='new',
                                                             assignment_token='one'))
        self.assertEqual(self.target.read_text(), 'new')

    def test_another_task_is_still_refused(self):
        with self.assertRaisesRegex(ValueError, 'Another active task owns this file'):
            self.write(assignment_token='two')
        self.assertFalse(self.target.exists())
        self.assertEqual(self.store.get('tasks', 'a2')['files'], [])

    def test_operator_without_token_is_still_refused(self):
        with self.assertRaisesRegex(ValueError, 'coworker task is editing'):
            self.write()
        self.assertEqual(file_registry.operator_claims(self.store), [])

    def test_invalid_token_is_refused_not_treated_as_operator(self):
        with self.assertRaisesRegex(ValueError, 'Assignment expired or invalid'):
            self.write(path=self.project / 'free.txt', assignment_token='bogus')
        self.assertFalse((self.project / 'free.txt').exists())
        self.assertEqual(file_registry.operator_claims(self.store), [])

    def test_new_file_written_by_a_task_becomes_its_lock(self):
        # a2 has no planned file list, so a new file it writes becomes its lock (a1 may only write docs/A.md).
        self.write(path=self.project / 'docs' / 'B.md', assignment_token='two')
        self.assertIn('docs/B.md', self.store.get('tasks', 'a2')['files'])
        with self.assertRaisesRegex(ValueError, 'coworker task is editing'):
            self.write(path=self.project / 'docs' / 'B.md')
        with self.assertRaises(ValueError):
            self.write(path=self.project / 'docs' / 'B.md', assignment_token='one')
        self.assertEqual(self.store.get('tasks', 'a1')['files'], ['docs/A.md'])

    def test_operator_claim_still_blocks_a_task(self):
        file_registry.claim_operator(self.store, self.project / 'docs' / 'C.md')
        with self.assertRaisesRegex(ValueError, 'operator is editing'):
            self.write(path=self.project / 'docs' / 'C.md', assignment_token='two')  # a2 has no file list

    def test_project_write_file_tool_lets_the_owner_write_and_blocks_others(self):
        self.bridge.call('write_file', {'path': 'docs/A.md', 'text': 'y'}, 'one')
        self.assertEqual(self.target.read_text(), 'y')
        with self.assertRaisesRegex(ValueError, 'Another active task owns this file'):
            self.bridge.call('write_file', {'path': 'docs/A.md', 'text': 'z'}, 'two')
        self.assertEqual(self.target.read_text(), 'y')

    def test_write_tools_advertise_the_assignment_token(self):
        from notiontool.bridge import OPERATOR_TOOLS
        for tool in OPERATOR_TOOLS:
            if tool['name'] in ('nt_write_file', 'nt_edit_file'):
                self.assertIn('assignment_token', tool['inputSchema']['properties'])


if __name__ == '__main__':
    unittest.main()
