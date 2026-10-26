"""Browser access for MCP tools via the Chrome DevTools Protocol (Edge or Chrome).

The browser runs with its own profile (``<data>/browser-profile``) so it never
touches the user's normal profile. It listens for DevTools only on 127.0.0.1.
Enabled through the ``mcp_browser_access`` setting.
"""
from pathlib import Path
import base64
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
import urllib.parse
import urllib.request


PORT = 9223
CANDIDATES = [
    r'%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe',
    r'%ProgramFiles%\Microsoft\Edge\Application\msedge.exe',
    r'%ProgramFiles%\Google\Chrome\Application\chrome.exe',
    r'%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe',
    r'%LocalAppData%\Google\Chrome\Application\chrome.exe',
]

# Phase 4 Fix K: an owned hidden or occluded tab must keep running its fetch readers and
# timers. Without these flags Edge throttles or freezes it, and a finished Notion stream is
# never read to its end.
NO_THROTTLE_FLAGS = ('--disable-background-timer-throttling', '--disable-renderer-backgrounding',
                     '--disable-backgrounding-occluded-windows',
                     '--disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling')

SNAPSHOT_JS = r'''(() => {
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const sel = e => {
    if (e.id) return '#' + CSS.escape(e.id);
    if (e.name) return e.tagName.toLowerCase() + '[name="' + e.name + '"]';
    const parts = []; let n = e;
    while (n && n.nodeType === 1 && parts.length < 5) {
      let p = n.tagName.toLowerCase(), s = n, i = 1;
      while ((s = s.previousElementSibling)) if (s.tagName === n.tagName) i++;
      parts.unshift(p + ':nth-of-type(' + i + ')'); n = n.parentElement;
    }
    return parts.join(' > ');
  };
  const pick = (q, f, n) => Array.from(document.querySelectorAll(q)).filter(vis).slice(0, n).map(f);
  return {
    title: document.title, url: location.href,
    text: (document.body ? document.body.innerText : '').slice(0, 20000),
    links: pick('a[href]', a => ({text: a.innerText.trim().slice(0, 80), href: a.href}), 80),
    buttons: pick('button,[role=button],input[type=submit],input[type=button]',
                  b => ({text: (b.innerText || b.value || b.getAttribute('aria-label') || '').trim().slice(0, 80), selector: sel(b)}), 60),
    inputs: pick('input:not([type=hidden]):not([type=submit]):not([type=button]),textarea,select,[contenteditable=true]',
                 i => ({type: i.type || i.tagName.toLowerCase(), name: i.name || '', placeholder: i.placeholder || '',
                        label: i.getAttribute('aria-label') || '', selector: sel(i)}), 60)
  };
})()'''


class Browser:
    def __init__(self, store, port=0, profile_name='', start_url='about:blank', edge_only=False):
        self.store, self.port = store, 0
        self.lock = threading.Lock()
        self.process = None
        self.ids = 0
        self.profile_name, self.start_url, self.edge_only = profile_name, start_url, edge_only

    # ---- process / http ---------------------------------------------------
    def executable(self):
        configured = self.store.settings().get('browser_path', '')
        if configured and Path(configured).is_file():
            return configured
        for candidate in CANDIDATES:
            if self.edge_only and 'Edge' not in candidate:
                continue
            path = os.path.expandvars(candidate)
            if os.path.isfile(path):
                return path
        for name in (('msedge',) if self.edge_only else ('msedge', 'google-chrome', 'chromium', 'chromium-browser', 'chrome')):
            found = shutil.which(name)
            if found:
                return found
        raise RuntimeError('No Edge or Chrome found; set the browser path in Settings')

    def http(self, path, method='GET'):
        request = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}', method=method)
        with urllib.request.urlopen(request, timeout=5) as response:
            body = response.read().decode('utf-8', errors='replace')
        try:
            return json.loads(body)
        except ValueError:
            return body

    def probe(self, port):
        """Return /json/version of a DevTools endpoint on 127.0.0.1, or None if it does not answer."""
        if not isinstance(port, int) or not 1 <= port <= 65535:
            return None
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/json/version', timeout=2) as response:
                data = json.loads(response.read().decode('utf-8', errors='replace'))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or '/devtools/browser/' not in str(data.get('webSocketDebuggerUrl', '')):
            return None
        return data

    @staticmethod
    def file_endpoint(profile):
        """Port and browser path from DevToolsActivePort, or (0, '') if missing or invalid."""
        try:
            lines = (Path(profile) / 'DevToolsActivePort').read_text().splitlines()
            port = int(lines[0])
        except (OSError, ValueError, IndexError):
            return 0, ''
        if not 1 <= port <= 65535 or len(lines) < 2 or not lines[1].startswith('/devtools/browser/'):
            return 0, ''
        return port, lines[1]

    def list_processes(self):
        """Main browser processes with a --user-data-dir: [{'pid', 'cmd', 'ports'}]. Windows only, read-only."""
        if os.name != 'nt':
            return []
        script = ("$ErrorActionPreference='SilentlyContinue'; ConvertTo-Json -Depth 3 -Compress -InputObject @("
                  "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' OR Name='chrome.exe'\" | "
                  "Where-Object { $_.CommandLine -and $_.CommandLine -notmatch '--type=' -and $_.CommandLine -match '--user-data-dir' } | "
                  "ForEach-Object { [pscustomobject]@{ pid=$_.ProcessId; cmd=$_.CommandLine; ports=@(Get-NetTCPConnection -State Listen "
                  "-OwningProcess $_.ProcessId -LocalAddress 127.0.0.1 | Select-Object -ExpandProperty LocalPort) } })")
        try:
            result = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
                                    capture_output=True, text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
            data = json.loads(result.stdout or '[]')
        except (OSError, ValueError, subprocess.SubprocessError):
            return []
        return [item for item in (data if isinstance(data, list) else [data]) if isinstance(item, dict)]

    @staticmethod
    def same_dir(left, right):
        try:
            return os.path.normcase(os.path.abspath(str(left)).rstrip('\\/')) == os.path.normcase(os.path.abspath(str(right)).rstrip('\\/'))
        except (TypeError, ValueError):
            return False

    def running_ports(self, profile):
        """Candidate DevTools ports of running browser processes that use exactly this profile."""
        ports = []
        for item in self.list_processes():
            cmd = str(item.get('cmd') or '')
            match = (re.search(r'"--user-data-dir=([^"]+)"', cmd) or re.search(r'--user-data-dir="([^"]+)"', cmd)
                     or re.search(r'--user-data-dir=(\S+)', cmd))
            if not match or not self.same_dir(match.group(1), profile):
                continue
            explicit = re.search(r'--remote-debugging-port=(\d+)', cmd)
            found = [int(explicit.group(1))] if explicit and int(explicit.group(1)) > 0 else []
            raw = item.get('ports') or []
            for port in (raw if isinstance(raw, list) else [raw]):
                try:
                    found.append(int(port))
                except (TypeError, ValueError):
                    pass
            ports.extend(port for port in found if port not in ports)
        return ports

    def reuse(self, profile):
        """Attach to a healthy running browser for this profile; never stops or kills anything."""
        port, browser_path = self.file_endpoint(profile)
        if port:
            version = self.probe(port)
            if version is not None and str(version.get('webSocketDebuggerUrl', '')).endswith(browser_path):
                self.port = port
                return version
        if not self.profile_name:
            return None
        for candidate in self.running_ports(profile):
            version = self.probe(candidate)
            if version is not None:
                self.port = candidate
                return version
        return None

    def ensure(self, reopen=False):
        if self.process is not None and self.process.poll() is not None:
            if not reopen:
                raise RuntimeError('The NotionTool browser was closed. Open it manually to reconnect.')
            self.process = None
            self.port = 0
        if self.process is not None and self.process.poll() is None and self.port:
            return self.http('/json/version')
        if self.process is None and self.port:
            version = self.probe(self.port)
            if version is not None:
                return version
            if not reopen:
                profile = Path(self.store.directory) / 'browser-profiles' / (self.profile_name or '')
                reused = self.reuse(profile) if self.profile_name else None
                if reused is not None:
                    return reused
                raise RuntimeError('The NotionTool browser was closed. Open it manually to reconnect.')
            self.port = 0
        profile = Path(self.store.directory) / 'browser-profiles' / (self.profile_name or uuid.uuid4().hex)
        self.profile = profile.resolve()
        profile.mkdir(parents=True, exist_ok=True)
        reused = self.reuse(profile)
        if reused is not None:
            return reused
        (profile / 'DevToolsActivePort').unlink(missing_ok=True)
        args = [self.executable(), '--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1',
                f'--user-data-dir={profile}', '--no-first-run', '--no-default-browser-check',
                *NO_THROTTLE_FLAGS, self.start_url]
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        options = self._launch_options(args)
        self.process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags, **options)
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                lines=(profile/'DevToolsActivePort').read_text().splitlines()
                self.port=int(lines[0])
                if not 1<=self.port<=65535 or not lines[1].startswith('/devtools/browser/'):
                    raise RuntimeError('Invalid owned browser endpoint')
                return self.http('/json/version')
            except OSError:
                time.sleep(0.3)
        raise RuntimeError('Browser did not start its DevTools endpoint')

    def _launch_options(self, args):
        """Owned Notion transport Edge: off screen and never activated (games, full screen)."""
        options = {}
        if self.hidden():
            args.insert(-1, '--window-position=-32000,-32000')
            if os.name == 'nt':
                startup = subprocess.STARTUPINFO()
                startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startup.wShowWindow = 4  # SW_SHOWNOACTIVATE
                options['startupinfo'] = startup
        return options

    def hidden(self):
        """True for the owned Notion transport browser (setting edge_transport_hidden, default on)."""
        try:
            on = self.store.settings().get('edge_transport_hidden', True)
        except Exception:
            on = True
        return bool(on and self.edge_only and str(self.profile_name or '').startswith('notion-'))

    def _window_bounds(self, tab, bounds):
        win = self.send(tab, 'Browser.getWindowForTarget', {'targetId': tab.get('id')}, 5).get('windowId')
        if win is None:
            return False
        self.send(tab, 'Browser.setWindowBounds', {'windowId': win, 'bounds': {'windowState': 'normal'}}, 5)
        self.send(tab, 'Browser.setWindowBounds', {'windowId': win, 'bounds': bounds}, 5)
        return True

    def hide_window(self, tab):
        """Hide the transport window: off screen and, when it is the hidden transport browser,
        fully removed from the Windows taskbar (SW_HIDE). Never raises."""
        try:
            moved = self._window_bounds(tab, {'left': -32000, 'top': -32000, 'width': 1200, 'height': 800})
            if self.hidden():
                self.hide_native()
            return moved
        except Exception:
            return False

    def show_window(self, tab):
        """Bring the window of tab back on screen (the user opened Notion in Edge). Never raises."""
        try:
            shown = self._window_bounds(tab, {'left': 80, 'top': 80, 'width': 1280, 'height': 860})
            self.show_native()
            return shown
        except Exception:
            return False

    def _owned_pids(self):
        """PIDs of browser processes that use exactly this owned profile (transport included)."""
        profile = str(getattr(self, 'profile', '') or '')
        if os.name != 'nt' or not profile:
            return []
        pids = []
        for item in self.list_processes():
            cmd = str(item.get('cmd') or '')
            if profile.lower() in cmd.lower():
                try:
                    pids.append(int(item.get('pid') or 0))
                except (TypeError, ValueError):
                    pass
        return pids

    def _native_windows(self):
        """Top-level visible HWNDs of this owned browser's processes."""
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        pids = set(self._owned_pids())
        if not pids:
            return []
        found = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        @WNDENUMPROC
        def cb(hwnd, _lparam):
            if user32.IsWindowVisible(hwnd):
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value in pids:
                    found.append(hwnd)
            return True

        user32.EnumWindows(cb, 0)
        return found

    def hide_native(self):
        """SW_HIDE this owned browser's windows so no taskbar button exists. Never raises."""
        self._set_native_visible(False)

    def show_native(self):
        """SW_SHOW this owned browser's windows (undo a hide_native). Never raises."""
        self._set_native_visible(True)

    def _set_native_visible(self, visible):
        if os.name != 'nt':
            return False
        try:
            import ctypes
            user32 = ctypes.windll.user32
            show = 5 if visible else 0  # SW_SHOW / SW_HIDE
            # The browser window may appear a moment after the process starts: retry briefly.
            for _ in range(8):
                wins = self._native_windows()
                if wins or not visible:
                    break
                time.sleep(0.25)
            for hwnd in self._native_windows():
                user32.ShowWindow(hwnd, show)
            return True
        except Exception:
            return False

    def close(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.process=None
        self.port=0

    def tabs(self):
        self.ensure()
        return [t for t in self.http('/json/list') if t.get('type') == 'page']

    def tab(self, tab_id=''):
        tabs = self.tabs()
        if tab_id:
            match = next((t for t in tabs if t['id'] == tab_id), None)
            if not match:
                raise RuntimeError('Tab not found')
            return match
        if tabs:
            return tabs[0]
        return self.new_tab('about:blank')

    def new_tab(self, url):
        path = '/json/new?' + urllib.parse.quote(url, safe=':/?&=%#')
        try:
            return self.http(path, 'PUT')
        except OSError:
            return self.http(path)

    # ---- devtools ---------------------------------------------------------
    def send(self, tab, method, params=None, timeout=30):
        import websocket  # websocket-client
        ws = websocket.create_connection(tab['webSocketDebuggerUrl'], timeout=timeout, suppress_origin=True)
        try:
            self.ids += 1
            ident = self.ids
            ws.send(json.dumps(dict(id=ident, method=method, params=params or {})))
            deadline = time.time() + timeout
            while time.time() < deadline:
                message = json.loads(ws.recv())
                if message.get('id') == ident:
                    if 'error' in message:
                        raise RuntimeError(message['error'].get('message', 'DevTools error'))
                    return message.get('result', {})
            raise RuntimeError('DevTools call timed out')
        finally:
            ws.close()

    def wake(self, tab, timeout=5):
        # Phase 4 Fix K: return a frozen or background-throttled tab to the active lifecycle state.
        # No navigation and no page request; only the page's task queues run again.
        return self.send(tab, 'Page.setWebLifecycleState', {'state': 'active'}, timeout)

    def evaluate(self, tab, expression, timeout=30):
        result = self.send(tab, 'Runtime.evaluate', dict(expression=expression, returnByValue=True, awaitPromise=True), timeout)
        if result.get('exceptionDetails'):
            details = result['exceptionDetails']
            raise RuntimeError('JavaScript error: ' + str(details.get('exception', {}).get('description') or details.get('text'))[:300])
        return result.get('result', {}).get('value')

    def wait_loaded(self, tab, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.evaluate(tab, 'document.readyState', 5) == 'complete':
                    return True
            except Exception:
                pass
            time.sleep(0.3)
        return False

    # ---- tools ------------------------------------------------------------
    def call(self, name, args):
        with self.lock:
            return getattr(self, name)(args)

    def browser_tabs(self, args):
        return [dict(id=t['id'], title=t.get('title', ''), url=t.get('url', '')) for t in self.tabs()]

    def browser_open(self, args):
        url = str(args.get('url', '')).strip()
        if not url:
            raise ValueError('url is required')
        if '://' not in url and not url.startswith('about:'):
            url = 'https://' + url
        if urllib.parse.urlsplit(url).scheme not in ('https', 'http') and url != 'about:blank':
            raise ValueError('Browser navigation requires HTTP or HTTPS')
        if args.get('new_tab'):
            tab = self.new_tab(url)
        else:
            tab = self.tab(args.get('tab_id', ''))
            self.send(tab, 'Page.navigate', dict(url=url))
        loaded = self.wait_loaded(tab)
        return dict(tab_id=tab['id'], loaded=loaded, title=self.evaluate(tab, 'document.title'), url=self.evaluate(tab, 'location.href'))

    def browser_snapshot(self, args):
        tab = self.tab(args.get('tab_id', ''))
        return dict(tab_id=tab['id'], **self.evaluate(tab, SNAPSHOT_JS))

    def browser_screenshot(self, args):
        tab = self.tab(args.get('tab_id', ''))
        data = self.send(tab, 'Page.captureScreenshot', dict(format='png'))['data']
        folder = Path(self.store.directory) / 'screenshots'
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f'screenshot-{int(time.time() * 1000)}.png'
        path.write_bytes(base64.b64decode(data))
        return dict(tab_id=tab['id'], path=str(path), bytes=path.stat().st_size)

    def browser_click(self, args):
        tab = self.tab(args.get('tab_id', ''))
        selector, text = args.get('selector', ''), args.get('text', '')
        if not selector and not text:
            raise ValueError('selector or text is required')
        script = '''((sel, txt) => {
          let el = sel ? document.querySelector(sel) : null;
          if (!el && txt) {
            const want = txt.trim().toLowerCase();
            el = Array.from(document.querySelectorAll('a,button,[role=button],input[type=submit],input[type=button],label,summary'))
              .find(e => (e.innerText || e.value || e.getAttribute('aria-label') || '').trim().toLowerCase().includes(want));
          }
          if (!el) return {clicked: false};
          el.scrollIntoView({block: 'center'}); el.click();
          return {clicked: true, tag: el.tagName.toLowerCase(), text: (el.innerText || el.value || '').trim().slice(0, 80)};
        })(%s, %s)''' % (json.dumps(selector), json.dumps(text))
        result = self.evaluate(tab, script)
        if not result.get('clicked'):
            raise RuntimeError('Element not found')
        self.wait_loaded(tab, 10)
        return result

    def browser_type(self, args):
        tab = self.tab(args.get('tab_id', ''))
        selector = str(args.get('selector', ''))
        if not selector:
            raise ValueError('selector is required')
        focused = self.evaluate(tab, '''(sel => { const el = document.querySelector(sel); if (!el) return false;
            el.scrollIntoView({block: 'center'}); el.focus();
            if ('value' in el) el.value = ''; return true; })(%s)''' % json.dumps(selector))
        if not focused:
            raise RuntimeError('Element not found')
        self.send(tab, 'Input.insertText', dict(text=str(args.get('text', ''))))
        if args.get('submit'):
            for kind in ('keyDown', 'keyUp'):
                self.send(tab, 'Input.dispatchKeyEvent', dict(type=kind, key='Enter', code='Enter',
                          windowsVirtualKeyCode=13, nativeVirtualKeyCode=13, text='\r' if kind == 'keyDown' else ''))
            self.wait_loaded(tab, 10)
        return dict(typed=True, selector=selector)

    def browser_eval(self, args):
        tab = self.tab(args.get('tab_id', ''))
        value = self.evaluate(tab, str(args.get('expression', '')))
        text = json.dumps(value, ensure_ascii=False, default=str)
        return json.loads(text) if len(text) < 30000 else text[:30000]

    def browser_close(self, args):
        tab_id = str(args.get('tab_id', ''))
        if not tab_id:
            raise ValueError('tab_id is required')
        self.ensure()
        self.http('/json/close/' + tab_id)
        return dict(closed=tab_id)
