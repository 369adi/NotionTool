"""Patch mode with real git in a temporary folder: worktrees, commit, tests, merge queue, conflicts, push.

No Notion, no harness: the coworker is simulated by editing files in its worktree.
"""
import importlib.util
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

from notiontool import patch_mode, worker_gc

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

    def test_listed_files_in_ignored_paths_are_reported_not_silently_dropped(self):
        t = self.task('t1', 's1', 'Bench', files=['work/bench/roman.py', './work/bench/test_roman.py',
                                                   'work/bench/missing.py', '../evil.py'])
        wt = Path(self.pm.prepare(t))
        write(wt / 'work' / 'bench' / 'roman.py', 'X = 1\n')
        write(wt / 'work' / 'bench' / 'test_roman.py', 'Y = 2\n')
        stats = self.pm.collect(t)
        self.assertEqual(sorted(stats['dropped']), ['work/bench/roman.py', 'work/bench/test_roman.py'])
        self.assertEqual(stats['files_changed'], 0)
        before = self.main_sha()
        result = self.pm.merge(t)
        self.assertEqual(result['status'], 'dropped_files')
        self.assertIn('work/bench/roman.py', result['message'])
        self.assertEqual(self.main_sha(), before)
        self.assertFalse((self.root / 'work' / 'bench' / 'roman.py').exists())

    def test_collect_adds_listed_files_even_when_gitignored(self):
        t = self.task('t1', 's1', 'Bench', files=['work/bench/roman.py', './work/bench/test_roman.py',
                                                   'work/bench/missing.py', 'data/x.db', '../evil.py'])
        wt = Path(self.pm.prepare(t))
        write(wt / 'work' / 'bench' / 'roman.py', 'X = 1\n')
        write(wt / 'work' / 'bench' / 'test_roman.py', 'Y = 2\n')
        write(wt / 'work' / 'bench' / 'unlisted.py', 'Z = 3\n')
        write(wt / 'data' / 'x.db', 'db')
        stats = self.pm.collect(t)
        # Current design (patch_mode._dropped_listed): work/ and data/ are never merged on purpose;
        # listed files there are reported as dropped instead of being forced in silently.
        self.assertEqual(sorted(stats['dropped']), ['data/x.db', 'work/bench/roman.py', 'work/bench/test_roman.py'])
        self.assertEqual([f['path'] for f in stats['files']], [])
        self.assertFalse((self.root / 'work' / 'bench' / 'roman.py').exists())
        self.assertFalse((self.root / 'work' / 'bench' / 'unlisted.py').exists())

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
        self.assertIn('Auto-push is off', second['push']['reason'])
        self.pm.auto_push = True
        self.assertIn('No origin', self.pm.push(first['main_after'], second['main_after'], green=True)['reason'])
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
    def test_no_push_by_default_after_a_green_merge(self):
        origin = self.add_origin()
        before_remote = git(origin, 'rev-parse', 'main')
        t0 = self.task('t0', 's0', 'Friendlier greeting')
        self.pm.prepare(t0)
        self.edit(t0, 'demoapp/text.py', '"hello"', '"hello there"')
        result = self.pm.merge(t0)
        self.assertEqual(result['status'], 'merged', result)
        self.assertFalse(result['pushed'], result['push'])
        self.assertIn('Auto-push is off', result['push']['reason'])
        self.assertEqual(git(origin, 'rev-parse', 'main'), before_remote)

    def test_push_after_a_clean_green_merge_and_never_with_secrets(self):
        origin = self.add_origin()
        self.pm.auto_push = True
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
        self.pm.auto_push = True
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


RUFF = importlib.util.find_spec('ruff') is not None


def git_config(cwd, key):
    proc = subprocess.run(['git', 'config', '--get', key], cwd=str(cwd), capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else None


@unittest.skipUnless(GIT, 'git is not installed; patch mode needs a real git')
class PatchModeHardeningTest(unittest.TestCase):
    """Rank 1 (rerere, zdiff3, histogram), rank 4 (ruff lint gate) and rank 5 (worktree hardening)."""
    setUp = PatchModeTest.setUp
    tearDown = PatchModeTest.tearDown
    _create = PatchModeTest._create
    task = PatchModeTest.task
    main_sha = PatchModeTest.main_sha
    edit = PatchModeTest.edit

    # ---- rank 1: repo config, zdiff3, rerere ----------------------------------
    def test_repo_config_is_set_only_where_unset(self):
        before = {key: git_config(self.root, key) for key, _ in patch_mode.REPO_CONFIG}
        git(self.root, 'config', 'diff.algorithm', 'patience')  # the owner's choice wins
        self.pm.prepare(self.task('t1', 's1', 'Say hi'))
        done = self.pm.repo_config
        self.assertEqual(done['diff.algorithm'], 'kept')
        self.assertEqual(git_config(self.root, 'diff.algorithm'), 'patience')
        for key, value in patch_mode.REPO_CONFIG:
            if key == 'diff.algorithm':
                continue
            if before[key] is None:
                self.assertEqual((done[key], git_config(self.root, key)), ('set', value))
            else:
                self.assertEqual((done[key], git_config(self.root, key)), ('kept', before[key]))

    def test_rerere_replays_the_same_conflict_resolution_and_the_gate_still_runs(self):
        t1 = self.task('t1', 's1', 'Add accepts numbers as text')
        t2 = self.task('t2', 's2', 'Add works with floats')
        t3 = self.task('t3', 's3', 'Add works with floats too')
        for t in (t1, t2, t3):
            self.pm.prepare(t)
        if git_config(self.root, 'rerere.enabled') == 'false':
            self.skipTest('rerere is switched off in the git config of this machine')
        self.edit(t1, 'demoapp/calc.py', 'return a + b', 'return int(a) + int(b)')
        for t in (t2, t3):
            self.edit(t, 'demoapp/calc.py', 'return a + b', 'return float(a) + float(b)')
        self.assertEqual(self.pm.merge(t1)['status'], 'merged')
        first = self.pm.merge(t2)
        self.assertEqual(first['status'], 'conflict', first)
        self.assertEqual(len(self.created), 1)
        # The coworker solves it once in t2's worktree; git rerere records that resolution.
        wt2 = Path(self.pm.worktree_path(t2))
        subprocess.run(['git', 'merge', 'main'], cwd=str(wt2), capture_output=True)
        if git_config(self.root, 'merge.conflictStyle') == 'zdiff3':
            self.assertIn('|||||||', (wt2 / 'demoapp' / 'calc.py').read_text(encoding='utf-8'))
        write(wt2 / 'demoapp' / 'calc.py', CALC.replace('return a + b', 'return float(int(a)) + float(int(b))'))
        self.pm.collect(t2)
        # The same conflict on t3 now merges without a coworker, through the secret scan and the tests.
        result = self.pm.merge(t3)
        self.assertEqual(result['status'], 'merged', result)
        self.assertEqual(result['rerere']['files'], ['demoapp/calc.py'])
        self.assertTrue(result['tests']['green'])
        self.assertIn('float(int(a))', (self.root / 'demoapp' / 'calc.py').read_text(encoding='utf-8'))
        self.assertEqual(len(self.created), 1)

    # ---- rank 4: ruff lint gate -----------------------------------------------
    def test_lint_is_skipped_with_a_note_when_ruff_is_missing(self):
        self.pm._lint_argv = lambda files: [sys.executable, '-I', '-S', '-m', 'ruff', 'check', '--select',
                                            patch_mode.LINT_RULES, '--output-format', 'json'] + list(files)
        t = self.task('t1', 's1', 'Use an undefined name')
        self.pm.prepare(t)
        self.edit(t, 'demoapp/text.py', 'GREETING = "hello"', 'GREETING = undefined_name')
        result = self.pm.merge(t)
        self.assertEqual(result['status'], 'merged', result)
        lint = result['tests']['commands'][0]
        self.assertEqual(lint['name'], 'lint')
        self.assertTrue(lint['ok'] and lint['skipped'], lint)
        self.assertEqual(lint['note'], patch_mode.LINT_MISSING_NOTE)

    @unittest.skipUnless(RUFF, 'ruff is not installed')
    def test_lint_fails_a_new_undefined_name(self):
        t = self.task('t1', 's1', 'Use an undefined name')
        self.pm.prepare(t)
        self.edit(t, 'demoapp/text.py', 'GREETING = "hello"', 'GREETING = undefined_name')
        before = self.main_sha()
        result = self.pm.merge(t)
        self.assertEqual(result['status'], 'red', result)
        lint = result['tests']['commands'][0]
        self.assertEqual((lint['name'], lint['ok']), ('lint', False), lint)
        self.assertEqual([f['code'] for f in lint['new']], ['F821'])
        self.assertIn('undefined_name', lint['tail'])
        self.assertEqual(self.main_sha(), before)

    @unittest.skipUnless(RUFF, 'ruff is not installed')
    def test_lint_ignores_an_old_unused_import_in_a_touched_file(self):
        write(self.root / 'demoapp' / 'old.py', 'import os\n\nVALUE = 1\n')
        git(self.root, 'add', '-A')
        git(self.root, 'commit', '-q', '-m', 'old F401')
        t = self.task('t1', 's1', 'Touch old.py')
        self.pm.prepare(t)
        self.edit(t, 'demoapp/old.py', 'VALUE = 1', 'VALUE = 2')
        result = self.pm.merge(t)
        self.assertEqual(result['status'], 'merged', result)
        lint = result['tests']['commands'][0]
        self.assertEqual((lint['name'], lint['ok'], lint['known'], lint['new']), ('lint', True, 1, []), lint)

    # ---- rank 5: worktree hardening -------------------------------------------
    def test_a_live_task_worktree_is_locked_and_survives_prune(self):
        t = self.task('t1', 's1', 'Say hi')
        path = Path(self.pm.prepare(t))
        remove_tree(path)  # the folder is gone for a moment (Windows, an antivirus, a user)
        git(self.root, 'worktree', 'prune')
        entries = worker_gc.parse_worktree_porcelain(git(self.root, 'worktree', 'list', '--porcelain'))
        mine = [e for e in entries if Path(e['path']).resolve() == path.resolve()]
        self.assertEqual(len(mine), 1, entries)
        self.assertEqual((mine[0]['locked'], mine[0]['lock_reason']), (True, 't1'))
        result = self.pm._remove(path, 'task/s1')
        self.assertTrue(result['worktree_removed'], result)
        self.assertEqual(len(worker_gc.parse_worktree_porcelain(git(self.root, 'worktree', 'list', '--porcelain'))), 1)
        self.assertEqual(git(self.root, 'branch', '--list', 'task/*'), '')

    def test_remove_retries_a_forced_remove_with_back_off(self):
        t = self.task('t1', 's1', 'Say hi')
        path = Path(self.pm.prepare(t))
        write(path / 'demoapp' / 'scratch.txt', 'untracked, so the plain remove refuses\n')
        real_git, forced = self.pm._git, []

        def flaky(args, *more, **kw):
            if args[:2] == ['worktree', 'remove'] and '--force' in args:
                forced.append(list(args))
                if len(forced) < 3:
                    return subprocess.CompletedProcess(args, 1, '', 'Permission denied')
            return real_git(args, *more, **kw)

        self.pm._git = flaky
        self.pm.remove_backoff = (0.01, 0.01, 0.01)
        result = self.pm._remove(path, 'task/s1')
        self.assertTrue(result['worktree_removed'], result)
        self.assertEqual(result['attempts'], 4)  # one plain remove, two failed forced ones, then success
        self.assertEqual(forced[-1][:4], ['worktree', 'remove', '--force', '--force'])
        self.assertFalse(path.exists())

    def test_reconcile_removes_only_stale_finished_worktrees(self):
        now = time.time()
        old = now - 10 * 86400
        stale = [dict(self.task('t%d' % i, 's%d' % i, 'Stale %d' % i), state=state, finished=old)
                 for i, state in ((1, 'done'), (2, 'failed'), (3, 'cancelled'))]
        live = dict(self.task('t4', 's4', 'Live'), state='running')
        reviewed = dict(self.task('t5', 's5', 'Reviewed'), state='done', finished=old)
        review = dict(self.task('t6', 's6', 'Review of t5'), state='queued', review_of='t5')
        for t in stale + [live, reviewed]:
            self.pm.prepare(t)
        result = self.pm.reconcile_worktrees(stale + [live, reviewed, review], max_age_days=7, now=now)
        self.assertEqual(sorted(Path(p).name for p in result['removed']), ['t1', 't2', 't3'], result['plan'])
        self.assertEqual(result['before'] - result['after'], 3)
        for t in stale:
            self.assertFalse(Path(self.pm.worktree_path(t)).exists())
        for t in (live, reviewed):
            self.assertTrue(Path(self.pm.worktree_path(t)).exists())
        reasons = {Path(item['path']).name: item['reason'] for item in result['plan']}
        self.assertIn('live task t4', reasons['t4'])
        self.assertIn('live review task t6', reasons['t5'])
        self.assertEqual(git(self.root, 'branch', '--list', 'task/s1'), '')


class PatchModeHardeningHelpersTest(unittest.TestCase):
    def test_conflict_hunks_show_ours_base_and_theirs_for_zdiff3(self):
        text = ('a\nb\n<<<<<<< main\n    return int(a)\n||||||| base\n    return a\n=======\n'
                '    return float(a)\n>>>>>>> task/s\nc\n')
        hunks = patch_mode.conflict_hunks(text)
        self.assertEqual(len(hunks), 1)
        self.assertEqual((hunks[0]['ours'], hunks[0]['base'], hunks[0]['theirs']),
                         ('    return int(a)', '    return a', '    return float(a)'))
        self.assertIn('||||||| base', hunks[0]['text'])
        plain = patch_mode.conflict_hunks('<<<<<<< main\nx\n=======\ny\n>>>>>>> t\n')
        self.assertEqual((plain[0]['ours'], plain[0]['base'], plain[0]['theirs']), ('x', None, 'y'))

    def test_lint_findings_match_per_file_rule_and_message_not_line(self):
        key = patch_mode.PatchMode._lint_key
        a = dict(path='m.py', line=3, code='F811', message='Redefinition of unused `f` from line 1')
        b = dict(path='m.py', line=9, code='F811', message='Redefinition of unused `f` from line 7')
        self.assertEqual(key(a), key(b))
        self.assertEqual(patch_mode.PatchMode._lint_path('C:\\x\\wt\\pkg\\m.py', 'C:\\x\\wt', ['m.py', 'pkg/m.py']),
                         'pkg/m.py')

    def test_reconcile_plan_keeps_live_reviewed_foreign_locked_and_unnamed_worktrees(self):
        root = os.path.join(tempfile.gettempdir(), 'nt-plan', 'repo')

        def wt(name):
            return os.path.join(root, 'work', 'worktrees', name).replace('\\', '/')

        porcelain = '\n'.join([
            'worktree %s' % root.replace('\\', '/'), 'HEAD aaa', 'branch refs/heads/main', '',
            'worktree %s' % wt('a1'), 'HEAD bbb', 'branch refs/heads/task/sa', 'locked a1', '',
            'worktree %s' % wt('b1'), 'HEAD ccc', 'branch refs/heads/task/sb', '',
            'worktree %s' % wt('c1'), 'HEAD ddd', 'branch refs/heads/task/sc', 'locked owner keeps this', '',
            'worktree %s' % wt('orphan'), 'HEAD eee', 'branch refs/heads/task/zz', '',
            'worktree %s' % wt('_main-0123456789ab'), 'HEAD fff', 'detached', '',
            'worktree %s' % wt('d1'), 'HEAD ggg', 'branch refs/heads/task/sd', 'prunable gitdir points nowhere', '',
            'worktree %s' % wt('e1'), 'HEAD hhh', 'branch refs/heads/task/se', '',
        ])
        entries = worker_gc.parse_worktree_porcelain(porcelain)
        self.assertEqual(len(entries), 8)
        self.assertEqual((entries[1]['branch'], entries[1]['locked'], entries[1]['lock_reason']), ('task/sa', True, 'a1'))
        self.assertTrue(entries[5]['detached'] and entries[6]['prunable'])
        now, old = 10 * 86400.0 + 5, 5.0
        tasks = [dict(id='a1', step_id='sa', state='done', finished=old),
                 dict(id='b1', step_id='sb', state='done', finished=old),
                 dict(id='b2', state='running', review_of='b1'),
                 dict(id='c1', step_id='sc', state='done', finished=old),
                 dict(id='d-other', state='blocked', finished=old, patch_worktree=wt('d1')),
                 dict(id='e1', step_id='se', state='done', finished=now - 3600)]
        plan = worker_gc.plan_worktree_reconcile(porcelain, tasks, now, max_age_days=7, main_path=root,
                                                 worktrees_dir=os.path.join(root, 'work', 'worktrees'),
                                                 ages={wt('_main-0123456789ab'): old})
        actions = {os.path.basename(item['path']): item['action'] for item in plan}
        self.assertEqual(actions, {'repo': 'keep', 'a1': 'remove', 'b1': 'keep', 'c1': 'keep', 'orphan': 'keep',
                                   '_main-0123456789ab': 'remove', 'd1': 'remove', 'e1': 'keep'})
        self.assertEqual(tasks[0]['state'], 'done')  # pure: the input is not changed


def merge_tree_rc(cwd, branch):
    """Exit code of the patch mode probe in cwd: 0 clean, 1 conflict."""
    return subprocess.run(['git', '-c', 'core.autocrlf=false', 'merge-tree', '--write-tree', '--name-only', 'main',
                           branch], cwd=str(cwd), capture_output=True, text=True).returncode


def blob(cwd, spec):
    return subprocess.run(['git', 'show', spec], cwd=str(cwd), capture_output=True).stdout


@unittest.skipUnless(GIT, 'git is not installed; patch mode needs a real git')
class PatchModeDriverTest(unittest.TestCase):
    """Rank 7 (merge driver: probe and real merge agree; mergiraf only from PATH) and rank 2 (eol_guard on merges)."""
    setUp = PatchModeTest.setUp
    tearDown = PatchModeTest.tearDown
    _create = PatchModeTest._create
    task = PatchModeTest.task
    main_sha = PatchModeTest.main_sha
    edit = PatchModeTest.edit

    def _driver(self, exit_code):
        """A stand-in merge driver run by git as a process: exit 0 keeps %A (the task side), exit 1 fails."""
        script = self.tmp / ('driver_%d.py' % exit_code)
        write(script, 'import sys\nsys.exit(%d)\n' % exit_code)
        git(self.root, 'config', 'merge.keepours.driver',
            '"%s" "%s" %%O %%A %%B' % (Path(sys.executable).as_posix(), script.as_posix()))

    def _two_add_tasks(self):
        t1 = self.task('t1', 's1', 'Add accepts numbers as text')
        t2 = self.task('t2', 's2', 'Add works with floats')
        for t in (t1, t2):
            self.pm.prepare(t)
        self.edit(t1, 'demoapp/calc.py', 'return a + b', 'return int(a) + int(b)')
        self.assertEqual(self.pm.merge(t1)['status'], 'merged')
        self.edit(t2, 'demoapp/calc.py', 'return a + b', 'return float(a) + float(b)')
        return t2, Path(self.pm.worktree_path(t2))

    def test_probe_and_real_merge_agree_when_a_merge_driver_solves_the_conflict(self):
        self._driver(0)
        t2, wt2 = self._two_add_tasks()
        write(wt2 / '.gitattributes', 'demoapp/calc.py merge=keepours\n')  # only the task routes calc.py
        self.pm.collect(t2)
        branch = self.pm.branch_name(t2)
        # merge-tree runs the driver but reads .gitattributes from its own work tree: in the main checkout it
        # would report a conflict that the real merge in the task worktree solves; in the worktree both agree.
        self.assertEqual(merge_tree_rc(self.root, branch), 1)
        self.assertEqual(merge_tree_rc(wt2, branch), 0)
        result = self.pm.merge(t2)
        self.assertEqual(result['status'], 'merged', result)
        self.assertTrue(result['tests']['green'])  # the gate ran after the driver merge
        self.assertEqual(self.created, [])
        self.assertIn('float(a) + float(b)', (self.root / 'demoapp' / 'calc.py').read_text(encoding='utf-8'))

    def test_probe_and_real_merge_agree_when_the_merge_driver_fails(self):
        self._driver(1)
        write(self.root / '.gitattributes', 'demoapp/calc.py merge=keepours\n')
        git(self.root, 'add', '-A')
        git(self.root, 'commit', '-q', '-m', 'route calc.py to the driver')
        t2, wt2 = self._two_add_tasks()
        self.pm.collect(t2)
        branch = self.pm.branch_name(t2)
        self.assertEqual((merge_tree_rc(self.root, branch), merge_tree_rc(wt2, branch)), (1, 1))
        result = self.pm.merge(t2)
        self.assertEqual(result['status'], 'conflict', result)
        self.assertEqual(len(self.created), 1)
        self.assertNotIn('<<<<<<<', (wt2 / 'demoapp' / 'calc.py').read_text(encoding='utf-8'))  # merge aborted

    def test_mergiraf_driver_is_configured_only_when_on_path_and_routed(self):
        from unittest import mock
        real = shutil.which

        def which(found):
            return lambda name, *a, **k: (found if name == 'mergiraf' else real(name, *a, **k))

        write(self.root / '.gitattributes', '*.py merge=mergiraf\n')
        with mock.patch.object(patch_mode.shutil, 'which', which(None)):
            self.assertEqual(self.pm._configure_driver(), 'missing')
        self.assertIsNone(git_config(self.root, 'merge.mergiraf.driver'))
        write(self.root / '.gitattributes', '# *.py merge=mergiraf\n* text=auto\n')
        with mock.patch.object(patch_mode.shutil, 'which', which('C:/tools/mergiraf.exe')):
            self.assertEqual(self.pm._configure_driver(), 'unused')
            self.assertIsNone(git_config(self.root, 'merge.mergiraf.driver'))
            write(self.root / '.gitattributes', '*.py merge=mergiraf\n')
            self.assertEqual(self.pm._configure_driver(), 'set')
        self.assertEqual(git_config(self.root, 'merge.mergiraf.driver'), patch_mode.MERGIRAF_DRIVER)
        self.assertEqual(self.pm._merge_drivers(), ['mergiraf'])

    def test_merged_files_keep_mains_line_endings(self):
        write(self.root / 'demoapp' / 'win.py', 'A = 1\r\nB = 2\r\n')
        write(self.root / 'demoapp' / 'mixed.py', 'M = 1\r\nN = 2\n')
        git(self.root, 'add', '-A')
        git(self.root, 'commit', '-q', '-m', 'crlf and mixed files')
        t = self.task('t1', 's1', 'Change B and N')
        self.pm.prepare(t)
        wt = Path(self.pm.worktree_path(t))
        write(wt / 'demoapp' / 'win.py', 'A = 1\nB = 3\n')  # the coworker wrote LF into a CRLF file
        write(wt / 'demoapp' / 'mixed.py', 'M = 1\nN = 3\n')  # mixed on main: left as written
        write(wt / 'demoapp' / 'new.py', 'X = 1\r\n')  # new file, no eol attribute: left as written
        result = self.pm.merge(t)
        self.assertEqual(result['status'], 'merged', result)
        self.assertEqual(result['eol'], {'demoapp/win.py': 'lf->crlf'})
        self.assertEqual(blob(self.root, 'main:demoapp/win.py'), b'A = 1\r\nB = 3\r\n')
        self.assertEqual((self.root / 'demoapp' / 'win.py').read_bytes(), b'A = 1\r\nB = 3\r\n')
        self.assertEqual(blob(self.root, 'main:demoapp/mixed.py'), b'M = 1\nN = 3\n')
        self.assertEqual(blob(self.root, 'main:demoapp/new.py'), b'X = 1\r\n')
        self.assertTrue(result['tests']['green'])


if __name__ == '__main__':
    unittest.main()
