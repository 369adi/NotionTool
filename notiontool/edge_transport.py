"""Notion requests run as same-origin fetches in an owned Edge robots.txt tab.

Streams are drained incrementally. An interrupted POST is never retried here.
"""
import json
import queue
import re
import threading
import time
import uuid
from .browser import Browser
from .vendor.transport import NotionHttpError, NotionStreamError, _cookie_domain_allowed


class EdgeTransport:
    _base = 'https://app.notion.com'

    def __init__(self, store, sessions, account_id, budget, browser=None):
        self.store, self.sessions, self.account_id, self.budget = store, sessions, account_id, budget
        self.browser = browser or Browser(store, profile_name='notion-' + account_id,
            start_url=self._base + '/robots.txt', edge_only=True)
        self.lock = threading.RLock()
        self.ready_process = None
        self.target = None
        self.local = threading.local()
        # Phase 4 Fix K: wake bookkeeping of the hidden transport tab.
        self.woken_at = 0.0
        self.wake_logged_at = 0.0
        self.stall_wake_after = 8.0
        # Shared stream poller: one DevTools call per tick for all active streams instead of one per stream.
        self._watch_cv = threading.Condition()
        self._watched = {}
        self._poller = None

    def _watch(self, target, key):
        inbox = queue.Queue()
        with self._watch_cv:
            self._watched[key] = (target, inbox)
            if self._poller is None or not self._poller.is_alive():
                self._poller = threading.Thread(target=self._poll_loop, name='edge-poller', daemon=True)
                self._poller.start()
            self._watch_cv.notify_all()
        return inbox

    def _unwatch(self, key):
        with self._watch_cv:
            self._watched.pop(key, None)

    def _poll_loop(self):
        idle = 0
        while True:
            with self._watch_cv:
                if not self._watched:
                    self._watch_cv.wait(5)
                    if not self._watched:
                        self._poller = None
                        return
                groups = {}
                for key, (target, inbox) in self._watched.items():
                    tid = target.get('id') if isinstance(target, dict) else target
                    groups.setdefault(tid or id(target), (target, []))[1].append((key, inbox))
            flowing = False
            for target, items in groups.values():
                keys = [key for key, _ in items]
                try:
                    results = self.browser.evaluate(target, '''(keys => {
                      const all = window.__notiontoolRequests || {}, out = {};
                      for (const key of keys) {
                        const q = all[key];
                        out[key] = q ? {status:q.status, retry:q.retry, done:q.done, error:q.error,
                          text:q.chunks.splice(0, 128).join(''), remaining:q.chunks.length} : null;
                      }
                      return out;
                    })(''' + json.dumps(keys) + ')') or {}
                    if len(keys) == 1 and keys[0] not in results and ('done' in results or 'status' in results):
                        results = {keys[0]: results}
                except Exception:
                    for _, inbox in items:
                        inbox.put({'_poll_error': True})
                    continue
                for key, inbox in items:
                    result = results.get(key)
                    if result and (result.get('text') or result.get('remaining')):
                        flowing = True
                    inbox.put(result)
            # Fast while bytes flow, slower while the models think; the tab stays free for new sends.
            idle = 0 if flowing else idle + 1
            time.sleep(.1 if idle < 3 else min(.4, .1 * 1.5 ** (idle - 2)))

    @property
    def retry_after(self):
        return getattr(self.local, 'retry_after', 30)

    def wake(self, target, force=False, reason='periodic'):
        # Phase 4 Fix K: keep the hidden transport tab out of Edge's frozen lifecycle state.
        # A frozen tab still answers DevTools evaluations, but its fetch readers and timers stop:
        # a Notion stream that already ended is never read to its end and the task waits until
        # task_timeout, while every other request through the tab times out. Waking needs no
        # navigation and no Notion request, so it stays outside the request budget (I11).
        now = time.monotonic()
        if now - self.woken_at < (self.stall_wake_after if force else 15):
            return False
        self.woken_at = now
        frozen = None
        if force:
            try:
                self.browser.evaluate(target, 'new Promise(r => setTimeout(() => r(1), 0))', 2)
                frozen = False
            except Exception:
                frozen = True
        try:
            wake = getattr(self.browser, 'wake', None)
            if wake is not None:
                wake(target)
            else:
                self.browser.send(target, 'Page.setWebLifecycleState', {'state': 'active'}, 5)
        except Exception:
            return False
        if frozen and now - self.wake_logged_at >= 60:
            self.wake_logged_at = now
            event = getattr(self.store, 'event', None)
            if event is not None:
                try:
                    event('edge_tab_woken', account_id=self.account_id, reason=str(reason)[:80], frozen=True)
                except Exception:
                    pass
        return True

    def prepare(self):
        with self.lock:
            self.browser.ensure()
            if self.ready_process is not self.browser.process:
                tabs = self.browser.tabs()
                self.target = next((t for t in tabs if t.get('url') == self._base + '/robots.txt'), None)
                if not self.target:
                    self.target = self.browser.new_tab(self._base + '/robots.txt')
                self.browser.send(self.target, 'Page.navigate', {'url':self._base + '/robots.txt'})
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    if self.browser.evaluate(self.target, 'location.origin') == self._base and self.browser.evaluate(
                            self.target, 'document.readyState') == 'complete':
                        break
                    time.sleep(.15)
                else:
                    raise OSError('Edge Notion transport tab did not finish loading')
                cookies = []
                for cookie in (self.sessions.load(self.account_id) or {}).get('cookies', []):
                    if not _cookie_domain_allowed(cookie.get('domain', '')):
                        continue
                    c = {k: v for k, v in cookie.items() if v is not None and k in
                         ('name', 'value', 'domain', 'path', 'secure', 'httpOnly', 'sameSite', 'expires')}
                    if c.get('expires', 0) <= 0:
                        c.pop('expires', None)
                    if c.get('sameSite') not in ('Strict', 'Lax', 'None'):
                        c.pop('sameSite', None)
                    cookies.append(c)
                if cookies:
                    self.browser.send(self.target, 'Network.setCookies', {'cookies': cookies})
                self.ready_process = self.browser.process
            # Navigation of the transport tab must never forward a request to another origin.
            if self.browser.evaluate(self.target, 'location.origin') != self._base:
                raise OSError('Restore the Notion robots.txt transport tab before continuing')
            self.wake(self.target)
            return self.target

    def open(self):
        self.prepare()
        return {'opened': True, 'account_id': self.account_id, 'transport': 'edge',
                'url': self._base + '/robots.txt'}

    def open_app(self):
        """Open regular signed-in Notion in a visible tab of the same Edge profile.
        The robots.txt transport tab is kept as is and stays the only API tab."""
        self.browser.ensure(reopen=True)
        target = self.prepare()
        url = self._base + '/'
        before = {t.get('id') for t in self.browser.tabs()}
        tab_id = ''
        try:
            created = self.browser.new_tab(url)
            tab_id = created.get('id', '') if isinstance(created, dict) else ''
        except Exception:
            tab_id = ''
        deadline = time.monotonic() + 8
        found = None
        while time.monotonic() < deadline and not found:
            found = next((t for t in self.browser.tabs() if t.get('id') == tab_id or
                          (t.get('id') not in before and t.get('url', '').startswith(self._base)
                           and not t.get('url', '').endswith('/robots.txt'))), None)
            if not found and not tab_id:
                # Fallback: browser-wide CDP target creation via the transport tab.
                try:
                    tab_id = self.browser.send(target, 'Target.createTarget', {'url': url}, 10).get('targetId', '')
                except Exception:
                    tab_id = 'failed'
            if not found:
                time.sleep(.3)
        if not found:
            raise OSError('Edge did not open the Notion tab; the robots.txt transport tab is unchanged')
        try:
            self.browser.send(found, 'Page.bringToFront', {}, 10)
        except Exception:
            pass
        return {'opened': True, 'verified': True, 'account_id': self.account_id, 'transport': 'edge',
                'tab_id': found.get('id'), 'url': found.get('url', url)}

    def _start(self, path, body, headers, timeout, cancel):
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9]*', path):
            raise ValueError('Invalid Notion endpoint')
        target = self.prepare()
        ticket = self.budget.acquire(cancel, source=path)
        key = uuid.uuid4().hex
        # Forbidden browser headers are supplied by Edge itself.
        headers = {k: v for k, v in (headers or {}).items()
                   if k.lower() not in ('origin', 'referer', 'cookie', 'host', 'content-length')}
        headers['Content-Type'] = 'application/json'
        script = r'''((key, path, body, headers, timeout) => {
          const all = window.__notiontoolRequests ||= {};
          const controller = new AbortController();
          const q = all[key] = {chunks: [], done: false, status: 0, size: 0, controller};
          const timer = setTimeout(() => controller.abort(), timeout * 1000);
          (async () => {
            try {
              const response = await fetch('/api/v3/' + path, {method:'POST', credentials:'include',
                headers, body:JSON.stringify(body), signal:controller.signal, redirect:'error'});
              q.status = response.status; q.retry = response.headers.get('Retry-After');
              const reader = response.body.getReader(), decoder = new TextDecoder();
              while (true) {
                const {done, value} = await reader.read();
                if (done) break;
                q.size += value.length;
                if (q.size > 32 * 1024 * 1024) throw new Error('Response too large');
                q.chunks.push(decoder.decode(value, {stream:true}));
              }
              q.chunks.push(decoder.decode());
            } catch (_) { q.error = true; }
            finally { clearTimeout(timer); q.done = true; }
          })();
          return true;
        })'''
        args = json.dumps([key, path, body, headers, timeout], ensure_ascii=True)
        self.browser.evaluate(target, script + '(...' + args + ')')
        return target, key, ticket

    def _chunks(self, path, body, headers, timeout, cancel=None):
        target, key, ticket = self._start(path, body, headers, timeout, cancel)
        deadline = time.monotonic() + timeout + 5
        # Phase 3: every started request reports its outcome to the shared coordinator.
        begun, status, latency, outcome = time.monotonic(), 0, None, 'neutral'
        progress_at, last_seen = begun, None
        inbox = self._watch(target, key)
        try:
            while time.monotonic() < deadline:
                if cancel is not None and cancel.is_set():
                    raise NotionStreamError('Edge request interrupted; reconcile before reuse')
                # One shared poller reads every active stream of this tab in a single DevTools call.
                try:
                    result = inbox.get(timeout=1.0)
                except queue.Empty:
                    if time.monotonic() - progress_at > self.stall_wake_after:
                        self.wake(target, force=True, reason='stalled ' + path)
                        progress_at = time.monotonic()
                    continue
                if isinstance(result, dict) and result.get('_poll_error'):
                    outcome = 'failed'
                    raise NotionStreamError('Edge request polling failed; reconcile before reuse')
                if not result:
                    outcome = 'failed'
                    raise NotionStreamError('Edge request state lost; reconcile before reuse')
                if result.get('status') and latency is None:
                    status, latency = result['status'], time.monotonic() - begun
                if result.get('status') == 429:
                    outcome = 'rate_limited'
                    self.local.retry_after = self.budget.limited(result.get('retry'))
                    raise NotionHttpError(429, retry_after=self.retry_after)
                if result.get('status', 0) >= 400:
                    code = result['status']
                    outcome = 'blocked' if code in (401, 403) else 'failed' if code >= 500 else 'invalid'
                    raise NotionHttpError(code)
                if result.get('error'):
                    outcome = 'failed'
                    raise NotionStreamError('Edge request interrupted; reconcile before reuse')
                seen_now = (result.get('status'), result.get('remaining'), result.get('done'))
                if result.get('text') or seen_now != last_seen:
                    progress_at, last_seen = time.monotonic(), seen_now
                elif time.monotonic() - progress_at > self.stall_wake_after:
                    # Phase 4 Fix K: no new bytes for a while; a frozen tab would hold them back.
                    self.wake(target, force=True, reason='stalled ' + path)
                    progress_at = time.monotonic()
                if result.get('text'):
                    yield result['text']
                if result.get('done') and not result.get('remaining'):
                    outcome = 'ok'
                    self._save_cookies(target)
                    return
            outcome = 'failed'
            raise NotionStreamError('Edge request timed out; reconcile before reuse')
        finally:
            self._unwatch(key)
            release = getattr(self.budget, 'release', None)
            if release is not None:
                release(ticket, outcome, status=status, latency=latency, account_id=self.account_id)
            try:
                self.browser.evaluate(target, '''(key => {
                  const q = window.__notiontoolRequests?.[key];
                  if(q) { q.controller.abort(); delete window.__notiontoolRequests[key]; }
                })(''' + json.dumps(key) + ')', 5)
            except Exception:
                pass

    def _save_cookies(self, target):
        with self.lock:
            result = self.browser.send(target, 'Network.getAllCookies')
            cookies = [c for c in result.get('cookies', []) if _cookie_domain_allowed(c.get('domain', ''))]
            session = self.sessions.load(self.account_id) or {}
            session['cookies'] = cookies
            self.sessions.save(self.account_id, session)

    def request(self, path, body, headers=None, timeout=60, **kwargs):
        try:
            return 200, ''.join(self._chunks(path, body, headers, timeout))
        except NotionHttpError as exc:
            return exc.status, str(exc)

    def stream(self, path, body, headers=None, timeout=300, cancel=None):
        if path == 'runInferenceTranscript':
            timeout = max(timeout, self.store.settings()['task_timeout'])
        pending = ''
        seen = False
        for chunk in self._chunks(path, body, headers, timeout, cancel):
            pending += chunk
            while '\n' in pending:
                line, pending = pending.split('\n', 1)
                if line.strip():
                    try:
                        json.loads(line)
                    except ValueError:
                        raise NotionStreamError('Notion did not return a valid event stream') from None
                    seen = True
                    yield line
        if pending.strip():
            try:
                json.loads(pending)
            except ValueError:
                raise NotionStreamError('Notion returned an incomplete event stream') from None
            seen = True
            yield pending
        if not seen:
            raise NotionStreamError('Notion returned an empty event stream')

    def close(self):
        self.browser.close()
