"""NotionTool desktop app: overlay wiring, page actions, live state and lifecycle (op-fix-20).

See docs/DESKTOP_APP.md. DesktopApp (desktop_app.py) starts a SEPARATE Notion desktop instance
with its own profile and a DevTools port on 127.0.0.1. DesktopShell reuses the NotionShell bridge
(binding ntHarness, window.__ntHarnessReply, window.__ntHarnessPush) on that instance and injects
the desktop parts in notiontool/web/desktop. DesktopController owns the lifecycle for server.py.

Rules:
- The user's normal Notion is never touched. Only Notion.exe processes whose command line carries
  our profile directory are ever stopped (DesktopApp.is_our_process).
- No cookie, token or secret goes into the page (NotionShell checks every injected string).
- new_welcome_chat creates a Welcome chat through POST /api/main/create (request_id required), the
  same path as the local UI; it is the only chat-creating action and never a normal New Chat.
- App-local settings (app_*) live in data/desktop_app.json, not in the harness settings.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from . import notion_shell
from .notion_shell import BINDING, NotionShell, _js, chat_url
from .desktop_app import DesktopApp

DESK_DIR = Path(__file__).parent / 'web' / 'desktop'
# In the frozen build __file__ lives in a temporary _MEI folder; the harness data stays next to dist\.
DATA_DIR = (Path(sys.executable).resolve().parent.parent if getattr(sys, 'frozen', False)
            else Path(__file__).resolve().parent.parent) / 'data'
APP_FILE = 'desktop_app.json'
DEFAULT_PORT = 9333
# File stems in load order; overlay.js registers the 'sidebar' part. 'theme' (design system) is first.
PART_FILES = ('theme', 'chrome', 'declutter', 'overlay', 'shell', 'live', 'settings', 'extras', 'studio', 'stepcap', 'donesound')
MOUNT_ORDER = ('theme', 'chrome', 'declutter', 'sidebar', 'shell', 'live', 'settings', 'extras', 'studio')
DESK_ACTIONS = tuple(notion_shell.ACTIONS) + ('new_welcome_chat', 'settings_schema', 'app_settings_get',
                                              'app_settings_set', 'main_switch_start', 'app_chat_create',
                                              'app_chat_rename', 'app_chat_promote', 'main_refresh',
                                              'chat_context_switch')
CREATE_WAIT = 150.0
SWITCH_ACTIVE = ('brief_requested', 'brief_ready', 'prompt_sent')

_BOOT_HEAD = '''(function(){
  if (window.top !== window) return;
  var host = String(location.hostname || '');
  if (!(host === 'notion.so' || host === 'notion.com' || host.endsWith('.notion.so') || host.endsWith('.notion.com'))) return;
  var BUILD = '__NT_DESK_BUILD__';
  if (window.__ntDeskInstalled) {
    // Same build: nothing to do. Older build (harness restarted with new files): tear it down first.
    if (window.__ntDeskBuild === BUILD) return;
    // Always this build's teardown: it stops every handle in __ntDeskMounted/__ntDeskParts and also
    // cleans pages whose older build had no teardown of its own.
    try { (__NT_DESK_TEARDOWN__)(); } catch (e) {}
  }
  window.__ntDeskInstalled = true;
  window.__ntDeskBuild = BUILD;
  // Handles returned by each mounted part ({stop}); the next build's teardown stops them.
  window.__ntDeskMounted = {};
  window.__ntDeskParts = window.__ntDeskMounted;
  var css = '''
_BOOT_BODY = r''';
  function addStyle() {
    var old = document.getElementById('nt-desk-style');
    if (old) {
      if (old.getAttribute('data-nt-desk-build') !== BUILD) { old.textContent = css; old.setAttribute('data-nt-desk-build', BUILD); }
      return true;
    }
    var root = document.head || document.documentElement;
    if (!root) return false;
    var style = document.createElement('style');
    style.id = 'nt-desk-style';
    style.textContent = css;
    style.setAttribute('data-nt-desk-build', BUILD);
    root.appendChild(style);
    return true;
  }
  if (!addStyle()) document.addEventListener('readystatechange', addStyle);
  document.addEventListener('DOMContentLoaded', addStyle);
  try { if (localStorage.getItem('ntDeskOff') === '1') document.documentElement.classList.add('nt-desk-off'); } catch (e) {}
  window.__ntDesk = window.__ntDesk || {};
  var pending = {}, seq = 0, listeners = [], current = null;
  window.__ntHarnessReply = function (id, res) {
    var p = pending[id];
    if (!p) return;
    delete pending[id];
    if (res && res.ok) p.resolve(res.result); else p.reject(new Error((res && res.error) || 'Request failed'));
  };
  window.__ntHarnessPush = function (s) {
    current = s;
    listeners.slice().forEach(function (fn) { try { fn(s); } catch (e) {} });
  };
  function el(tag, props, kids) {
    var node = document.createElement(tag);
    props = props || {};
    Object.keys(props).forEach(function (key) {
      var value = props[key];
      if (value === undefined || value === null || value === false) return;
      if (key === 'cls' || key === 'className') node.className = value;
      else if (key === 'text') node.textContent = String(value);
      else if (key === 'style' && typeof value === 'object') Object.keys(value).forEach(function (k) { node.style[k] = value[k]; });
      else if (key.slice(0, 2) === 'on' && typeof value === 'function') node.addEventListener(key.slice(2).toLowerCase(), value);
      else node.setAttribute(key, value === true ? '' : String(value));
    });
    (Array.isArray(kids) ? kids : (kids == null ? [] : [kids])).forEach(function (kid) {
      if (kid === null || kid === undefined || kid === false) return;
      node.appendChild(typeof kid === 'object' ? kid : document.createTextNode(String(kid)));
    });
    return node;
  }
  var toastBox = null;
  function toast(text, kind) {
    if (!document.body) return;
    if (!toastBox || !toastBox.isConnected) { toastBox = el('div', {cls: 'nt-desk-toasts'}); document.body.appendChild(toastBox); }
    var item = el('div', {cls: 'nt-desk-toast nt-desk-toast-' + (kind || 'info'), text: String(text || '')});
    toastBox.appendChild(item);
    setTimeout(function () { if (item.parentNode) item.parentNode.removeChild(item); }, kind === 'error' ? 8000 : 4000);
  }
  var ctx = {
    call: function (action, args) {
      return new Promise(function (resolve, reject) {
        if (typeof window.ntHarness !== 'function') { reject(new Error('NotionTool harness is not connected')); return; }
        var id = 'd' + (++seq) + '-' + Date.now();
        pending[id] = {resolve: resolve, reject: reject};
        setTimeout(function () { if (pending[id]) { delete pending[id]; reject(new Error('The harness did not answer in time')); } }, 240000);
        try { window.ntHarness(JSON.stringify({id: id, action: action, args: args || {}})); }
        catch (e) { delete pending[id]; reject(e); }
      });
    },
    onState: function (fn) { listeners.push(fn); return function () { var i = listeners.indexOf(fn); if (i >= 0) listeners.splice(i, 1); }; },
    getState: function () { return current; },
    el: el,
    toast: toast
  };
  window.__ntDeskCtx = ctx;
  window.__ntDeskTeardown = __NT_DESK_TEARDOWN__;
'''
_BOOT_TAIL = r'''
  var mounted = false;
  function mount() {
    if (mounted) return;
    if (!document.body) { setTimeout(mount, 100); return; }
    mounted = true;
    MOUNT_ORDER.forEach(function (name) {
      var part = window.__ntDesk[name];
      if (typeof part !== 'function') return;
      try { var handle = part(ctx); if (handle) window.__ntDeskMounted[name] = handle; } catch (e) { try { console.warn('NotionTool part failed: ' + name, e); } catch (x) {} }
    });
    ctx.call('state', {}).then(function (s) { if (s) window.__ntHarnessPush(s); }, function () {});
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount); else mount();
})();
'''
BUILD_MARK = '__NT_DESK_BUILD__'
TEARDOWN_MARK = '__NT_DESK_TEARDOWN__'
_BUILD_RE = re.compile(r"var BUILD = '([0-9a-f]{16})';")
# Asked on attach: which overlay build (if any) runs in the open page.
PROBE_JS = ("(function () { return {installed: !!window.__ntDeskInstalled, "
            "build: String(window.__ntDeskBuild || '')}; })()")
# Startup self-check of the pinned Notion runtime: overlay present + which Notion build runs.
VERIFY_JS = ("(function () { return {installed: !!window.__ntDeskInstalled, "
             "ua: String(navigator.userAgent || '')}; })()")
# Removes an older overlay from the open page without reloading it. Also installed as
# window.__ntDeskTeardown so a later build can call it. Notion's own nodes are never removed:
# only nodes we created (data-nt-desk-own, nt-desk-* id, or only non-mark nt-desk-* classes and no
# Notion content inside) go; Notion nodes only lose our classes and data-nt-desk* attributes.
TEARDOWN_JS = r'''
function () {
  var out = {stopped: 0, removed: 0, cleaned: 0, errors: 0};
  var MARKS = ['nt-desk-hidden', 'nt-desk-off', 'nt-desk-chrome-on', 'nt-desk-topbar', 'nt-desk-composer',
               'nt-desk-composer-empty', 'nt-desk-message', 'nt-desk-code', 'nt-desk-upsell', 'nt-desk-sidebar',
               'nt-desk-heading', 'nt-desk-main-row', 'nt-desk-active', 'nt-desk-operator', 'nt-desk-newchat',
               'nt-desk-busy'];
  var DATA = ['data-nt-desk', 'data-nt-desk-thread', 'data-nt-desk-theme'];
  var seen = [];
  [window.__ntDeskMounted, window.__ntDeskParts].forEach(function (parts) {
    if (!parts || typeof parts !== 'object') return;
    Object.keys(parts).forEach(function (name) {
      var part = parts[name];
      if (!part || typeof part.stop !== 'function' || seen.indexOf(part) >= 0) return;
      seen.push(part);
      try { part.stop(); out.stopped += 1; } catch (e) { out.errors += 1; }
    });
  });
  window.__ntDeskMounted = {};
  window.__ntDeskParts = window.__ntDeskMounted;
  // Parts of a build without handles cannot be stopped and may append their old nodes again;
  // removed nodes are marked dead so the new style keeps them hidden.
  function drop(node) { try { node.setAttribute('data-nt-desk-dead', '1'); } catch (e) {} node.remove(); out.removed += 1; }
  function ourClass(token) { return String(token || '').indexOf('nt-desk-') === 0; }
  function tokens(node) { return node && node.classList ? Array.prototype.slice.call(node.classList) : []; }
  function has(node, name) { return !!(node && node.hasAttribute && node.hasAttribute(name)); }
  function foreign(node) {
    if (has(node, 'data-block-id') || has(node, 'contenteditable') || has(node, 'data-nt-desk')) return true;
    return tokens(node).some(function (token) { return !ourClass(token); });
  }
  function ours(node) {
    if (!node || node === document.documentElement || node === document.head || node === document.body) return false;
    var own = has(node, 'data-nt-desk-own') || ourClass(node.id);
    if (!own) {
      var list = tokens(node);
      if (!list.length) return false;
      if (list.some(function (token) { return MARKS.indexOf(token) >= 0; })) return false;
    }
    if (foreign(node)) return false;
    var kids = node.querySelectorAll ? node.querySelectorAll('*') : [];
    for (var i = 0; i < kids.length; i++) if (foreign(kids[i])) return false;
    return true;
  }
  var style = document.getElementById('nt-desk-style');
  if (style) drop(style);
  Array.prototype.slice.call(document.querySelectorAll('*')).forEach(function (node) {
    if (node.isConnected === false || !ours(node)) return;
    drop(node);
  });
  Array.prototype.slice.call(document.querySelectorAll('*')).forEach(function (node) {
    var hit = false;
    tokens(node).forEach(function (token) {
      if (ourClass(token) || token === 'nt-theme') { node.classList.remove(token); hit = true; }
    });
    DATA.forEach(function (name) { if (has(node, name)) { node.removeAttribute(name); hit = true; } });
    if (hit) out.cleaned += 1;
  });
  window.__ntDesk = {};
  window.__ntDeskInstalled = false;
  window.__ntDeskBuild = '';
  window.__ntDeskTeardown = null;
  return out;
}
'''
_BASE_CSS = '''[data-nt-desk-dead] { display: none !important; }

.nt-desk-toasts{position:fixed;right:16px;bottom:16px;z-index:2147483000;display:flex;flex-direction:column;gap:8px;pointer-events:none}
.nt-desk-toast{pointer-events:auto;max-width:360px;padding:10px 14px;border-radius:8px;font:13px/1.4 -apple-system,"Segoe UI",sans-serif;color:#fff;background:#2f3437;box-shadow:0 4px 18px rgba(0,0,0,.25)}
.nt-desk-toast-error{background:#c4372b}
.nt-desk-toast-ok,.nt-desk-toast-success{background:#2e8b57}
html.nt-desk-off .nt-desk-hidden{display:revert !important}
'''


# ---- assets -----------------------------------------------------------------
def part_files(asset_dir=None):
    """(stem, js, css) for every desktop part that exists, in load order."""
    base = Path(asset_dir) if asset_dir else DESK_DIR
    parts = []
    for stem in PART_FILES:
        js_path = base / (stem + '.js')
        if not js_path.exists():
            continue
        css_path = base / (stem + '.css')
        css = css_path.read_text(encoding='utf-8') if css_path.exists() else ''
        parts.append((stem, js_path.read_text(encoding='utf-8'), css))
    return parts


def _raw_source(asset_dir=None):
    parts = part_files(asset_dir)
    css = _BASE_CSS + '\n'.join(c for _, _, c in parts)
    body = ''.join('\n/* part: %s */\n%s\n' % (stem, js) for stem, js, _ in parts)
    teardown = TEARDOWN_JS.strip()
    return (_BOOT_HEAD.replace(TEARDOWN_MARK, teardown, 1) + _js(css) + _BOOT_BODY.replace(TEARDOWN_MARK, teardown, 1) + body
            + _BOOT_TAIL.replace('MOUNT_ORDER', _js(list(MOUNT_ORDER)), 1))


def source_hash(raw):
    """Build id of an overlay source: the first 16 hex digits of its sha256."""
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]


def build_hash(asset_dir=None):
    return source_hash(_raw_source(asset_dir))


def build_source(asset_dir=None):
    """The injected overlay. It carries its build id (window.__ntDeskBuild) so a re-attach can tell
    whether the open page still runs an older overlay."""
    raw = _raw_source(asset_dir)
    return raw.replace(BUILD_MARK, source_hash(raw), 1)


def source_build(source):
    match = _BUILD_RE.search(source or '')
    return match.group(1) if match else ''


# ---- environment ------------------------------------------------------------
def find_exe(run=subprocess.run, root=None):
    """Notion.exe for the NotionTool window: our PINNED copy (notion_runtime.py), never the live system
    install, so a Notion Store update cannot change the NotionTool window. First start makes the copy.
    Only if the copy cannot be made (disk full, access denied) the system Notion is used as fallback."""
    from . import notion_runtime
    try:
        return notion_runtime.ensure(run=run, root=root)
    except FileNotFoundError:
        raise
    except Exception:  # noqa: BLE001 - copy failed: still start, from the system install
        return notion_runtime.system_exe(run)


def default_profile():
    return str(Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'NotionTool-App' / 'profile')


def our_processes(profile_dir, run=subprocess.run):
    """PIDs of Notion.exe processes started with our profile directory (never the user's Notion)."""
    try:
        out = run(['powershell.exe', '-NoProfile', '-Command',
                   "Get-CimInstance Win32_Process -Filter \"Name='Notion.exe'\" | "
                   "ForEach-Object { [string]$_.ProcessId + '|' + $_.CommandLine }"],
                  capture_output=True, text=True, timeout=20)
    except Exception:
        return []
    pids = []
    for line in (out.stdout or '').splitlines():
        pid, _, cmd = line.partition('|')
        if pid.strip().isdigit() and DesktopApp.is_our_process(cmd, profile_dir):
            pids.append(int(pid.strip()))
    return pids


def stop_processes(profile_dir, run=subprocess.run):
    pids = our_processes(profile_dir, run)
    for pid in pids:
        try:
            run(['taskkill', '/PID', str(pid), '/T', '/F'], capture_output=True, text=True, timeout=20)
        except Exception:
            pass
    return pids


# ---- app-local settings -----------------------------------------------------
def app_setting_entries():
    from . import desktop_settings
    return [e for e in desktop_settings.HARNESS_SETTINGS if e.get('app_local')]


def app_defaults():
    return {e['key']: e.get('default') for e in app_setting_entries()}


def load_app_settings(data_dir=None):
    values = app_defaults()
    values.setdefault('app_autostart', False)
    path = Path(data_dir or DATA_DIR) / APP_FILE
    try:
        stored = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(stored, dict):
            values.update({k: v for k, v in stored.items() if k in values})
    except (OSError, ValueError):
        pass
    return values


def save_app_settings(changes, data_dir=None):
    entries = {e['key']: e for e in app_setting_entries()}
    entries.setdefault('app_autostart', dict(key='app_autostart', type='bool'))
    values = load_app_settings(data_dir)
    for key, value in dict(changes or {}).items():
        entry = entries.get(key)
        if entry is None:
            raise ValueError('Unknown app setting: ' + str(key))
        if entry.get('type') == 'bool':
            value = value if isinstance(value, bool) else str(value).lower() in ('true', '1', 'on', 'yes')
        elif entry.get('type') == 'choice':
            if value not in (entry.get('options') or ()):
                raise ValueError('%s must be one of: %s' % (key, ', '.join(entry.get('options') or ())))
        values[key] = value
    path = Path(data_dir or DATA_DIR) / APP_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(values, indent=2, sort_keys=True), encoding='utf-8')
    os.replace(tmp, path)
    return values


# ---- state ------------------------------------------------------------------
def _num(value, default=0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _attention(base):
    items = []
    for kind in ('awaiting_main', 'stalled', 'stale_context_tasks'):
        for item in base.get(kind) or []:
            if isinstance(item, dict):
                ident = item.get('id') or item.get('task_id') or item.get('run_id') or ''
                text = (item.get('message') or item.get('title') or item.get('reason')
                        or item.get('worker_label') or ident)
            else:
                ident, text = str(item), str(item)
            items.append(dict(kind=kind, id=str(ident)[:100], message=str(text)[:200]))
    return items


# I33: every Main of the account is shown, also user-renamed ones (e.g. 'Roblox') and several at once.
MAIN_PURPOSES = ('main', 'operator', 'spare_main')
ARCHIVED_PURPOSES = ('archived_main', 'archived_operator')
DEFAULT_TITLES = ('', 'main', 'welcome to notion', 'willkommen bei notion', 'new chat', 'neuer chat', 'new ai chat')


def _gone(worker):
    """Archived or retired workers/threads are never a Main."""
    purpose = str(worker.get('purpose') or '')
    return (purpose in ARCHIVED_PURPOSES or purpose.startswith('archived') or purpose.startswith('retired')
            or bool(worker.get('retired')) or bool(worker.get('archived')) or worker.get('state') in ('retired', 'archived'))


def _is_main_worker(worker):
    purpose = worker.get('purpose')
    if purpose in MAIN_PURPOSES:
        return True
    # A Main the user took over and renamed himself is released to purpose 'external' but stays a Main chat.
    return purpose == 'external' and bool(worker.get('main_number') or worker.get('released_chat'))


def _main_title(worker, chat=None):
    """The user's own (Notion) title wins; generic defaults fall back to the chat title or 'Main N'."""
    chat = chat or {}
    for value in (worker.get('user_set_title'), worker.get('notion_title'), chat.get('user_set_title')):
        if isinstance(value, str) and value.strip():
            return value.strip()[:120]
    label = str(worker.get('label') or '').strip()
    if label and label.lower() not in DEFAULT_TITLES:
        return label[:120]
    title = str(chat.get('title') or '').strip()
    if title and title.lower() not in DEFAULT_TITLES:
        return title[:120]
    if worker.get('main_number'):
        return 'Main %s' % worker.get('main_number')
    return label or title or 'Main'


ARCHIVE_MAX = 40


def archived_chats(all_workers, account='', limit=ARCHIVE_MAX):
    """Old chats replaced by a context switch (worker retired_threads), newest first, for the sidebar
    'Archive' section. Read only: nothing is sent to Notion; a click only opens the old chat."""
    rows, seen = [], set()
    for worker in all_workers:
        if account and worker.get('account_id') not in ('', None, account):
            continue
        label = str(worker.get('label') or ('Main' if worker.get('purpose') in ('main', 'operator') else 'Coworker'))[:60]
        threads = [t for t in (worker.get('retired_threads') or []) if t]
        total = len(threads)
        for index, thread in enumerate(threads):
            if thread in seen:
                continue
            seen.add(thread)
            rows.append(dict(thread_id=thread, url=chat_url(thread), worker_id=worker.get('id', ''),
                             name='%s - old %d' % (label, index + 1), order=(float(worker.get('created') or 0), index - total)))
    rows.sort(key=lambda r: r['order'], reverse=True)
    for row in rows:
        row.pop('order', None)
    return rows[:limit]


def collect_mains(chats, all_workers, account=''):
    """All non-archived Mains of the account: chat records, operator chat and Main workers without a
    chat record (also user-renamed ones). Deduplicated by thread_id; coworkers are never listed."""
    by_id = {w['id']: w for w in all_workers}
    retired = set()
    for w in all_workers:
        retired.update(t for t in w.get('retired_threads') or [] if t)
    mains, seen = [], {}

    def add(entry):
        thread = entry['thread_id']
        if thread and thread in retired:
            return
        key = thread or ('chat:' + entry['chat_id'])
        old = seen.get(key)
        if old is not None:
            old['operator'] = old['operator'] or entry['operator']
            if not old['chat_id']:
                old['chat_id'] = entry['chat_id']
            return
        seen[key] = entry
        mains.append(entry)

    for chat in chats:
        if account and chat.get('account_id') and chat.get('account_id') != account:
            continue
        if chat.get('archived') or chat.get('state') == 'archived':
            continue
        wid = chat.get('main_worker') or (chat.get('worker_id') if chat.get('operator') else '')
        worker = by_id.get(wid) or {}
        if not wid or _gone(worker):
            continue
        # Chats often carry no account_id: the Main worker's account decides. A Main of another
        # account is not reachable in this window ('Du hast keinen Zugriff auf diesen Chat').
        if account and worker.get('account_id') and worker.get('account_id') != account:
            continue
        thread = worker.get('thread_id') or chat.get('thread_id') or ''
        add(dict(chat_id=chat['id'], worker_id=wid, thread_id=thread,
                 operator=bool(chat.get('operator')) or worker.get('purpose') == 'operator',
                 title=_main_title(worker, chat), role='main', url=chat_url(thread) if thread else ''))
    for worker in all_workers:
        if not _is_main_worker(worker) or _gone(worker):
            continue
        if account and worker.get('account_id') and worker.get('account_id') != account:
            continue
        thread = worker.get('thread_id') or ''
        if not thread:
            continue
        add(dict(chat_id=str(worker.get('chat_id') or ''), worker_id=worker['id'], thread_id=thread,
                 operator=worker.get('purpose') == 'operator', title=_main_title(worker), role='main',
                 url=chat_url(thread)))
    return mains


BUSY_TASK_STATES = ('running', 'sending', 'sent', 'uncertain', 'reviewing', 'cancelling', 'claimed',
                    'tool_pending', 'executing', 'fixing')


def _task_label(task):
    """Short label of a coworker's current task: title, else summary, else the first prompt line; '' without task."""
    if not task:
        return ''
    for key in ('title', 'summary'):
        value = str(task.get(key) or '').strip()
        if value:
            return value.splitlines()[0].strip()
    for line in str(task.get('prompt') or '').splitlines():
        line = line.strip()
        if line and not line.startswith('End your answer with a final line RESULT'):
            return line
    return 'Working'


def desktop_state(server, app_settings=None, now=None):
    """The pushed state for the desktop parts (see docs/DESKTOP_APP.md, part contract)."""
    store = server.store
    now = time.time() if now is None else now
    try:
        from .harness_account import selected_account
        account = selected_account(store) or ''
    except Exception:
        account = ''
    try:
        coordination = server.engine.coordination()
    except Exception:
        coordination = {}
    stalled = getattr(server, 'stalled_tasks', None)
    base = notion_shell.build_state(store, account, coordination, now=now,
                                    stalled=stalled if isinstance(stalled, dict) else None)
    settings = store.settings()
    app = app_settings if app_settings is not None else load_app_settings()
    all_workers = store.all('workers')
    # Bug 2026-10-21: only coworkers of the selected account were listed, so coworkers working for a
    # Main on another account always looked idle/missing in the sidebar. Every coworker with an active
    # task is shown, whatever account it belongs to.
    working = {t.get('worker_id') for t in store.all('tasks')
               if t.get('worker_id') and t.get('state') in BUSY_TASK_STATES}
    workers = [w for w in all_workers if not account or w.get('account_id') == account or w['id'] in working]
    by_id = {w['id']: w for w in all_workers}

    mains = collect_mains(store.all('chats'), all_workers, account)
    main_threads = {m['thread_id'] for m in mains if m['thread_id']}
    hidden = set()
    for worker in all_workers:
        if worker.get('purpose') not in ('main', 'operator') and worker.get('thread_id'):
            hidden.add(worker['thread_id'])
        hidden.update(t for t in worker.get('retired_threads') or [] if t)
    hidden -= main_threads

    tasks = store.all('tasks')
    active = {}
    for task in tasks:
        if task.get('worker_id') and task.get('state') in BUSY_TASK_STATES:
            active[task.get('worker_id')] = task
    limit = int(_num(settings.get('context_token_limit'), 1200000)) or 1200000
    worker_rows, ask = [], None
    for worker in sorted(workers, key=lambda w: (w.get('purpose') != 'operator', w.get('purpose') != 'main',
                                                 str(w.get('label') or ''), w['id'])):
        purpose = worker.get('purpose')
        # Managed Welcome Coworkers that have not joined yet are shown too, marked as being prepared.
        preparing = (purpose == 'worker' and worker.get('join_state') == 'pending' and bool(worker.get('pool_admitted'))
                     and not _gone(worker))
        if purpose not in ('worker', 'main', 'operator') or (worker.get('enabled') is False and not preparing):
            continue
        usage = worker.get('context_usage') or {}
        task = active.get(worker['id']) or {}
        worker_rows.append(dict(id=worker['id'], label=str(worker.get('label') or worker['id'][-12:])[:80],
                                purpose=purpose, state='preparing' if preparing else str(worker.get('state') or ''),
                                preparing=preparing,
                                task_id=task.get('id', ''), task_title=_task_label(task)[:160],
                                thread_id=worker.get('thread_id') or '',
                                url=chat_url(worker['thread_id']) if worker.get('thread_id') else '',
                                created_at=_num(worker.get('created_at') or worker.get('thread_created_at')
                                                or worker.get('created'), 0),
                                generation=worker.get('generation') or 0,
                                model=str(task.get('model') or worker.get('model') or '')[:60],
                                effort=str(task.get('effort') or '')[:20], since=worker.get('created'),
                                context=dict(estimated_tokens=int(_num(usage.get('estimated_tokens'))),
                                             limit=limit)))
        notice = worker.get('context_ask')
        if ask is None and isinstance(notice, dict) and purpose in ('operator', 'main'):
            ask = dict(worker_id=worker['id'], tokens=int(_num(notice.get('tokens'))),
                       turns=int(_num(notice.get('turns'))),
                       limit_tokens=int(_num(notice.get('limit_tokens') or notice.get('ask_tokens'))))


    for row in worker_rows:  # team_rotation.py: chat age for the UI in seconds (None while unknown)
        row['age_s'] = int(max(0, now - row['created_at'])) if row.get('created_at') else None
    from .operator_main import RUN_FINISHED, plain_run_summary, plain_task_summary, run_outcome
    all_runs = sorted(store.all('runs'), key=lambda r: r.get('created') or 0)
    open_runs = [r for r in all_runs if r.get('state') not in RUN_FINISHED][-20:]
    open_ids = {r['id'] for r in open_runs}
    # open requests plus the last 20 finished ones (the Live view's History)
    runs = [r for r in all_runs if r['id'] in open_ids or r.get('state') in RUN_FINISHED]
    runs = [r for r in runs if r['id'] in open_ids] + [r for r in runs if r['id'] not in open_ids][-20:]
    runs.sort(key=lambda r: r.get('created') or 0)
    run_ids = {r['id'] for r in runs}
    run_rows = [dict(id=r['id'], state=str(r.get('state') or ''),
                     goal=str(r.get('goal') or r.get('title') or '')[:200], operator=bool(r.get('operator')),
                     chat_id=str(r.get('chat_id') or ''), main_worker=str(r.get('main_worker') or ''),
                     needs_main=bool(r.get('needs_main')), summary=plain_run_summary(r), outcome=run_outcome(r),
                     # Why the run waits for the Main (engine.py sets these per task); shown on the problem card.
                     needs_main_reasons={str(k)[:100]: str(v)[:240] for k, v in
                                         list((r.get('needs_main_reasons') if isinstance(r.get('needs_main_reasons'), dict) else {}).items())[:5]},
                     created=r.get('created'), finished=r.get('finished') or r.get('updated'),
                     tasks_total=sum(1 for t in tasks if t.get('run_id') == r['id']),
                     tasks_done=sum(1 for t in tasks if t.get('run_id') == r['id'] and t.get('state') == 'done'))
                for r in runs]
    task_rows = []
    for task in tasks:
        if task.get('run_id') not in run_ids:
            continue
        recent = now - _num(task.get('finished') or task.get('updated'), 0) < 1800
        if task.get('worker_id') in active and active[task['worker_id']] is task or recent or task.get('run_id') in open_ids:
            task_rows.append(dict(id=task['id'], run_id=task.get('run_id', ''),
                                  title=str(task.get('title') or '')[:160], state=str(task.get('state') or ''),
                                  worker_id=task.get('worker_id', ''), last_tool_at=task.get('last_tool_at'),
                                  last_output_at=task.get('last_output_at'),
                                  model=str(task.get('model') or '')[:60], effort=str(task.get('effort') or '')[:20],
                                  started=task.get('started'), created=task.get('created'),
                                  remote_unknown=bool(task.get('remote_unknown')),
                                  summary=plain_task_summary(task), finished=task.get('finished'),
                                  checked=task.get('main_resolution') == 'accept',
                                  files=[str(f)[:200] for f in (task.get('files') or [])][:20],
                                  result=str(task.get('result') or '')[:2000], error=str(task.get('error') or '')[:300]))
    task_rows = task_rows[-60:]
    events = []
    try:
        for event in store.events(limit=50) or []:
            doc = event if isinstance(event, dict) else {}
            events.append(dict(ts=doc.get('ts'), kind=str(doc.get('kind') or ''),
                               message=str(doc.get('message') or doc.get('reason') or '')[:200]))
    except Exception:
        events = []
    switches = sorted(store.all('main_switches'), key=lambda s: s.get('created') or 0)[-5:]
    limits = base.get('limits') or {}
    hide = {key[len('app_hide_'):]: bool(value) for key, value in app.items() if key.startswith('app_hide_')}
    try:  # own chats (New chat dialog and chats the user renamed himself) for the 'Chats' section
        from . import own_chats
        private = own_chats.private_chats(store, account)
    except Exception:
        private = []
    archived = archived_chats(all_workers, account)
    return dict(ts=round(float(now), 3), connected=True, account_id=account,
                mains=mains, private_chats=private, archived_chats=archived, hidden_threads=sorted(hidden),
                app_chat_filter=app.get('app_chat_filter') or 'mains_only', app_hide=hide,
                declutter=app.get('app_declutter', True) is not False,
                done_sound=app.get('app_done_sound', True) is not False,
                workers=worker_rows, runs=run_rows, tasks=task_rows, attention=_attention(base),
                events=events[-50:], context_ask=ask,
                switches=[dict(id=s.get('id', ''), state=s.get('state', ''), reason=str(s.get('reason') or '')[:160])
                          for s in switches],
                rate=dict(used_per_min=int(limits.get('window_used') or 0),
                          limit_per_min=int(limits.get('requests_per_min') or 180)),
                chats=base.get('chats'), links=base.get('links'),
                awaiting_main=base.get('awaiting_main'), stalled=base.get('stalled'),
                stale_context_tasks=base.get('stale_context_tasks'),
                token_budget=_token_budget(store, now), ai_disable=_ai_disable(store),
                steps_hour=_steps_hour(store, account, now),
                idle_mode=bool(store.settings().get('idle_mode', True)))


def _steps_hour(store, account, now):
    """AI steps used / left in the last 60 min of this account (step_guard inf_hard_60m, owner 2026-10-22)."""
    try:
        from . import step_guard
        st = step_guard.state(store, now, account=account) if account else step_guard.state(store, now)
        lim = st.get('limits') or step_guard.DEFAULTS
        limit = int(lim.get('inf_hard_60m') or 450)
        used = int(st.get('ai_steps_60m') or 0)
        s15 = int(st.get('steps_15m') or 0)
        limit15 = int(lim.get('step_hard_15m') or 1000)
        # rolling window (step_guard._ai_window): next_free_at = the oldest counted AI step drops out,
        # resets_at = the newest one drops out (bar fully free), under_limit_at = used < limit again
        def when(key):
            value = st.get(key)
            return round(float(value), 1) if isinstance(value, (int, float)) and value > 0 else None
        return dict(used=used, limit=limit, left=max(0, limit - used), steps_15m=s15, limit_15m=limit15,
                    level=str(st.get('level') or 'ok'), wait=int(st.get('wait') or 0),
                    resets_at=when('ai_resets_at'), next_free_at=when('ai_next_free_at'),
                    under_limit_at=when('ai_under_limit_at'), window_s=3600, window_15m_s=900)
    except Exception:
        return None


def _token_budget(store, now):
    """6-hour token budget for the Live view and the composer block (token_budget.public)."""
    try:
        from .token_budget import public
        report = public(store, now=now)
        report.pop('history', None)
        report['by_worker'] = report.get('by_worker', [])[:12]
        return report
    except Exception:
        return None


def _ai_disable(store):
    """Newest 'Notion AI was disabled' detections (ai_disable_detector), newest first."""
    try:
        from . import ai_disable_detector
        rows = ai_disable_detector.reports(store, limit=3) if hasattr(ai_disable_detector, 'reports') else []
        return [dict(at=r.get('detected_at') or r.get('at'), account=str(r.get('account') or '')[:80],
                     tokens_6h=int(((r.get('budget') or {}).get('used')) or 0),
                     cause=str(((r.get('causes') or [{}])[0] or {}).get('cause') or '')[:240]) for r in rows]
    except Exception:
        return []


# ---- handlers ---------------------------------------------------------------
def desktop_handlers(server, data_dir=None, sleep=time.sleep, clock=time.monotonic, wait=CREATE_WAIT):
    handlers = notion_shell.default_handlers(server)
    store = server.store

    def state(args):
        return desktop_state(server, load_app_settings(data_dir))

    def new_welcome_chat(args):
        request_id = 'desk-' + uuid.uuid4().hex[:16]
        op = server.action('/api/main/create', {'request_id': request_id})
        op_id = (op or {}).get('id') or 'create_main:' + request_id
        deadline = clock() + wait
        while clock() < deadline:
            current = store.get('operations', op_id) or op or {}
            if current.get('state') == 'failed':
                raise ValueError(current.get('error') or 'The Welcome chat could not be created')
            if current.get('state') == 'done' and current.get('thread_id'):
                return dict(title=current.get('title', ''), chat_id=current.get('chat_id', ''),
                            thread_id=current['thread_id'], url=chat_url(current['thread_id']))
            sleep(1.0)
        current = store.get('operations', op_id) or {}
        return dict(pending=True, title=current.get('title', ''), state=current.get('state', ''))

    def settings_schema(args):
        from . import desktop_settings
        current = dict(notion_shell.PAGE_DEFAULTS)
        current.update(store.settings())
        current.update(load_app_settings(data_dir))
        return desktop_settings.settings_schema(current)

    def app_settings_get(args):
        return load_app_settings(data_dir)

    def app_settings_set(args):
        values = args.get('values') if isinstance(args.get('values'), dict) else args
        if not values:
            raise ValueError('No settings given')
        return save_app_settings(values, data_dir)

    def main_switch_start(args):
        for record in store.all('main_switches'):
            if record.get('state') in SWITCH_ACTIVE:
                return dict(record, duplicate=True)
        return server.action('/api/main/switch/start', {'request_id': 'desk-switch-' + time.strftime('%Y%m%d-%H%M')})

    def app_chat_create(args):
        # New chat dialog: Welcome chat, renamed to the user's name, then listed in NotionTool.
        from . import own_chats
        body = {k: args.get(k) for k in ('name', 'main', 'request_id') if k in args}
        result = server.action('/api/app/chat/create', body) or {}
        ident = result.get('id') or ''
        deadline = clock() + wait
        while ident and clock() < deadline:
            ledger = store.get(own_chats.APP_LEDGER, ident) or {}
            if ledger.get('state') == 'done' and ledger.get('thread_id'):
                return dict(own_chats._public(ledger, False), url=chat_url(ledger['thread_id']))
            if ledger.get('state') in ('failed', 'rename_failed'):
                raise ValueError(ledger.get('error') or 'The chat could not be created')
            sleep(1.0)
        return dict(result, pending=True)

    def app_chat_rename(args):
        from . import own_chats
        return server.action('/api/app/chat/rename', {k: args.get(k) for k in ('thread_id', 'name')})

    def app_chat_promote(args):
        from . import own_chats
        return server.action('/api/app/chat/promote',
                             {k: args.get(k) for k in ('main', 'thread_id', 'chat_id') if k in args})

    def chat_context_switch(args):
        # Context switch button (sidebar): a Coworker gets a fresh chat before its next task
        # (/api/chat/compress, nothing sent now); a Main starts the ask-first Main switch.
        args = args or {}
        worker_id = str(args.get('worker_id') or '')
        if worker_id:
            worker = store.get('workers', worker_id) or {}
            if worker.get('purpose') == 'worker':
                return dict(server.action('/api/chat/compress', {'worker_id': worker_id,
                            'reason': 'Context switch button'}) or {}, kind='coworker')
        for record in store.all('main_switches'):
            if record.get('state') in SWITCH_ACTIVE:
                return dict(record, duplicate=True, kind='main')
        body = {'request_id': 'desk-switch-' + time.strftime('%Y%m%d-%H%M%S')}
        if args.get('chat_id'):
            body['chat_id'] = str(args['chat_id'])
        return dict(server.action('/api/main/switch/start', body) or {}, kind='main')

    handlers.update(app_chat_create=app_chat_create, app_chat_rename=app_chat_rename,
                    app_chat_promote=app_chat_promote, chat_context_switch=chat_context_switch)
    handlers.update(state=state, runs=lambda args: state(args)['runs'], new_welcome_chat=new_welcome_chat,
                    settings_schema=settings_schema, app_settings_get=app_settings_get,
                    app_settings_set=app_settings_set, main_switch_start=main_switch_start,
                    main_refresh=lambda args: server.action('/api/main/refresh',
                        dict(request_id=str((args or {}).get('request_id') or '')[:100])))
    return handlers


# ---- shell ------------------------------------------------------------------
class DesktopShell(NotionShell):
    """NotionShell bridge on the dedicated Notion desktop instance instead of an Edge tab."""

    def __init__(self, app, account_id, handlers, *, asset_dir=None, secrets=(), **kwargs):
        super().__init__(None, account_id, {}, asset_dir=asset_dir or DESK_DIR, secrets=secrets, **kwargs)
        self.app = app
        self.handlers = {k: v for k, v in dict(handlers or {}).items() if k in DESK_ACTIONS}
        self.build = ''
        self.page_refresh = ''
        self.teardown_result = None

    def injected_source(self):
        source = build_source(self.asset_dir)
        if self._leaks(source, notion_shell.INJECT_MARKERS):
            raise ValueError('The desktop overlay contains a credential marker or the local host address')
        self.build = source_build(source)
        return source

    def page_build(self, session):
        """{installed, build} of the overlay in the open page, or None if the page cannot be asked."""
        try:
            result = session.send('Runtime.evaluate', {'expression': PROBE_JS, 'returnByValue': True}, 10) or {}
        except Exception:
            return None
        value = (result.get('result') or {}).get('value') if isinstance(result, dict) else None
        return value if isinstance(value, dict) else None

    def refresh_page(self, session):
        """On attach: compare the page's overlay build with ours and tear an older one down.

        Never reloads the page. Returns 'fresh' (no overlay yet), 'current' (same build, nothing to do),
        'replaced' (older or unmarked overlay removed) or 'unknown' (the page could not be asked; the new
        source still runs and its own guard tears down a marked older build).
        """
        page = self.page_build(session)
        if page is None:
            return 'unknown'
        if not page.get('installed'):
            return 'fresh'
        if self.build and page.get('build') == self.build:
            return 'current'
        try:
            result = session.send('Runtime.evaluate', {'expression': '(' + TEARDOWN_JS.strip() + ')()',
                                                       'returnByValue': True}, 15) or {}
            self.teardown_result = (result.get('result') or {}).get('value') if isinstance(result, dict) else None
        except Exception as exc:  # noqa: BLE001 - the new source is still evaluated below
            self.teardown_result = {'error': str(exc)[:200]}
        return 'replaced'

    def info(self):
        info = super().info()
        if isinstance(info, dict):
            info = dict(info, build=self.build, page_refresh=self.page_refresh)
        return info

    def start(self, timeout=90, sleep=time.sleep):
        with self._lock:
            if self._closed:
                raise RuntimeError('This desktop shell was closed; create a new one')
            if self._started:
                return self.info()
            source = self.injected_source()
            deadline = self.clock() + timeout
            info = self.app.start(timeout=timeout)
            while not info.get('ready'):
                if self.clock() >= deadline:
                    raise TimeoutError('The NotionTool window shows no Notion page yet')
                sleep(1.0)
                info = self.app.start(timeout=timeout)
            self.target_id = info.get('target_id', '')
            self.session = self.app.session
            self.session.on_event = self._on_event
            # Re-attach after a harness restart: replace an older overlay in the open page right away
            # (inject() adds the script for new documents and also evaluates it now). No reload.
            self.page_refresh = self.refresh_page(self.session)
            self.app.inject(source)
            self.script_id = self.app.script_id
            self.enable_model_pin(self.session)  # the composer never sends 'Auto' (notion_shell.pin_config_body)
            self._started = True
            return self.info()

    def dispatch(self, payload):
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
        action, args = message.get('action'), message.get('args')
        args = {} if args is None else args
        if not isinstance(action, str) or action not in DESK_ACTIONS:
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
        return self._reply(ident, {'ok': True, 'result': notion_shell.scrub(result)})

    def close(self, stop_app=False):
        """Detach from the window (it stays open unless stop_app). Idempotent."""
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
        if session is not None and not getattr(session, 'closed', False):
            session.on_event = None
            if self.script_id:
                self._quiet(session, 'Page.removeScriptToEvaluateOnNewDocument', {'identifier': self.script_id})
            self._quiet(session, 'Runtime.removeBinding', {'name': BINDING})
        if not stop_app:
            self.app._terminate = None
        try:
            self.app.close()
        except Exception:
            pass
        if self._own_executor:
            self.executor.shutdown(wait=False)
        return True


# ---- lifecycle --------------------------------------------------------------
class _Attached:
    pid = None


class DesktopController:
    """Start, watch and stop the NotionTool desktop window for server.py."""

    def __init__(self, server, *, port=DEFAULT_PORT, profile_dir=None, exe_finder=find_exe, data_dir=None,
                 app_factory=None, http_get=None, stopper=stop_processes, push_interval=1.0,
                 runtime_check=None, runtime_root=None):
        # Startup self-check of the pinned Notion runtime (notion_runtime.py). On by default only with the
        # real finder; tests with fake finders/apps keep their old behaviour unless they turn it on.
        self.runtime_check = (exe_finder is find_exe) if runtime_check is None else bool(runtime_check)
        self.runtime_root = runtime_root
        self.runtime = None
        self._restart_after = False
        self.server = server
        self.port = int(port)
        self.profile_dir = profile_dir or default_profile()
        self.exe_finder = exe_finder
        self.data_dir = data_dir
        self.app_factory = app_factory or DesktopApp
        self.http_get = http_get
        self.stopper = stopper
        self.push_interval = push_interval
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.shell = None
        self.thread = None
        self.state = 'stopped'
        self.error = ''
        self.started_at = None
        self.pusher = None

    def status(self):
        shell = self.shell
        app = getattr(shell, 'app', None)
        return dict(state=self.state, error=self.error, port=self.port, profile=self.profile_dir,
                    pid=getattr(app, 'pid', None), target_id=getattr(shell, 'target_id', '') if shell else '',
                    started_at=self.started_at, runtime=self.runtime,
                    push=self.pusher.stats.snapshot() if getattr(self, 'pusher', None) is not None else None)

    def autostart(self):
        if load_app_settings(self.data_dir).get('app_autostart'):
            return self.start()
        return self.status()

    def start(self):
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                return self.status()
            self.stop_event.clear()
            self.state, self.error, self.started_at = 'starting', '', time.time()
            self.thread = threading.Thread(target=self._run, daemon=True, name='desktop-app')
            self.thread.start()
        return self.status()

    def _ensure_protocol(self):
        """Undo the notion:// handler takeover of 2026-10-21 (the Store Notion ignores it)."""
        if getattr(self, '_protocol_restored', False):
            return
        self._protocol_restored = True
        try:
            from . import protocol_forward
            protocol_forward.restore()
        except Exception:
            pass

    def _start_login_assist(self):
        try:
            from .window_login import LoginAssist
            if getattr(self, '_assist', None) is None:
                kwargs = {'http_get': self.http_get} if self.http_get is not None else {}
                kwargs['on_signed_in'] = getattr(self.server, 'window_signed_in', None)
                self._assist = LoginAssist(self.server.store, self.port, **kwargs)
            self._assist.start()
        except Exception as exc:  # noqa: BLE001
            try:
                self.server.store.event('window_login_failed', message='assist: ' + str(exc)[:180])
            except Exception:
                pass

    def _stop_login_assist(self):
        assist = getattr(self, '_assist', None)
        if assist is not None:
            assist.stop()

    def _quit_with_window(self, checks=3, interval=1.0):
        """User closed the NotionTool window: quit NotionTool completely (user rule I49).

        Stops the Notion app processes of the NotionTool profile (background/tray) and asks main.py to
        exit, so a new start is a clean start. Only when the window is really gone (DevTools down on
        every check), never on a short hiccup. Setting quit_with_window=False keeps the old behaviour."""
        exit_event = getattr(self.server, 'exit_event', None)
        if exit_event is None:
            return False
        try:
            if self.server.store.settings().get('quit_with_window', True) is False:
                return False
        except Exception:
            pass
        for _ in range(checks):
            if self.stop_event.wait(interval):
                return True  # the app is stopping anyway
            if self._devtools_up():
                return False  # window still (or again) there: reattach instead
        try:
            self.server.store.event('desktop_app_quit', message='NotionTool window closed: NotionTool stops completely')
        except Exception:
            pass
        try:
            self.stopper(self.profile_dir)
        except Exception:
            pass
        exit_event.set()
        return True

    def _await_reopen(self, interval=2.0):
        """After the user closed the window: attach again as soon as it is reopened (second NotionTool start)."""
        runner = threading.current_thread()

        def watch():
            if runner is not threading.current_thread():
                runner.join()
            while not self.stop_event.is_set():
                if self._devtools_up():
                    self.start()
                    return
                self.stop_event.wait(interval)

        threading.Thread(target=watch, daemon=True, name='desktop-app-reopen').start()

    def _devtools_up(self):
        from .desktop_app import _default_http_get
        getter = self.http_get or _default_http_get
        try:
            return isinstance(getter('http://127.0.0.1:%d/json/version' % self.port), dict)
        except Exception:
            return False

    def _heal_error_page(self, shell):
        """For about a minute after start: reload only Notion's 'Something went wrong' page (error_page_heal.py)."""
        from . import error_page_heal
        try:
            error_page_heal.heal(lambda: getattr(getattr(shell, 'app', None), 'session', None),
                                 self.stop_event, self.server.store)
        except Exception:  # noqa: BLE001
            pass

    def _verify_runtime(self, shell, tries=10, interval=1.0):
        """Startup self-check of the pinned Notion runtime (notion_runtime.py).

        The overlay must really be in the page. Loaded -> this Notion version becomes last_good. Missing on
        the version that is on trial -> roll back to the last good version and restart the window (returns
        False). Missing without an older good version -> state 'degraded' with a clear message (True)."""
        from . import notion_runtime
        value = None
        for _ in range(max(1, tries)):
            session = getattr(getattr(shell, 'app', None), 'session', None)
            try:
                result = session.send('Runtime.evaluate', {'expression': VERIFY_JS, 'returnByValue': True}, 10) or {}
                value = (result.get('result') or {}).get('value') if isinstance(result, dict) else None
            except Exception:  # noqa: BLE001
                value = None
            if isinstance(value, dict) and value.get('installed'):
                break
            if self.stop_event.wait(interval):
                return True
        ua = value.get('ua', '') if isinstance(value, dict) else ''
        running = notion_runtime.ua_version(ua)
        state = notion_runtime.load_state(self.runtime_root)
        current = state.get('current') or ''
        on_pinned = bool(current) and notion_runtime.matches(current, ua)
        if isinstance(value, dict) and value.get('installed'):
            if on_pinned:
                notion_runtime.mark_good(current, self.runtime_root)
            self.runtime = dict(ok=True, notion=running, pinned=current, last_good=state.get('last_good') or '')
            return True
        message = 'The NotionTool overlay did not load on Notion %s' % (running or '?')
        self.runtime = dict(ok=False, notion=running, pinned=current, last_good=state.get('last_good') or '')
        try:
            self.server.store.event('desktop_overlay_missing', message=message[:200])
        except Exception:
            pass
        if on_pinned and notion_runtime.rollback(self.runtime_root):
            self._restart_after = True
            self.error = message + '; going back to Notion ' + str(state.get('last_good'))
            return False
        self.state, self.error = 'degraded', message + ' (no older working Notion version to go back to)'
        return True

    def _restart_later(self):
        runner = threading.current_thread()

        def go():
            runner.join(timeout=30)
            if not self.stop_event.is_set():
                self.start()

        threading.Thread(target=go, daemon=True, name='desktop-app-rollback').start()

    def _make_app(self):
        secrets = notion_shell.shell_secrets(self.server)
        kwargs = dict(secrets=secrets)
        if self.http_get is not None:
            kwargs['http_get'] = self.http_get
        if self._devtools_up():
            # Our window is already open (for example after a harness restart): attach, never spawn twice.
            kwargs['spawn'] = lambda argv: _Attached()
            exe = 'attached'
        else:
            exe = self.exe_finder()
        return self.app_factory(exe, self.profile_dir, self.port, **kwargs)

    def _run(self):
        shell = None
        try:
            from .harness_account import selected_account
            account = selected_account(self.server.store) or ''
            app = self._make_app()
            shell = DesktopShell(app, account, desktop_handlers(self.server, self.data_dir),
                                 secrets=notion_shell.shell_secrets(self.server))
            self.shell = shell
            # The Store Notion swallows notion:// links, so 'Continue in desktop app' can never reach this
            # window. window_login.py catches 'Continue in browser' and moves the session in via DevTools.
            self._ensure_protocol()
            self._start_login_assist()
            while True:
                try:
                    shell.start()
                    break
                except Exception as exc:  # noqa: BLE001
                    # 'Continue in browser' returns via notion://; route it to this window (protocol_forward.py).
                    self._ensure_protocol()
                    # Login screen or no workspace page yet: keep waiting until the user has logged in.
                    self.state = 'waiting_login'
                    self.error = 'Log in once in the NotionTool window (%s)' % (str(exc)[:120] or type(exc).__name__)
                    if self.stop_event.wait(5):
                        return
            # The login assist keeps running: it idles on workspace pages and wakes up after a sign-out.
            self.state, self.error = 'running', ''
            self.server.store.event('desktop_app_started', message='NotionTool window attached on port %d' % self.port)
            # Pinned runtime self-check: overlay really loaded? Else roll back to the last good Notion.
            if self.runtime_check and not self._verify_runtime(shell):
                return
            # After a restart Notion sometimes shows 'Oops! Something went wrong ... Refresh'. Reload that page
            # once or twice from here (backend, not desktop JS) so the window is ready without a manual click.
            threading.Thread(target=self._heal_error_page, args=(shell,), daemon=True,
                             name='desktop-heal-error-page').start()
            # Cached state + non-blocking pusher with a 3 s heartbeat (desktop_state_cache.py). The old loop
            # pushed only on change, so an idle harness sent nothing and the overlay showed
            # 'lost connection to the harness' although the bridge was fine.
            from . import desktop_state_cache
            cache = desktop_state_cache.DesktopStateCache(self.server, self.data_dir)
            self.pusher = desktop_state_cache.StatePusher(shell, cache, store=self.server.store)
            while not self.stop_event.is_set() and shell.alive:
                try:
                    self.pusher.tick()
                except Exception as exc:
                    self.error = 'state: ' + str(exc)[:160]
                self.stop_event.wait(self.push_interval)
            if not self.stop_event.is_set():
                self.state, self.error = 'stopped', 'The NotionTool window was closed'
                if not self._quit_with_window():
                    self._await_reopen()
        except Exception as exc:
            self.state, self.error = 'failed', str(exc)[:300]
            try:
                self.server.store.event('desktop_app_failed', message=self.error[:200])
            except Exception:
                pass
        finally:
            if shell is not None:
                try:
                    shell.close(stop_app=False)
                except Exception:
                    pass
            if self._restart_after:
                # Rollback: close the window that runs the failed Notion and start the last good one.
                self._restart_after = False
                self.state = 'restarting'
                try:
                    self.stopper(self.profile_dir)
                except Exception:
                    pass
                self._restart_later()

    def stop(self, close_window=True):
        self.stop_event.set()
        thread = self.thread
        if thread is not None:
            thread.join(timeout=10)
        stopped = self.stopper(self.profile_dir) if close_window else []
        self.state = 'stopped'
        self.shell = None
        return dict(self.status(), stopped_pids=stopped)
