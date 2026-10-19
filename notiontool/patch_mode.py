"""Patch mode: each coworker works in its own git worktree; NotionTool commits, tests and merges the patch.

Used by the engine only when the app setting patch_mode is on (default off). Flow for one task:

  prepare(task) -> worktree work/worktrees/<task_id> on branch task/<step_id>, made from main HEAD
  collect(task) -> commit the coworker's changes there (never ignored or secret-looking files) + diff stats
  verify(task)  -> the task's test files plus the desktop suite, run inside the worktree with a timeout
  merge(task)   -> one merge at a time: merge main into the branch, secret scan, tests, fast-forward main,
                   remove the worktree, push to origin if it exists. A real conflict is never guessed: it
                   becomes one 'resolve conflict' follow-up task and the branch is kept.

Never force-pushes. Never commits data/, work/, caches, logs, databases or files that look like secrets.
"""
import contextlib
import fnmatch
import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SETTING = 'patch_mode'
DEFAULT_ENABLED = False
# Entry for desktop_settings.HARNESS_SETTINGS; the integration task adds it there.
SETTING_ENTRY = dict(key=SETTING, label='Patch mode (one git worktree per coworker)', group='Coworkers',
                     type='bool', min=None, max=None, options=None, default=DEFAULT_ENABLED,
                     help='Each coworker works in its own git worktree. NotionTool commits, tests and merges '
                          'its patch. When off, file locks are used instead.')

BASE = 'main'
WORKTREES = 'work/worktrees'
DESKTOP_PATTERN = 'test_desktop*.py'
TEST_TIMEOUT_S = 900
GIT_TIMEOUT_S = 120
PUSH_TIMEOUT_S = 180
LOCK_STALE_S = 1800
CONTEXT_LINES = 3
# A test line with this text may hold a fake secret (for example a fake token in a test).
ALLOW_MARKER = 'notiontool: fake-secret'
_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

# Paths that must never be committed, even when a .gitignore misses them.
FORBIDDEN_PATH = re.compile(
    r'(^|/)(data|work|dist|build|__pycache__|\.pytest_cache|\.test-data|project-memory|browser-profiles?|sessions)/'
    r'|\.(db|db-shm|db-wal|db-journal|sqlite\d*|log|out|py[cod]|key|pem|pfx|p12)$'
    r'|(^|/)\.env(\.[^/]*)?$|secret|cookie|credential|token[^/]*\.(json|txt)$|host_api[^/]*\.json$', re.I)

SECRET_TEXT = (
    ('GitHub token', re.compile(r'\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{40,}')),
    ('AWS key', re.compile(r'\bAKIA[0-9A-Z]{16}\b')),
    ('Private key', re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----')),
    ('API key', re.compile(r'\bsk-[A-Za-z0-9_-]{20,}')),
    ('Bearer token', re.compile(r'\bBearer\s+[A-Za-z0-9._~+/=-]{24,}')),
    ('Notion token_v2', re.compile(r'token_v2["\']?\s*[=:]\s*["\']?[A-Za-z0-9%]{40,}')),
    ('Host API key', re.compile(r'host_api_key["\']?\s*[=:]\s*["\'][^"\']{12,}["\']', re.I)),
    ('Cookie header', re.compile(r'\bcookie["\']?\s*:\s*["\']?[^"\'\s=]+=[^"\'\s;]{20,}', re.I)),
)
EMAIL = re.compile(r'[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})')
SAFE_EMAIL_DOMAIN = re.compile(
    r'(^|\.)(example\.(com|org|net)|example|test|invalid|localhost)$|^users\.noreply\.github\.com$', re.I)
MARKER = re.compile(r'^(<{7}|>{7})( |$)')


class GitError(RuntimeError):
    pass


def enabled(settings):
    """True when the patch_mode setting is on. Default off."""
    value = (settings or {}).get(SETTING, DEFAULT_ENABLED)
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'on', 'yes')
    return bool(value)


def _slug(value):
    text = re.sub(r'[^A-Za-z0-9._-]+', '-', str(value or '')).strip('.-')
    text = re.sub(r'\.{2,}', '.', text)[:80].strip('.-')
    if not text:
        raise ValueError('A task id is required for patch mode')
    return text


def task_key(task):
    return _slug(task.get('id') or task.get('task_id'))


def _goal_line(task):
    for key in ('goal', 'title', 'prompt'):
        text = str(task.get(key) or '').strip()
        if text:
            return text.splitlines()[0][:200]
    return 'No goal given'


def _text(value):
    if isinstance(value, bytes):
        return value.decode('utf-8', 'replace')
    return value or ''


def _trailer(body, name):
    match = re.search(r'^' + re.escape(name) + r':[ \t]*(.+)$', body or '', re.M)
    return match.group(1).strip() if match else ''


def _secret_kind(text):
    for kind, pattern in SECRET_TEXT:
        if pattern.search(text):
            return kind
    for match in EMAIL.finditer(text):
        if not SAFE_EMAIL_DOMAIN.search(match.group(1)):
            return 'Email address'
    return None


def conflict_hunks(text, context=CONTEXT_LINES):
    """Only the conflicting parts of a file with conflict markers, each with a few lines around it."""
    lines = (text or '').splitlines()
    hunks, i = [], 0
    while i < len(lines):
        if lines[i].startswith('<<<<<<<'):
            end = i
            while end < len(lines) and not lines[end].startswith('>>>>>>>'):
                end += 1
            start, stop = max(0, i - context), min(len(lines), end + 1 + context)
            hunks.append(dict(line=i + 1, text='\n'.join(lines[start:stop])))
            i = end + 1
        else:
            i += 1
    return hunks


def _git_env():
    env = dict(os.environ)
    for name in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_OBJECT_DIRECTORY'):
        env.pop(name, None)
    env.update(GIT_TERMINAL_PROMPT='0', LC_ALL='C', LANG='C', GIT_OPTIONAL_LOCKS='0')
    return env


_ROOT_LOCKS = {}
_ROOT_LOCKS_GUARD = threading.Lock()


def _root_lock(root):
    with _ROOT_LOCKS_GUARD:
        return _ROOT_LOCKS.setdefault(os.path.normcase(str(root)), threading.Lock())


class PatchMode:
    def __init__(self, root, base=BASE, remote='origin', python=None, test_timeout=TEST_TIMEOUT_S,
                 desktop_pattern=DESKTOP_PATTERN, create_task=None, lock_wait=900):
        self.root = Path(root).resolve()
        self.base = base
        self.remote = remote
        self.python = python or ('python' if getattr(sys, 'frozen', False) else sys.executable)
        self.test_timeout = test_timeout
        self.desktop_pattern = desktop_pattern
        self.create_task = create_task
        self.lock_wait = lock_wait
        self._lock = _root_lock(self.root)
        self._follow_ups = {}
        self._queue = None
        self._queue_guard = threading.Lock()

    # ---- git ---------------------------------------------------------------
    def _git(self, args, cwd=None, check=True, timeout=GIT_TIMEOUT_S, input=None):
        cmd = ['git', '-c', 'core.autocrlf=false', '-c', 'core.quotepath=false', '-c', 'color.ui=false'] + list(args)
        try:
            proc = subprocess.run(cmd, cwd=str(cwd or self.root), env=_git_env(), input=input, capture_output=True,
                                  text=True, encoding='utf-8', errors='replace', timeout=timeout,
                                  creationflags=_NO_WINDOW)
        except subprocess.TimeoutExpired:
            raise GitError('git %s timed out after %s s' % (args[0], timeout))
        if check and proc.returncode != 0:
            raise GitError('git %s failed: %s' % (' '.join(args[:2]), (proc.stderr or proc.stdout).strip()[-600:]))
        return proc

    def _out(self, args, cwd=None):
        return self._git(args, cwd=cwd).stdout.strip()

    def _lines(self, args, cwd=None):
        return [line for line in self._git(args, cwd=cwd).stdout.splitlines() if line.strip()]

    def rev(self, ref, cwd=None):
        return self._out(['rev-parse', '--verify', '-q', ref + '^{commit}'], cwd=cwd)

    def _is_ancestor(self, older, newer):
        return self._git(['merge-base', '--is-ancestor', older, newer], check=False).returncode == 0

    def _branch_exists(self, branch):
        return self._git(['show-ref', '--verify', '-q', 'refs/heads/' + branch], check=False).returncode == 0

    def _common_dir(self):
        return (self.root / self._out(['rev-parse', '--git-common-dir'])).resolve()

    # ---- names -------------------------------------------------------------
    def branch_name(self, task):
        if task.get('patch_branch'):
            return str(task['patch_branch'])
        return 'task/' + _slug(task.get('step_id') or task.get('id') or task.get('task_id'))

    def worktree_path(self, task):
        if task.get('patch_worktree'):
            path = Path(task['patch_worktree']).resolve()
            if (self.root / WORKTREES).resolve() not in path.parents:
                raise ValueError('A patch worktree must be inside %s' % (self.root / WORKTREES))
            return path
        return self.root / WORKTREES / task_key(task)

    def _existing(self, task):
        path = self.worktree_path(task)
        if not (path / '.git').exists():
            raise GitError('There is no worktree for task %s; prepare it first' % task_key(task))
        return path

    # ---- 1. prepare --------------------------------------------------------
    def prepare(self, task):
        """Create (or reuse) the task worktree from main HEAD. Returns its absolute path."""
        path, branch = self.worktree_path(task), self.branch_name(task)
        self._git(['check-ref-format', '--branch', branch])
        self._ensure_ignored()
        if (path / '.git').exists():
            current = self._out(['rev-parse', '--abbrev-ref', 'HEAD'], cwd=path)
            if current != branch:
                raise GitError('%s is on branch %s, not %s' % (path, current, branch))
            return str(path)
        self._git(['worktree', 'prune'])
        path.parent.mkdir(parents=True, exist_ok=True)
        if self._branch_exists(branch):
            self._git(['worktree', 'add', '-q', str(path), branch])
        else:
            self._git(['worktree', 'add', '-q', '-b', branch, str(path), self.base])
        return str(path)

    def _ensure_ignored(self):
        """The worktrees folder must never show up in the main checkout."""
        probe = WORKTREES + '/probe/file'
        if self._git(['check-ignore', '-q', '--no-index', probe], check=False).returncode == 0:
            return
        exclude = self._common_dir() / 'info' / 'exclude'
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with open(exclude, 'a', encoding='utf-8', newline='\n') as handle:
            handle.write('\n/' + WORKTREES + '/\n')

    # ---- 2. collect --------------------------------------------------------
    def collect(self, task):
        """Commit the coworker's changes in its worktree. Returns diff stats against main for the proof record."""
        path = self._existing(task)
        self._git(['add', '-A', '--', '.'], cwd=path)  # respects .gitignore: ignored files are never added
        staged = self._lines(['diff', '--cached', '--name-only', '--no-renames'], cwd=path)
        skipped = [name for name in staged if FORBIDDEN_PATH.search(name)]
        for i in range(0, len(skipped), 50):
            self._git(['reset', '-q', '--'] + skipped[i:i + 50], cwd=path)
        kept = [name for name in staged if name not in skipped]
        merging = self._git(['rev-parse', '-q', '--verify', 'MERGE_HEAD'], cwd=path, check=False).returncode == 0
        commit = None
        if kept or merging:
            self._git(['commit', '-q', '--no-verify', '-F', '-'], cwd=path, input=self._message(task))
            commit = self.rev('HEAD', cwd=path)
        stats = self.diff_stats(task, cwd=path)
        stats.update(commit=commit, skipped=skipped)
        return stats

    def _message(self, task):
        goal = _goal_line(task)
        return 'Task %s: %s\n\nNotionTool-Task: %s\nNotionTool-Step: %s\nNotionTool-Goal: %s\n' % (
            task_key(task), goal, task_key(task), task.get('step_id') or '', goal)

    def diff_stats(self, task, cwd=None):
        """Files and +/- lines of the task branch against main (binary files count 0 lines)."""
        path = Path(cwd) if cwd else self._existing(task)
        base = self._out(['merge-base', self.base, 'HEAD'], cwd=path)
        files, added, deleted = [], 0, 0
        for line in self._lines(['diff', '--numstat', '--no-renames', base, 'HEAD'], cwd=path):
            plus, minus, name = line.split('\t', 2)
            binary = plus == '-'
            a, d = (0, 0) if binary else (int(plus), int(minus))
            files.append(dict(path=name, added=a, deleted=d, binary=binary))
            added += a
            deleted += d
        return dict(task_id=task_key(task), branch=self.branch_name(task), worktree=str(path), base=base,
                    head=self.rev('HEAD', cwd=path), files=files, files_changed=len(files), added=added,
                    deleted=deleted)

    # ---- 3. verify ---------------------------------------------------------
    def test_commands(self, task, cwd):
        """The task's own test files (planned or changed) plus the desktop suite."""
        cwd = Path(cwd)
        names = [str(f).replace('\\', '/') for f in task.get('files') or []]
        try:
            names += [f['path'] for f in self.diff_stats(task, cwd=cwd)['files']]
        except GitError:
            pass
        modules = []
        for name in names:
            match = re.match(r'^(?:\./)?(tests/test_\w+)\.py$', name)
            if not match or fnmatch.fnmatch(match.group(1).split('/')[-1] + '.py', self.desktop_pattern):
                continue
            module = match.group(1).replace('/', '.')
            if (cwd / (match.group(1) + '.py')).is_file() and module not in modules:
                modules.append(module)
        commands = []
        if modules:
            commands.append(('task tests', [self.python, '-m', 'unittest'] + modules))
        if (cwd / 'tests').is_dir():
            commands.append(('desktop suite', [self.python, '-m', 'unittest', 'discover', '-s', 'tests', '-p',
                                               self.desktop_pattern, '-t', '.']))
        return commands

    def verify(self, task, cwd=None):
        """Run the tests inside the worktree. Returns result 'green' or 'red' with the output tail of each run."""
        path = Path(cwd) if cwd else self._existing(task)
        started = time.monotonic()
        results = []
        for name, argv in self.test_commands(task, path):
            result = self._run_tests(name, argv, path)
            results.append(result)
            if not result['ok']:
                break
        green = all(r['ok'] for r in results)
        return dict(green=green, result='green' if green else 'red', commands=results,
                    seconds=round(time.monotonic() - started, 2))

    def _run_tests(self, name, argv, cwd):
        env = dict(os.environ)
        env.update(PYTHONPATH=str(cwd), PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8',
                   NOTIONTOOL_PATCH_VERIFY='1')
        started = time.monotonic()
        try:
            proc = subprocess.run(argv, cwd=str(cwd), env=env, capture_output=True, text=True, encoding='utf-8',
                                  errors='replace', timeout=self.test_timeout, creationflags=_NO_WINDOW)
            code, out, timed_out = proc.returncode, (proc.stdout or '') + (proc.stderr or ''), False
        except subprocess.TimeoutExpired as exc:
            code, timed_out = None, True
            out = _text(exc.stdout) + _text(exc.stderr) + '\nTimed out after %s s' % self.test_timeout
        ok = not timed_out and code in (0, 5)  # 5: no tests found
        return dict(name=name, command=' '.join(argv[1:]), code=code, ok=ok, timed_out=timed_out,
                    seconds=round(time.monotonic() - started, 2), tail=out[-2000:])

    # ---- secret scan -------------------------------------------------------
    def secret_scan(self, old, new, cwd=None):
        """Scan what old..new adds. Reports paths, lines and kinds only, never the secret itself."""
        hits, markers = [], []
        for name in self._lines(['diff', '--name-only', '--no-renames', '--diff-filter=d', old, new], cwd=cwd):
            if FORBIDDEN_PATH.search(name):
                hits.append(dict(path=name, line=None, kind='File that must not be committed'))
        current, number = None, 0
        diff = self._git(['diff', '--no-renames', '--no-ext-diff', '--unified=0', old, new], cwd=cwd).stdout
        for line in diff.splitlines():
            if line.startswith('+++ '):
                current = line[6:] if line.startswith('+++ b/') else None
            elif line.startswith('@@'):
                match = re.search(r'\+(\d+)', line)
                number = int(match.group(1)) if match else 0
            elif line.startswith('+') and current:
                text = line[1:]
                if MARKER.match(text):
                    markers.append(dict(path=current, line=number))
                if ALLOW_MARKER not in text:
                    kind = _secret_kind(text)
                    if kind:
                        hits.append(dict(path=current, line=number, kind=kind))
                number += 1
        return dict(clean=not hits and not markers, hits=hits, conflict_markers=markers)

    # ---- 4. merge ----------------------------------------------------------
    def merge(self, task):
        """Merge one finished task into main. Only one merge runs at a time (threads and processes)."""
        with self._merge_slot():
            return self._merge(task)

    def submit(self, task):
        """Merge queue: merges run one after another in submit order. Returns a Future with the result."""
        with self._queue_guard:
            if self._queue is None:
                self._queue = ThreadPoolExecutor(max_workers=1, thread_name_prefix='patch-merge')
            return self._queue.submit(self.merge, task)

    def close(self):
        with self._queue_guard:
            if self._queue is not None:
                self._queue.shutdown(wait=True)
                self._queue = None

    @contextlib.contextmanager
    def _merge_slot(self):
        with self._lock:
            lock = self._common_dir() / 'notiontool-merge.lock'
            deadline = time.monotonic() + self.lock_wait
            while True:
                try:
                    fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                except FileExistsError:
                    try:
                        age = time.time() - lock.stat().st_mtime
                    except OSError:
                        continue
                    if age > LOCK_STALE_S:
                        with contextlib.suppress(OSError):
                            lock.unlink()
                        continue
                    if time.monotonic() > deadline:
                        raise TimeoutError('Another merge is still running')
                    time.sleep(0.2)
                    continue
                os.write(fd, json.dumps(dict(pid=os.getpid(), at=time.time())).encode('utf-8'))
                os.close(fd)
                break
            try:
                yield
            finally:
                with contextlib.suppress(OSError):
                    lock.unlink()

    def _merge(self, task):
        path, branch = self._existing(task), self.branch_name(task)
        result = dict(task_id=task_key(task), branch=branch, worktree=str(path), pushed=False)
        stats = self.collect(task)
        old_main = self.rev(self.base)
        result.update(stats=stats, main_before=old_main)
        if not stats['files_changed'] and self._is_ancestor(branch, self.base):
            result['cleanup'] = self._remove(path, branch)
            return dict(result, status='nothing', message='The task changed no files; nothing to merge')
        probe = self._git(['merge-tree', '--write-tree', '--name-only', self.base, branch], check=False)
        if probe.returncode == 1:
            return self._conflict(task, result, probe.stdout, old_main)
        if probe.returncode != 0:
            return dict(result, status='error', message=(probe.stderr or probe.stdout).strip()[-600:])
        before = self.rev('HEAD', cwd=path)
        joined = self._git(['merge', '-q', '--no-edit', '-m', 'Merge %s into %s' % (self.base, branch), self.base],
                           cwd=path, check=False)
        if joined.returncode != 0:
            self._git(['merge', '--abort'], cwd=path, check=False)
            return dict(result, status='error',
                        message='git merge failed: ' + (joined.stderr or joined.stdout).strip()[-600:])
        head = self.rev('HEAD', cwd=path)
        scan = self.secret_scan(old_main, head, cwd=path)
        result['scan'] = scan
        if not scan['clean']:
            self._undo(path, before)
            if scan['conflict_markers'] and not scan['hits']:
                return dict(result, status='unresolved', message='Conflict markers are still in the patch; main was not changed')
            return dict(result, status='secret_found',
                        message='The secret scan found %d problem(s); main was not changed' % len(scan['hits']))
        check = self.verify(task, cwd=path)
        result['tests'] = check
        if not check['green']:
            self._undo(path, before)
            return dict(result, status='red', message='Tests are red; main was not changed')
        if self.rev(self.base) != old_main:
            self._undo(path, before)
            return dict(result, status='main_moved', message='main changed during the merge; merge again')
        ok, detail = self._advance_main(old_main, head)
        if not ok:
            self._undo(path, before)
            return dict(result, status='main_busy', message='Could not fast-forward main: ' + detail)
        result.update(status='merged', message='Merged into main', main_after=head)
        result['cleanup'] = self._remove(path, branch)
        result['push'] = self.push(old_main, head, green=True)
        result['pushed'] = result['push']['pushed']
        return result

    def _undo(self, path, before):
        """Drop only the automatic merge commit; the coworker's commits stay on the branch."""
        self._git(['reset', '-q', '--hard', before], cwd=path, check=False)

    def _advance_main(self, old, new):
        head = self._git(['symbolic-ref', '-q', '--short', 'HEAD'], check=False).stdout.strip()
        if head == self.base:
            # Updates the main checkout; git refuses (and changes nothing) if local edits would be overwritten.
            proc = self._git(['merge', '--ff-only', '-q', new], check=False)
        else:
            proc = self._git(['update-ref', 'refs/heads/' + self.base, new, old], check=False)
        if proc.returncode != 0:
            return False, (proc.stderr or proc.stdout).strip()[-600:]
        now = self.rev(self.base)
        return now == new, 'main is at %s' % now

    def _remove(self, path, branch):
        removed = self._git(['worktree', 'remove', str(path)], check=False).returncode == 0
        if not removed and self._is_ancestor(branch, self.base):
            removed = self._git(['worktree', 'remove', '--force', str(path)], check=False).returncode == 0
        self._git(['worktree', 'prune'], check=False)
        deleted = removed and self._git(['branch', '-q', '-d', branch], check=False).returncode == 0
        return dict(worktree_removed=removed, branch_deleted=deleted)

    # ---- conflicts ---------------------------------------------------------
    def _conflict(self, task, result, output, old_main):
        lines = output.splitlines()
        tree = lines[0].strip() if lines else ''
        rest = lines[1:]
        if '' in rest:
            cut = rest.index('')
            names, messages = rest[:cut], [m for m in rest[cut + 1:] if m.strip()]
        else:
            names, messages = rest, []
        files = []
        for name in names:
            if name.strip() and name not in files:
                files.append(name)
        conflicts = []
        for name in files:
            blob = self._git(['cat-file', '-p', '%s:%s' % (tree, name)], check=False)
            hunks = conflict_hunks(blob.stdout) if blob.returncode == 0 else []
            conflicts.append(dict(path=name, hunks=hunks, notes=[m for m in messages if name in m]))
        base = self._out(['merge-base', self.base, result['branch']])
        others = self._goals_on_main(base, files)
        follow = self.follow_up_task(task, result, conflicts, others)
        key = '%s@%s' % (result['branch'], old_main)
        new = key not in self._follow_ups
        created = self._follow_ups.get(key)
        if new:
            created = self.create_task(follow) if self.create_task else None
            self._follow_ups[key] = created if created is not None else follow
        return dict(result, status='conflict',
                    message='Real merge conflict in %s; sent to one coworker' % ', '.join(files),
                    conflicts=conflicts, follow_up=follow, follow_up_created=new, created=created,
                    follow_up_key=key)

    def _goals_on_main(self, base, files):
        out = self._git(['log', '--format=%H%x1f%B%x1e', '%s..%s' % (base, self.base), '--'] + files,
                        check=False).stdout
        goals, plain = [], []
        for record in out.split('\x1e'):
            if '\x1f' not in record:
                continue
            sha, body = record.strip().split('\x1f', 1)
            task_id, goal = _trailer(body, 'NotionTool-Task'), _trailer(body, 'NotionTool-Goal')
            if goal:
                if not any(g['task_id'] == task_id for g in goals):
                    goals.append(dict(task_id=task_id, goal=goal, commit=sha[:12]))
            elif body.strip() and not body.startswith('Merge '):
                plain.append(dict(task_id=None, goal=body.strip().splitlines()[0][:200], commit=sha[:12]))
        return goals or plain[:3]

    def follow_up_task(self, task, result, conflicts, others):
        """One 'resolve conflict' task: only the conflicting hunks of both sides and the two goals."""
        lines = ['Resolve a merge conflict. Do not guess: the result must keep what both goals need.', '',
                 'Work ONLY in this folder: %s (branch %s).' % (result['worktree'], result['branch']),
                 'Steps: run "git merge %s" in that folder, fix only the conflicts below, run the tests, then stop. '
                 'NotionTool commits, tests and merges the result.' % self.base, '',
                 'Goal of this task (%s): %s' % (task_key(task), _goal_line(task))]
        for other in others or [dict(task_id=None, commit=None, goal='Unknown (the change on %s has no task goal)' % self.base)]:
            lines.append('Goal of the change on %s (%s): %s' % (
                self.base, other.get('task_id') or other.get('commit') or 'no task', other['goal']))
        lines += ['', 'Conflicting hunks. Between <<<<<<< and ======= is %s; between ======= and >>>>>>> is this task.'
                  % self.base]
        for item in conflicts:
            lines += ['', 'File %s:' % item['path']]
            for hunk in item['hunks']:
                lines += ['```', hunk['text'], '```']
            if not item['hunks']:
                lines += item['notes'] or ['(No text hunks: one side changed the file, the other deleted or renamed it.)']
        text = '\n'.join(lines)
        return dict(kind='resolve_conflict', role='executor', title='Resolve conflict: %s' % result['branch'],
                    goal=text, prompt=text, files=[c['path'] for c in conflicts],
                    step_id=task.get('step_id') or task_key(task), origin_task_id=task_key(task),
                    patch_branch=result['branch'], patch_worktree=result['worktree'], conflicts=conflicts,
                    other_goals=others)

    # ---- 5. push -----------------------------------------------------------
    def push(self, old, new, green):
        """Push main to origin, never forced: only if origin exists, tests are green and the scan is clean."""
        if not green:
            return dict(pushed=False, reason='Tests are not green; nothing was pushed')
        if self._git(['remote', 'get-url', self.remote], check=False).returncode != 0:
            return dict(pushed=False, reason='No %s remote; nothing was pushed' % self.remote)
        tracked = self._git(['rev-parse', '-q', '--verify', 'refs/remotes/%s/%s' % (self.remote, self.base)],
                            check=False).stdout.strip()
        since = tracked if tracked and self._is_ancestor(tracked, new) else old
        scan = self.secret_scan(since, new)
        if not scan['clean']:
            return dict(pushed=False, reason='The secret scan found something; nothing was pushed', scan=scan)
        args = ['push', '--porcelain', self.remote, '%s:refs/heads/%s' % (new, self.base)]
        if any(a.startswith('+') or 'force' in a for a in args[1:]):
            raise GitError('Force-push is never allowed')
        proc = self._git(args, check=False, timeout=PUSH_TIMEOUT_S)
        if proc.returncode == 0:
            return dict(pushed=True, reason='Pushed to %s/%s' % (self.remote, self.base))
        return dict(pushed=False, reason='Push was rejected (never forced): '
                    + (proc.stderr or proc.stdout).strip()[-400:])
