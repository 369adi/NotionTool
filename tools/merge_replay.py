"""Replay the task branches that were rebuilt by hand, with and without the mergiraf merge driver (rank 7).

Usage:
    python tools/merge_replay.py [--date 2026-10-22] [--branch task/x ...] [--onto REV] [--ext .js ...]
                                 [--tests "python -m pytest -q"] [--test-timeout 900] [--plain-only] [--no-tests]
                                 [--json]

Which branches: every task/* branch whose reflog on --date shows a merge, reset, rebase, cherry-pick or amend
(the automatic merge was tried and the branch was put back or rebuilt), plus every --branch given. Branches
that were deleted after their merge cannot be replayed; the report says how many were found.

Per branch the coworker tip (the newest 'commit' reflog entry, else the branch tip) is merged with --onto (default:
main as it was right before the first merge attempt that day), the same direction patch mode uses (main into
the branch). This runs twice in a throw-away detached worktree under work/_merge_replay/: once plain and once with
mergiraf as merge driver for *.py (plus --ext). The report lists the conflicting files, the files that merge
clean, the py_compile result and the test result. The test gate runs after every driver merge that leaves no
conflict. Rule from docs/research/notiontool-upgrades.md (rank 7): add '*.py merge=mergiraf' only when at least a
third of the plain conflicting files merge clean with the driver on branches that pass py_compile and the tests.

mergiraf is GPL-3.0: git only runs it as a separate process; nothing here imports or bundles it. If mergiraf
is not on PATH this script says so and exits 0 (--plain-only still counts the plain conflicts).
Nothing is committed: every merge is aborted and every replay worktree is removed again.
"""
import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MERGIRAF = 'mergiraf'
MERGIRAF_DRIVER = 'mergiraf merge --git %O %A %B -s %S -x %X -y %Y -p %P -l %L'
DEFAULT_DATE = '2026-10-22'
HAND_REBUILT = 15  # docs/research/03-candidates-a.md: 15 finished branches were rebuilt by hand that day
REBUILD_ACTIONS = ('merge', 'reset:', 'rebase', 'cherry-pick', 'commit (amend)', 'commit (merge)', 'pull')
RULE = 1.0 / 3.0
_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def git(args, cwd, check=False, timeout=300, config=()):
    cmd = ['git', '-c', 'core.autocrlf=false', '-c', 'core.quotepath=false', '-c', 'color.ui=false']
    for item in config:
        cmd += ['-c', item]
    proc = subprocess.run(cmd + list(args), cwd=str(cwd), capture_output=True, text=True, encoding='utf-8',
                          errors='replace', timeout=timeout, creationflags=_NO_WINDOW,
                          env=dict(os.environ, GIT_TERMINAL_PROMPT='0', GIT_MERGE_AUTOEDIT='no'))
    if check and proc.returncode != 0:
        raise RuntimeError('git %s failed: %s' % (' '.join(args[:2]), (proc.stderr or proc.stdout).strip()[-400:]))
    return proc


def lines(proc):
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def reflog(root, branch):
    """[(sha, iso_time, message)] newest first."""
    out = git(['reflog', 'show', '--date=iso-strict', '--format=%H%x09%gd%x09%gs', 'refs/heads/' + branch], root)
    found = []
    for line in out.stdout.splitlines():
        parts = line.split('\t', 2)
        if len(parts) != 3:
            continue
        when = re.search(r'@\{([^}]*)\}', parts[1])
        found.append((parts[0], when.group(1) if when else '', parts[2]))
    return found


def pick_branches(root, date, named):
    picked = {}
    for branch in lines(git(['for-each-ref', '--format=%(refname:short)', 'refs/heads/task/'], root)):
        entries = reflog(root, branch)
        that_day = [e for e in entries if e[1].startswith(date)]
        attempts = [e for e in that_day if e[2].startswith(REBUILD_ACTIONS)]
        if attempts:
            picked[branch] = entries
    for branch in named:
        if branch not in picked:
            picked[branch] = reflog(root, branch)
    return picked


def coworker_tip(root, branch, entries, date):
    """The coworker's own commit: the newest plain 'commit' reflog entry up to the end of date, else the tip."""
    for sha, when, message in entries:
        if when[:10] <= date and message.startswith(('commit:', 'commit (amend):', 'commit (initial):')):
            return sha
    return git(['rev-parse', '--verify', '-q', branch + '^{commit}'], root, check=True).stdout.strip()


def main_before(root, base, entries, date):
    """main right before the first merge attempt on date (falls back to main at the end of date)."""
    first = [when for _, when, message in entries if when.startswith(date) and message.startswith(REBUILD_ACTIONS)]
    moment = min(first) if first else date + 'T23:59:59'
    sha = git(['rev-list', '-1', '--first-parent', '--before=' + moment, base], root).stdout.strip()
    return sha or git(['rev-parse', base], root, check=True).stdout.strip()


def py_compile_files(path, names):
    """compile() every merged .py file (the py_compile check without writing __pycache__)."""
    bad = []
    for name in names:
        if not name.endswith('.py'):
            continue
        file = Path(path) / name
        if not file.exists():
            continue
        try:
            compile(file.read_bytes(), str(file), 'exec', dont_inherit=True)
        except (SyntaxError, ValueError) as exc:
            bad.append('%s: %s' % (name, exc))
    return bad


def run_tests(path, names, command, timeout):
    if command:
        argv = shlex.split(command, posix=(os.name != 'nt'))
    else:
        tests = [n for n in names if re.match(r'tests/test_[^/]*\.py$', n) and (Path(path) / n).exists()]
        if not tests:
            return dict(ran=False, ok=True, note='the branch names no test file')
        argv = [sys.executable, '-m', 'pytest', '-q', '-x', '-p', 'no:cacheprovider'] + tests
    started = time.time()
    try:
        proc = subprocess.run(argv, cwd=str(path), capture_output=True, text=True, encoding='utf-8', errors='replace',
                              timeout=timeout, creationflags=_NO_WINDOW,
                              env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
    except subprocess.TimeoutExpired:
        return dict(ran=True, ok=False, note='timed out after %d s' % timeout)
    tail = [line for line in (proc.stdout + proc.stderr).splitlines() if line.strip()][-1:]
    return dict(ran=True, ok=proc.returncode == 0, note=(tail[0] if tail else 'exit %d' % proc.returncode),
                seconds=round(time.time() - started, 1))


def replay(root, scratch, tip, onto, driver, exts, tests, test_timeout, no_tests):
    """Merge onto into a detached worktree at tip; returns the files and gate results, then cleans up."""
    path = Path(tempfile.mkdtemp(prefix='replay-', dir=str(scratch)))
    shutil.rmtree(path, ignore_errors=True)
    config, attributes = [], None
    if driver:
        attributes = path.with_suffix('.gitattributes')
        attributes.write_text(''.join('*%s merge=mergiraf\n' % ext for ext in ['.py'] + list(exts)), encoding='utf-8')
        config = ['core.attributesFile=' + attributes.as_posix(), 'merge.mergiraf.name=mergiraf',
                  'merge.mergiraf.driver=' + MERGIRAF_DRIVER]
    try:
        git(['worktree', 'add', '-q', '--detach', str(path), tip], root, check=True)
        joined = git(['merge', '--no-ff', '--no-commit', '-q', onto], path, config=config, timeout=900)
        conflicts = lines(git(['diff', '--name-only', '--diff-filter=U'], path))
        merged = lines(git(['diff', '--cached', '--name-only', '--diff-filter=AMR', onto], path))
        result = dict(conflicts=conflicts, files=merged, merge_exit=joined.returncode)
        if conflicts:
            return result
        result['py_compile'] = py_compile_files(path, merged)
        if no_tests:
            result['tests'] = dict(ran=False, ok=None, note='skipped (--no-tests)')
        elif result['py_compile']:
            result['tests'] = dict(ran=False, ok=False, note='skipped: py_compile failed')
        else:
            result['tests'] = run_tests(path, merged, tests, test_timeout)  # the gate after every driver merge
        return result
    finally:
        git(['merge', '--abort'], path) if path.exists() else None
        git(['worktree', 'remove', '--force', '--force', str(path)], root)
        shutil.rmtree(path, ignore_errors=True)
        git(['worktree', 'prune'], root)
        if attributes is not None:
            attributes.unlink(missing_ok=True)


def gate_text(run):
    if run.get('conflicts'):
        return 'not run (conflicts left)'
    compile_text = 'py_compile ok' if not run.get('py_compile') else 'py_compile FAILED (%s)' % '; '.join(
        run['py_compile'][:3])
    tests = run.get('tests') or {}
    state = {True: 'tests ok', False: 'tests FAILED', None: 'tests skipped'}[tests.get('ok')]
    return '%s; %s (%s)' % (compile_text, state, tests.get('note', ''))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--repo', default=None, help='project repo (default: the git top level of the cwd)')
    parser.add_argument('--base', default='main')
    parser.add_argument('--date', default=DEFAULT_DATE)
    parser.add_argument('--branch', action='append', default=[], help='replay this branch too (repeatable)')
    parser.add_argument('--onto', default=None, help='merge this revision instead of main before the first attempt')
    parser.add_argument('--ext', action='append', default=[], help='also route this extension to mergiraf, e.g. .js')
    parser.add_argument('--tests', default=None, help='test command (default: pytest on the tests/ files merged)')
    parser.add_argument('--test-timeout', type=int, default=900)
    parser.add_argument('--plain-only', action='store_true', help='only the run without driver (no mergiraf needed)')
    parser.add_argument('--no-tests', action='store_true')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)

    found = shutil.which(MERGIRAF)
    if not found and not args.plain_only:
        print('mergiraf is not on PATH: the replay needs it as git merge driver (install it, e.g. '
              '"cargo install --locked mergiraf", see https://mergiraf.org). Nothing was replayed; '
              "'*.py merge=mergiraf' stays out of .gitattributes.")
        return 0
    root = Path(args.repo or git(['rev-parse', '--show-toplevel'], Path.cwd(), check=True).stdout.strip())
    scratch = root / 'work' / '_merge_replay'
    scratch.mkdir(parents=True, exist_ok=True)
    branches = pick_branches(root, args.date, args.branch)
    print('merge replay %s: %d task branch(es) with merge attempts or rebuilds that day (plan counts %d hand '
          'rebuilds; deleted branches cannot be replayed); mergiraf: %s'
          % (args.date, len(branches), HAND_REBUILT, found or 'not on PATH (plain run only)'))
    report, plain_total, solved_green = [], 0, 0
    for branch, entries in sorted(branches.items()):
        item = dict(branch=branch)
        try:
            tip = coworker_tip(root, branch, entries, args.date)
            onto = args.onto or main_before(root, args.base, entries, args.date)
            item.update(tip=tip, onto=onto)
            item['plain'] = replay(root, scratch, tip, onto, False, args.ext, args.tests, args.test_timeout,
                                   args.no_tests)
            if found and not args.plain_only:
                item['mergiraf'] = replay(root, scratch, tip, onto, True, args.ext, args.tests, args.test_timeout,
                                          args.no_tests)
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            item['error'] = str(exc)[-400:]
        plain = item.get('plain') or {}
        driven = item.get('mergiraf')
        conflicting = plain.get('conflicts', [])
        plain_total += len(conflicting)
        if driven is not None:
            clean = [n for n in conflicting if n not in driven.get('conflicts', [])]
            green = not driven.get('conflicts') and not driven.get('py_compile') and \
                (driven.get('tests') or {}).get('ok') is not False
            item['merged_clean'] = clean
            item['green'] = green
            if green:
                solved_green += len(clean)
        report.append(item)
    if args.json:
        print(json.dumps(dict(date=args.date, branches=report, plain_conflicts=plain_total,
                              solved_and_green=solved_green), indent=1))
        return 0
    for item in report:
        print('\n%s  tip %s  onto %s' % (item['branch'], item.get('tip', '?')[:10], item.get('onto', '?')[:10]))
        if item.get('error'):
            print('  error: ' + item['error'])
            continue
        plain = item['plain']
        print('  plain   : %d conflicting file(s)%s; %s' % (
            len(plain['conflicts']), (' (%s)' % ', '.join(plain['conflicts'][:8])) if plain['conflicts'] else '',
            gate_text(plain)))
        driven = item.get('mergiraf')
        if driven is not None:
            print('  mergiraf: %d conflicting file(s)%s; merged clean %d (%s); %s' % (
                len(driven['conflicts']), (' (%s)' % ', '.join(driven['conflicts'][:8])) if driven['conflicts'] else '',
                len(item['merged_clean']), ', '.join(item['merged_clean'][:8]) or '-', gate_text(driven)))
    print('\nTotal: %d conflicting file(s) without driver' % plain_total)
    if found and not args.plain_only:
        share = (solved_green / plain_total) if plain_total else 0.0
        verdict = 'MET: add \'*.py merge=mergiraf\' to .gitattributes' if plain_total and share >= RULE else \
            'NOT met: keep .gitattributes as it is'
        print('mergiraf merged %d of them clean on branches that pass py_compile and the tests (%.0f%%); rule '
              '>= 1/3: %s' % (solved_green, share * 100, verdict))
    return 0


if __name__ == '__main__':
    sys.exit(main())
