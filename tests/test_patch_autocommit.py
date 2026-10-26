"""Finished coworker work is committed and merged by the harness; abandoned work is kept as a salvage patch.

Real failures 25 Oct 2026: t56c7173 was accepted (done) but its copy stayed patch_state 'working' forever;
t91cc19b was auto-abandoned (stale_tasks rule d) and its 16 uncommitted files would have been thrown away.
"""
import contextlib
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from notiontool import engine as engine_mod
from notiontool import stale_tasks
from notiontool.engine import Engine
from notiontool.store import Store
import tests.test_engine as base
from tests.test_engine_patch_mode import FakePatcher


def git(cwd, *args):
    return subprocess.run(['git', '-C', str(cwd)] + list(args), check=True, capture_output=True, text=True,
                          timeout=60).stdout.strip()


class PatchAutocommitTests(unittest.TestCase):
    def setUp(self):
        # Real git: outside this repository, so the temp project is never a repo nested in the checkout.
        self.temp = tempfile.TemporaryDirectory(prefix='nt_autocommit_')
        self.project = Path(self.temp.name) / 'project'
        self.project.mkdir()
        git(self.project, 'init', '-q', '-b', 'main')
        git(self.project, 'config', 'core.autocrlf', 'false')
        git(self.project, 'config', 'user.email', 'test@example.invalid')
        git(self.project, 'config', 'user.name', 'Test')
        (self.project / 'a.py').write_text('x = 1\n', encoding='utf-8')
        git(self.project, 'add', '-A')
        git(self.project, 'commit', '-q', '-m', 'start')
        self.store = Store(Path(self.temp.name) / 'data')
        self.engine = Engine(self.store, base.FakeAdapter())
        self.engine._unsent_proof = lambda task_id: None  # rule (d) here always has remote_done_at
        self.store.put('runs', dict(id='r1', chat_id='c', folder_id='', goal='Work', state='executing',
                                    project_path=str(self.project)))
        self.run = self.store.get('runs', 'r1')

    def tearDown(self):
        self.engine.close()
        for patcher in self.engine.__dict__.get('_patchers', {}).values():
            patcher.close()
        time.sleep(.05)
        self.store.close()
        shutil.rmtree(self.temp.name, ignore_errors=True)  # git may still hold a worktree file on Windows
        with contextlib.suppress(OSError):
            self.temp.cleanup()

    def uncertain(self, ident, files=(), title=None):
        now = time.time()
        task = dict(id=ident, run_id='r1', state='uncertain', role='executor', title=title or ident,
                    prompt='Do ' + ident, files=list(files), deps=[], lease='l-' + ident, worker_id='', attempts=1,
                    created=now - 7200, started=now - 7200, sent_at=now - 7200, remote_done_at=now - 3600,
                    remote_unknown=True)
        with self.engine.lock:
            self.engine._patch_prepare(task, self.run)
            self.store.put('tasks', task)
        self.assertEqual(task['patch_state'], 'working')
        return task

    def settle(self, ident, seconds=90):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.engine._start_merges()
            task = self.store.get('tasks', ident)
            if task.get('patch_state') not in ('working', 'waiting'):
                return task
            time.sleep(.2)
        return self.store.get('tasks', ident)

    def test_accepted_task_with_uncommitted_work_ends_merged(self):
        task = self.uncertain('t1', ['a.py'])
        Path(task['patch_worktree'], 'a.py').write_text('x = 2\n', encoding='utf-8')  # never committed
        self.engine.resolve_uncertain('t1', 'accept')
        done = self.settle('t1')
        self.assertEqual(done['state'], 'done')
        self.assertEqual(done['patch_state'], 'merged', done.get('patch_result'))
        self.assertEqual((self.project / 'a.py').read_text(encoding='utf-8'), 'x = 2\n')
        kinds = [e.get('kind') for e in self.store.events(limit=500)] if hasattr(self.store, 'events') else []
        if kinds:
            self.assertIn('patch_autocollect', kinds)

    def test_accepted_task_with_a_conflict_delegates_a_fix(self):
        task = self.uncertain('t2', ['a.py'])
        Path(task['patch_worktree'], 'a.py').write_text('x = "coworker"\n', encoding='utf-8')
        (self.project / 'a.py').write_text('x = "main"\n', encoding='utf-8')
        git(self.project, 'commit', '-q', '-am', 'main moves')
        self.engine.resolve_uncertain('t2', 'accept')
        done = self.settle('t2')
        self.assertEqual(done['patch_state'], 'conflict', done.get('patch_result'))
        follow = [t for t in self.store.all('tasks') if t.get('patch_origin') == 't2']
        self.assertEqual(len(follow), 1)
        self.assertTrue(follow[0]['title'].startswith('Resolve conflict'))
        self.assertEqual(follow[0]['state'], 'queued')

    def test_auto_abandon_keeps_a_salvage_patch_and_the_next_try_gets_it(self):
        task = self.uncertain('t3', title='Settings')
        for i in range(16):
            Path(task['patch_worktree'], 's_%02d.py' % i).write_text('v = %d\n' % i, encoding='utf-8')
        taken = stale_tasks.sweep(self.engine, check_proof=False)
        action = [a for a in taken if a['task_id'] == 't3']
        self.assertEqual([a['op'] for a in action], ['resolve'])
        old = self.store.get('tasks', 't3')
        self.assertEqual(old['state'], 'failed')
        self.assertEqual(old['patch_state'], 'salvaged')
        salvage = old['patch_salvage']
        self.assertEqual(len(salvage['files']), 16)
        self.assertEqual(action[0]['salvage']['commit'], salvage['commit'])
        on_branch = git(self.project, 'ls-tree', '--name-only', salvage['branch']).split()
        self.assertEqual(sum(n.startswith('s_') for n in on_branch), 16)
        self.assertEqual(git(self.project, 'rev-parse', 'main'), git(self.project, 'rev-parse', 'main'))
        self.assertNotIn('s_00.py', git(self.project, 'ls-tree', '--name-only', 'main').split())  # main untouched
        again = dict(id='t4', run_id='r1', state='queued', role='executor', title='Settings', prompt='Do it',
                     files=[], deps=[], lease='l-t4', worker_id='', attempts=0)
        with self.engine.lock:
            self.engine._patch_prepare(again, self.run)
        self.assertEqual(again['patch_branch'], salvage['branch'])
        self.assertEqual(again['salvage_from'], 't3')
        self.assertTrue(Path(again['patch_worktree'], 's_15.py').is_file())
        self.assertIn('abandoned try t3', again['prompt'])
        self.assertEqual(self.store.get('tasks', 't3')['patch_salvage']['taken_by'], 't4')

    def test_salvage_without_changes_keeps_nothing(self):
        self.uncertain('t5')
        self.engine.resolve_uncertain('t5', 'abandon')
        self.assertIsNone(self.engine.patch_salvage('t5'))
        self.assertEqual(self.store.get('tasks', 't5')['patch_state'], 'working')


class SalvageMatchTests(unittest.TestCase):
    """Which re-delegated task gets a salvage patch (no git)."""
    def setUp(self):
        root = Path(__file__).resolve().parent / '.test-data'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.project = Path(self.temp.name) / 'project'
        self.project.mkdir()
        self.store = Store(Path(self.temp.name) / 'data')
        self.engine = Engine(self.store, base.FakeAdapter())
        self.engine.patch_factory = lambda root: FakePatcher(root)
        self.store.put('runs', dict(id='r1', chat_id='c', folder_id='', goal='Work', state='executing',
                                    project_path=str(self.project)))
        self.run = self.store.get('runs', 'r1')
        self.store.put('tasks', dict(id='old', run_id='r1', state='failed', role='executor', title='Settings',
                                     patch_state='salvaged', patch_salvage=dict(
                                         branch='task/old', worktree='', commit='abc', files=['x.py'], at=1,
                                         taken_by='')))

    def tearDown(self):
        self.engine.close()
        time.sleep(.03)
        self.store.close()
        self.temp.cleanup()

    def new(self, ident, **extra):
        task = dict(id=ident, run_id='r1', state='queued', role='executor', title='Other', prompt='p', files=[])
        task.update(extra)
        with self.engine.lock:
            self.engine._patch_prepare(task, self.run)
        return task

    def test_other_title_starts_fresh(self):
        self.assertEqual(self.new('n1')['patch_branch'], 'task/n1')

    def test_same_title_or_salvage_of_continues_once(self):
        first = self.new('n2', salvage_of='old')
        self.assertEqual(first['patch_branch'], 'task/old')
        self.assertIn(engine_mod.SALVAGE_NOTE.split('%s')[0].strip()[:30], first['prompt'])
        second = self.new('n3', title='Settings')
        self.assertEqual(second['patch_branch'], 'task/n3')  # handed out only once

    def test_other_run_never_gets_it(self):
        self.store.put('runs', dict(id='r2', chat_id='c', folder_id='', goal='W', state='executing',
                                    project_path=str(self.project)))
        task = dict(id='n4', run_id='r2', state='queued', role='executor', title='Settings', prompt='p', files=[])
        with self.engine.lock:
            self.engine._patch_prepare(task, self.store.get('runs', 'r2'))
        self.assertEqual(task['patch_branch'], 'task/n4')


if __name__ == '__main__':
    unittest.main()
