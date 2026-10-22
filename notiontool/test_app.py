"""Test app for Coworkers (B40): a second, isolated NotionTool started from the current source code.

It runs hidden with its own data folder (work/test-app-data, wiped on every fresh start) and its own
port (default 8791), with NOTIONTOOL_TEST_INSTANCE=1 so server.launch starts only the local API: no Notion
window, no gateway, no updater, no backups. The real app is never touched.

start() also sets NOTIONTOOL_FAKE_NOTION=1: server.launch then installs the simulated Notion
(fake_notion.py, state file <test-app-data>/fake_notion.json), seeds the simulated accounts and chats into
the store (boot_fake_notion) and runs the real loops that need Notion data (turn watch, main wake,
coworker upkeep). start(fake=False) gives the plain test instance exactly as before.

scenario(name) proves one behaviour end to end in the test app and returns verdict passed/failed with
the events it saw: restart_cut, too_large, deleted_chat, wake.
"""
import json, os, shutil, socket, sqlite3, subprocess, sys, threading, time, urllib.error, urllib.request
from pathlib import Path

DEFAULT_PORT = 8791
REAL_PORT = 8790  # the real NotionTool; the test app never binds, probes or stops it
BOOT_SECONDS = 60
CREATE_NO_WINDOW = 0x08000000
FAKE_ENV = 'NOTIONTOOL_FAKE_NOTION'
FAKE_READY_EVENT = 'fake_notion_ready'
_PROCS = {}  # pid -> Popen handle opened by start(); stop() waits for, kills and releases every one
# Stop flag: the scenario tests' deadline guard sets it after a timeout. While it is set start() opens no new
# process and _wait() returns at once, so a timed-out scenario thread cannot start an app behind the next test.
HALT = threading.Event()
TREE_KILL_SECONDS = 15  # fallback taskkill /T /F after kill() or after a hung taskkill
REMOVE_SECONDS = 10     # fresh start: retry removing the old data folder this long
_LAST_RESTART = {}      # pid, port, boot_seconds of the latest scenario restart (evidence for a failure)
# Every wait in this module is bounded (a hung tasklist/taskkill or child must never block a test run).
CMD_SECONDS = 15        # tasklist / taskkill
STOP_WAIT_SECONDS = 10  # graceful end of a process tree before kill()
KILL_WAIT_SECONDS = 5   # after kill()
PORT_FREE_SECONDS = 15  # until the stopped app's port no longer accepts connections
SCENARIOS = ('restart_cut', 'too_large', 'deleted_chat', 'wake')
# Upper bounds per scenario after the restart (turn watch checks every 30 s, grace 150 s for the cut).
SCENARIO_SECONDS = dict(restart_cut=150, too_large=110, deleted_chat=90, wake=90)
EVENT_FIELDS = ('message', 'thread_id', 'worker_id', 'account_id', 'run_id', 'run_ids', 'state', 'chat_id',
                'operation_id', 'reason', 'error', 'task_id', 'name', 'source')
WATCH_KINDS = ('circuit_open', 'turn_watch_waiting', 'chat_pool_error', 'crash', 'exception')


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


def _run(cmd, timeout=CMD_SECONDS):
    """subprocess.run with a hard timeout and no inherited stdin; stdout bytes, or None if it failed or hung.

    The old stop()/_alive() ran tasklist and taskkill without a timeout: one hung tasklist (WMI busy, the
    console of the dying app) blocked the whole unittest run forever although no test-app python was left.
    """
    try:
        return subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout,
                              creationflags=CREATE_NO_WINDOW if os.name == 'nt' else 0).stdout or b''
    except (subprocess.TimeoutExpired, OSError):
        return None


def _alive(pid):
    """Is pid a running test-app python? Own processes are asked through their Popen handle (no subprocess)."""
    if not pid:
        return False
    proc = _PROCS.get(pid)
    if proc is not None:
        return proc.poll() is None
    if os.name == 'nt':
        out = _run(['tasklist', '/FI', 'PID eq %d' % pid, '/NH', '/FO', 'CSV'])
        if out is None:
            return True  # unknown (tasklist hung): treat as running so stop() still ends it
        # tasklist prints in the OEM code page (umlauts broke the text decoding): compare bytes. CSV gives the
        # exact pid field; a pid reused by a non-python process is not ours and is never killed.
        return ('"%d"' % pid).encode('ascii') in out and b'python' in out.lower()
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _kill_tree(pid, timeout=CMD_SECONDS):
    """taskkill /PID pid /T /F (Windows) or SIGTERM; None when the command hung or could not run."""
    if os.name == 'nt':
        return _run(['taskkill', '/PID', str(pid), '/T', '/F'], timeout=timeout)
    try:
        os.kill(pid, 15)
        return b''
    except OSError:
        return None


def _children(pid):
    """Running child processes of pid (Windows: by ParentProcessId, bounded by CMD_SECONDS); [] if unknown."""
    if os.name != 'nt' or not pid:
        return []
    out = _run(['powershell', '-NoProfile', '-NonInteractive', '-Command',
                "Get-CimInstance Win32_Process -Filter 'ParentProcessId=%d' | ForEach-Object { $_.ProcessId }"
                % int(pid)])
    return [int(x) for x in (out or b'').split() if x.isdigit() and int(x) != os.getpid()]


def _leftover_children(pid):
    """Children of an ended test-app tree that still run (a console host may need a moment to go)."""
    kids = _children(pid)
    if kids:
        time.sleep(1.0)
        kids = _children(pid)
    return kids


def _close_pipes(proc):
    for pipe in (proc.stdin, proc.stdout, proc.stderr):
        if pipe is not None:
            try:
                pipe.close()
            except OSError:
                pass


def _end(pid, proc=None):
    """End one test-app process tree: terminate, wait (bounded), kill if still there, close its pipes.

    After proc.kill() or a hung taskkill, taskkill /PID pid /T /F runs once more (at most TREE_KILL_SECONDS)
    so no child of the tree survives. Returns (gone, leftover child pids that still run).
    """
    if proc is not None:
        ran = proc.poll() is None
        if ran:
            fallback = _kill_tree(pid) is None  # None: taskkill hung or could not run
            try:
                proc.wait(timeout=STOP_WAIT_SECONDS)
            except subprocess.TimeoutExpired:
                proc.kill()
                fallback = True
                try:
                    proc.wait(timeout=KILL_WAIT_SECONDS)
                except subprocess.TimeoutExpired:
                    pass
            if fallback:
                _kill_tree(pid, timeout=TREE_KILL_SECONDS)
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
        _close_pipes(proc)
        return proc.poll() is not None, _leftover_children(pid)
    if not _alive(pid):
        return True, []
    fallback = _kill_tree(pid) is None
    deadline = time.time() + STOP_WAIT_SECONDS
    while _alive(pid) and time.time() < deadline:  # release the database before it is reused
        time.sleep(0.3)
    if fallback or _alive(pid):
        _kill_tree(pid, timeout=TREE_KILL_SECONDS)
    return not _alive(pid), _leftover_children(pid)


def _port_free(port, seconds=PORT_FREE_SECONDS):
    """Wait (bounded) until nothing accepts connections on port. TIME_WAIT leftovers accept nothing and the
    app binds with SO_REUSEADDR, so they do not block the next start. Port 8790 is never probed."""
    port = int(port)
    if port == REAL_PORT:
        return True
    deadline = time.time() + seconds
    while True:
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.5):
                pass
        except OSError:
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.3)


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
    """Stop every test-app process start() opened plus the one recorded in work/test-app.json.

    Each process tree is terminated (taskkill /T /F on Windows) and waited for with a timeout, killed if it is
    still there, its pipes are closed and its port must stop accepting connections, so the next test can start.
    Every wait is bounded. Never touches this process, the real app or port 8790.
    """
    _, _, info, _ = _paths(root)
    st = _state(info)
    pid = st.get('pid')
    targets = {p: (proc, getattr(proc, 'test_app_port', None)) for p, proc in list(_PROCS.items())}
    if pid and pid not in targets:
        targets[pid] = (None, st.get('port'))
    left, ports = [], set()
    for p, (proc, port) in targets.items():
        if p == os.getpid():
            continue
        gone, kids = _end(p, proc)
        if gone:
            _PROCS.pop(p, None)  # reaped: no 'subprocess is still running' ResourceWarning left behind
        else:
            left.append(p)  # stays in _PROCS so the next stop() tries again
        left.extend(k for k in kids if k not in left)  # children of the tree that survived the tree kill
        if port and int(port) != REAL_PORT:
            ports.add(int(port))
    busy = [port for port in sorted(ports) if not _port_free(port)]
    try:
        Path(info).unlink()
    except OSError:
        pass
    return {'stopped': not left, 'pid': pid, 'pids': sorted(targets), 'left_running': left,
            'port_free': not busy, 'busy_ports': busy}


def _remove_data(data, seconds=REMOVE_SECONDS):
    """Fresh start: remove the old data folder, retrying (bounded) while a closing process still holds a file.
    Returns '' when it is gone, else the reason (the folder or notiontool.db is still there)."""
    data = Path(data)
    deadline = time.time() + seconds
    error = ''
    while True:
        try:
            if data.exists():
                shutil.rmtree(data)
        except OSError as exc:  # a file is still held open: retry until the deadline
            error = '%s: %s' % (type(exc).__name__, exc)
        if not data.exists():
            return ''
        if time.time() >= deadline:
            db = data / 'notiontool.db'
            return '%s still exists after %d s (%s)' % (db if db.exists() else data, seconds, error or 'no error')
        time.sleep(0.5)


def _last_seq(data):
    """Highest event seq in the test database (0 if there is none yet)."""
    db = Path(data) / 'notiontool.db'
    if not db.is_file():
        return 0
    try:
        con = sqlite3.connect(str(db), timeout=5)
        try:
            row = con.execute('SELECT MAX(seq) FROM events').fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return 0
    return int(row[0] or 0) if row else 0


def _ready_since(data, after_seq, began):
    """The fake_notion_ready event written by this boot: seq above the last one before the start and not older
    than the start; an older ready event of the previous boot never counts."""
    db = Path(data) / 'notiontool.db'
    if not db.is_file():
        return None
    try:
        con = sqlite3.connect(str(db), timeout=5)
        try:
            return con.execute('SELECT seq FROM events WHERE kind=? AND seq>? AND ts>=? ORDER BY seq DESC LIMIT 1',
                               (FAKE_READY_EVENT, int(after_seq), float(began) - 1)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None


def start(root=None, port=DEFAULT_PORT, boot_seconds=BOOT_SECONDS, fresh=True, fake=True):
    """Start the test app. fresh=False keeps the data folder (a restart); fake=False is the plain test app."""
    root, data, info, log = _paths(root)
    if HALT.is_set():
        return {'ok': False, 'reason': 'start refused: the stop flag is set (a scenario timed out)'}
    stop(root)
    db = data / 'notiontool.db'
    if fresh:
        gone = _remove_data(data)
        if gone:
            return {'ok': False, 'reason': 'fresh start: the old test data could not be removed: ' + gone}
    elif fake and not db.is_file():
        return {'ok': False, 'reason': 'restart refused: the expected test database %s is missing' % db}
    data.mkdir(parents=True, exist_ok=True)
    port = int(port or DEFAULT_PORT)
    if port == REAL_PORT:
        raise ValueError('port %d belongs to the real NotionTool; the test app never uses it' % REAL_PORT)
    port = _free(port)
    before = _last_seq(data)
    if HALT.is_set():  # set while stop() or the removal ran: open no process
        return {'ok': False, 'reason': 'start refused: the stop flag is set (a scenario timed out)', 'port': port}
    env = dict(os.environ, NOTIONTOOL_TEST_INSTANCE='1', PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1')
    env.pop('NOTIONTOOL_FAKE_STATE', None)  # the simulated state always lives in the test data folder
    if fake:
        env[FAKE_ENV] = '1'
    else:
        env.pop(FAKE_ENV, None)
    began = time.time()
    with open(log, 'w' if fresh else 'a', encoding='utf-8') as out:
        proc = subprocess.Popen([python_exe(), str(root / 'main.py'), '--serve', '--data-dir', str(data),
                                 '--port', str(port)], cwd=str(root), env=env, stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW if os.name == 'nt' else 0)
    # The log handle is closed here (end of the with block); the child keeps its own copy. stop() reaps proc.
    proc.test_app_port = port
    _PROCS[proc.pid] = proc
    Path(info).write_text(json.dumps({'pid': proc.pid, 'port': port, 'started': began, 'fake': bool(fake)}),
                          encoding='utf-8')
    while time.time() - began < boot_seconds:
        if proc.poll() is not None:
            return {'ok': False, 'reason': 'The test app exited while starting (code %s)' % proc.returncode,
                    'port': port, 'log_tail': _tail(log)}
        if HALT.is_set():  # a scenario timed out meanwhile: give up the boot, stop() reaps proc
            return {'ok': False, 'reason': 'boot abandoned: the stop flag was set', 'pid': proc.pid, 'port': port}
        if _serving(port) and (not fake or _ready_since(data, before, began)):
            if fake and not db.is_file():
                return {'ok': False, 'reason': 'the expected test database %s is gone after the boot' % db,
                        'pid': proc.pid, 'port': port, 'log_tail': _tail(log)}
            return {'ok': True, 'pid': proc.pid, 'port': port, 'boot_seconds': round(time.time() - began, 1),
                    'url': 'http://127.0.0.1:%d/' % port, 'fake_notion': bool(fake)}
        time.sleep(0.5)
    why = ('no %s event from this boot (last seq before the start: %d)' % (FAKE_READY_EVENT, before)
           if fake and _serving(port) else 'the test app did not answer')
    return {'ok': False, 'reason': '%s within %d s' % (why, boot_seconds), 'pid': proc.pid,
            'port': port, 'log_tail': _tail(log)}


def status(root=None):
    _, data, info, log = _paths(root)
    st = _state(info)
    pid, port = st.get('pid'), st.get('port')
    running = bool(pid and _alive(pid))
    return {'running': running, 'serving': bool(running and port and _serving(port)), 'pid': pid, 'port': port,
            'fake_notion': bool(st.get('fake')), 'startup_errors': startup_errors(data), 'log_tail': _tail(log, 10)}


def check(root=None, port=DEFAULT_PORT, settle_seconds=8, keep=False, fake=True):
    """Start the test app from the current code, let it run a moment, collect errors, stop it.

    verdict 'passed' only when it booted, still serves after settle_seconds and logged no failures.
    fake=True (default, like start()) boots it on the simulated Notion; fake=False is the plain test
    instance exactly as before (no accounts, no simulated Notion, no Notion-data loops).
    """
    root, data, _, log = _paths(root)
    started = start(root, port, fake=bool(fake))
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
            'log_tail': '' if passed else tail, 'kept_running': bool(keep), 'fake_notion': bool(fake)}


# --- simulated Notion inside the test app (called by server.launch in fake mode) ---------------------

def _outcome(status):
    """Same outcome mapping as EdgeTransport._chunks for the NetworkBudget."""
    if status == 429:
        return 'rate_limited'
    if status in (401, 403):
        return 'blocked'
    if status >= 500:
        return 'failed'
    if status >= 400:
        return 'invalid'
    return 'ok'


def _budget_transport(transport, budget):
    """Wrap one FakeTransport so every request/stream acquires and releases a real NetworkBudget ticket."""
    request, stream, account = transport.request, transport.stream, getattr(transport, 'account_key', '')

    def budgeted_request(path, body, headers=None, timeout=45, **kw):
        ticket, began = budget.acquire(None, source=path), time.monotonic()
        try:
            status, text = request(path, body, headers, timeout, **kw)
        except BaseException:
            budget.release(ticket, 'failed', account_id=account)
            raise
        if status == 429:
            transport.retry_after = budget.limited(transport.retry_after)
        budget.release(ticket, _outcome(status), status, time.monotonic() - began, account_id=account)
        return status, text

    def budgeted_stream(path, body, headers=None, timeout=60, cancel=None):
        ticket, began = budget.acquire(cancel, source=path), time.monotonic()
        outcome, status = 'failed', 0
        try:
            for line in stream(path, body, headers, timeout=timeout, cancel=cancel):
                yield line
            outcome, status = 'ok', 200
        except GeneratorExit:
            outcome = 'neutral'
            raise
        except BaseException as exc:
            status = int(getattr(exc, 'status', 0) or 0)
            outcome = _outcome(status) if status else 'failed'
            raise
        finally:
            budget.release(ticket, outcome, status, time.monotonic() - began, account_id=account)

    transport.request, transport.stream, transport.budgeted = budgeted_request, budgeted_stream, True


def route_through_budget(adapter):
    """Send the simulated connection through the adapter's real NetworkBudget (pacing, pause, circuit_open).

    EdgeTransport acquires a budget ticket per request; FakeTransport does not, so without this the
    circuit breaker could never open in fake mode. Returns True when the budget is in the path.
    """
    budget = getattr(adapter, 'network_budget', None)
    if budget is None or not hasattr(budget, 'acquire'):
        return False
    plain_client, lock = adapter.client, threading.Lock()

    def client(account_id, anonymous=False):
        made = plain_client(account_id, anonymous)
        transport = getattr(made, 'transport', None)
        with lock:
            if transport is not None and not getattr(transport, 'budgeted', False):
                _budget_transport(transport, budget)
        return made

    adapter.client = client
    return True


def boot_fake_notion(app):
    """Fake mode of the test app: simulated Notion, seeded store and the real Notion-data loops."""
    if getattr(app, 'test_instance', False) is not True:
        raise RuntimeError('Simulated Notion refused: only the test app (NOTIONTOOL_TEST_INSTANCE=1) may use it')
    from . import fake_notion
    from .coworker_upkeep import CoworkerUpkeep
    from .main_wake import MainWake
    state = fake_notion.install(app.adapter)
    app.fake_notion = state
    if route_through_budget(app.adapter):
        app.store.event('fake_notion_budget', message='Simulated requests go through the real NetworkBudget')
    added = fake_notion.seed_scenario(app.store, state)  # the one shared seed (same records as the unit tests)
    threading.Thread(target=app.turn_watch.loop, daemon=True, name='turn-watch').start()
    app.coworker_upkeep = CoworkerUpkeep(app.store, app.adapter, app.engine)
    threading.Thread(target=app.coworker_upkeep.loop, daemon=True, name='coworker-upkeep').start()
    app.main_wake = MainWake(app.store, app.adapter, app.engine)
    threading.Thread(target=app.main_wake.loop, daemon=True, name='main-wake').start()
    app.store.event(FAKE_READY_EVENT, message='Simulated Notion installed (%d accounts, %d workers seeded now)'
                    % (added['accounts'], added['workers']), source=str(state.path))
    return state


# --- scenarios ---------------------------------------------------------------------------------------

def _events(data, since=0.0, kinds=None):
    db = Path(data) / 'notiontool.db'
    if not db.is_file():
        return []
    try:
        con = sqlite3.connect(str(db), timeout=5)
        try:
            rows = con.execute('SELECT seq, ts, kind, doc FROM events WHERE ts>=? ORDER BY seq',
                               (float(since),)).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []
    out = []
    for seq, ts, kind, doc in rows:
        if kinds and kind not in kinds:
            continue
        try:
            fields = json.loads(doc)
        except ValueError:
            fields = {}
        item = dict(seq=seq, at=round(ts, 1), kind=kind)
        for name in EVENT_FIELDS:
            value = fields.get(name)
            if value not in (None, '', [], {}):
                item[name] = value[:200] if isinstance(value, str) else value
        out.append(item)
    return out


def _object(data, kind, ident):
    try:
        con = sqlite3.connect(str(Path(data) / 'notiontool.db'), timeout=5)
        try:
            row = con.execute('SELECT doc FROM objects WHERE kind=? AND id=?', (kind, str(ident))).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return json.loads(row[0]) if row else None


def _wait(probe, seconds, interval=2.0):
    """Poll probe until it returns something truthy or seconds pass (always bounded). A probe that fails for a
    moment (the app is replacing fake_notion.json, the database is busy) counts as 'not yet'."""
    deadline = time.time() + seconds
    while True:
        try:
            value = probe()
        except (OSError, ValueError, sqlite3.Error):
            value = None
        if value or time.time() >= deadline or HALT.is_set():  # stop flag: a timed-out scenario ends at once
            return value
        time.sleep(interval)


def _text(entry):
    content = entry.get('content')
    if isinstance(content, list):
        return ''.join(str(c.get('content', '')) for c in content if isinstance(c, dict))
    return str(content or entry.get('text') or '')


def _entries_since(fs, thread_id, since):
    thread = fs.thread(thread_id) or {}
    return [e for e in thread.get('entries', []) if int(e.get('created_at') or 0) >= int(since * 1000)]


def _answered(fs, thread_id, since, needle):
    """The user message containing needle that the app sent after since, if a reply followed it."""
    entries = _entries_since(fs, thread_id, since)
    for index, entry in enumerate(entries):
        if entry.get('kind') == 'user_message' and needle in _text(entry):
            if any(e.get('kind') == 'assistant_message' for e in entries[index + 1:]):
                return _text(entry)[:160]
    return None


def _api_key(data):
    from .vendor.sessions import SessionStore
    return (SessionStore(data_dir=str(Path(data) / 'sessions')).load_identity('host_api') or {}).get('key', '')


def _post(port, key, path, body, timeout=60):
    request = urllib.request.Request('http://127.0.0.1:%d%s' % (port, path), data=json.dumps(body).encode('utf-8'),
                                     method='POST', headers={'Authorization': 'Bearer ' + key,
                                                             'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b'{}')
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b'{}')
        except ValueError:
            return exc.code, {}


def _restart(root, port):
    result = start(root, port, fresh=False)
    _LAST_RESTART.clear()  # kept for the failure evidence of scenario()
    _LAST_RESTART.update({k: result.get(k) for k in ('ok', 'pid', 'port', 'boot_seconds', 'reason')})
    return result


def _result(passed, reasons, events, **details):
    return dict(passed=bool(passed), reasons=[] if passed else reasons, events=events, details=details)


def _sc_restart_cut(root, port, data, fs):
    """Main stopped after a tool result (no reply); after a restart the turn watch sends the continue."""
    from .fake_notion import _entity, fake_uuid
    main = fs.threads_of('ready', 'main')[0]['id']
    stop(root)
    now = time.time()
    step = fake_uuid('scenario', 'restart_cut', now)
    with fs.edit() as d:
        thread = d['threads'][main]
        thread['sequence'] += 1
        thread['entries'].append(_entity('user_message', step, thread['sequence'], now - 170,
                                         'Scenario restart_cut: run the build and report', client_message_id=step))
        thread['sequence'] += 1
        thread['entries'].append(dict(kind='tool_result', id=fake_uuid('scenario', 'tool', now),
                                      sequence=thread['sequence'], created_at=int((now - 165) * 1000),
                                      tool_use_id='toolu_scenario_restart_cut', result={'ok': True}))
        thread['session_status'] = 'idle'
        thread['latest_user_or_trigger_time'] = int((now - 170) * 1000)
    restarted = _restart(root, port)
    if not restarted.get('ok'):
        return _result(False, ['restart failed: ' + restarted.get('reason', '')], [])
    kinds = ('turn_interrupted', 'turn_stalled', 'turn_restart_continue', 'turn_restart_cut')
    wid = 'ready:' + main

    def probe():
        seen = [e for e in _events(data, now, kinds) if e.get('thread_id') == main or e.get('worker_id') == wid]
        sent = _answered(fs, main, now, 'Continue') or _answered(fs, main, now, 'Weiter')
        return (seen, sent) if seen and sent else None
    found = _wait(probe, SCENARIO_SECONDS['restart_cut'])
    events = _events(data, now, kinds + WATCH_KINDS)
    if not found:
        return _result(False, ['no automatic continue reached the interrupted Main within %d s'
                               % SCENARIO_SECONDS['restart_cut']], events)
    return _result(True, [], events, continue_message=found[1], main_thread=main)


def _sc_too_large(root, port, data, fs):
    """A Coworker transcript over the size limit is read with the smaller limit; no skip, no circuit_open."""
    cowork = fs.threads_of('ready', 'coworker')[0]['id']
    stop(root)
    fs.set_fault(cowork, 'too_large', max_limit=10)
    now = time.time()
    restarted = _restart(root, port)
    if not restarted.get('ok'):
        return _result(False, ['restart failed: ' + restarted.get('reason', '')], [])

    def probe():
        reads = [c for c in fs.calls('getThreadTranscript') if c.get('thread_id') == cowork and c.get('at', 0) >= now]
        return reads if len(reads) >= 2 else None
    reads = _wait(probe, SCENARIO_SECONDS['too_large'])
    time.sleep(3)  # let the scan finish and write its events
    # All app events since the restart plus the simulated transcript reads (the evidence for the fallback).
    events = _events(data, now) + [dict(kind='fake_getThreadTranscript', at=round(c.get('at', 0), 1),
                                        thread_id=c.get('thread_id')) for c in (reads or [])]
    skipped = [e for e in events if e['kind'] == 'turn_restart_cut_skipped' and cowork in json.dumps(e)]
    circuit = [e for e in events if e['kind'] == 'circuit_open']
    # circuit_open can only fire when the simulated requests pass the real NetworkBudget (route_through_budget).
    routed = bool(_events(data, 0.0, ('fake_notion_budget',)))
    circuit_check = 'checked' if routed else 'not_applicable: the simulated connection bypasses the NetworkBudget'
    reasons = []
    if not reads:
        reasons.append('the test app never retried the too-large Coworker transcript')
    if skipped:
        reasons.append('the transcript read failed (turn_restart_cut_skipped)')
    if circuit:
        reasons.append('circuit_open event seen')
    return _result(not reasons, reasons, events, transcript_reads=len(reads or []), coworker_thread=cowork,
                   circuit_open_check=circuit_check)


def _sc_deleted_chat(root, port, data, fs):
    """A private chat deleted in Notion: a new chat with the same name is accepted right away."""
    old = next(t for t in fs.threads_of('ready', 'private') if t['title'] == 'Project notes')
    stop(root)
    now = time.time()
    with fs.edit() as d:
        d['threads'][old['id']].update(alive=False, deleted_at=now)
    restarted = _restart(root, port)
    if not restarted.get('ok'):
        return _result(False, ['restart failed: ' + restarted.get('reason', '')], [])
    request_id = 'scenario-deleted-chat-%d' % int(now * 1000)
    code, answer = _post(restarted['port'], _api_key(data), '/api/app/chat/create',
                         dict(name='Project notes', main=False, request_id=request_id, account_id='ready'))
    ledger_id = str(answer.get('id') or 'appchat:' + request_id)

    def probe():
        ledger = _object(data, 'app_chats', ledger_id) or {}
        return ledger if ledger.get('state') in ('done', 'failed') else None
    ledger = (_wait(probe, SCENARIO_SECONDS['deleted_chat']) if code == 200 else None) or \
        _object(data, 'app_chats', ledger_id) or {}
    events = _events(data, now, ('app_chat_created', 'app_chat_name_refresh_skipped', 'operation') + WATCH_KINDS)
    old_worker = _object(data, 'workers', 'ready:' + old['id']) or {}
    created = [t for t in fs.threads_of('ready') if t['id'] != old['id'] and t['title'] == 'Project notes'
               and t.get('alive', True)]
    reasons = []
    if code != 200 or answer.get('error'):
        reasons.append('create refused (HTTP %s): %s' % (code, str(answer.get('error', ''))[:200]))
    if ledger.get('state') != 'done':
        reasons.append('chat creation ended in state %r: %s' % (ledger.get('state'), str(ledger.get('error', ''))[:200]))
    if not created:
        reasons.append('no new alive chat named Project notes in the simulated Notion')
    return _result(not reasons, reasons, events, http_status=code, ledger_state=ledger.get('state'),
                   old_worker_state=old_worker.get('state'), new_thread=(created[0]['id'] if created else ''))


def _sc_wake(root, port, data, fs):
    """A finished operator run: Main gets the cowork-finished message once."""
    from .store import Store
    try:
        from .main_wake import WAKE_HEAD
    except ImportError:
        WAKE_HEAD = 'Cowork run finished'
    main = fs.threads_of('ready', 'main')[0]['id']
    cowork = fs.threads_of('ready', 'coworker')[0]['id']
    stop(root)
    now = time.time()
    run_id = 'run_scenario_wake'
    store = Store(data)
    try:
        store.batch([('runs', dict(id=run_id, operator=True, state='done', created=now - 120, updated=now,
                                   title='Scenario wake', summary='Simulated cowork run finished', error='',
                                   account_id='ready', main_worker='ready:' + main)),
                     ('tasks', dict(id='task_scenario_wake', run_id=run_id, state='done', title='Simulated part',
                                    worker_id='ready:' + cowork, updated=now))])
    finally:
        store.close()
    restarted = _restart(root, port)
    if not restarted.get('ok'):
        return _result(False, ['restart failed: ' + restarted.get('reason', '')], [])
    needle = str(WAKE_HEAD).split(':')[-1].strip()[:40] or 'Cowork run finished'

    def probe():
        sent = [e for e in _events(data, now, ('main_wake_sent',))]
        reply = _answered(fs, main, now, needle)
        return (sent, reply) if sent and reply else None
    found = _wait(probe, SCENARIO_SECONDS['wake'])
    events = _events(data, now, ('main_wake_sent', 'main_wake_wait_busy', 'main_wake_failed') + WATCH_KINDS)
    if not found:
        return _result(False, ['Main did not get the cowork-finished message within %d s'
                               % SCENARIO_SECONDS['wake']], events)
    return _result(True, [], events, wake_message=found[1], main_thread=main)


SCENARIO_FUNCS = dict(restart_cut=_sc_restart_cut, too_large=_sc_too_large, deleted_chat=_sc_deleted_chat,
                      wake=_sc_wake)


def scenario(name, root=None, port=DEFAULT_PORT, keep=False):
    """Run one scenario in a fresh fake-mode test app; returns {'scenario', 'verdict', 'events', 'reasons', ...}."""
    name = str(name or '')
    if name not in SCENARIOS:
        raise ValueError('scenario name must be one of: ' + ', '.join(SCENARIOS))
    from .fake_notion import FakeState
    root, data, _, log = _paths(root)
    began = time.time()
    _LAST_RESTART.clear()
    first = start(root, port, fresh=True)
    if not first.get('ok'):
        tail = first.get('log_tail') or _tail(log)
        details = dict(first_start=first, events_since_start=_events(data, began), log_tail=tail)
        stop(root)
        return dict(scenario=name, verdict='failed', events=[], reasons=['the test app did not start: '
                    + str(first.get('reason', ''))], details=details, log_tail=tail)
    fs = FakeState(path=data / 'fake_notion.json')
    try:
        outcome = SCENARIO_FUNCS[name](root, first['port'], data, fs)
    except Exception as exc:
        outcome = _result(False, ['%s: %s' % (type(exc).__name__, exc)], _events(data, began, WATCH_KINDS))
    tail = _tail(log)
    if 'Traceback (most recent call last)' in tail and outcome['passed']:
        outcome = dict(outcome, passed=False, reasons=['traceback in the test app log'])
    if not outcome['passed']:  # evidence: both boots, every event since the scenario began, the log tail
        outcome = dict(outcome, details=dict(
            outcome.get('details') or {},
            first_start={k: first.get(k) for k in ('pid', 'port', 'boot_seconds')},
            restart=dict(_LAST_RESTART) or {'ok': None, 'reason': 'no restart was reached'},
            events_since_start=_events(data, began), log_tail=tail))
    if not keep:
        stop(root)
    return dict(scenario=name, verdict='passed' if outcome['passed'] else 'failed', events=outcome['events'],
                reasons=outcome['reasons'], details=outcome['details'], seconds=round(time.time() - began, 1),
                log_tail='' if outcome['passed'] else tail, kept_running=bool(keep))


def _flag(value, default):
    """Tool arguments may arrive as bool, number or text ('false', '0', 'no', 'off')."""
    if value is None or value == '':
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ('0', 'false', 'no', 'off')
    return bool(value)


def run(action='check', **kw):
    """nt_test_app entry. fake defaults to True (simulated Notion); fake=False is the plain test instance."""
    action = str(action or 'check')
    port = kw.get('port') or DEFAULT_PORT
    fake, keep = _flag(kw.get('fake'), True), _flag(kw.get('keep'), False)
    if action == 'start':
        return start(port=port, fake=fake)
    if action == 'stop':
        return stop()
    if action == 'status':
        return status()
    if action == 'check':
        return check(port=port, keep=keep, fake=fake)
    if action == 'scenario':
        if not fake:
            raise ValueError('scenarios need the simulated Notion: fake must be true')
        return scenario(kw.get('name'), port=port, keep=keep)
    raise ValueError('action must be start, status, stop, check or scenario')
