import shutil
import subprocess
import tempfile
import time
import unittest
from concurrent.futures import Future
from pathlib import Path

from notiontool import engine as engine_mod
from notiontool.engine import Engine
from notiontool.store import Store
import tests.test_engine as base

ROOT = Path(__file__).resolve().parent.parent


class FakePatcher:
    """Stands in for patch_mode.PatchMode: no git, no tests, no merge."""
    def __init__(self, root):
        self.root = Path(root)
        self.fail = False
        self.result = dict(status='merged', message='Merged.')
        self.submitted = []

    def branch_name(self, task):
        return task.get('patch_branch') or 'task/' + task['id']

    def prepare(self, task):
        if self.fail:
            raise RuntimeError('git worktree add failed')
        return task.get('patch_worktree') or str(self.root / 'work' / 'worktrees' / task['id'])

    def submit(self, task):
        self.submitted.append(task['id'])
        future = Future()
        future.set_result(dict(self.result))
        return future

    def close(self):
        pass


def git(cwd, *args):
    subprocess.run(['git', '-C', str(cwd)] + list(args), check=True, capture_output=True, timeout=60)


class EnginePatchModeTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent / '.test-data'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.project = Path(self.temp.name) / 'project'
        (self.project / 'tests').mkdir(parents=True)
        (self.project / 'docs').mkdir()
        self.store = Store(Path(self.temp.name) / 'data')
        self.engine = Engine(self.store, base.FakeAdapter())
        self.fake = FakePatcher(self.project)
        self.engine.patch_factory = lambda root: self.fake
        self.store.put('runs', dict(id='r1', chat_id='c', folder_id='', goal='Work', state='executing',
                                    project_path=str(self.project)))
        self.run = self.store.get('runs', 'r1')

    def tearDown(self):
        self.engine.close()
        time.sleep(.03)
        self.store.close()
        self.temp.cleanup()

    def make_git(self):
        git(self.project, 'init', '-q', '-b', 'main')
        git(self.project, 'config', 'user.email', 'test@example.invalid')
        git(self.project, 'config', 'user.name', 'Test')
        (self.project / 'a.py').write_text('x = 1\n', encoding='utf-8')
        git(self.project, 'add', 'a.py')
        git(self.project, 'commit', '-q', '-m', 'start')

    def put(self, ident, files, state='running'):
        task = dict(id=ident, run_id='r1', state=state, lease='l-' + ident, files=list(files), role='executor',
                    title=ident, worker_id='w-' + ident, prompt='Do ' + ident)
        self.store.put('tasks', task)
        return task

    def set_patch(self, value):
        settings = dict(self.store.get('settings', 'app', {}) or {}, id='app', patch_mode=value)
        self.store.put('settings', settings)

    # ---- locks when patch mode is off ----------------------------------------
    def test_bare_folder_in_files_never_locks_everything(self):
        self.put('t1', ['tests/'])
        other = self.put('t2', ['tests/test_x.py'], state='queued')
        self.assertFalse(self.engine._conflicts(other, self.run))
        self.put('t3', ['docs'])  # an existing folder without a slash is advisory too
        self.assertFalse(self.engine._conflicts(self.put('t4', ['docs/A.md'], state='queued'), self.run))

    def test_real_file_overlap_still_locks_when_off(self):
        self.assertFalse(self.engine._patch_on(self.run))  # not a git project
        self.put('t1', ['docs/A.md'])
        self.assertTrue(self.engine._conflicts(self.put('t2', ['docs/A.md'], state='queued'), self.run))

    # ---- patch mode on --------------------------------------------------------
    def test_patch_mode_is_on_by_default_for_a_git_project_and_skips_locks(self):
        self.make_git()
        self.assertTrue(engine_mod.PATCH_MODE_DEFAULT)
        self.assertTrue(self.engine._patch_on(self.run))
        self.put('t1', ['a.py'])
        self.assertFalse(self.engine._conflicts(self.put('t2', ['a.py'], state='queued'), self.run))
        self.set_patch(False)
        self.assertFalse(self.engine._patch_on(self.run))
        self.assertTrue(self.engine._conflicts(self.store.get('tasks', 't2'), self.run))

    def test_absolute_resources_still_lock_in_patch_mode(self):
        self.make_git()
        first = self.put('t1', [])
        first['absolute_resources'] = ['c:/shared/x.txt']
        self.store.put('tasks', first)
        second = self.put('t2', [], state='queued')
        second['absolute_resources'] = ['c:/shared/x.txt']
        self.assertTrue(self.engine._conflicts(second, self.run))

    def test_prepare_sets_the_worktree_and_the_prompt_note(self):
        task = self.put('t1', ['a.py'], state='queued')
        self.engine._patch_prepare(task, self.run)
        self.assertEqual(task['patch_state'], 'working')
        self.assertEqual(task['patch_branch'], 'task/t1')
        self.assertTrue(task['patch_worktree'].endswith('t1'))
        note = engine_mod.PATCH_NOTE % (task['patch_worktree'], task['patch_branch'])
        self.assertIn('Work only inside this folder: ' + task['patch_worktree'], note)
        self.assertIn('Do not commit', note)

    def test_prepare_failure_falls_back_without_patch_fields(self):
        self.fake.fail = True
        task = self.put('t1', ['a.py'], state='queued')
        self.engine._patch_prepare(task, self.run)
        self.assertNotIn('patch_worktree', task)
        kinds = [row[0] for row in self.store.db.execute('SELECT kind FROM events').fetchall()]
        self.assertIn('patch_prepare_failed', kinds)

    def wait_state(self, ident, want):
        for _ in range(200):
            if self.store.get('tasks', ident).get('patch_state') == want:
                return
            time.sleep(.02)
        self.fail('patch_state stayed %r' % self.store.get('tasks', ident).get('patch_state'))

    def test_waiting_task_is_merged_once(self):
        task = self.put('t1', ['a.py'], state='done')
        task.update(patch_worktree=str(self.project / 'work' / 'worktrees' / 't1'), patch_branch='task/t1', patch_state='waiting')
        self.store.put('tasks', task)
        self.engine._start_merges()
        self.engine._start_merges()
        self.wait_state('t1', 'merged')
        self.assertEqual(self.fake.submitted, ['t1'])
        status = self.engine.patch_status()
        self.assertEqual(status['merges_today'], 1)
        self.assertEqual(status['labels']['t1'], 'Merged')
        self.assertEqual(self.store.get('tasks', 't1')['state'], 'done')  # I5: the task itself is never re-run

    def test_conflict_creates_one_follow_up_in_the_same_worktree(self):
        task = self.put('t1', ['a.py'], state='done')
        task.update(patch_worktree='W', patch_branch='task/t1', patch_state='waiting')
        self.store.put('tasks', task)
        result = dict(status='conflict', message='Conflict in a.py', follow_up=dict(title='Resolve conflict', prompt='Fix a.py', files=['a.py']))
        self.engine._patch_result('t1', result)
        self.engine._patch_result('t1', result)
        origin = self.store.get('tasks', 't1')
        self.assertEqual(origin['patch_state'], 'conflict')
        follow = [t for t in self.store.all('tasks') if t.get('patch_origin') == 't1']
        self.assertEqual(len(follow), 1)
        self.assertEqual((follow[0]['patch_worktree'], follow[0]['patch_branch']), ('W', 'task/t1'))
        status = self.engine.patch_status()
        self.assertEqual(status['conflicts_waiting'], 1)
        self.assertEqual(status['labels']['t1'], 'Conflict - being resolved')
        self.engine._patch_result(follow[0]['id'], dict(status='merged'))
        self.assertEqual(self.store.get('tasks', 't1')['patch_state'], 'merged')
        self.assertEqual(self.engine.patch_status()['conflicts_waiting'], 0)

    def test_red_tests_do_not_merge_and_main_moved_retries(self):
        self.put('t1', ['a.py'], state='done')
        self.engine._patch_result('t1', dict(status='main_moved'))
        self.assertEqual(self.store.get('tasks', 't1')['patch_state'], 'waiting')
        self.assertEqual(self.engine.patch_status()['labels']['t1'], 'Waiting to merge')
        self.engine._patch_result('t1', dict(status='red', message='2 tests failed'))
        self.assertEqual(self.store.get('tasks', 't1')['patch_state'], 'failed')

    # ---- wiring --------------------------------------------------------------
    def test_engine_hooks_are_wired(self):
        text = (ROOT / 'notiontool' / 'engine.py').read_text(encoding='utf-8')
        self.assertIn('self._patch_prepare(task, run)', text)
        self.assertIn('prompt_task = dict(task, prompt=', text)
        self.assertIn("task['patch_state'] = 'waiting'", text)
        self.assertIn('self._start_merges()', text)
        self.assertNotIn(b'\r\n', (ROOT / 'notiontool' / 'engine.py').read_bytes())

    def test_server_route_and_setting_keep_crlf(self):
        data = (ROOT / 'notiontool' / 'server.py').read_bytes()
        self.assertIn(b"path=='/api/patch'", data)
        self.assertIn(b"settings['patch_mode']", data)
        self.assertEqual(data.count(b'\n'), data.count(b'\r\n'))

    def test_settings_js_switch_and_status_text(self):
        js = ROOT / 'notiontool' / 'web' / 'desktop' / 'settings.js'
        self.assertIn('Work in parallel copies (recommended)', js.read_text(encoding='utf-8'))
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        script = ("global.window={};global.document={};require(%s);var L=window.__ntDesk.settingsLogic;"
                  "console.log(L.patchText({merges_today:3,conflicts_waiting:1,waiting_to_merge:2}));console.log(L.patchText(null));") % repr(str(js))
        out = subprocess.run([node, '-e', script], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.splitlines(), ['Merges today: 3 - Conflicts waiting: 1 - Waiting to merge: 2', 'Status: not available yet'])


if __name__ == '__main__':
    unittest.main()
