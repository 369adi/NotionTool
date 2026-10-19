"""Merge gate baseline: a red main never fails a task (main_red + retry), and /api/task/remerge.

PatchMode part uses a real git in a temporary folder; the engine part uses a fake patcher.
"""
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from notiontool import engine as engine_mod
from notiontool import patch_mode
from notiontool.engine import Engine
from notiontool.store import Store
import tests.test_engine as base
from tests.test_engine_patch_mode import FakePatcher
from tests.test_patch_mode import git, write, remove_tree

ROOT = Path(__file__).resolve().parent.parent
GIT = shutil.which('git')

GREEN = ('import unittest\n\nfrom demoapp import calc\n\n\nclass CalcTest(unittest.TestCase):\n'
         '    def test_add(self):\n        self.assertEqual(calc.add(2, 3), 5)\n')
RED = GREEN.replace('5)', '6)')


@unittest.skipUnless(GIT, 'git is not installed')
class BaselineMergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='nt-base-'))
        self.root = self.tmp / 'repo'
        self.root.mkdir()
        git(self.root, 'init', '-q', '-b', 'main')
        for key, value in (('user.name', 'T'), ('user.email', 't@example.test'), ('core.autocrlf', 'false'),
                           ('commit.gpgsign', 'false')):
            git(self.root, 'config', key, value)
        write(self.root / '.gitignore', 'work/\ndata/\n__pycache__/\n')
        write(self.root / 'demoapp' / '__init__.py', '')
        write(self.root / 'demoapp' / 'calc.py', 'def add(a, b):\n    return a + b\n')
        write(self.root / 'demoapp' / 'text.py', 'GREETING = "hello"\n')
        write(self.root / 'tests' / '__init__.py', '')
        self.pm = patch_mode.PatchMode(self.root, test_timeout=120)

    def tearDown(self):
        self.pm.close()
        remove_tree(self.tmp)

    def commit(self, test_text):
        write(self.root / 'tests' / 'test_desktop_calc.py', test_text)
        git(self.root, 'add', '-A')
        git(self.root, 'commit', '-q', '-m', 'state')
        return git(self.root, 'rev-parse', 'main')

    def task_with_edit(self, tid, rel, text):
        task = dict(id=tid, step_id=tid, goal='Change ' + rel, files=[rel])
        path = Path(self.pm.prepare(task))
        write(path / rel, text)
        return task

    def test_main_red_is_not_blamed_on_the_task_and_is_cached(self):
        main = self.commit(RED)  # main itself is red, like 49abeb2
        task = self.task_with_edit('t1', 'demoapp/text.py', 'GREETING = "hi"\n')
        result = self.pm.merge(task)
        self.assertEqual(result['status'], 'main_red')
        self.assertEqual(result['main_red_commit'], main)
        self.assertIn('test_add', result['message'])
        self.assertIn('tests.test_desktop_calc.CalcTest.test_add', result['failing'])
        self.assertEqual(git(self.root, 'rev-parse', 'main'), main)  # main not changed
        self.assertFalse((self.root / 'work' / 'worktrees' / ('_main-' + main[:12])).exists())  # baseline copy removed
        tail = patch_mode.test_tail(result)
        self.assertTrue(0 < len(tail) <= patch_mode.TAIL_LIMIT)
        self.assertIn('[main ', tail)
        cached = dict(self.pm._baseline)
        self.assertEqual(len(cached), 1)
        self.assertEqual(self.pm.merge(task)['status'], 'main_red')
        self.assertEqual(self.pm._baseline, cached)  # same main commit: no second baseline run

    def test_only_branch_red_stays_red(self):
        main = self.commit(GREEN)
        task = self.task_with_edit('t2', 'demoapp/calc.py', 'def add(a, b):\n    return a - b\n')
        result = self.pm.merge(task)
        self.assertEqual(result['status'], 'red')
        self.assertTrue(result['message'].startswith('Tests are red; main was not changed'))
        self.assertIn('test_add', result['message'])
        self.assertFalse(result['main_tests']['red'])
        self.assertEqual(git(self.root, 'rev-parse', 'main'), main)

    def test_new_task_test_module_missing_on_main_counts_as_green_there(self):
        self.commit(GREEN)
        argv = ['python', '-m', 'unittest', 'tests.test_new_thing']
        self.assertIsNone(self.pm._on_main(argv, self.root))
        argv = ['python', '-m', 'unittest', 'discover', '-s', 'tests']
        self.assertEqual(self.pm._on_main(argv, self.root), argv)


class EngineMainRedTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent / '.test-data'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.project = Path(self.temp.name) / 'project'
        self.project.mkdir()
        self.store = Store(Path(self.temp.name) / 'data')
        self.engine = Engine(self.store, base.FakeAdapter())
        self.fake = FakePatcher(self.project)
        self.fake.head = 'aaa'
        self.fake.rev = lambda ref: self.fake.head
        self.engine.patch_factory = lambda root: self.fake
        self.store.put('runs', dict(id='r1', chat_id='c', folder_id='', goal='Work', state='executing',
                                    project_path=str(self.project)))

    def tearDown(self):
        self.engine.close()
        time.sleep(.03)
        self.store.close()
        self.temp.cleanup()

    def put(self, ident, worktree=True, **extra):
        folder = self.project / 'work' / 'worktrees' / ident
        if worktree:
            folder.mkdir(parents=True)
            (folder / '.git').write_text('gitdir: x\n', encoding='utf-8')
        task = dict(id=ident, run_id='r1', state='done', role='executor', title=ident, files=[],
                    patch_worktree=str(folder), patch_branch='task/' + ident)
        task.update(extra)
        self.store.put('tasks', task)
        return task

    def kinds(self):
        return [row[0] for row in self.store.db.execute('SELECT kind FROM events').fetchall()]

    def main_red(self):
        cmd = dict(name='desktop suite', ok=False, tail='x' * 5000)
        return dict(status='main_red', message='main itself is red at aaa (tests.t.C.test_a)', main_red_commit='aaa',
                    failing=['tests.t.C.test_a'], tests=dict(commands=[cmd]),
                    main_tests=dict(commit='aaa', red=True, commands=[cmd]))

    def wait_state(self, ident, want):
        for _ in range(200):
            if self.store.get('tasks', ident).get('patch_state') == want:
                return
            time.sleep(.02)
        self.fail('patch_state stayed %r' % self.store.get('tasks', ident).get('patch_state'))

    def test_main_red_waits_without_using_tries_and_retries_when_main_moves(self):
        self.assertIn('main_red', engine_mod.PATCH_RETRY)
        self.put('t1', patch_state='waiting')
        for _ in range(engine_mod.PATCH_RETRY_LIMIT + 2):
            self.engine._patch_result('t1', self.main_red())
        task = self.store.get('tasks', 't1')
        self.assertEqual((task['patch_state'], task.get('patch_tries', 0)), ('waiting', 0))
        self.assertEqual(task['patch_main_red'], 'aaa')
        self.assertEqual(task['patch_result']['failing'], ['tests.t.C.test_a'])
        self.assertTrue(0 < len(task['patch_result']['tests']) <= 1500)
        self.assertEqual(self.kinds().count('main_red'), 1)  # one operator event per red main commit
        self.engine._start_merges()
        self.assertEqual(self.fake.submitted, [])  # main still red: no merge
        self.fake.head = 'bbb'  # the fix landed
        self.engine._start_merges()
        self.wait_state('t1', 'merged')
        self.assertEqual(self.fake.submitted, ['t1'])
        self.assertNotIn('patch_main_red', self.store.get('tasks', 't1'))

    def test_branch_red_still_fails_with_tail(self):
        self.put('t1', patch_state='waiting')
        cmd = dict(name='task tests', ok=False, tail='FAIL: test_x (tests.test_a.A.test_x)')
        self.engine._patch_result('t1', dict(status='red', message='Tests are red', tests=dict(commands=[cmd])))
        task = self.store.get('tasks', 't1')
        self.assertEqual(task['patch_state'], 'failed')
        self.assertIn('test_x', task['patch_result']['tests'])
        self.assertNotIn('main_red', self.kinds())

    def failed(self, ident, status='red', at=None, **extra):
        return self.put(ident, patch_state='failed', patch_tries=1,
                        patch_result=dict(status=status, message='m', at=at or time.time()), **extra)

    def test_remerge_single(self):
        self.failed('t1', status='main_red')
        out = self.engine.remerge(task_id='t1')
        self.assertEqual((out['tasks'], out['count']), (['t1'], 1))
        task = self.store.get('tasks', 't1')
        self.assertEqual((task['patch_state'], task['patch_tries']), ('waiting', 0))
        self.assertIn('patch_remerge', self.kinds())

    def test_remerge_since(self):
        now = time.time()
        self.failed('old', at=now - 7200)
        self.failed('new1', at=now - 60)
        self.failed('new2', status='main_red', at=now - 30)
        self.failed('gone', at=now - 30, worktree=False)
        self.failed('secret', status='secret_found', at=now - 30)
        out = self.engine.remerge(since=now - 3600)
        self.assertEqual(sorted(out['tasks']), ['new1', 'new2'])
        self.assertEqual([s['id'] for s in out['skipped']], ['gone'])
        self.assertEqual(self.store.get('tasks', 'old')['patch_state'], 'failed')
        self.assertEqual(self.store.get('tasks', 'gone')['patch_state'], 'failed')
        self.assertEqual(self.store.get('tasks', 'secret')['patch_state'], 'failed')

    def test_remerge_refuses_without_worktree_or_wrong_state(self):
        self.failed('t1', worktree=False)
        with self.assertRaisesRegex(ValueError, 'no worktree'):
            self.engine.remerge(task_id='t1')
        self.assertEqual(self.store.get('tasks', 't1')['patch_state'], 'failed')
        self.failed('t2', status='conflict')
        with self.assertRaises(ValueError):
            self.engine.remerge(task_id='t2')
        self.failed('t3', state='running')
        with self.assertRaises(ValueError):
            self.engine.remerge(task_id='t3')
        with self.assertRaises(ValueError):
            self.engine.remerge()

    def test_server_route_keeps_crlf(self):
        data = (ROOT / 'notiontool' / 'server.py').read_bytes()
        self.assertIn(b"path=='/api/task/remerge'", data)
        self.assertIn(b'self.engine.remerge(since=float(body', data)
        self.assertEqual(data.count(b'\n'), data.count(b'\r\n'))
        for name in ('engine.py', 'patch_mode.py'):
            self.assertNotIn(b'\r\n', (ROOT / 'notiontool' / name).read_bytes())


if __name__ == '__main__':
    unittest.main()
