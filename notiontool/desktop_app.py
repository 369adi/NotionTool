"""NotionTool desktop app launcher (op-fix-19, see docs/DESKTOP_APP.md).

Starts a SEPARATE Notion desktop instance with our own profile and a DevTools port on
127.0.0.1, finds its Notion page target, keeps one persistent CdpSession on it and injects the
desktop overlay into the current and every new document.

Safety rules:
- The user's normal Notion client and profile are never touched: only the pid this object
  spawned is ever terminated; is_our_process() checks a command line before any external stop.
- DevTools is only reached on 127.0.0.1; WebSocket URLs for other hosts are refused.
- Injected sources are checked against notion_shell.INJECT_MARKERS (credential markers and the
  local host address) and the secrets handed in; a match is never sent.
- spawn, http_get, connect, terminate, sleep and clock are injectable; tests use fakes only.
"""
import json
import subprocess
import time
import urllib.request
from urllib.parse import urlsplit

from .notion_shell import BINDING, INJECT_MARKERS, CdpSession

HOST = '127.0.0.1'
NOTION_HOSTS = ('notion.so', 'www.notion.so', 'notion.com', 'www.notion.com', 'app.notion.com')
# Login, blank and handoff pages are not a workspace page: never attach the overlay there.
NON_WORKSPACE_PATHS = ('/blank', '/login', '/logout', '/signup', '/desktopwithbrowserlogin', '/onboarding')
POLL_INTERVAL = 0.25


def _default_spawn(argv):
    return subprocess.Popen(argv, shell=False, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _default_http_get(url):
    """JSON from the local DevTools endpoint, or None while it is not up."""
    if urlsplit(url).hostname != HOST:
        raise ValueError('DevTools is only reached on ' + HOST)
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return json.loads(response.read().decode('utf-8'))
    except (OSError, ValueError):
        return None


def _default_connect(ws_url):
    return CdpSession(ws_url)


def is_notion_page(target):
    if not isinstance(target, dict) or target.get('type') != 'page':
        return False
    parts = urlsplit(str(target.get('url') or ''))
    if parts.scheme != 'https' or (parts.hostname or '').lower() not in NOTION_HOSTS:
        return False
    path = (parts.path or '').rstrip('/').lower()
    return not any(path == p or path.startswith(p + '/') for p in NON_WORKSPACE_PATHS)


def local_ws(ws_url, port):
    parts = urlsplit(str(ws_url or ''))
    return parts.scheme == 'ws' and parts.hostname == HOST and parts.port == port


class DesktopApp:
    def __init__(self, exe_path, profile_dir, port=9333, *, spawn=None, http_get=None, connect=None,
                 terminate=None, sleep=time.sleep, clock=time.monotonic, secrets=()):
        self.exe_path, self.profile_dir, self.port = str(exe_path), str(profile_dir), int(port)
        self._spawn = spawn or _default_spawn
        self._http_get = http_get or _default_http_get
        self._connect = connect or _default_connect
        self._terminate = terminate
        self._sleep, self._clock = sleep, clock
        self.secrets = tuple(s for s in secrets if s)
        self.proc = None
        self.pid = None
        self.session = None
        self.target_id = ''
        self.ws_url = ''
        self.script_id = ''
        self._closed = False

    def argv(self):
        return [self.exe_path, '--user-data-dir=' + self.profile_dir, '--remote-debugging-port=' + str(self.port)]

    def _url(self, path):
        return 'http://%s:%d%s' % (HOST, self.port, path)

    def info(self, ready):
        return dict(pid=self.pid, port=self.port, target_id=self.target_id, ws_url=self.ws_url, ready=ready)

    def start(self, timeout=30):
        """Spawn the instance (once), wait for DevTools, attach to the first Notion page."""
        if self._closed:
            raise RuntimeError('This desktop app was closed; create a new one')
        if self.proc is None:
            self.proc = self._spawn(self.argv())
            self.pid = getattr(self.proc, 'pid', None)
        deadline = self._clock() + timeout
        while not isinstance(self._http_get(self._url('/json/version')), dict):
            if self._clock() >= deadline:
                raise TimeoutError('Notion DevTools did not answer on port %d within %ss' % (self.port, timeout))
            self._sleep(POLL_INTERVAL)
        if self.session is not None and self.alive:
            return self.info(True)
        targets = self._http_get(self._url('/json/list'))
        target = next((t for t in (targets if isinstance(targets, list) else [])
                       if is_notion_page(t) and local_ws(t.get('webSocketDebuggerUrl'), self.port)), None)
        if target is None:
            self.target_id, self.ws_url = '', ''
            return self.info(False)
        self.target_id = str(target.get('id') or '')
        self.ws_url = str(target['webSocketDebuggerUrl'])
        self.session = self._connect(self.ws_url)
        return self.info(True)

    def _leaks(self, text):
        return any(m in text for m in INJECT_MARKERS) or any(s in text for s in self.secrets)

    def inject(self, source):
        """Register binding and overlay for new documents and run it in the current one."""
        if not self.alive:
            raise RuntimeError('No open DevTools session; call start() first')
        source = str(source or '')
        if not source.strip():
            raise ValueError('Empty overlay source')
        if self._leaks(source):
            raise ValueError('The overlay contains a credential marker or the local host address; it is not injected')
        session = self.session
        session.send('Page.enable')
        session.send('Runtime.enable')
        session.send('Runtime.addBinding', {'name': BINDING})
        result = session.send('Page.addScriptToEvaluateOnNewDocument', {'source': source}) or {}
        self.script_id = str(result.get('identifier', ''))
        session.send('Runtime.evaluate', {'expression': source})
        return self.script_id

    @property
    def alive(self):
        return self.session is not None and not getattr(self.session, 'closed', False) and not self._closed

    def close(self):
        """Close the session once and stop only the process this object spawned. Idempotent."""
        if self._closed:
            return
        self._closed = True
        session, self.session = self.session, None
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
        if self.pid is not None and self._terminate is not None:
            try:
                self._terminate(self.pid)
            except Exception:
                pass

    @staticmethod
    def is_our_process(cmdline, profile_dir):
        """True only if our profile dir appears in the command line (case-insensitive)."""
        if isinstance(cmdline, (list, tuple)):
            cmdline = ' '.join(str(part) for part in cmdline)
        line, profile = str(cmdline or '').lower(), str(profile_dir or '').strip().lower()
        return bool(profile) and profile in line
