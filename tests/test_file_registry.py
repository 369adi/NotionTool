import tempfile
import time
import unittest
from pathlib import Path

from notiontool import file_registry, plain_protocol
from notiontool.bridge import Bridge
from notiontool.engine import Engine
from notiontool.store import Store
import tests.test_engine as base


class FileRegistryTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent / '.test-data'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.project = Path(self.temp.name) / 'project'
        (self.project / 'docs').mkdir(parents=True)
        self.data = Path(self.temp.name) / 'data'
        self.store = Store(self.data)
        self.adapter = base.FakeAdapter()
        self.engine = Engine(self.store, self.adapter)
        self.store.put('runs', dict(id='r1', chat_id='c', folder_id='', goal='Improve docs', state='executing',
                                    project_path=str(self.project)))
        for ident, lease, title in (('a1', 'one', 'Docs'), ('a2', 'two', 'Router')):
            self.store.put('tasks', dict(id=ident, run_id='r1', state='running', lease=lease, files=[], role='executor',
                                         title=title, worker_id='w-' + ident))

    def tearDown(self):
        self.engine.close()
        time.sleep(.03)
        self.store.close()
        self.temp.cleanup()

    def test_operator_edit_blocks_a_coworker_write_of_the_same_file(self):
        file_registry.claim_operator(self.store, self.project / 'notiontool' / 'engine.py')
        with self.assertRaisesRegex(ValueError, 'operator is editing'):
            self.engine.claim_file('a1', 'one', 'notiontool/engine.py')
        self.engine.claim_file('a1', 'one', 'docs/OTHER.md')
        self.assertEqual(self.store.get('tasks', 'a1')['files'], ['docs/OTHER.md'])

    def test_coworker_file_blocks_the_operator_until_the_task_ends(self):
        self.engine.claim_file('a1', 'one', 'docs/A.md')
        with self.assertRaisesRegex(ValueError, 'coworker task is editing'):
            file_registry.claim_operator(self.store, self.project / 'docs' / 'A.md')
        task = self.store.get('tasks', 'a1')
        task['state'] = 'done'
        self.store.put('tasks', task)
        file_registry.claim_operator(self.store, self.project / 'docs' / 'A.md')
        self.assertEqual(len(file_registry.operator_claims(self.store)), 1)

    def test_expired_operator_claim_no_longer_blocks_and_is_cleaned_up(self):
        old = time.time() - file_registry.TTL - 5
        file_registry.claim_operator(self.store, self.project / 'x.py', now=old)
        self.assertEqual(file_registry.operator_claims(self.store), [])
        self.engine.claim_file('a1', 'one', 'x.py')
        file_registry.claim_operator(self.store, self.project / 'y.py')
        self.assertEqual([Path(c['path']).name for c in self.store.all(file_registry.KIND)], ['y.py'])

    def test_repeated_operator_edits_log_one_claim_event(self):
        for _ in range(3):
            file_registry.claim_operator(self.store, self.project / 'z.py')
        count = self.store.db.execute("SELECT COUNT(*) FROM events WHERE kind='file_claimed'").fetchone()[0]
        self.assertEqual(count, 1)

    def test_claims_survive_a_restart(self):
        self.engine.claim_file('a1', 'one', 'docs/A.md')
        file_registry.claim_operator(self.store, self.project / 'notiontool' / 'bridge.py')
        self.engine.close()
        self.store.close()
        self.store = Store(self.data)
        names = sorted(Path(item['path']).name for item in file_registry.in_use(self.store))
        self.engine = Engine(self.store, self.adapter)
        self.assertEqual(names, ['A.md', 'bridge.py'])

    def test_summary_lists_other_chats_files_relative_to_the_project(self):
        self.assertEqual(file_registry.summary(self.store, str(self.project)), '')
        self.engine.claim_file('a1', 'one', 'docs/A.md')
        file_registry.claim_operator(self.store, self.project / 'notiontool' / 'engine.py')
        text = file_registry.summary(self.store, str(self.project), exclude_task='a2')
        self.assertIn(str(Path('docs/A.md')) + ' (Docs (a1))', text)
        self.assertIn(str(Path('notiontool/engine.py')) + ' (operator)', text)
        self.assertNotIn('A.md', file_registry.summary(self.store, str(self.project), exclude_task='a1'))

    def test_executor_and_planner_prompts_carry_the_claims(self):
        claims = '\nOther chats are editing these files right now: docs/A.md (operator).\n'
        run = dict(goal='Improve docs', project_path=str(self.project), file_claims=claims, capacity=2, allocation={})
        executor = dict(role='executor', prompt='Edit docs/B.md', acceptance='file exists', files=['docs/B.md'], title='B')
        planner = dict(role='planner', prompt='', attempts=1, title='Planner')
        reviewer = dict(role='reviewer', prompt='', acceptance='', files=[], title='Review')
        self.assertIn('docs/A.md (operator)', plain_protocol.prompt(executor, run, '', 'tok', True))
        self.assertIn('docs/A.md (operator)', plain_protocol.prompt(planner, run, '', 'tok', True))
        self.assertNotIn('docs/A.md (operator)', plain_protocol.prompt(reviewer, run, '', 'tok', True))

    def test_table_cells_lose_markdown_escapes(self):
        text = ('| ID | Title | Work to do | Depends on | Files | How to check |\n'
                '| T1 | Router model\\_reason | Edit notiontool/model\\_routing.py | none | '
                'notiontool/model\\_routing.py; docs\\\\SCREEN\\_CAPTURE.md | tests pass |')
        task = plain_protocol.table_plan(text)['tasks'][0]
        self.assertEqual(task['title'], 'Router model_reason')
        self.assertEqual(task['files'], ['notiontool/model_routing.py', 'docs\\SCREEN_CAPTURE.md'])
        self.assertIn('forward slashes', plain_protocol.TABLE)
        self.assertIn('forward slashes', plain_protocol.MODEL_TABLE)

    def test_project_shell_does_not_hold_the_whole_project_folder(self):
        result = Bridge(self.store, self.engine).call('run_shell', {'command': 'echo ok'}, 'one')
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(self.store.get('tasks', 'a1')['files'], [])
        self.engine.claim_file('a2', 'two', 'docs/A.md')
        self.assertEqual(self.store.get('tasks', 'a2')['files'], ['docs/A.md'])

    def test_operator_write_tool_registers_the_file_and_respects_coworkers(self):
        bridge = Bridge(self.store, self.engine)
        target = self.project / 'notes.txt'
        bridge.operator_call(None, 'nt_write_file', {'path': str(target), 'content': 'hello'})
        self.assertEqual(target.read_text(), 'hello')
        self.assertEqual([Path(c['path']).name for c in file_registry.operator_claims(self.store)], ['notes.txt'])
        self.engine.claim_file('a1', 'one', 'docs/A.md')
        with self.assertRaisesRegex(ValueError, 'coworker task is editing'):
            bridge.operator_call(None, 'nt_write_file', {'path': str(self.project / 'docs' / 'A.md'), 'content': 'x'})
        self.assertFalse((self.project / 'docs' / 'A.md').exists())


    def test_claimed_paths_become_project_relative_with_forward_slashes(self):
        root = self.project
        self.assertEqual(file_registry.normalize(root, 'docs\\SCREEN_CAPTURE.md'), 'docs/SCREEN_CAPTURE.md')
        self.assertEqual(file_registry.normalize(root, './docs/./SCREEN_CAPTURE.md'), 'docs/SCREEN_CAPTURE.md')
        self.assertEqual(file_registry.normalize(root, '.\\docs\\sub\\..\\SCREEN_CAPTURE.md'), 'docs/SCREEN_CAPTURE.md')
        self.assertEqual(file_registry.normalize(root, str(root / 'docs' / 'SCREEN_CAPTURE.md')), 'docs/SCREEN_CAPTURE.md')
        self.assertEqual(file_registry.normalize(root, str(root).replace('\\', '/') + '/docs/SCREEN_CAPTURE.md'),
                         'docs/SCREEN_CAPTURE.md')

    def test_mixed_case_spellings_share_one_key_and_keep_forward_slashes(self):
        stored = file_registry.normalize(self.project, 'Docs\\Screen_Capture.MD')
        self.assertEqual(stored.casefold(), 'docs/screen_capture.md')
        self.assertNotIn('\\', stored)
        self.assertEqual(file_registry.resource_keys(self.project, ['docs/SCREEN_CAPTURE.md']),
                         file_registry.resource_keys(self.project, ['DOCS\\screen_capture.md']))

    def test_two_spellings_give_one_claim(self):
        files = ['docs/SCREEN_CAPTURE.md', 'docs\\SCREEN_CAPTURE.md', './docs/screen_capture.md',
                 str(self.project / 'docs' / 'SCREEN_CAPTURE.md')]
        self.assertEqual(file_registry.normalize_files(self.project, files), ['docs/SCREEN_CAPTURE.md'])
        task = self.store.get('tasks', 'a1')
        task['files'] = files[:2]
        self.store.put('tasks', task)
        claims = [c for c in file_registry.task_claims(self.store) if c['task_id'] == 'a1']
        self.assertEqual(len(claims), 1)
        self.assertEqual(Path(claims[0]['path']), (self.project / 'docs' / 'SCREEN_CAPTURE.md').resolve())

    def test_two_spellings_conflict_across_tasks_and_with_the_operator(self):
        self.engine.claim_file('a1', 'one', 'docs/SCREEN_CAPTURE.md')
        for spelling in ('docs\\SCREEN_CAPTURE.md', './DOCS/screen_capture.md'):
            with self.subTest(spelling=spelling), self.assertRaisesRegex(ValueError, 'Another active task owns this file'):
                self.engine.claim_file('a2', 'two', spelling)
        self.assertEqual(self.store.get('tasks', 'a2')['files'], [])
        with self.assertRaisesRegex(ValueError, 'coworker task is editing'):
            file_registry.claim_operator(self.store, str(self.project).replace('\\', '/') + '/DOCS/Screen_Capture.md')
        key = file_registry.resource_keys(self.project, ['docs\\SCREEN_CAPTURE.md'])[0]
        others = file_registry.task_claims(self.store, exclude_task='a2')
        self.assertTrue(any(file_registry.overlap(key, claim['key']) for claim in others))

    def test_paths_outside_the_project_are_rejected(self):
        outside = Path(self.temp.name) / 'other.md'
        sibling = Path(self.temp.name) / 'project2' / 'x.md'
        for path in ('../other.md', '..\\other.md', 'docs/../../other.md', str(outside),
                     str(outside).replace('\\', '/'), str(sibling), '', '.', 'docs/..'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                file_registry.normalize(self.project, path)
        with self.assertRaises(ValueError):
            file_registry.normalize_files(self.project, ['docs/A.md', '../other.md'])


    def test_summary_skips_scratch_and_unrelated_files(self):
        scratch = self.project / 'work' / '_helper.py'
        scratch.parent.mkdir(parents=True, exist_ok=True)
        scratch.write_text('x', encoding='utf-8')
        other = self.project / 'notiontool' / 'engine.py'
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_text('x', encoding='utf-8')
        file_registry.claim_operator(self.store, scratch)
        file_registry.claim_operator(self.store, other)
        plain = file_registry.summary(self.store, str(self.project))
        self.assertNotIn('_helper.py', plain)
        self.assertIn('engine.py', plain)
        unrelated = dict(id='x1', files=['docs/Z.md'])
        self.assertEqual(file_registry.summary(self.store, str(self.project), task=unrelated), '')
        mine = dict(id='x2', files=['notiontool/engine.py'])
        self.assertIn('engine.py', file_registry.summary(self.store, str(self.project), task=mine))
        copy = dict(mine, patch_worktree=str(self.project / 'wt'))
        note = file_registry.summary(self.store, str(self.project), task=copy)
        self.assertIn('own copy', note)
        self.assertNotIn('do not write', note)

if __name__ == '__main__':
    unittest.main()
