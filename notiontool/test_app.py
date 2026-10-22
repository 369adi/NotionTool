"""Test app for Coworkers (B40): a second, isolated NotionTool started from the current source code.

It runs hidden with its own empty data folder (work/test-app-data, wiped on every start) and its own
port (default 8791), with NOTIONTOOL_TEST_INSTANCE=1 so server.launch starts only the local API: no Notion
window, no accounts, no gateway, no updater, no watches. A fix is proven when the test app boots from the
changed code and serves HTTP without startup errors. The real app is never touched.
"""
import json, os, shutil, socket, sqlite3, subprocess, sys, time, urllib.error, urllib.request
from pathlib import Path

DEFAULT_PORT = 8791
BOOT_SECONDS = 60
CREATE_NO_WINDOW = 0x08000000


def _root():
    from .devtools import ROOT
    return Path(ROOT)


def _paths(root=None):
    root = Path(root or _root())
    work = root / 'work'
    return root, work / 'test-app-data', work / 'test-app.json', work / 'test-app.log'


def python_exe():
    """A real Python for running the source (the frozen EXE cannot run main.py)."""
    env = os.environ.get('NOTIONTOOL_PYTHON')
    if env and Path(env).is_file():
        return env
    if not getattr(sys, 'frozen', False):
        return sys.executable
    found = shutil.which('python') or shutil.which('py')
    if found and 'WindowsApps' not in found:
        return found
    guess = Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs' / 'Python' / 'Python312' / 'python.exe'
    if guess.is_file():
        return str(guess)
    raise RuntimeError('No Python found for the test app; set NOTIONTOOL_PYTHON')


def _free(port):
    with socket.socket() as s:
        try:
            s.bind(('127.0.0.1', port))
            return port
        except OSError:
            s.bind(('127.0.0.1', 0))
            return s.getsockname()[1]


def _serving(port, timeout=2.0):
    try:
        urllib.request.urlopen('http://127.0.0.1:%d/' % port, timeout=timeout).read(64)
        return True
    except urllib.error.HTTPError:
        return True  # any HTTP answer means the server runs
    except Exception:
        return False


def _alive(pid):
    if not pid:
        return False
    if os.name == 'nt':
        out = subprocess.run(['tasklist', '/FI', 'PID eq %d' % pid, '/NH'], capture_output=True, text=True,
                             creationflags=CREATE_NO_WINDOW).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _tail(log, lines=25):
    try:
        return '\n'.join(Path(log).read_text(encoding='utf-8', errors='replace').splitlines()[-lines:])[-3000:]
    except OSError:
        return ''


def _state(info):
    try:
        return json.loads(Path(info).read_text(encoding='utf-8'))
    except Exception:
        return {}


def startup_errors(data):
    """Failure events the test app wrote while booting (its own fresh database)."""
    db = Path(data) / 'notiontool.db'
    if not db.is_file():
        return []
    try:
        con = sqlite3.connect(str(db), timeout=5)
        rows = con.execute('SELECT kind, doc FROM events ORDER BY seq').fetchall()
        con.close()
    except sqlite3.Error as exc:
        return ['database: %s' % exc]
    bad = []
    for kind, doc in rows:
        if kind.endswith('_failed') or kind.endswith('_error') or kind in ('crash', 'exception'):
            try:
                msg = json.loads(doc).get('message', '')
            except Exception:
                msg = ''
            bad.append('%s: %s' % (kind, str(msg)[:200]))
    return bad[:10]


def stop(root=None):
    _, _, info, _ = _paths(root)
    pid = _state(info).get('pid')
    if pid and _alive(pid):
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], capture_output=True,
                           creationflags=CREATE_NO_WINDOW)
        else:
            os.kill(pid, 15)
    try:
        Path(info).unlink()
    except OSError:
        pass
    return {'stopped': True, 'pid': pid}


def start(root=None, port=DEFAULT_PORT, boot_seconds=BOOT_SECONDS):
    root, data, info, log = _paths(root)
    stop(root)
    shutil.rmtree(data, ignore_errors=True)
    data.mkdir(parents=True, exist_ok=True)
    port = _free(int(port or DEFAULT_PORT))
    env = dict(os.environ, NOTIONTOOL_TEST_INSTANCE='1', PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1')
    with open(log, 'w', encoding='utf-8') as out:
        proc = subprocess.Popen([python_exe(), str(root / 'main.py'), '--serve', '--data-dir', str(data),
                                 '--port', str(port)], cwd=str(root), env=env, stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW if os.name == 'nt' else 0)
    Path(info).write_text(json.dumps({'pid': proc.pid, 'port': port, 'started': time.time()}), encoding='utf-8')
    began = time.time()
    while time.time() - began < boot_seconds:
        if proc.poll() is not None:
            return {'ok': False, 'reason': 'The test app exited while starting (code %s)' % proc.returncode,
                    'port': port, 'log_tail': _tail(log)}
        if _serving(port):
            return {'ok': True, 'pid': proc.pid, 'port': port, 'boot_seconds': round(time.time() - began, 1),
                    'url': 'http://127.0.0.1:%d/' % port}
        time.sleep(0.5)
    return {'ok': False, 'reason': 'The test app did not answer within %d s' % boot_seconds, 'pid': proc.pid,
            'port': port, 'log_tail': _tail(log)}


def status(root=None):
    _, data, info, log = _paths(root)
    st = _state(info)
    pid, port = st.get('pid'), st.get('port')
    running = bool(pid and _alive(pid))
    return {'running': running, 'serving': bool(running and port and _serving(port)), 'pid': pid, 'port': port,
            'startup_errors': startup_errors(data), 'log_tail': _tail(log, 10)}


def check(root=None, port=DEFAULT_PORT, settle_seconds=8, keep=False):
    """Start the test app from the current code, let it run a moment, collect errors, stop it.

    verdict 'passed' only when it booted, still serves after settle_seconds and logged no failures.
    """
    root, data, _, log = _paths(root)
    started = start(root, port)
    if not started.get('ok'):
        stop(root)
        return dict(started, verdict='failed')
    time.sleep(settle_seconds)
    serving = _serving(started['port'])
    errors = startup_errors(data)
    tail = _tail(log)
    traceback = 'Traceback (most recent call last)' in tail
    if not keep:
        stop(root)
    passed = serving and not errors and not traceback
    reasons = ([] if serving else ['stopped answering']) + errors + (['traceback in the log'] if traceback else [])
    return {'verdict': 'passed' if passed else 'failed', 'port': started['port'],
            'boot_seconds': started.get('boot_seconds'), 'reasons': reasons,
            'log_tail': '' if passed else tail, 'kept_running': bool(keep)}


def run(action='check', **kw):
    action = str(action or 'check')
    if action == 'start':
        return start(port=kw.get('port') or DEFAULT_PORT)
    if action == 'stop':
        return stop()
    if action == 'status':
        return status()
    if action == 'check':
        return check(port=kw.get('port') or DEFAULT_PORT, keep=bool(kw.get('keep')))
    raise ValueError('action must be start, status, stop or check')
