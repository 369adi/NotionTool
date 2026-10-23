"""Notion AI chat page as the app UI (Phase 8b, part A).

NotionShell opens a visible tab in the account's own Edge profile on the Notion AI chat page,
injects the overlay (notiontool/web/notion_shell/overlay.css and overlay.js) into every new
document, and bridges the page to the local harness through one DevTools binding.

Contract shared with overlay.js:
- Python registers the binding ``ntHarness`` (Runtime.addBinding).
- The page calls ``window.ntHarness(JSON.stringify({id, action, args}))``.
- Python answers with ``window.__ntHarnessReply(id, {ok, result|error})`` (Runtime.evaluate).
- Python pushes live state with ``window.__ntHarnessPush(state)`` at most once per second and only
  when the state changed (``ts`` alone is not a change).
- Only the actions in ACTIONS run; everything else is answered with 'Action not allowed'.
- run_confirm only confirms an existing run in state awaiting_main (POST /api/run/confirm, the same
  App.confirm_run_request the HTTP route calls). It never creates or opens a chat.
- The pushed state carries attention items (awaiting_main, stalled, stale_context_tasks), at most
  ATTENTION_LIMIT each, with short strings only.

Why a persistent DevTools session: ``Browser.send`` opens one WebSocket per call and closes it.
A binding (Runtime.addBinding) and a new-document script (Page.addScriptToEvaluateOnNewDocument)
live only as long as the DevTools session that added them, and Runtime.bindingCalled /
Runtime.executionContextCreated events only arrive on an open session. Sent through
``Browser.send`` they would be removed again at once. NotionShell therefore keeps one CdpSession on
the shell tab. CdpSession uses the websocket-client package that browser.py already uses, so no
new dependency is added; everything else is stdlib.

Invariants:
- I7: no token, cookie or secret goes into the page. The injected source is overlay.css plus
  overlay.js inside a fixed wrapper. Every injected and evaluated string is checked against the
  secret values handed to the shell and against credential markers before it is sent; results are
  scrubbed of credential-like keys.
- I11: the shell makes no Notion request of its own. It talks DevTools to the local Edge and reads
  the local store only. The Notion page loads like any signed-in tab of that profile.
- I1: the shell never creates chats (Phase 8b). There is no new_welcome_chat action and no
  /api/main/create path in this module; the page can only open existing chats.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import re
import threading
import time


BINDING = 'ntHarness'
ACTIONS = ('state', 'runs', 'run_pause', 'run_cancel', 'run_retry', 'task_reconcile', 'settings_get',
           'settings_set', 'mcp_list', 'mcp_refresh', 'run_confirm', 'mcp_add', 'mcp_bridge', 'host_key',
           'bridge_tunnel')
# Fields the page may send to POST /api/mcp/add (the old app's add form: name, URL, token, write_auto).
MCP_ADD_FIELDS = ('name', 'url', 'token', 'write_auto')
ATTENTION_LIMIT = 10
ATTENTION_TEXT = 160
PUSH_INTERVAL = 1.0
ASSET_DIR = Path(__file__).parent / 'web' / 'notion_shell'
FALLBACK_BASE = ''.join(('https://', 'www.', 'notion', '.so'))
CHAT_PATH = '/chat'
ACTIVE_TASK_STATES = ('running', 'cancelling', 'uncertain')
RESULT_LINK_SECONDS = 30.0
# Credential markers that must never appear in an injected or evaluated string (I7).
SECRET_MARKERS = ('Bearer ', 'X-NotionTool-Token', 'token_v2')
# The injected overlay must also never address the local host API directly (live data comes
# through the binding only).
INJECT_MARKERS = SECRET_MARKERS + ('127.0.0.1',)
# Keys dropped from every result before it goes into the page.
SECRET_KEYS = {'authorization', 'cookie', 'cookies', 'headers', 'identity', 'lease', 'handoff', 'password',
               'secret', 'token', 'api_key', 'apikey', 'host_api_key', 'bridge_token'}
SECRET_SUFFIXES = ('_token', '_key', '_secret', '_password', '_cookie', '_cookies')

# Keys POST /api/settings accepts (server.py App.action, path '/api/settings'). The page may set
# only these. host_api_port, host_api_allow_network, host_api_bind, browser_path and
# public_mcp_url are accepted by the route too, but they are kept out of the page on purpose:
# they open the host API to the network, choose the executable the app launches, or redirect
# the MCP bridge, and any script on the Notion page can call the binding.
INT_SETTINGS = ('max_workers', 'per_account', 'max_attempts', 'task_timeout', 'max_fix_rounds',
                'context_token_limit', 'context_turn_limit', 'context_handoff_chars', 'max_runs_per_chat',
                'coworker_max_age_hours')
CHOICE_SETTINGS = ('coworker_fresh_context', 'context_replace_mode', 'notion_transport', 'coworker_rotate_mode',
                   'main_context_policy')
TEXT_SETTINGS = ('theme',)
BOOL_SETTINGS = ('harness_enabled', 'mcp_full_access', 'mcp_browser_access', 'coworker_delete_old_chat', 'idle_mode')
# Shown on the page even before they were saved once; the values are the server defaults.
PAGE_DEFAULTS = {'coworker_rotate_mode': 'new_welcome', 'main_context_policy': 'hint',
                 'coworker_delete_old_chat': True, 'idle_mode': True}
PAGE_EXCLUDED_SETTINGS = ('host_api_port', 'host_api_allow_network', 'host_api_bind', 'browser_path',
                          'public_mcp_url')

_WRAPPER_HEAD = '''(function(){
  if (window.top !== window) return;
  var host = String(location.hostname || '');
  if (!(host === 'notion.so' || host === 'notion.com' || host.endsWith('.notion.so') || host.endsWith('.notion.com'))) return;
  if (window.__ntShellInstalled) return;
  window.__ntShellInstalled = true;
  var css = '''
_WRAPPER_STYLE = ''';
  function addStyle() {
    if (document.getElementById('nt-shell-style')) return true;
    var root = document.head || document.documentElement;
    if (!root) return false;
    var style = document.createElement('style');
    style.id = 'nt-shell-style';
    style.textContent = css;
    root.appendChild(style);
    return true;
  }
  if (!addStyle()) document.addEventListener('readystatechange', addStyle);
  document.addEventListener('DOMContentLoaded', addStyle);
  /* overlay.js */
'''
_WRAPPER_TAIL = '\n})();\n'

_PUSH_JS = ('(function(s){var n=0;(function go(){if(typeof window.__ntHarnessPush==="function"){'
            'window.__ntHarnessPush(s);return;}if(++n<40)setTimeout(go,250);})();return true;})(%s)')


def notion_base():
    try:
        from .edge_transport import EdgeTransport
        return EdgeTransport._base
    except Exception:
        return FALLBACK_BASE


def chat_url(thread_id=''):
    """URL of the Notion AI chat page, or of one chat thread on it."""
    url = notion_base() + CHAT_PATH
    thread = str(thread_id or '').replace('-', '')
    return url + ('?t=' + thread if thread else '')


def _secret_key(key):
    lower = str(key).lower()
    return lower in SECRET_KEYS or lower.endswith(SECRET_SUFFIXES)


def scrub(value, depth=0):
    """Copy of a JSON-like value without credential-like keys (I7)."""
    if depth > 20:
        return None
    if isinstance(value, dict):
        return {str(k): scrub(v, depth + 1) for k, v in value.items() if not _secret_key(k)}
    if isinstance(value, (list, tuple, set)):
        return [scrub(v, depth + 1) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _js(value):
    # ensure_ascii also escapes U+2028/U+2029, so the text is a valid JavaScript expression.
    return json.dumps(value, ensure_ascii=True, separators=(',', ':'), default=str)


def _state_key(state):
    if isinstance(state, dict):
        state = {k: v for k, v in state.items() if k != 'ts'}
    return json.dumps(state, sort_keys=True, ensure_ascii=True, default=str)


# ---- Model pin: the Notion composer must never send 'Auto' -----------------------------------
# Notion's own UI sends an 'updated-config' step without model/reasoningEffort when the composer
# chip fell back to Auto (seen live 2026-10-21: values {} and {modelFromUser: true}). The shell
# pauses runInferenceTranscript requests of the page (CDP Fetch) and fills in the last explicit
# choice the user made in the menu, else Opus + high. Explicit choices pass unchanged.
PIN_DEFAULT_MODEL = 'albuquerque-quinn'  # Opus 5.5
PIN_DEFAULT_EFFORT = 'high'
PIN_PATTERN = '*runInferenceTranscript*'


def _pin_log_path():
    import sys
    if getattr(sys, 'frozen', False):  # dist/NotionTool.exe -> <root>/data
        return Path(sys.executable).resolve().parent.parent / 'data' / 'model_pin.log'
    return Path(__file__).resolve().parents[1] / 'data' / 'model_pin.log'


def pin_log(entry):
    """Append one JSON line to data/model_pin.log (proof which model really went to Notion). Never raises."""
    try:
        path = _pin_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = dict(entry, at=time.strftime('%Y-%m-%d %H:%M:%S'))
        with open(path, 'a', encoding='utf-8') as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except Exception:
        pass


def request_thread_id(post):
    """I50: Notion thread id (lowercase hex, no dashes) a runInferenceTranscript body writes to; '' if none."""
    try:
        data = json.loads(post) if isinstance(post, str) else None
    except ValueError:
        return ''
    if not isinstance(data, dict):
        return ''
    return re.sub(r'[^0-9a-f]', '', str(data.get('threadId') or '').lower())


def send_block_reason(ages, thread):
    """I50 desktop send block: the reason when ``thread`` is a chat at or over the send limit in a
    chat_ages payload ({limit_h, chats: [{thread_id, age_h, blocked}]}); '' otherwise. Unknown age or an
    unknown thread is never blocked (same rule as server.Application.require_young_chat)."""
    if not thread or not isinstance(ages, dict):
        return ''
    try:
        limit = float(ages.get('limit_h') or 23)
    except (TypeError, ValueError):
        limit = 23.0
    for chat in ages.get('chats') or []:
        if not isinstance(chat, dict):
            continue
        if re.sub(r'[^0-9a-f]', '', str(chat.get('thread_id') or '').lower()) != thread:
            continue
        age = chat.get('age_h')
        known = isinstance(age, (int, float)) and not isinstance(age, bool)
        if chat.get('blocked') or (known and age >= limit):
            return ('chat_too_old: this chat is %s h old (limit %g h); sending is blocked. '
                    'Use Context Switch for a fresh chat.' % ('%.1f' % age if known else '%g+' % limit, limit))
    return ''


def request_shape(post):
    """Small, content-free view of a runInferenceTranscript body: top-level keys, the step types and
    every updated-config value (model/effort fields only, no message text). Finds where the composer's
    real model choice lives. Never raises."""
    try:
        data = json.loads(post) if isinstance(post, str) else None
        if not isinstance(data, dict):
            return {}
        steps = [s for s in data.get('transcript') or [] if isinstance(s, dict)]
        configs = []
        for step in steps:
            if step.get('type') == 'updated-config':
                value = step.get('value') if isinstance(step.get('value'), dict) else {}
                configs.append({k: (v if isinstance(v, (str, int, float, bool)) or v is None else type(v).__name__)
                                for k, v in value.items()})
        top = {k: (v if isinstance(v, (str, int, float, bool)) and len(str(v)) < 80 else type(v).__name__)
               for k, v in data.items() if k != 'transcript'}
        def flat(value):
            value = value if isinstance(value, dict) else {}
            return {k: (v if isinstance(v, (str, int, float, bool)) or v is None else type(v).__name__)
                    for k, v in value.items()}
        base = next((s for s in steps if s.get('type') == 'config'), {})
        return dict(top=top, step_types=[s.get('type') for s in steps][-12:], configs=configs[-3:],
                    base_config=flat(base.get('value')), debug_overrides=flat(data.get('debugOverrides')))
    except Exception:
        return {}


def sent_config(post):
    """(model, effort) of the last updated-config step in a request body, or ('', '')."""
    try:
        data = json.loads(post) if isinstance(post, str) else None
        steps = [s for s in (data or {}).get('transcript') or [] if isinstance(s, dict) and s.get('type') == 'updated-config']
        value = steps[-1].get('value') or {} if steps else {}
        return str(value.get('model') or ''), str(value.get('reasoningEffort') or '')
    except Exception:
        return '', ''


def pin_config_body(post, pin):
    """Patched JSON body (str) with model/effort filled in, or None when nothing changes.

    pin is a dict updated with explicit picks found in the request (model, effort)."""
    try:
        data = json.loads(post) if isinstance(post, str) else None
    except ValueError:
        return None
    transcript = data.get('transcript') if isinstance(data, dict) else None
    if not isinstance(transcript, list):
        return None
    configs = [s for s in transcript if isinstance(s, dict) and s.get('type') == 'updated-config']
    for step in configs:
        value = step.get('value') if isinstance(step.get('value'), dict) else {}
        model = str(value.get('model') or '')
        if model and model.lower() != 'auto':
            pin['model'] = model
        effort = str(value.get('reasoningEffort') or '').strip().lower()
        if effort and effort != 'auto':
            pin['effort'] = effort
    if not configs:
        return None
    last = configs[-1]
    value = dict(last.get('value') if isinstance(last.get('value'), dict) else {})
    changed = False
    if not value.get('model') or str(value.get('model')).lower() == 'auto':
        value['model'] = pin.get('model') or PIN_DEFAULT_MODEL
        changed = True
    if not value.get('reasoningEffort') or str(value.get('reasoningEffort')).lower() == 'auto':
        # Never carry an old pick (e.g. a one-off 'low') into later turns: Auto effort is always high.
        value['reasoningEffort'] = PIN_DEFAULT_EFFORT
        changed = True
    if not changed:
        return None
    value['modelFromUser'] = True
    last['value'] = value
    return json.dumps(data, ensure_ascii=False)


class CdpSession:
    """One persistent DevTools WebSocket for a page target.

    Needed for session-scoped calls (Runtime.addBinding, Page.addScriptToEvaluateOnNewDocument) and
    for events (Runtime.bindingCalled, Runtime.executionContextCreated); see the module docstring.
    ``on_event(method, params)`` runs on the reader thread and must not block.
    """

    def __init__(self, ws_url, timeout=15):
        import websocket  # websocket-client, already required by browser.py
        self._ws = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True)
        self._ws.settimeout(0.5)
        self._ids = 0
        self._lock = threading.Lock()
        self._pending = {}
        self.on_event = None
        self.closed = False
        self._reader = threading.Thread(target=self._read, daemon=True, name='notion-shell-cdp')
        self._reader.start()

    def send(self, method, params=None, timeout=15):
        if threading.current_thread() is self._reader:
            raise RuntimeError('DevTools calls must not run on the reader thread')
        with self._lock:
            if self.closed:
                raise RuntimeError('DevTools session closed')
            self._ids += 1
            ident = self._ids
            slot = {'event': threading.Event()}
            self._pending[ident] = slot
            self._ws.send(json.dumps({'id': ident, 'method': method, 'params': params or {}}))
        if not slot['event'].wait(timeout):
            self._pending.pop(ident, None)
            raise RuntimeError('DevTools call timed out: ' + method)
        message = slot.get('message') or {}
        if 'error' in message:
            raise RuntimeError(str(message['error'].get('message', 'DevTools error')))
        return message.get('result', {})

    def _read(self):
        import websocket
        while not self.closed:
            try:
                raw = self._ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except Exception:
                break
            try:
                message = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if 'id' in message:
                slot = self._pending.pop(message['id'], None)
                if slot is not None:
                    slot['message'] = message
                    slot['event'].set()
            elif message.get('method'):
                callback = self.on_event
                if callback is not None:
                    try:
                        callback(message['method'], message.get('params') or {})
                    except Exception:
                        pass
        self.closed = True
        for slot in list(self._pending.values()):
            slot['message'] = {'error': {'message': 'DevTools session closed'}}
            slot['event'].set()
        self._pending.clear()

    def close(self):
        self.closed = True
        try:
            self._ws.close()
        except Exception:
            pass


class NotionShell:
    """The Notion AI chat page with the harness overlay, in the account's own Edge profile.

    ``browser`` is the account's Browser (the one its EdgeTransport uses, profile
    ``notion-<account_id>``), so the tab is signed in through the profile itself and the shell
    never handles cookies. ``handlers`` maps action names from ACTIONS to callables taking the
    ``args`` dict; see default_handlers(server).
    """

    def __init__(self, browser, account_id, handlers, *, url='', asset_dir=None, secrets=(), connect=None,
                 clock=time.monotonic, timer=threading.Timer, executor=None):
        self.browser = browser
        self.account_id = account_id
        self.handlers = {k: v for k, v in dict(handlers or {}).items() if k in ACTIONS}
        self.url = url or chat_url()
        self.asset_dir = Path(asset_dir) if asset_dir else ASSET_DIR
        self.secrets = tuple(s for s in (str(x) for x in secrets if x) if len(s) >= 6)
        self.connect = connect or (lambda tab: CdpSession(tab['webSocketDebuggerUrl']))
        self.clock = clock
        self.timer = timer
        self._own_executor = executor is None
        self.executor = executor or ThreadPoolExecutor(max_workers=4, thread_name_prefix='notion-shell')
        self._lock = threading.RLock()
        self.session = None
        self.tab = None
        self.target_id = ''
        self.script_id = ''
        self._started = False
        self._closed = False
        self._latest = None
        self._pending = None
        self._pending_key = None
        self._sent_key = None
        self._last_push = None
        self._push_timer = None

    # ---- lifecycle --------------------------------------------------------
    @property
    def alive(self):
        session = self.session
        return self._started and not self._closed and session is not None and not getattr(session, 'closed', False)

    def info(self):
        return dict(account_id=self.account_id, target_id=self.target_id, url=self.url,
                    started=self._started, closed=self._closed, alive=self.alive)

    def injected_source(self):
        """overlay.css (through a small style injector) plus overlay.js, for every new document."""
        css = (self.asset_dir / 'overlay.css').read_text(encoding='utf-8')
        js = (self.asset_dir / 'overlay.js').read_text(encoding='utf-8')
        source = _WRAPPER_HEAD + _js(css) + _WRAPPER_STYLE + js + _WRAPPER_TAIL
        if self._leaks(source, INJECT_MARKERS):
            raise ValueError('The overlay contains a credential marker or the local host address; it is not injected')
        return source

    def start(self):
        """Open the shell tab, register binding and overlay, then load the Notion AI chat page.

        CDP calls on the shell's own session (see the module docstring for why not Browser.send):
        Page.enable, Runtime.enable, Runtime.addBinding (name ntHarness),
        Page.addScriptToEvaluateOnNewDocument (overlay), Page.navigate, Page.bringToFront.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError('This Notion shell was closed; create a new one')
            if self._started:
                return self.info()
            source = self.injected_source()
            self.browser.ensure()
            self.tab = self._open_tab()
            self.target_id = self.tab.get('id', '')
            session = self.connect(self.tab)
            self.session = session
            try:
                session.on_event = self._on_event
                session.send('Page.enable')
                session.send('Runtime.enable')
                session.send('Runtime.addBinding', {'name': BINDING})
                self.enable_model_pin(session)
                self.script_id = session.send('Page.addScriptToEvaluateOnNewDocument',
                                              {'source': source}).get('identifier', '')
                session.send('Page.navigate', {'url': self.url})
            except Exception:
                self._started = True
                self.close()
                raise
            try:
                session.send('Page.bringToFront')
            except Exception:
                pass
            self._started = True
            return self.info()

    def _open_tab(self):
        """Open the shell tab in the account's Edge profile, starting on about:blank.

        Uses the existing Browser.new_tab (DevTools /json/new). Only if that fails,
        Target.createTarget is sent with Browser.send through an existing tab of the same browser
        (the fallback EdgeTransport.open_app uses). about:blank first, so the binding and the overlay
        are registered before the first Notion document loads.
        """
        try:
            tab = self.browser.new_tab('about:blank')
        except Exception:
            tab = None
        if not isinstance(tab, dict) or not tab.get('id'):
            anchor = self.browser.tab()
            created = self.browser.send(anchor, 'Target.createTarget', {'url': 'about:blank'}, 10)
            tab = {'id': created.get('targetId', '')}
        if not tab.get('webSocketDebuggerUrl'):
            tab = self.browser.tab(tab['id'])
        return tab

    def close(self):
        """Remove the overlay script and the binding, close the tab, detach handlers. Idempotent.

        Page.removeScriptToEvaluateOnNewDocument, Runtime.removeBinding and Target.closeTarget run on
        the shell's session; if Target.closeTarget fails, Browser.http('/json/close/<id>') is used.
        """
        with self._lock:
            if self._closed:
                return False
            self._closed = True
            timer, self._push_timer = self._push_timer, None
            session, self.session = self.session, None
            self.handlers = {}
            self._pending = None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass
        if session is not None:
            session.on_event = None
            if self.script_id:
                self._quiet(session, 'Page.removeScriptToEvaluateOnNewDocument', {'identifier': self.script_id})
            self._quiet(session, 'Runtime.removeBinding', {'name': BINDING})
            closed = self._quiet(session, 'Target.closeTarget', {'targetId': self.target_id}) if self.target_id else True
            if not closed and self.target_id:
                try:
                    self.browser.http('/json/close/' + self.target_id)
                except Exception:
                    pass
            try:
                session.close()
            except Exception:
                pass
        if self._own_executor:
            self.executor.shutdown(wait=False)
        return True

    @staticmethod
    def _quiet(session, method, params):
        try:
            session.send(method, params, 5)
            return True
        except Exception:
            return False

    # ---- events -----------------------------------------------------------
    def _on_event(self, method, params):
        # Runs on the session reader thread: hand off, never block it (a reply waits for the reader).
        if self._closed or method not in ('Runtime.bindingCalled', 'Runtime.executionContextCreated',
                                          'Fetch.requestPaused'):
            return
        try:
            self.executor.submit(self._handle_event, method, params)
        except RuntimeError:
            pass

    def enable_model_pin(self, session=None):
        """Pause the page's runInferenceTranscript requests so 'Auto' never reaches Notion. Never raises."""
        session = session or self.session
        try:
            session.send('Fetch.enable', {'patterns': [{'urlPattern': PIN_PATTERN, 'requestStage': 'Request'}]})
            self.model_pin_on = True
            error = ''
        except Exception as exc:
            self.model_pin_on = False
            error = str(exc)[:200]
        pin_log(dict(event='pin_enabled' if self.model_pin_on else 'pin_enable_failed',
                     shell=type(self).__name__, account=str(getattr(self, 'account_id', ''))[:40], error=error))
        return self.model_pin_on

    def _pin_request(self, params):
        import base64
        ident = params.get('requestId')
        if not ident:
            return
        request = params.get('request') or {}
        if str(request.get('method') or '').upper() == 'POST' and self._block_old_chat(ident, request):
            return  # I50: refused before it reaches Notion
        patched = None
        try:
            if str(request.get('method') or '').upper() == 'POST':
                pin = self.__dict__.setdefault('model_pin', {})
                patched = pin_config_body(request.get('postData'), pin)
        except Exception:
            patched = None
        args = {'requestId': ident}
        if patched is not None:
            args['postData'] = base64.b64encode(patched.encode('utf-8')).decode('ascii')
            self.__dict__['model_pin_fixed'] = self.__dict__.get('model_pin_fixed', 0) + 1
        try:
            before = sent_config(request.get('postData'))
            after = sent_config(patched) if patched is not None else before
            pin_log(dict(event='request', account=str(getattr(self, 'account_id', ''))[:40],
                         has_body=bool(request.get('postData')), page_model=before[0], page_effort=before[1],
                         sent_model=after[0], sent_effort=after[1], patched=patched is not None,
                         shape=request_shape(request.get('postData'))))
        except Exception:
            pass
        try:
            self.session.send('Fetch.continueRequest', args, 10)
        except Exception:
            try:
                self.session.send('Fetch.continueRequest', {'requestId': ident}, 10)
            except Exception:
                pass

    # I50 send block: the desktop message box is Notion's own composer; its sends arrive in _pin_request as
    # paused runInferenceTranscript requests. chat_ages_source() returns a chat_ages payload (DesktopShell
    # wires the chat_ages action); a chat 23 h or older is failed there and never reaches Notion.
    chat_ages_source = None

    def _block_old_chat(self, ident, request):
        source = getattr(self, 'chat_ages_source', None)
        if not callable(source):
            return False
        try:
            thread = request_thread_id(request.get('postData'))
            reason = send_block_reason(source() if thread else None, thread)
        except Exception:
            return False
        if not reason:
            return False
        try:
            self.session.send('Fetch.failRequest', {'requestId': ident, 'errorReason': 'BlockedByClient'}, 10)
        except Exception:
            pass  # never continue it: the request stays refused
        pin_log(dict(event='send_blocked_too_old', account=str(getattr(self, 'account_id', ''))[:40],
                     thread=thread[:40], reason=reason[:200]))
        try:
            self.session.send('Runtime.evaluate', {'expression': "(function(){try{var c=window.__ntDeskCtx;"
                              "if(c&&c.toast)c.toast(" + _js(reason) + ",'error');}catch(e){}})()"}, 5)
        except Exception:
            pass
        return True

    def _handle_event(self, method, params):
        try:
            if self._closed:
                return
            if method == 'Fetch.requestPaused':
                self._pin_request(params)
            elif method == 'Runtime.bindingCalled':
                if params.get('name') == BINDING:
                    self.dispatch(params.get('payload'))
            elif method == 'Runtime.executionContextCreated':
                aux = (params.get('context') or {}).get('auxData') or {}
                if aux.get('isDefault', True):
                    self.repush()
        except Exception:
            pass  # one bad call must never stop the bridge

    def dispatch(self, payload):
        """Handle one window.ntHarness(JSON.stringify({id, action, args})) call and reply."""
        try:
            message = json.loads(payload) if isinstance(payload, str) else None
        except ValueError:
            message = None
        if not isinstance(message, dict):
            return self._reply(None, {'ok': False, 'error': 'Bad request: expected JSON {id, action, args}'})
        ident = message.get('id')
        if isinstance(ident, bool) or not isinstance(ident, (str, int, float)):
            ident = None
        if isinstance(ident, str):
            ident = ident[:200]
        action = message.get('action')
        args = message.get('args')
        args = {} if args is None else args
        if not isinstance(action, str) or action not in ACTIONS:
            return self._reply(ident, {'ok': False, 'error': 'Action not allowed'})
        if not isinstance(args, dict):
            return self._reply(ident, {'ok': False, 'error': 'args must be an object'})
        handler = self.handlers.get(action)
        if handler is None:
            return self._reply(ident, {'ok': False, 'error': 'Action not available'})
        try:
            result = handler(args)
        except Exception as exc:  # noqa: BLE001 - the page gets the message, the bridge keeps running
            return self._reply(ident, {'ok': False, 'error': (str(exc) or type(exc).__name__)[:300]})
        return self._reply(ident, {'ok': True, 'result': scrub(result)})

    def _reply(self, ident, payload):
        text = _js(payload)
        if self._leaks(text, SECRET_MARKERS):
            text = _js({'ok': False, 'error': 'Reply withheld: it contained a credential'})
        return self._evaluate('window.__ntHarnessReply&&window.__ntHarnessReply(%s,%s)' % (_js(ident), text))

    # ---- push -------------------------------------------------------------
    def push(self, state):
        """Queue state for window.__ntHarnessPush: at most once per PUSH_INTERVAL, only on change."""
        with self._lock:
            if self._closed:
                return False
            self._latest = state
            key = _state_key(state)
            if key == self._sent_key:
                self._pending = None
                return False
            self._pending, self._pending_key = state, key
        return self._flush()

    def repush(self):
        """Send the last state again, for a new document after a reload or navigation."""
        with self._lock:
            if self._closed or self._latest is None:
                return False
            self._sent_key = None
            latest = self._latest
        return self.push(latest)

    def _flush(self):
        with self._lock:
            if self._closed or self._pending is None:
                return False
            if self._last_push is not None:
                wait = self._last_push + PUSH_INTERVAL - self.clock()
                if wait > 0:
                    if self._push_timer is None:
                        self._push_timer = self.timer(wait, self._timer_fired)
                        self._push_timer.daemon = True
                        self._push_timer.start()
                    return False
            state, key = self._pending, self._pending_key
            self._pending = None
            self._last_push = self.clock()
        if self._evaluate(_PUSH_JS % _js(scrub(state))):
            with self._lock:
                self._sent_key = key
            return True
        return False

    def _timer_fired(self):
        with self._lock:
            self._push_timer = None
        self._flush()

    # ---- page access ------------------------------------------------------
    def _leaks(self, text, markers):
        return any(m in text for m in markers) or any(s in text for s in self.secrets)

    def _evaluate(self, expression):
        session = self.session
        if session is None or self._closed:
            return False
        if self._leaks(expression, SECRET_MARKERS):
            return False  # I7: never evaluate a string that carries a credential
        try:
            session.send('Runtime.evaluate', {'expression': expression, 'returnByValue': True}, 10)
            return True
        except Exception:
            return False


# ---- state ---------------------------------------------------------------
def _remote_unknown(task):
    """True when the harness lost track of a task's remote Notion state and it needs a reconcile.

    The engine marks such tasks with the flag ``remote_unknown``; a plain ``state == 'remote_unknown'``
    is accepted as well.
    """
    return bool(task.get('remote_unknown')) or task.get('state') == 'remote_unknown'


def _short(value, limit=ATTENTION_TEXT):
    return ('' if value is None else str(value))[:limit]


def _attention(store, runs, tasks, worker_ids, account_id, stalled, now):
    """Attention items for the overlay, each list at most ATTENTION_LIMIT long. Never raises.

    awaiting_main: runs in state awaiting_main with their needs_main task ids and reasons.
    stalled: running tasks flagged by server.check_stalls (memory only, passed in as ``stalled``,
    task id -> {last_progress_at, worker_label}) or tasks carrying ``stalled`` themselves.
    stale_context_tasks: stale_tasks.stale_context_tasks(store), report only.
    """
    run_ids = {r.get('id') for r in runs}
    awaiting = []
    for run in sorted(runs, key=lambda r: r.get('created') or 0):
        if run.get('state') != 'awaiting_main':
            continue
        reasons = run.get('needs_main_reasons') if isinstance(run.get('needs_main_reasons'), dict) else {}
        needs = run.get('needs_main') if isinstance(run.get('needs_main'), (list, tuple)) else []
        ids = [i for i in needs if i is not None]
        ids += [k for k in reasons if k not in ids]
        awaiting.append(dict(id=_short(run.get('id'), 100),
                             goal=_short(run.get('goal') or run.get('title') or run.get('id')),
                             needs_main=[dict(task_id=_short(i, 100), reason=_short(reasons.get(i)))
                                         for i in ids[:ATTENTION_LIMIT]]))
    awaiting = awaiting[-ATTENTION_LIMIT:]

    def own(task):
        return not account_id or task.get('worker_id') in worker_ids or task.get('run_id') in run_ids

    entries = dict(stalled) if isinstance(stalled, dict) else {}
    flagged = []
    for task in tasks:
        ident = task.get('id')
        entry = entries.get(ident)
        if (entry is None and not task.get('stalled')) or not own(task):
            continue
        entry = entry if isinstance(entry, dict) else {}
        try:
            since = float(entry.get('last_progress_at') or 0)
            age = int(max(0.0, now - since)) if since else int(task.get('stall_age_s') or 0)
        except (TypeError, ValueError):
            age = 0
        flagged.append(dict(task_id=_short(ident, 100), title=_short(task.get('title')), stall_age_s=age,
                            worker_label=_short(entry.get('worker_label') or task.get('worker_label'), 80)))
    flagged.sort(key=lambda item: -item['stall_age_s'])

    stale = []
    try:
        from .stale_tasks import stale_context_tasks
        items = stale_context_tasks(store, now=now)
    except Exception:
        items = []
    for item in items:
        if account_id and item.get('worker_id') not in worker_ids and item.get('worker_state') != 'missing':
            continue
        stale.append(dict(id=_short(item.get('id'), 100), title=_short(item.get('title')),
                          state=_short(item.get('state'), 40), age_seconds=int(item.get('age_seconds') or 0),
                          worker_state=_short(item.get('worker_state'), 40)))
        if len(stale) >= ATTENTION_LIMIT:
            break
    return awaiting, flagged[:ATTENTION_LIMIT], stale


def build_state(store, account_id=None, coordination=None, now=None, stalled=None):
    """Harness store (workers, tasks, runs) -> the pushed state shape.

    ``stalled`` is server.stalled_tasks (task id -> {last_progress_at, worker_label}); it lives in
    memory only, so the state handler passes it in.
    """
    if account_id is None:
        try:
            from .harness_account import selected_account
            account_id = selected_account(store)
        except Exception:
            account_id = ''
    now = time.time() if now is None else now
    coordination = coordination or {}
    workers = [w for w in store.all('workers') if not account_id or w.get('account_id') == account_id]
    worker_ids = {w['id'] for w in workers}
    chat_rows = {c['id']: c for c in store.all('chats')}
    tasks = store.all('tasks')

    def run_main(run):
        return run.get('main_worker') or chat_rows.get(run.get('chat_id'), {}).get('main_worker', '')

    runs = [r for r in store.all('runs')
            if not account_id or r.get('account_id') == account_id or run_main(r) in worker_ids]
    runs_by_id = {r['id']: r for r in runs}
    active = {}
    for task in tasks:
        wid = task.get('worker_id')
        if wid in worker_ids and task.get('state') in ACTIVE_TASK_STATES:
            previous = active.get(wid)
            if previous is None or (task.get('started') or 0) >= (previous.get('started') or 0):
                active[wid] = task
    chats = []
    for worker in sorted(workers, key=lambda w: (w.get('purpose') != 'main', str(w.get('label') or ''), w['id'])):
        purpose = worker.get('purpose')
        if purpose not in ('main', 'worker'):
            continue
        task = active.get(worker['id']) or {}
        chats.append(dict(id=worker['id'],
                          title=str(worker.get('label') or ('Main' if purpose == 'main' else 'Coworker'))[:120],
                          role='main' if purpose == 'main' else 'coworker',
                          state=str(worker.get('state') or 'unknown'),
                          task_title=str(task.get('title') or '')[:200], task_role=str(task.get('role') or ''),
                          started=float(task.get('started') or 0)))
    links, seen = [], set()
    for task in tasks:
        run = runs_by_id.get(task.get('run_id'))
        wid = task.get('worker_id')
        if run is None or wid not in worker_ids:
            continue
        main = run_main(run)
        if not main or main == wid or main not in worker_ids:
            continue
        if task.get('state') in ACTIVE_TASK_STATES:
            link = (main, wid, 'review' if task.get('role') == 'reviewer' else 'assign')
        elif task.get('state') == 'done' and now - float(task.get('finished') or 0) <= RESULT_LINK_SECONDS:
            link = (wid, main, 'result')
        else:
            continue
        if link not in seen:
            seen.add(link)
            links.append({'from': link[0], 'to': link[1], 'kind': link[2]})
    run_rows = []
    for run in sorted(runs, key=lambda r: r.get('created') or 0)[-20:]:
        own = [t for t in tasks if t.get('run_id') == run['id']]
        run_rows.append(dict(id=run['id'], title=str(run.get('goal') or run.get('title') or run['id'])[:160],
                             state=str(run.get('state') or ''), stage=str(run.get('stage') or ''),
                             tasks_done=sum(1 for t in own if t.get('state') == 'done'), tasks_total=len(own),
                             reconcile_task_ids=[str(t['id']) for t in own
                                                 if t.get('id') is not None and _remote_unknown(t)]))
    limits = dict(requests_per_min=int(coordination.get('limit_per_minute') or 0),
                  window_used=int(coordination.get('window_count') or 0))
    awaiting, flagged, stale = _attention(store, runs, tasks, worker_ids, account_id, stalled, now)
    return dict(ts=round(float(now), 3), account_id=account_id or '', chats=chats, links=links, runs=run_rows,
                limits=limits, awaiting_main=awaiting, stalled=flagged, stale_context_tasks=stale)


# ---- handlers --------------------------------------------------------------
def page_settings_keys():
    """Settings the page may read and write: a subset of what POST /api/settings accepts."""
    try:
        from .local_mcp import SETTINGS as local_settings
    except Exception:
        local_settings = ()
    keys = INT_SETTINGS + CHOICE_SETTINGS + TEXT_SETTINGS + BOOL_SETTINGS + tuple(local_settings)
    return tuple(dict.fromkeys(k for k in keys if k not in PAGE_EXCLUDED_SETTINGS))


def shell_secrets(server):
    """Values that must never reach the page: UI token, host API key, bridge token."""
    return tuple(v for v in (getattr(server, name, '') for name in ('token', 'host_api_key', 'bridge_token')) if v)


def _ident(args):
    ident = str(args.get('id') or '').strip()
    if not ident:
        raise ValueError('An id is required')
    return ident[:100]


def default_handlers(server, wait_timeout=240.0, sleep=time.sleep, clock=time.monotonic, copy_text=None):
    """Map the whitelisted actions to the existing server and engine methods.

    Every write goes through App.action (server.py), so validation and the engine lock stay
    exactly as for the local UI. run_confirm uses App.confirm_run_request, the method the HTTP
    route POST /api/run/confirm calls (server.py), because App.action has no confirm path.
    Nothing here sends a Notion request itself (I11) and nothing here creates a chat (I1).
    """
    store = server.store

    def account():
        from .harness_account import selected_account
        return selected_account(store)

    def coordination():
        try:
            return server.engine.coordination()
        except Exception:
            return {}

    def state(args):
        stalled = getattr(server, 'stalled_tasks', None)
        return build_state(store, account(), coordination(), stalled=stalled if isinstance(stalled, dict) else None)

    def runs(args):
        return state(args)['runs']

    def run_pause(args):
        return server.action('/api/run/pause', {'id': _ident(args), 'paused': bool(args.get('paused', True))})

    def run_cancel(args):
        return server.action('/api/run/cancel', {'id': _ident(args)})

    def run_retry(args):
        return server.action('/api/run/retry', {'id': _ident(args)})

    def run_confirm(args):
        ident = args.get('id') if isinstance(args, dict) else None
        if not isinstance(ident, str) or not ident.strip():
            raise ValueError('A run id (non-empty string) is required')
        confirm = getattr(server, 'confirm_run_request', None)
        if not callable(confirm):
            raise ValueError('Run confirmation is not available')
        status, payload = confirm({'id': ident.strip()[:100]})
        if status != 200:
            error = payload.get('error') if isinstance(payload, dict) else None
            raise ValueError(str(error or 'Run confirmation failed (%s)' % status))
        return payload

    def task_reconcile(args):
        body = {'id': _ident(args)}
        # {id} checks an unconfirmed task again; {id, resolution, note} records the Main's decision.
        resolution = args.get('resolution')
        if resolution:
            if resolution not in ('accept', 'abandon'):
                raise ValueError('resolution must be accept or abandon')
            body.update(resolution=resolution, note=str(args.get('note') or '')[:500])
        return server.action('/api/task/reconcile', body)

    def settings_get(args):
        settings = dict(PAGE_DEFAULTS)
        settings.update(store.settings())
        return {k: settings[k] for k in page_settings_keys() if k in settings}

    def settings_set(args):
        values = args.get('values') if isinstance(args.get('values'), dict) else args
        allowed = set(page_settings_keys())
        unknown = sorted(str(k) for k in values if k not in allowed)
        if unknown:
            raise ValueError('Setting not allowed: ' + ', '.join(unknown))
        if not values:
            raise ValueError('No settings given')
        server.action('/api/settings', dict(values))
        return settings_get({})

    def mcp_list(args):
        return [{k: v for k, v in dict(s).items() if k != 'tools'} for s in server.mcp_servers.public()]

    def mcp_refresh(args):
        return server.action('/api/mcp/refresh', {})

    def mcp_add(args):
        # Same body as the old app's add form: {name, url, token, write_auto} -> POST /api/mcp/add.
        body = {k: args[k] for k in MCP_ADD_FIELDS if k in args and args[k] is not None}
        if not str(body.get('name') or '').strip() or not str(body.get('url') or '').strip():
            raise ValueError('A name and an HTTPS MCP URL are required')
        return server.action('/api/mcp/add', body)

    def mcp_bridge(args):
        # Exactly the 'bridge' part of the server state (server.py App.state).
        return dict(local_url=f'http://127.0.0.1:{getattr(server, "port", "")}/mcp',
                    public_url=store.settings().get('public_mcp_url', ''))

    def host_key(args):
        # POST /api/host/key {rotate}. The key itself never goes into the page: the reply would be
        # withheld by NotionShell._reply (the key is one of shell_secrets) and any script on the
        # Notion page can call the binding. The page gets a masked form; copy=True puts the full
        # key on this PC's clipboard instead.
        rotate = bool(args.get('rotate'))
        res = server.action('/api/host/key', {'rotate': rotate}) or {}
        key = str(res.get('api_key') or '')
        if rotate and key:
            server.host_api_key = key
        copied = False
        if args.get('copy') and key:
            (copy_text or _copy_to_clipboard)(key)
            copied = True
        return dict(masked=_mask(key), has_key=bool(key), rotated=rotate, copied=copied, port=res.get('port'))

    def bridge_tunnel(args):
        return server.action('/api/bridge/tunnel', {})

    return dict(state=state, runs=runs, run_pause=run_pause, run_cancel=run_cancel, run_retry=run_retry,
                task_reconcile=task_reconcile, settings_get=settings_get, settings_set=settings_set,
                mcp_list=mcp_list, mcp_refresh=mcp_refresh, run_confirm=run_confirm, mcp_add=mcp_add,
                mcp_bridge=mcp_bridge, host_key=host_key, bridge_tunnel=bridge_tunnel)


def _mask(key):
    key = str(key or '')
    if not key:
        return ''
    return '\u2022' * 8 + (key[-4:] if len(key) >= 12 else '')


def _copy_to_clipboard(text):
    """Put text on the Windows clipboard of this PC (the page never sees it)."""
    import subprocess
    subprocess.run(['clip.exe'], input=str(text).encode('utf-16-le'), check=True, timeout=10,
                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
