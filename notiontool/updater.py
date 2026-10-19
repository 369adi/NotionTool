"""Automatic updates for installs that came from GitHub.

Only two install kinds update themselves; local developer builds never do:
- release: a NotionTool.exe built by the GitHub workflow (it contains notiontool/_build.py).
  It polls the latest GitHub release, downloads NotionTool.exe, verifies its SHA-256
  and swaps itself.
- git: a source checkout (git clone) of the repository. It fetches origin and
  fast-forwards when the working tree is clean.
The restart happens only while no coworker task is running, and the MCP gateway
(process 2) keeps running, so MCP calls are held instead of failing.
The repository is private: the GitHub token comes from GH_TOKEN/GITHUB_TOKEN,
`gh auth token` or the Git credential manager. It is used for the request only and
is never logged or stored.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = '369adi/NotionTool'
API = 'https://api.github.com/repos/'
CHECK_MINUTES = 30
ASSET = 'NotionTool.exe'
NO_WINDOW = 0x08000000 if os.name == 'nt' else 0

try:
    import notiontool._build as BUILD_INFO
except Exception:  # local builds and source runs have no build stamp
    BUILD_INFO = None


def build_number():
    try:
        return int(getattr(BUILD_INFO, 'BUILD', 0) or 0)
    except (TypeError, ValueError):
        return 0


def release_number(tag):
    tail = str(tag or '').rsplit('-', 1)[-1]
    return int(tail) if str(tag or '').startswith('build-') and tail.isdigit() else 0


def app_root():
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def install_kind(root=None, frozen=None, stamped=None):
    frozen = getattr(sys, 'frozen', False) if frozen is None else frozen
    stamped = BUILD_INFO is not None if stamped is None else stamped
    if frozen:
        return 'release' if stamped else 'local'
    return 'git' if (Path(root or app_root()) / '.git').exists() else 'local'


def _env(**extra):
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='never', **extra)
    for key in list(env):
        if key.startswith('_PYI') or key == '_MEIPASS2':
            env.pop(key, None)
    return env


def _run(args, cwd=None, timeout=60, input=None):
    p = subprocess.run(args, cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=timeout,
                       input=input, env=_env(), creationflags=NO_WINDOW)
    return p.returncode, (p.stdout or '').strip(), (p.stderr or '').strip()


def github_token():
    for key in ('GH_TOKEN', 'GITHUB_TOKEN'):
        if os.environ.get(key):
            return os.environ[key]
    try:
        code, out, _ = _run(['gh', 'auth', 'token'], timeout=15)
        if code == 0 and out:
            return out.splitlines()[0].strip()
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        code, out, _ = _run(['git', 'credential', 'fill'], timeout=15, input='protocol=https\nhost=github.com\n\n')
        if code == 0:
            for line in out.splitlines():
                if line.startswith('password='):
                    return line[len('password='):]
    except (OSError, subprocess.SubprocessError):
        pass
    return ''


class _NoAuthRedirect(urllib.request.HTTPRedirectHandler):
    """Asset downloads redirect to a signed storage URL; the token must not follow."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            new.remove_header('Authorization')
        return new


_OPENER = urllib.request.build_opener(_NoAuthRedirect)


def _request(url, token, accept='application/vnd.github+json'):
    headers = {'Accept': accept, 'User-Agent': 'NotionTool-updater', 'X-GitHub-Api-Version': '2022-11-28'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    return urllib.request.Request(url, headers=headers)


def spawn_detached(command, cwd):
    flags = 0x00000008 | 0x00000200 if os.name == 'nt' else 0  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    env = _env(PYINSTALLER_RESET_ENVIRONMENT='1')  # a new onefile process unpacks its own copy
    for extra in ((0x01000000, 0) if os.name == 'nt' else (0,)):  # CREATE_BREAKAWAY_FROM_JOB if allowed
        try:
            return subprocess.Popen([str(c) for c in command], cwd=str(cwd), env=env, creationflags=flags | extra,
                                    close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        except OSError:
            if extra == 0:
                raise


def wait_pid(pid, timeout):
    """True once the process has exited (or never existed)."""
    if os.name != 'nt':
        end = time.time() + timeout
        while time.time() < end:
            try:
                os.kill(pid, 0)
            except OSError:
                return True
            time.sleep(0.5)
        return False
    import ctypes
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.OpenProcess.restype, k.OpenProcess.argtypes = ctypes.c_void_p, [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    k.WaitForSingleObject.restype, k.WaitForSingleObject.argtypes = ctypes.c_uint32, [ctypes.c_void_p, ctypes.c_uint32]
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = k.OpenProcess(0x00100000, False, int(pid))  # SYNCHRONIZE
    if not handle:
        return True
    try:
        return k.WaitForSingleObject(handle, int(timeout * 1000)) == 0
    finally:
        k.CloseHandle(handle)


def _pop(args, flag):
    if flag not in args:
        return None
    index = args.index(flag)
    value = args[index + 1] if index + 1 < len(args) else ''
    del args[index:index + 2]
    return value


def handoff(argv):
    """Start-up hook: wait for the previous instance and install a staged EXE.

    --wait-for PID      wait until the old instance has exited (its instance lock is free)
    --apply-update EXE  this process is the downloaded EXE: copy itself over EXE,
                        start EXE with the remaining arguments and exit
    """
    args = list(argv)
    pid = _pop(args, '--wait-for')
    target = _pop(args, '--apply-update')
    if pid and pid.isdigit():
        wait_pid(int(pid), 120)
    if not target:
        return args
    target = Path(target)
    log = target.parent / 'data' / 'runtime' / 'update' / 'update.log'
    note = lambda text: _note(log, text)
    try:
        if target.is_file():
            shutil.copy2(target, target.with_name(target.name + '.bak'))
    except OSError as exc:
        note(f'backup failed: {exc}')
    swapped = False
    for _ in range(90):  # the onefile launcher of the old instance may hold the file a moment longer
        try:
            shutil.copy2(sys.executable, target)
            swapped = True
            break
        except OSError:
            time.sleep(1)
    note('swapped' if swapped else 'swap failed; starting the previous version')
    spawn_detached([target, *args[1:]], target.parent)
    sys.exit(0)


def _note(log, text):
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, 'a', encoding='utf-8') as f:
            f.write(time.strftime('[%Y-%m-%d %H:%M:%S] ') + text + '\n')
    except OSError:
        pass


class Updater:
    def __init__(self, app, kind=None):
        self.app = app
        self.store = app.store
        self.root = app_root()
        self.kind = kind or install_kind(self.root)
        self.staged = None
        self.stopped = False
        self.last_note = ''
        self.folder = Path(getattr(app, 'data_dir', None) or getattr(self.store, 'directory', 'data')) / 'runtime' / 'update'

    def enabled(self):
        return self.kind in ('release', 'git') and bool(self.store.settings().get('auto_update', True))

    def start(self):
        if self.kind in ('release', 'git'):
            threading.Thread(target=self.loop, daemon=True, name='auto-update').start()

    def event(self, kind, message):
        try:
            self.store.event(kind, '', message=message[:300])
        except Exception:
            pass

    def notice(self, kind, message):
        if message != self.last_note:  # one entry per distinct problem, not one every 30 minutes
            self.last_note = message
            self.event(kind, message)

    def loop(self):
        time.sleep(90)  # let the app, the bridge and the gateway settle first
        while not self.stopped:
            try:
                if self.enabled():
                    if self.staged is None:
                        self.staged = self.check()
                        if self.staged:
                            self.event('update_ready', 'Auto-update: ' + self.staged['label'] + ' is ready; it installs as soon as no coworker is working')
                    if self.staged is not None and self.idle():
                        self.apply()
            except Exception as exc:
                self.notice('update_failed', 'Auto-update: ' + str(exc)[:200])
            try:
                minutes = max(5, int(self.store.settings().get('update_check_minutes', CHECK_MINUTES) or CHECK_MINUTES))
            except (TypeError, ValueError):
                minutes = CHECK_MINUTES
            for _ in range(30 if self.staged else minutes * 60):
                if self.stopped:
                    return
                time.sleep(1)

    def idle(self):
        return not any(t.get('state') == 'running' for t in self.store.all('tasks'))

    def check(self):
        return self.check_release() if self.kind == 'release' else self.check_git()

    # ---- release EXE ---------------------------------------------------------
    def check_release(self):
        token = github_token()
        try:
            with _OPENER.open(_request(API + REPO + '/releases/latest', token), timeout=30) as response:
                release = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 404):
                raise RuntimeError('No access to the GitHub releases. The repository is private: sign in once with '
                                   '"gh auth login" or Git so NotionTool can download updates') from None
            raise RuntimeError(f'GitHub answered HTTP {exc.code}') from None
        tag = str(release.get('tag_name') or '')
        number = release_number(tag)
        if number <= build_number():
            return None
        assets = {a.get('name'): a for a in release.get('assets') or []}
        exe, checksum = assets.get(ASSET), assets.get(ASSET + '.sha256')
        if not exe or not checksum:
            raise RuntimeError(f'Release {tag} has no {ASSET} with checksum')
        with _OPENER.open(_request(checksum['url'], token, 'application/octet-stream'), timeout=60) as response:
            expected = response.read().decode('utf-8', 'replace').split()[0].strip().lower()
        self.folder.mkdir(parents=True, exist_ok=True)
        target = self.folder / f'NotionTool-{number}.exe'
        part = target.with_name(target.name + '.part')
        digest = hashlib.sha256()
        with _OPENER.open(_request(exe['url'], token, 'application/octet-stream'), timeout=120) as response, open(part, 'wb') as out:
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
        if digest.hexdigest() != expected:
            part.unlink(missing_ok=True)
            raise RuntimeError(f'Checksum mismatch for {tag}; the download was discarded')
        os.replace(part, target)
        for old in self.folder.glob('NotionTool-*.exe*'):
            if old != target:
                old.unlink(missing_ok=True)
        return dict(kind='release', path=str(target), label=tag)

    # ---- git checkout --------------------------------------------------------
    def git(self, *args, timeout=60):
        return _run(['git', *args], cwd=self.root, timeout=timeout)

    def check_git(self):
        code, _, err = self.git('fetch', '--quiet', 'origin', timeout=120)
        if code:
            raise RuntimeError('git fetch failed: ' + err[:160])
        _, head, _ = self.git('rev-parse', 'HEAD')
        _, branch, _ = self.git('rev-parse', '--abbrev-ref', 'HEAD')
        upstream = 'origin/' + (branch if branch and branch != 'HEAD' else 'main')
        _, remote, _ = self.git('rev-parse', upstream)
        if not remote or remote == head:
            return None
        if self.git('merge-base', '--is-ancestor', head, remote)[0]:
            self.notice('update_blocked', 'Auto-update skipped: this checkout has local commits that are not on ' + upstream)
            return None
        if self.git('status', '--porcelain', '--untracked-files=no')[1]:
            self.notice('update_blocked', 'Auto-update skipped: the source folder has local changes')
            return None
        return dict(kind='git', commit=remote, head=head, label=remote[:7])

    # ---- install and restart -------------------------------------------------
    def apply(self):
        staged = self.staged
        args = list(sys.argv[1:])
        if staged['kind'] == 'git':
            code, _, err = self.git('merge', '--ff-only', staged['commit'])
            if code:
                self.staged = None
                raise RuntimeError('fast-forward failed: ' + err[:160])
            _, changed, _ = self.git('diff', '--name-only', staged['head'], staged['commit'])
            if 'requirements.txt' in changed.split():
                _run([sys.executable, '-m', 'pip', 'install', '--quiet', '-r', 'requirements.txt'], cwd=self.root, timeout=900)
            python = Path(sys.executable)
            pythonw = python.with_name('pythonw.exe')
            command = [pythonw if pythonw.is_file() else python, self.root / 'main.py', '--wait-for', os.getpid(), *args]
        else:
            command = [staged['path'], '--apply-update', Path(sys.executable).resolve(), '--wait-for', os.getpid(), *args]
        self.event('update_applying', 'Auto-update: installing ' + staged['label'] + ' and restarting (the MCP gateway holds calls meanwhile)')
        spawn_detached(command, self.root)
        self.stopped = True
        self.exit_app()

    def exit_app(self):
        guard = threading.Timer(25, lambda: os._exit(0))
        guard.daemon = True
        guard.start()
        window = getattr(self.app, 'window', None)
        if window is not None:
            try:
                window.destroy()  # main() then runs app.close() and exits
                return
            except Exception:
                pass
        try:
            self.app.close()
        finally:
            os._exit(0)
