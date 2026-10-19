"""Patch mode with real git in a temporary folder: worktrees, commit, tests, merge queue, conflicts, push.

No Notion, no harness: the coworker is simulated by editing files in its worktree.
"""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from notiontool import patch_mode

GIT = shutil.which('git')

CALC = 'def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n'
CALC_TEST = ('import unittest\n\nfrom demoapp import calc\n\n\nclass CalcTest(unittest.TestCase):\n'
             '    def test_add(self):\n        self.assertEqual(calc.add(2, 3), 5)\n\n'
             '    def test_mul(self):\n        self.assertEqual(calc.mul(2, 3), 6)\n')


def git(cwd, *args):
    proc = subprocess.run(['git', '-c', 'core.autocrlf=false'] + list(args), cwd=str(cwd), capture_output=True,
                          text=True, encoding='utf-8', errors='replace')
    if proc.returncode != 0:
        raise AssertionError('git %s failed: %s' % (' '.join(args), proc.stderr or proc.stdout))
    return proc.stdout.strip()


def write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode('utf-8'))


def _force_remove(func, path, *_):
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def remove_tree(path):
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_force_remove)
    else:
        shutil.rmtree(path, onerror=_force_remove)


@unittest.skipUnless(GIT, 'git is not installed; patch mode needs a real git')
class PatchModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='nt-patch-'))
        self.root = self.tmp / 'repo'
        self.root.mkdir()
        git(self.root, 'init', '-q', '-b', 'main')
        for key, value in (('user.name', 'Patch Test'), ('user.email', 'patch@example.test'),
                           ('core.autocrlf', 'false'), ('commit.gpgsign', 'false')):
            git(self.root, 'config', key, value)
        write(self.root / '.gitignore', 'work/\ndata/\n__pycache__/\n*.db\n*.log\n')
        write(self.root / 'demoapp' / '__init__.py', '')
        write(self.root / 'demoapp' / 'calc.py', CALC)
        write(self.root / 'demoapp' / 'text.py', 'GREETING = "hello"\n')
        write(self.root / 'tests' / '__init__.py', '')
        write(self.root / 'tests' / 'test_desktop_calc.py', CALC_TEST)
        git(self.root, 'add', '-A')
        git(self.root, 'commit', '-q', '-m', 'init')
        self.created = []
        self.pm = patch_mode.PatchMode(self.root, test_timeout=120, create_task=self._create)

    def _create(self, follow):
        self.created.append(follow)
        return dict(follow, id='t-resolve-%d' % len(self.created))

    def tearDown(self):
        self.pm.close()
        remove_tree(self.tmp)

    def task(self, tid, step, goal, files=()):
        return dict(id=tid, step_id=step, goal=goal, files=list(files))

    def main_sha(self):
        return git(self.root, 'rev-parse', 'main')

    def edit(self, task, rel, old, new):
        path = Path(self.pm.worktree_path(task)) / rel
        text = path.read_text(encoding='utf-8')
        self.assertIn(old, text)
        write(path, text.replace(old, new))

    def add_origin(self):
        origin = self.tmp / 'origin.git'
        git(self.tmp, 'init', '-q', '--bare', '-b', 'main', str(origin))
        git(self.root, 'remote', 'add', 'origin', str(origin))
        git(self.root, 'push', '-q', 'origin', 'main')
        return origin

    # ---- prepare / collect / verify ------------------------------------------
    def test_prepare_makes_a_worktree_from_main_head(self):
        t = self.task('t1', 's1', 'Say hi')
        path = self.pm.prepare(t)
        self.assertEqual(Path(path).resolve(), (self.root / 'work' / 'worktrees' / 't1').resolve())
        self.assertEqual(git(path, 'rev-parse', '--abbrev-ref', 'HEAD'), 'task/s1')
        self.assertEqual(git(path, 'rev-parse', 'HEAD'), self.main_sha())
        self.assertEqual(self.pm.prepare(t), path)
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')

    def test_collect_commits_repo_files_only_and_counts_lines(self):
        t = self.task('t1', 's1', 'Say hi')
        wt = Path(self.pm.prepare(t))
        self.edit(t, 'demoapp/text.py', '"hello"', '"hi"')
        write(wt / 'demoapp' / 'extra.py', 'A = 1\nB = 2\n')
        write(wt / 'data' / 'notion.db', 'db')
        write(wt / 'work' / 'out.txt', 'output')
        write(wt / 'run.log', 'log')
        write(wt / 'notes' / 'session_token.txt', 'not in .gitignore here, but the name looks like a secret')
        stats = self.pm.collect(t)
        self.assertTrue(stats['commit'])
        self.assertEqual(sorted(f['path'] for f in stats['files']), ['demoapp/extra.py', 'demoapp/text.py'])
        self.assertEqual((stats['files_changed'], stats['added'], stats['deleted']), (2, 3, 1))
        self.assertEqual(stats['skipped'], ['notes/session_token.txt'])
        for name in git(self.root, 'ls-tree', '-r', '--name-only', 'task/s1').splitlines():
            self.assertNotRegex(name, r'^(data|work)/|\.(db|log)$|token')
        again = self.pm.collect(t)
        self.assertIsNone(again['commit'])
        self.assertEqual(again['added'], 3)

    def test_verify_green_then_red(self):
        t = self.task('t1', 's1', 'Swap add')
        self.pm.prepare(t)
        self.edit(t, 'demoapp/calc.py', 'return a + b', 'return b + a')
        green = self.pm.verify(t)
        self.assertTrue(green['green'], green)
        self.assertEqual(green['result'], 'green')
        self.edit(t, 'demoapp/calc.py', 'return a * b', 'return a * b + 1')
        red = self.pm.verify(t)
        self.assertFalse(red['green'])
        self.assertEqual(red['result'], 'red')
        self.assertEqual(red['commands'][-1]['name'], 'desktop suite')

    def test_verify_runs_the_task_test_files(self):
        t = self.task('t1', 's1', 'Add a check', files=['tests/test_extra.py'])
        wt = Path(self.pm.prepare(t))
        write(wt / 'tests' / 'test_extra.py', 'import unittest\n\n\nclass Extra(unittest.TestCase):\n'
              '    def test_fails(self):\n        self.fail("task test is red")\n')
        result = self.pm.verify(t)
        self.assertFalse(result['green'])
        self.assertEqual(result['commands'][0]['name'], 'task tests')
        self.assertIn('task test is red', result['commands'][0]['tail'])

    def test_verify_times_out_as_red(self):
        pm = patch_mode.PatchMode(self.root, test_timeout=2)
        t = self.task('t1', 's1', 'Slow', files=['tests/test_slow.py'])
        wt = Path(pm.prepare(t))
        write(wt / 'tests' / 'test_slow.py', 'import time\nimport unittest\n\n\nclass Slow(unittest.TestCase):\n'
              '    def test_slow(self):\n        time.sleep(20)\n')
        result = pm.verify(t)
        self.assertFalse(result['green'])
        self.assertTrue(result['commands'][0]['timed_out'])
        self.assertLess(result['seconds'], 15)

    # ---- merge ---------------------------------------------------------------
    def test_clean_merge_fast_forwards_main_and_removes_the_worktree(self):
        t1 = self.task('t1', 's1', 'Friendlier greeting')
        t2 = self.task('t2', 's2', 'Add sub')
        self.pm.prepare(t1)
        self.pm.prepare(t2)
        self.edit(t1, 'demoapp/text.py', '"hello"', '"hello there"')
        self.edit(t2, 'demoapp/calc.py', 'return a * b\n', 'return a * b\n\n\ndef sub(a, b):\n    return a - b\n')
        first = self.pm.merge(t1)
        self.assertEqual(first['status'], 'merged', first)
        second = self.pm.merge(t2)  # main moved since t2 started: merged cleanly anyway
        self.assertEqual(second['status'], 'merged', second)
        self.assertEqual((second['stats']['files_changed'], second['stats']['added']), (1, 4))
        self.assertTrue(second['tests']['green'])
        self.assertIn('hello there', (self.root / 'demoapp' / 'text.py').read_text(encoding='utf-8'))
        self.assertIn('def sub', (self.root / 'demoapp' / 'calc.py').read_text(encoding='utf-8'))
        self.assertEqual(self.main_sha(), second['main_after'])
        for t in (t1, t2):
            self.assertFalse(Path(self.pm.worktree_path(t)).exists())
        self.assertEqual(git(self.root, 'branch', '--list', 'task/*'), '')
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')
        self.assertFalse(second['pushed'])
        self.assertIn('No origin', second['push']['reason'])
        self.assertEqual(self.created, [])

    def test_real_conflict_goes_to_one_follow_up_task_and_keeps_the_branch(self):
        t1 = self.task('t1', 's1', 'Add accepts numbers as text')
        t2 = self.task('t2', 's2', 'Add works with floats')
        self.pm.prepare(t1)
        self.pm.prepare(t2)
        self.edit(t1, 'demoapp/calc.py', 'return a + b', 'return int(a) + int(b)')
        self.edit(t2, 'demoapp/calc.py', 'return a + b', 'return float(a) + float(b)')
        self.assertEqual(self.pm.merge(t1)['status'], 'merged')
        main_after_t1 = self.main_sha()
        result = self.pm.merge(t2)
        self.assertEqual(result['status'], 'conflict', result)
        self.assertEqual(self.main_sha(), main_after_t1)
        self.assertTrue(Path(self.pm.worktree_path(t2)).exists())
        self.assertIn('task/s2', git(self.root, 'branch', '--list', 'task/s2'))
        self.assertEqual(len(self.created), 1)
        follow = self.created[0]
        self.assertEqual(follow['kind'], 'resolve_conflict')
        self.assertEqual(follow['files'], ['demoapp/calc.py'])
        text = follow['goal']
        for needed in ('Add accepts numbers as text', 'Add works with floats', 'return int(a) + int(b)',
                       'return float(a) + float(b)', str(self.pm.worktree_path(t2))):
            self.assertIn(needed, text)
        self.assertNotIn('return a * b', text)  # only the conflicting hunks, not the whole file
        self.assertEqual(len(follow['conflicts'][0]['hunks']), 1)
        # Trying again does not create a second follow-up (never resend).
        self.assertEqual(self.pm.merge(t2)['status'], 'conflict')
        self.assertEqual(len(self.created), 1)
        # The follow-up coworker resolves it in the same worktree; then it merges.
        resolve = dict(follow, id='t3')
        wt = Path(self.pm.prepare(resolve))
        self.assertEqual(wt, Path(self.pm.worktree_path(t2)))
        subprocess.run(['git', 'merge', 'main'], cwd=str(wt), capture_output=True)
        write(wt / 'demoapp' / 'calc.py', CALC.replace('return a + b', 'return float(int(a)) + float(int(b))'))
        done = self.pm.merge(resolve)
        self.assertEqual(done['status'], 'merged', done)
        self.assertIn('float(int(a))', (self.root / 'demoapp' / 'calc.py').read_text(encoding='utf-8'))
        self.assertEqual(git(self.root, 'branch', '--list', 'task/*'), '')

    def test_red_tests_block_the_merge(self):
        t = self.task('t1', 's1', 'Break mul')
        self.pm.prepare(t)
        self.edit(t, 'demoapp/calc.py', 'return a * b', 'return a * b * 2')
        before = self.main_sha()
        result = self.pm.merge(t)
        self.assertEqual(result['status'], 'red', result)
        self.assertEqual(self.main_sha(), before)
        self.assertEqual((self.root / 'demoapp' / 'calc.py').read_text(encoding='utf-8'), CALC)
        self.assertTrue(Path(self.pm.worktree_path(t)).exists())
        self.assertIn('task/s1', git(self.root, 'branch', '--list', 'task/s1'))
        self.assertFalse(result['pushed'])
        self.assertEqual(self.created, [])

    def test_merges_run_one_at_a_time_in_order(self):
        tasks = [self.task('t%d' % i, 's%d' % i, 'File %d' % i) for i in range(3)]
        for i, t in enumerate(tasks):
            wt = Path(self.pm.prepare(t))
            write(wt / 'demoapp' / ('part%d.py' % i), 'N = %d\n' % i)
        active, peak, order = [0], [0], []
        guard = threading.Lock()
        real_verify = self.pm.verify

        def tracked(task, cwd=None):
            with guard:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
                order.append(task['id'])
            try:
                time.sleep(0.3)
                return real_verify(task, cwd=cwd)
            finally:
                with guard:
                    active[0] -= 1

        self.pm.verify = tracked
        futures = [self.pm.submit(tasks[0]), self.pm.submit(tasks[1])]
        direct = {}
        thread = threading.Thread(target=lambda: direct.update(r=self.pm.merge(tasks[2])))
        thread.start()
        results = [f.result(timeout=180) for f in futures]
        thread.join(180)
        results.append(direct['r'])
        self.assertEqual([r['status'] for r in results], ['merged'] * 3, results)
        self.assertEqual(peak[0], 1)
        self.assertLess(order.index('t0'), order.index('t1'))
        for i in range(3):
            self.assertTrue((self.root / 'demoapp' / ('part%d.py' % i)).exists())

    # ---- push ----------------------------------------------------------------
    def test_push_after_a_clean_green_merge_and_never_with_secrets(self):
        origin = self.add_origin()
        t1 = self.task('t1', 's1', 'Friendlier greeting')
        self.pm.prepare(t1)
        self.edit(t1, 'demoapp/text.py', '"hello"', '"hello there"')
        result = self.pm.merge(t1)
        self.assertEqual(result['status'], 'merged', result)
        self.assertTrue(result['pushed'], result['push'])
        self.assertEqual(git(origin, 'rev-parse', 'main'), self.main_sha())
        t2 = self.task('t2', 's2', 'Add a client')
        wt = Path(self.pm.prepare(t2))
        write(wt / 'demoapp' / 'client.py', 'KEY = "%s"\n' % ('ghp_' + 'A1b2' * 9))
        before = self.main_sha()
        leaked = self.pm.merge(t2)
        self.assertEqual(leaked['status'], 'secret_found', leaked)
        self.assertEqual(leaked['scan']['hits'][0]['kind'], 'GitHub token')
        self.assertNotIn('A1b2A1b2', str(leaked['scan']))
        self.assertEqual(self.main_sha(), before)
        self.assertEqual(git(origin, 'rev-parse', 'main'), before)
        self.assertFalse(leaked['pushed'])

    def test_never_force_pushes_over_a_newer_origin(self):
        origin = self.add_origin()
        other = self.tmp / 'other'
        git(self.tmp, 'clone', '-q', str(origin), str(other))
        git(other, 'config', 'user.name', 'Other')
        git(other, 'config', 'user.email', 'other@example.test')
        write(other / 'README.md', 'pushed by someone else\n')
        git(other, 'add', '-A')
        git(other, 'commit', '-q', '-m', 'other')
        git(other, 'push', '-q', 'origin', 'main')
        theirs = git(origin, 'rev-parse', 'main')
        t = self.task('t1', 's1', 'Friendlier greeting')
        self.pm.prepare(t)
        self.edit(t, 'demoapp/text.py', '"hello"', '"hello there"')
        result = self.pm.merge(t)
        self.assertEqual(result['status'], 'merged', result)
        self.assertFalse(result['pushed'])
        self.assertIn('rejected', result['push']['reason'].lower())
        self.assertEqual(git(origin, 'rev-parse', 'main'), theirs)


class PatchModeHelpersTest(unittest.TestCase):
    def test_patch_mode_is_off_by_default(self):
        self.assertFalse(patch_mode.enabled({}))
        self.assertFalse(patch_mode.enabled(None))
        self.assertFalse(patch_mode.enabled({'patch_mode': 'off'}))
        self.assertTrue(patch_mode.enabled({'patch_mode': True}))
        entry = patch_mode.SETTING_ENTRY
        self.assertEqual((entry['key'], entry['type'], entry['default']), ('patch_mode', 'bool', False))

    def test_conflict_hunks_keep_only_the_conflict(self):
        text = 'a\nb\nc\nd\n<<<<<<< main\nx\n=======\ny\n>>>>>>> task/s\ne\nf\ng\nh\n'
        hunks = patch_mode.conflict_hunks(text)
        self.assertEqual(len(hunks), 1)
        self.assertTrue(hunks[0]['text'].startswith('b\n'))
        self.assertTrue(hunks[0]['text'].endswith('\ng'))

    def test_secret_kinds(self):
        self.assertEqual(patch_mode._secret_kind('k = "sk-' + 'x' * 24 + '"'), 'API key')
        self.assertEqual(patch_mode._secret_kind('mail someone@company' + '.de'), 'Email address')
        self.assertIsNone(patch_mode._secret_kind('mail a@example.test or b@users.noreply.github.com'))
        self.assertIsNone(patch_mode._secret_kind('def add(a, b): return a + b'))
        self.assertTrue(patch_mode.FORBIDDEN_PATH.search('data/notion.db'))
        self.assertFalse(patch_mode.FORBIDDEN_PATH.search('notiontool/patch_mode.py'))


if __name__ == '__main__':
    unittest.main()
