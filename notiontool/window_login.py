"""Sign the NotionTool window in without the notion:// deep link (2026-10-21).

The Store (MSIX) Notion receives every notion:// link from Windows; a registry handler is ignored,
so 'Continue in desktop app' always lands in the user's normal Notion. Instead:
1. A small hook on the window's login page catches the click on 'Continue in browser'.
2. NotionTool opens its own Edge window on the Notion login page (browser.Browser).
3. When a token_v2 cookie shows up there, the Notion cookies are copied into the NotionTool
   window via DevTools (Network.setCookies) and the window loads the workspace.
Cookies are never logged or written to events. All I/O is injectable for tests.
"""
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
LOGIN_URL = 'https://www.' + 'notion.so/login'
HOME_URL = 'https://app.' + 'notion.com/'  # Notion moved the app to app.notion.com; token_v2 lives there
NOTION_COOKIE_DOMAINS = ('notion.so', 'notion.com')
LOGIN_PATHS = ('/login', '/signup', '/desktopwithbrowserlogin', '/logout')
FLAG = '__ntLoginRequested'
BUTTON_TEXTS = ('continue in browser', 'im browser fortfahren', 'weiter im browser',
                # Add account -> Google: Notion would open the default browser (user 2026-10-21)
                'continue with google', 'sign in with google', 'log in with google',
                'mit google fortfahren', 'weiter mit google', 'mit google anmelden')

HOOK = r"""(() => {
  if (window.__ntLoginHook) return 'present';
  window.__ntLoginHook = true;
  const texts = %s;
  document.addEventListener('click', (event) => {
    const el = event.target && event.target.closest && event.target.closest('[role=button],button,a');
    if (!el) return;
    const text = (el.innerText || el.textContent || '').trim().toLowerCase();
    if (!texts.some((t) => text.includes(t))) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    window.%s = Date.now();
    let note = document.getElementById('__ntLoginNote');
    if (!note) {
      note = document.createElement('div');
      note.id = '__ntLoginNote';
      note.style.cssText = 'position:fixed;left:50%%;top:16px;transform:translateX(-50%%);z-index:2147483647;' +
        'background:#2383e2;color:#fff;font:600 14px system-ui;padding:10px 16px;border-radius:8px;' +
        'box-shadow:0 4px 16px rgba(0,0,0,.3)';
      document.body.appendChild(note);
    }
    note.textContent = 'NotionTool: sign in in the Edge window that just opened. Google works there too. This window signs in by itself (close Edge if nothing happens).';
  }, true);
  return 'installed';
})()""" % (repr(list(BUTTON_TEXTS)).replace("'", '"'), FLAG)

# 'Add an account' dialog inside the signed-in window (user 2026-10-21): catch only its Google tile.
ADD_HOOK = r"""(() => {
  if (window.__ntAddAccountHook) return 'present';
  window.__ntAddAccountHook = true;
  const titles = ['add an account', 'konto hinzuf'];
  const tile = (event) => {
    const el = event.target && event.target.closest && event.target.closest('[role=button],button,a');
    if (!el || (el.innerText || el.textContent || '').trim().toLowerCase() !== 'google') return null;
    for (let box = el.parentElement, i = 0; box && i < 20; box = box.parentElement, i++) {
      const text = (box.innerText || '').toLowerCase();
      if (titles.some((t) => text.includes(t))) return el;
    }
    return null;
  };
  for (const kind of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
    document.addEventListener(kind, (event) => {
      if (!tile(event)) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      if (kind !== 'click') return;
      window.%s = Date.now();
      let note = document.getElementById('__ntLoginNote');
      if (!note) {
        note = document.createElement('div');
        note.id = '__ntLoginNote';
        note.style.cssText = 'position:fixed;left:50%%;top:16px;transform:translateX(-50%%);z-index:2147483647;' +
          'background:#2383e2;color:#fff;font:600 14px system-ui;padding:10px 16px;border-radius:8px;' +
          'box-shadow:0 4px 16px rgba(0,0,0,.3)';
        document.body.appendChild(note);
      }
      note.textContent = 'NotionTool: sign in with Google in the Edge window that just opened. This window signs in by itself.';
    }, true);
  }
  return 'installed';
})()""" % FLAG


def is_notion_cookie(cookie):
    return str(cookie.get('domain', '')).lstrip('.').lower().endswith(NOTION_COOKIE_DOMAINS)


def has_token(cookies):
    return any(c.get('name') == 'token_v2' and c.get('value') for c in cookies)


def to_cookie_params(cookies):
    """Storage.getCookies entries -> Network.setCookies parameters."""
    out = []
    for c in cookies:
        if not is_notion_cookie(c) or not c.get('name'):
            continue
        p = dict(name=c['name'], value=str(c.get('value', '')), domain=c.get('domain', ''),
                 path=c.get('path') or '/', secure=bool(c.get('secure')), httpOnly=bool(c.get('httpOnly')))
        if c.get('sameSite') in ('Strict', 'Lax', 'None'):
            p['sameSite'] = c['sameSite']
        expires = c.get('expires')
        if isinstance(expires, (int, float)) and expires > 0 and not c.get('session'):
            p['expires'] = expires
        out.append(p)
    return out


def window_cookies(port=9333, http_get=None, connect=None):
    """Notion cookies of the signed-in NotionTool window (read over its own local CDP port)."""
    from urllib.parse import urlsplit
    from .desktop_app import _default_http_get, _default_connect, local_ws
    http_get = http_get or _default_http_get
    connect = connect or _default_connect
    for target in http_get('http://127.0.0.1:%d/json/list' % int(port)) or []:
        host = (urlsplit(str(target.get('url') or '')).hostname or '').lower()
        if target.get('type') != 'page' or not host.endswith(('notion.so', 'notion.com')):
            continue
        if not local_ws(target.get('webSocketDebuggerUrl'), int(port)):
            continue
        session = connect(target['webSocketDebuggerUrl'])
        try:
            cookies = (session.send('Network.getAllCookies', {}) or {}).get('cookies') or []
        finally:
            session.close()
        cookies = [c for c in cookies if is_notion_cookie(c)]
        if has_token(cookies):
            return cookies
    raise ValueError('The NotionTool window is not signed in')


def page_path(target):
    return (urlsplit(str(target.get('url') or '')).path or '').rstrip('/').lower()


def login_target(targets):
    """The window's login page target, or None (signed in or no window)."""
    from .desktop_app import is_notion_page
    pages = [t for t in (targets or []) if isinstance(t, dict) and t.get('type') == 'page']
    # A login page next to workspace tabs = 'Add account': hook it too (before 2026-10-21 it was
    # skipped, so Google opened the default browser).
    for t in pages:
        host = (urlsplit(str(t.get('url') or '')).hostname or '').lower()
        if host.endswith(NOTION_COOKIE_DOMAINS) and any(page_path(t) == p or page_path(t).startswith(p + '/')
                                                         for p in LOGIN_PATHS):
            return t
    return None

def add_account_target(targets):
    """The signed-in workspace page, where the 'Add an account' dialog opens (ADD_HOOK)."""
    from .desktop_app import is_notion_page
    for t in targets or []:
        if is_notion_page(t):
            return t
    return None


def _cookie_rows(profile):
    """Names of Notion cookies already written to the profile's cookie DB (values stay encrypted, unread)."""
    import shutil
    import sqlite3
    import tempfile
    names = set()
    for rel in (('Default', 'Network', 'Cookies'), ('Default', 'Cookies')):
        src = Path(profile).joinpath(*rel)
        if not src.is_file():
            continue
        tmp = Path(tempfile.gettempdir()) / ('ntwl-%d.db' % os.getpid())
        try:
            shutil.copyfile(src, tmp)
            con = sqlite3.connect('file:%s?mode=ro' % tmp.as_posix(), uri=True)
            try:
                for name, host in con.execute('select name, host_key from cookies'):
                    if str(host).lstrip('.').lower().endswith(NOTION_COOKIE_DOMAINS):
                        names.add(name)
            finally:
                con.close()
        except Exception:
            pass
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
    return names


def _headless_cookies(exe, profile, timeout=20):
    """Reopen the (closed) login profile headless with DevTools and read its Notion cookies."""
    import websocket
    port_file = Path(profile) / 'DevToolsActivePort'
    try:
        port_file.unlink()
    except OSError:
        pass
    proc = subprocess.Popen([exe, '--headless=new', '--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1',
                             '--user-data-dir=%s' % profile, '--no-first-run', '--no-default-browser-check', 'about:blank'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                lines = port_file.read_text().splitlines()
                port, path = int(lines[0]), lines[1]
                break
            except (OSError, ValueError, IndexError):
                time.sleep(0.1)
        else:
            raise RuntimeError('Browser did not start for reading the sign-in')
        ws = websocket.create_connection('ws://127.0.0.1:%d%s' % (port, path), timeout=10, suppress_origin=True)
        try:
            ws.send(json.dumps({'id': 1, 'method': 'Storage.getCookies', 'params': {}}))
            while True:
                message = json.loads(ws.recv())
                if message.get('id') == 1:
                    break
        finally:
            ws.close()
        return [c for c in (message.get('result') or {}).get('cookies', []) if is_notion_cookie(c)]
    finally:
        _stop(proc)


def _stop(proc):
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()


LOGIN_TITLE_WORDS = ('log in', 'login', 'sign in', 'sign up', 'signup', 'anmelden', 'registrieren',
                     'google', 'apple', 'microsoft account')


def signed_in_title(titles):
    """True if an Edge window title shows a signed-in Notion page (e.g. 'Notes | Notion - ... Edge')."""
    for t in titles or []:
        low = str(t).lower()
        if '| notion' in low and not any(w in low for w in LOGIN_TITLE_WORDS):
            return True
    return False


def _timing(step, t0):
    """Append a timing line to data/window_login_timing.log (diagnostics only)."""
    try:
        log = Path(__file__).resolve().parent.parent / 'data' / 'window_login_timing.log'
        with open(log, 'a', encoding='utf-8') as fh:
            fh.write('%s %-22s +%.1fs\n' % (time.strftime('%H:%M:%S'), step, time.time() - t0))
    except Exception:
        pass


def _enum_windows(pid, action):
    if os.name != 'nt' or not pid:
        return
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            action(user32, hwnd)
        return True
    user32.EnumWindows(proto(cb), 0)


def _window_titles(pid):
    import ctypes
    titles = []

    def grab(user32, hwnd):
        n = user32.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            titles.append(buf.value)
    try:
        _enum_windows(int(pid), grab)
    except Exception:
        return []
    return titles


def _close_gracefully(proc, timeout=6):
    """Close the Edge window normally so it flushes cookies before the headless read."""
    if proc is None or proc.poll() is not None:
        return
    try:
        _enum_windows(int(proc.pid), lambda user32, hwnd: user32.PostMessageW(hwnd, 0x0010, 0, 0))
        proc.wait(timeout=timeout)
    except Exception:
        pass


def capture_browser_cookies(store, timeout=1200, sleep=time.sleep, clock=time.time):
    """Sign-in in a plain Edge window and return the Notion cookies.

    Google refuses sign-in in a browser started with a DevTools port ('This browser or app may not be
    secure'), so the visible window has none. Done = token_v2 in the cookie DB or the user closed the
    window. Then the profile is reopened headless with DevTools only to read the cookies.
    """
    import shutil
    import uuid
    from .browser import Browser
    exe = Browser(store, edge_only=True).executable()
    profile = (Path(store.directory) / 'browser-profiles' / ('winlogin-' + uuid.uuid4().hex[:12])).resolve()
    profile.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    proc = subprocess.Popen([exe, '--user-data-dir=%s' % profile, '--no-first-run', '--no-default-browser-check',
                             '--disable-background-mode', '--disable-features=msEdgeStartupBoost', LOGIN_URL], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
    try:
        deadline = clock() + timeout
        seen = 0
        while clock() < deadline and proc.poll() is None:
            if 'token_v2' in _cookie_rows(profile):
                sleep(3)  # let the remaining cookies of the redirect land too
                break
            # Edge locks its cookie DB while it runs, so the check above usually sees nothing.
            # A signed-in Notion page in the window title means the login is done.
            seen = seen + 1 if signed_in_title(_window_titles(proc.pid)) else 0
            if seen >= 2:
                _timing('title seen', t0)
                break
            sleep(0.5)
        _close_gracefully(proc)  # WM_CLOSE: Edge writes its cookies to disk (terminate would lose them)
        _timing('edge closed', t0)
        _stop(proc)
        sleep(0.3)
        cookies = _headless_cookies(exe, profile)
        _timing('cookies read (%d)' % len(cookies), t0)
        return cookies if has_token(cookies) else []
    finally:
        _stop(proc)
        shutil.rmtree(profile, ignore_errors=True)


def transfer(session, cookies):
    """Put the cookies into the NotionTool window and open the workspace."""
    params = to_cookie_params(cookies)
    if not has_token(params):
        raise ValueError('No Notion sign-in cookie to transfer')
    session.send('Network.enable', {})
    session.send('Network.setCookies', {'cookies': params})
    session.send('Page.navigate', {'url': HOME_URL})
    return len(params)


class LoginAssist:
    """Background helper for DesktopController while the window shows the Notion login page."""

    def __init__(self, store, port=9333, *, http_get=None, connect=None, capture=None, event=None,
                 interval=1.0, on_signed_in=None):
        from .desktop_app import _default_http_get, _default_connect
        self.store = store
        self.port = int(port)
        self.http_get = http_get or _default_http_get
        self.connect = connect or _default_connect
        self.capture = capture or (lambda: capture_browser_cookies(store))
        self.event = event or (lambda kind, message: store.event(kind, message=message))
        self.interval = interval
        # Called with the cookies after the window signed in: the harness adopts the same account
        # and runs the first-login setup (MCP + Main + Coworkers).
        self.on_signed_in = on_signed_in
        self.stop_event = threading.Event()
        self.session = None
        self.target_id = ''
        self.busy = False
        self.thread = None

    def start(self):
        if self.thread is None or not self.thread.is_alive():
            self.stop_event.clear()
            self.thread = threading.Thread(target=self.run, daemon=True, name='window-login-assist')
            self.thread.start()
        return self

    def stop(self):
        self.stop_event.set()
        self._drop()

    def _drop(self):
        if self.session is not None:
            try:
                self.session.close()
            except Exception:
                pass
        self.session, self.target_id = None, ''

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception:
                self._drop()
            self.stop_event.wait(self.interval)

    def tick(self):
        targets = self.http_get('http://127.0.0.1:%d/json/list' % self.port)
        target = login_target(targets) or add_account_target(targets)
        if target is None:
            self._drop()
            return 'idle'
        if self.session is None or self.session.closed or self.target_id != target.get('id'):
            self._drop()
            from .desktop_app import local_ws
            if not local_ws(target.get('webSocketDebuggerUrl'), self.port):
                return 'refused'
            self.session = self.connect(target['webSocketDebuggerUrl'])
            self.target_id = str(target.get('id') or '')
            self.session.send('Page.enable', {})
            self.session.send('Page.addScriptToEvaluateOnNewDocument', {'source': HOOK})
            self.session.send('Page.addScriptToEvaluateOnNewDocument', {'source': ADD_HOOK})
        self.session.send('Runtime.evaluate', {'expression': HOOK})
        self.session.send('Runtime.evaluate', {'expression': ADD_HOOK})
        result = self.session.send('Runtime.evaluate', {'expression': 'window.%s || 0' % FLAG, 'returnByValue': True})
        requested = (result.get('result') or {}).get('value') or 0
        if requested and not self.busy:
            self.session.send('Runtime.evaluate', {'expression': 'window.%s = 0' % FLAG})
            self.busy = True
            threading.Thread(target=self._sign_in, args=(self.session,), daemon=True, name='window-login').start()
            return 'requested'
        return 'waiting'

    def _sign_in(self, session):
        try:
            self.event('window_login_started', 'Sign-in browser window opened for the NotionTool window')
            cookies = self.capture()
            if not cookies:
                self.event('window_login_failed', 'No Notion sign-in was detected in the browser window')
                return
            count = transfer(session, cookies)
            self.event('window_login_done', 'NotionTool window signed in (%d cookies transferred)' % count)
            if self.on_signed_in is not None:
                try:
                    self.on_signed_in(cookies)
                except Exception as exc:  # noqa: BLE001
                    self.event('first_login_setup_failed', 'harness sign-in: ' + str(exc)[:180])
        except Exception as exc:  # noqa: BLE001
            self.event('window_login_failed', str(exc)[:200])
        finally:
            self.busy = False
