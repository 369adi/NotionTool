"""Browser access for MCP tools via the Chrome DevTools Protocol (Edge or Chrome).

The browser runs with its own profile (``<data>/browser-profile``) so it never
touches the user's normal profile. It listens for DevTools only on 127.0.0.1.
Enabled through the ``mcp_browser_access`` setting.
"""
from pathlib import Path
import base64
import json
import os
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

    def ensure(self):
        if self.process is not None and self.process.poll() is None and self.port:
            return self.http('/json/version')
        profile = Path(self.store.directory) / 'browser-profiles' / (self.profile_name or uuid.uuid4().hex)
        self.profile = profile.resolve()
        profile.mkdir(parents=True, exist_ok=True)
        (profile / 'DevToolsActivePort').unlink(missing_ok=True)
        args = [self.executable(), '--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1',
                f'--user-data-dir={profile}', '--no-first-run', '--no-default-browser-check', self.start_url]
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        self.process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
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
