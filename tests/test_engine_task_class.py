"""Phase 4 batch 3 leftovers: task_class from the plan, normalized file claims, shared resource keys."""
import unittest
from pathlib import Path

from notiontool import file_registry, plain_protocol
from notiontool.protocol import ContractError
from tests import test_engine as fixtures


def plan(*tasks):
    return {'summary': 'plan', 'tasks': [dict(id='T%d' % i, title='Part %d' % i, prompt='Do part %d' % i,
                                              acceptance='Checked', depends_on=[], **extra)
                                         for i, extra in enumerate(tasks)]}


class EngineTaskClassTests(unittest.TestCase):
    worker = fixtures.EngineTests.worker
    tearDown = fixtures.EngineTests.tearDown

    def setUp(self):
        fixtures.EngineTests.setUp(self)
        self.root = Path(self.temp.name).resolve()
        self.run_record = dict(id='r1', chat_id='c', state='executing', capacity=2, project_path=str(self.root))
        self.store.put('runs', self.run_record)

    def running(self, ident, lease, files=()):
        task = dict(id=ident, run_id='r1', role='executor', title=ident, state='running', lease=lease,
                    worker_id='w0', attempts=1, files=list(files))
        self.store.put('tasks', task)
        return task

    def executors(self):
        return [t for t in self.engine.tasks('r1') if t['role'] == 'executor']

    # task_class
    def test_add_task_stores_task_class_trimmed_and_lowercased(self):
        task = self.engine.add_task(self.run_record, 'executor', 'Do it', task_class='  Visual ')
        self.assertEqual(self.store.get('tasks', task['id'])['task_class'], 'visual')

    def test_add_task_default_task_class_is_empty(self):
        task = self.engine.add_task(self.run_record, 'executor', 'Do it')
        self.assertEqual(self.store.get('tasks', task['id'])['task_class'], '')

    def test_import_plan_copies_task_class_into_each_task(self):
        self.engine._import_plan(self.run_record, plan(dict(task_class='UI'), dict(task_class=' code '), {}))
        classes = sorted(t['task_class'] for t in self.executors())
        self.assertEqual(classes, ['', 'code', 'ui'])

    def test_fix_plan_copies_task_class(self):
        self.engine._import_plan(self.run_record, plan(dict(task_class='Docs')), prefix='Fix: ')
        task = self.executors()[0]
        self.assertEqual((task['title'], task['task_class']), ('Fix: Part 0', 'docs'))

    def test_visual_follows_the_copied_task_class(self):
        self.engine._import_plan(self.run_record, plan(
            dict(task_class='visual'), dict(task_class='code', files=['look.py'])))
        by_class = {t['task_class']: t for t in self.executors()}
        by_class['code']['prompt'] = 'Check the Roblox Studio screen and the Blender window'
        self.assertTrue(plain_protocol.visual(by_class['visual'], self.run_record))
        self.assertFalse(plain_protocol.visual(by_class['code'], self.run_record))

    # claim_file
    def test_claim_file_stores_the_normalized_spelling(self):
        self.running('a1', 'L1')
        self.engine.claim_file('a1', 'L1', '.\\src\\a.py')
        self.assertEqual(self.store.get('tasks', 'a1')['files'], ['src/a.py'])

    def test_duplicate_spellings_are_saved_once(self):
        self.running('a1', 'L1')
        for spelling in ('src/a.py', 'src\\a.py', './src/a.py', 'SRC/A.py', str(self.root / 'src' / 'a.py')):
            self.engine.claim_file('a1', 'L1', spelling)
        self.assertEqual(self.store.get('tasks', 'a1')['files'], ['src/a.py'])

    def test_conflict_under_a_different_spelling(self):
        self.running('a1', 'L1', ['shared.py'])
        self.running('a2', 'L2')
        with self.assertRaisesRegex(ValueError, 'owns this file'):
            self.engine.claim_file('a2', 'L2', '.\\Shared.py')
        self.assertEqual(self.store.get('tasks', 'a2')['files'], [])

    def test_claim_outside_the_project_is_refused(self):
        self.running('a1', 'L1')
        with self.assertRaises(ValueError):
            self.engine.claim_file('a1', 'L1', '../outside.py')
        self.assertEqual(self.store.get('tasks', 'a1')['files'], [])

    def test_operator_check_still_applies(self):
        self.running('a1', 'L1')
        file_registry.claim_operator(self.store, self.root / 'op.py')
        with self.assertRaisesRegex(ValueError, 'operator is editing'):
            self.engine.claim_file('a1', 'L1', 'op.py')

    def test_inactive_lease_is_still_refused(self):
        self.running('a1', 'L1')
        with self.assertRaisesRegex(ValueError, 'no longer active'):
            self.engine.claim_file('a1', 'other', 'x.py')

    # _resource_keys and plan import
    def test_resource_keys_match_file_registry(self):
        files = ['src/a.py', 'src\\a.py', 'docs/b.md']
        self.assertEqual(self.engine._resource_keys(self.run_record, files),
                         file_registry.resource_keys(self.root, files))
        self.assertEqual(self.engine._resource_keys(self.run_record, []), [])

    def test_resource_keys_outside_path_raises_contract_error(self):
        with self.assertRaises(ContractError):
            self.engine._resource_keys(self.run_record, ['../escape.py'])

    def test_no_tasks_created_on_contract_error(self):
        with self.assertRaises(ContractError):
            self.engine._import_plan(self.run_record, plan(dict(files=['ok.py']), dict(files=['../escape.py'])))
        self.assertEqual(self.executors(), [])

    def test_project_root_falls_back_to_artifacts(self):
        self.assertEqual(self.engine._project_root(self.run_record), self.root)
        bare = dict(id='r2')
        self.assertEqual(self.engine._project_root(bare),
                         (Path(self.store.directory) / 'artifacts' / 'r2').resolve())


if __name__ == '__main__':
    unittest.main()
