"""Safe restart of NotionTool (Settings tab card 'Restart NotionTool').

POST /api/app/restart {mode} and the overlay actions app_restart / app_restart_status / app_health.

- mode 'idle' (default): new task starts are paused (engine._choose_worker returns no worker while the
  hold is on), then the restart runs once no task is running, uncertain, preparing, cancelling or at a
  checkpoint. Nothing is cancelled and nothing is sent again (I5).
- mode 'now': restarts right away. The overlay first lists the running tasks and warns that their
  answers may need manual checking.
- mode 'cancel': stops a 'When idle' wait and lets tasks start again.
- Only one restart at a time: a request while waiting or restarting is refused.
- Before the restart: a backup (backup.before_restart, if that module is present) and an
  'app_restart' note in the event log. Then work/_restart_bg.ps1 is started detached and hidden.
  The launcher is injectable; tests never start a real process.
"""
import os
import subprocess
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / 'work' / '_restart_bg.ps1'
BUSY_STATES = ('running', 'uncertain', 'preparing', 'cancelling', 'checkpoint')
MODES = ('idle', 'now', 'cancel')
POLL_SECONDS = 2.0
DESK_ACTION_NAMES = ('app_restart', 'app_restart_status', 'app_health')


def restart_argv(script=SCRIPT):
    """powershell command that starts the restart script in its own hidden PowerShell."""
    inner = '-NoProfile -ExecutionPolicy Bypass -File "' + str(script) + '"'
    command = "Start-Process powershell -WindowStyle Hidden -ArgumentList '" + inner.replace("'", "''") + "'"
    return ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden',
            '-Command', command]


def launch(argv):
    """Start argv detached from this process (same pattern as devtools.nt_restart_app)."""
    flags = 0
    if os.name == 'nt':
        flags = getattr(subprocess, 'DETACHED_PROCESS', 0x8) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x200)
    proc = subprocess.Popen(argv, cwd=str(ROOT), creationflags=flags, close_fds=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return getattr(proc, 'pid', None)


def _thread(fn):
    threading.Thread(target=fn, name='nt-restart-wait', daemon=True).start()


def running_tasks(store):
    """Tasks that are still busy: [{id, title, state}]."""
    out = []
    try:
        tasks = store.all('tasks') or []
    except Exception:
        return out
    for task in tasks:
        if isinstance(task, dict) and task.get('state') in BUSY_STATES:
            out.append(dict(id=task.get('id', ''), title=str(task.get('title') or '')[:120], state=task['state']))
    return out


class RestartController:
    def __init__(self, server, *, launcher=launch, backup=None, spawn=_thread, sleep=time.sleep,
                 clock=time.time, poll=POLL_SECONDS, script=SCRIPT):
        self.server = server
        self.launcher = launcher
        self.backup = backup
        self.spawn = spawn
        self.sleep = sleep
        self.clock = clock
        self.poll = poll
        self.script = script
        self.lock = threading.RLock()
        self.state = 'idle'      # idle | waiting | restarting | failed
        self.mode = ''
        self.holding = False
        self.requested_at = None
        self.error = ''
        self.backup_name = ''
        self._hooked = False

    # ---- hold: no new task starts while a 'When idle' restart waits ----
    def _install_hold(self):
        engine = getattr(self.server, 'engine', None)
        original = getattr(engine, '_choose_worker', None) if engine is not None else None
        if original is None or self._hooked:
            return
        controller = self

        def choose_worker(*args, **kwargs):
            if controller.holding:
                return None
            return original(*args, **kwargs)
        choose_worker.nt_restart_hold = True
        engine._choose_worker = choose_worker
        self._hooked = True

    def _release_hold(self):
        self.holding = False
        engine = getattr(self.server, 'engine', None)
        if self._hooked and engine is not None:
            current = getattr(engine, '__dict__', {}).get('_choose_worker')
            if getattr(current, 'nt_restart_hold', False):
                del engine.__dict__['_choose_worker']
        self._hooked = False

    def _event(self, kind, message, **fields):
        try:
            self.server.store.event(kind, message=message, **fields)
        except Exception:
            pass

    # ---- public ----
    def status(self):
        with self.lock:
            return dict(ok=True, state=self.state, mode=self.mode, holding=self.holding,
                        requested_at=self.requested_at, error=self.error, backup=self.backup_name,
                        running=running_tasks(self.server.store))

    def request(self, mode='idle'):
        mode = str(mode or 'idle').strip().lower()
        if mode not in MODES:
            raise ValueError('Unknown restart mode: ' + mode)
        with self.lock:
            if mode == 'cancel':
                if self.state != 'waiting':
                    return dict(self.status(), ok=False, error='No restart is waiting')
                self._release_hold()
                self.state, self.mode = 'idle', ''
                self._event('app_restart_cancelled', 'Restart cancelled; tasks can start again')
                return self.status()
            if self.state in ('waiting', 'restarting'):
                return dict(self.status(), ok=False, already=True, error='A restart is already in progress')
            self.mode, self.error, self.backup_name = mode, '', ''
            self.requested_at = self.clock()
            if mode == 'now':
                return self._restart(running_tasks(self.server.store))
            self.state = 'waiting'
            self.holding = True
            self._install_hold()
            self._event('app_restart_waiting', 'Restart requested: new tasks are paused until running tasks finish')
            if not running_tasks(self.server.store):
                return self._restart([])
            result = self.status()
        self.spawn(self._wait_loop)
        return result

    def check(self):
        """One step of the 'When idle' wait. True once the wait is over."""
        with self.lock:
            if self.state != 'waiting':
                return True
            if running_tasks(self.server.store):
                return False
            self._restart([])
            return True

    def _wait_loop(self):
        while not self.check():
            self.sleep(self.poll)

    def _restart(self, busy):
        """Backup, event note, launch. Caller holds self.lock."""
        self.state = 'restarting'
        try:
            self.backup_name = self._backup() or ''
        except Exception:
            self.backup_name = ''
        message = 'NotionTool restart (' + ('now' if self.mode == 'now' else 'when idle') + ')'
        if busy:
            message += '; answers may need manual checking: ' + ', '.join(t['title'] or t['id'] for t in busy)[:300]
        self._event('app_restart', message, mode=self.mode, running=[t['id'] for t in busy],
                    backup=self.backup_name)
        try:
            pid = self.launcher(restart_argv(self.script))
        except Exception as exc:  # noqa: BLE001 - reported to the overlay, tasks start again
            self.state = 'failed'
            self.error = 'The restart could not be started: ' + str(exc)[:200]
            self._release_hold()
            self._event('app_restart_failed', self.error)
            return dict(self.status(), ok=False)
        return dict(self.status(), started=True, pid=pid, running=busy)

    def _backup(self):
        backup = self.backup
        if backup is None:
            try:
                from . import backup as backup_module
            except Exception:
                return ''
            backup = getattr(backup_module, 'before_restart', None)
            if backup is None:
                return ''
        info = backup(self.server)
        if isinstance(info, dict):
            return str(info.get('name') or info.get('path') or '')
        return ''


def controller(server, **kwargs):
    """The single RestartController of this server (created on first use)."""
    ctl = getattr(server, '_nt_restart', None)
    if ctl is None:
        ctl = RestartController(server, **kwargs)
        server._nt_restart = ctl
    return ctl


def handle(server, body):
    """POST /api/app/restart {mode}; {status: true} only reads the state."""
    body = body if isinstance(body, dict) else {}
    if body.get('status'):
        return controller(server).status()
    return controller(server).request(body.get('mode') or 'idle')


def desk_handlers(server):
    """Overlay actions: app_restart, app_restart_status, app_health."""
    return {'app_restart': lambda args: controller(server).request((args or {}).get('mode') or 'idle'),
            'app_restart_status': lambda args: controller(server).status(),
            'app_health': lambda args: server.health()}


def install_desk_actions(desktop_shell=None):
    """Add the restart actions to desktop_shell (DESK_ACTIONS + desktop_handlers) at runtime.

    desktop_shell.py is owned by other work, so this hooks in without editing it. Idempotent.
    """
    if desktop_shell is None:
        from . import desktop_shell
    missing = tuple(a for a in DESK_ACTION_NAMES if a not in desktop_shell.DESK_ACTIONS)
    if missing:
        desktop_shell.DESK_ACTIONS = tuple(desktop_shell.DESK_ACTIONS) + missing
    original = desktop_shell.desktop_handlers
    if getattr(original, 'nt_restart_actions', False):
        return desktop_shell

    def desktop_handlers(server, *args, **kwargs):
        handlers = original(server, *args, **kwargs)
        handlers.update(desk_handlers(server))
        return handlers
    desktop_handlers.nt_restart_actions = True
    desktop_shell.desktop_handlers = desktop_handlers
    return desktop_shell
