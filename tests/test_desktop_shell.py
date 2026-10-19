import json
import tempfile
import unittest
from pathlib import Path

from notiontool import desktop_shell as ds


class FakeStore:
    def __init__(self):
        self.docs = {k: {} for k in ('workers', 'chats', 'tasks', 'runs', 'main_switches', 'operations', 'accounts')}
        self.evts = []

    def all(self, kind):
        return list(self.docs.get(kind, {}).values())

    def get(self, kind, ident, default=None):
        return self.docs.get(kind, {}).get(ident, default)

    def put(self, kind, doc):
        self.docs.setdefault(kind, {})[doc['id']] = doc

    def settings(self):
        return {'context_token_limit': 1000000, 'harness_account_id': 'acc'}

    def events(self, after=0, limit=150):
        return [dict(seq=1, ts=1.0, kind='task_finished', message='done')]

    def event(self, kind, **fields):
        self.evts.append((kind, fields))


class FakeServer:
    def __init__(self):
        self.store = FakeStore()
        self.actions = []

    def action(self, path, body):
        self.actions.append((path, body))
        if path == '/api/main/create':
            op = dict(id='create_main:' + body['request_id'], state='done', title='Main 7', thread_id='abc-123',
                      chat_id='c7')
            self.store.put('operations', op)
            return op
        if path == '/api/main/switch/start':
            return dict(id='sw', state='brief_requested')
        return {}


def seed(store):
    store.put('workers', dict(id='acc:main', account_id='acc', purpose='operator', thread_id='t-main',
                              label='Operator Main', state='ready',
                              context_ask=dict(tokens=1400000, turns=10), context_usage=dict(estimated_tokens=1400000)))
    store.put('workers', dict(id='acc:w1', account_id='acc', purpose='worker', thread_id='t-w1', label='Coworker 1',
                              state='busy', retired_threads=['t-old'], context_usage=dict(estimated_tokens=500000)))
    store.put('workers', dict(id='acc:old', account_id='acc', purpose='archived_operator', thread_id='t-arch'))
    store.put('chats', dict(id='c1', account_id='acc', main_worker='acc:main', operator=True, title='Operator Main'))
    store.put('chats', dict(id='c2', account_id='acc', main_worker='acc:old', title='Old'))
    store.put('runs', dict(id='r1', state='executing', goal='Build app', created=1))
    store.put('tasks', dict(id='t1', run_id='r1', worker_id='acc:w1', state='running', title='Sidebar'))


class DesktopShellTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = FakeServer()
        seed(self.server.store)

    def tearDown(self):
        self.tmp.cleanup()

    def test_source_is_clean_and_mounts_parts(self):
        src = ds.build_source()
        for marker in ('127.0.0.1', 'Bearer ', 'token_v2'):
            self.assertNotIn(marker, src)
        self.assertIn('__ntHarnessReply', src)
        self.assertIn('__ntHarnessPush', src)
        self.assertIn('"sidebar"', src)
        self.assertNotIn('MOUNT_ORDER.forEach', src)

    def test_state_lists_mains_and_hides_coworkers(self):
        state = ds.desktop_state(self.server, ds.load_app_settings(self.tmp.name), now=100.0)
        self.assertEqual([m['chat_id'] for m in state['mains']], ['c1'])
        self.assertIn('t-w1', state['hidden_threads'])
        self.assertIn('t-old', state['hidden_threads'])
        self.assertNotIn('t-main', state['hidden_threads'])
        self.assertEqual(state['context_ask']['tokens'], 1400000)
        self.assertEqual(state['app_chat_filter'], 'mains_only')
        self.assertTrue(state['app_hide']['home'])
        w1 = next(w for w in state['workers'] if w['id'] == 'acc:w1')
        self.assertEqual(w1['task_title'], 'Sidebar')
        self.assertEqual(w1['context']['limit'], 1000000)
        self.assertEqual(state['rate']['limit_per_min'], 180)
        json.dumps(state)

    def test_new_welcome_chat_uses_main_create_with_request_id(self):
        handlers = ds.desktop_handlers(self.server, self.tmp.name, sleep=lambda s: None)
        result = handlers['new_welcome_chat']({})
        path, body = self.server.actions[-1]
        self.assertEqual(path, '/api/main/create')
        self.assertTrue(body['request_id'].startswith('desk-'))
        self.assertEqual(result['thread_id'], 'abc-123')
        self.assertIn('abc123', result['url'])

    def test_app_settings_validate_and_persist(self):
        handlers = ds.desktop_handlers(self.server, self.tmp.name)
        out = handlers['app_settings_set']({'values': {'app_chat_filter': 'hide_coworkers', 'app_hide_home': 'false'}})
        self.assertEqual(out['app_chat_filter'], 'hide_coworkers')
        self.assertFalse(out['app_hide_home'])
        self.assertEqual(ds.load_app_settings(self.tmp.name)['app_chat_filter'], 'hide_coworkers')
        with self.assertRaises(ValueError):
            handlers['app_settings_set']({'values': {'app_chat_filter': 'everything'}})
        with self.assertRaises(ValueError):
            handlers['app_settings_set']({'values': {'host_api_key': 'x'}})

    def test_switch_start_is_not_duplicated(self):
        handlers = ds.desktop_handlers(self.server, self.tmp.name)
        self.assertEqual(handlers['main_switch_start']({})['state'], 'brief_requested')
        self.server.store.put('main_switches', dict(id='s1', state='prompt_sent'))
        self.assertTrue(handlers['main_switch_start']({})['duplicate'])

    def test_dispatch_allows_desktop_actions_only(self):
        replies = []

        class App:
            pid = None

        shell = ds.DesktopShell(App(), 'acc', {'state': lambda a: {'ok': 1}, 'evil': lambda a: 1})
        shell._reply = lambda ident, payload: replies.append(payload)
        shell.dispatch(json.dumps({'id': 1, 'action': 'state'}))
        shell.dispatch(json.dumps({'id': 2, 'action': 'evil'}))
        shell.dispatch(json.dumps({'id': 3, 'action': 'new_welcome_chat'}))
        self.assertTrue(replies[0]['ok'])
        self.assertEqual(replies[1]['error'], 'Action not allowed')
        self.assertEqual(replies[2]['error'], 'Action not available')

    def test_our_processes_only_match_our_profile(self):
        class Out:
            stdout = '11|C:\\Notion.exe --user-data-dir=C:\\Prof\\X\n12|C:\\Notion.exe\n'
        pids = ds.our_processes('C:\\prof\\x', run=lambda *a, **k: Out())
        self.assertEqual(pids, [11])


# ---- re-attach: build id, teardown of an older overlay (fake CDP client) -----------------------
import hashlib
import os
import shutil
import subprocess

from notiontool.desktop_app import DesktopApp

NODE = shutil.which('node') or r'C:\Program Files\nodejs\node.exe'


class FakeCdp:
    """Fake DevTools session: records every call and answers the build probe like the page would."""

    def __init__(self, page=None, probe_error=False):
        self.page = page
        self.probe_error = probe_error
        self.calls = []
        self.closed = False
        self.on_event = None

    def send(self, method, params=None, timeout=15):
        params = dict(params or {})
        self.calls.append((method, params))
        expr = params.get('expression', '')
        if method == 'Page.addScriptToEvaluateOnNewDocument':
            return {'identifier': 'script-1'}
        if method == 'Runtime.evaluate' and expr == ds.PROBE_JS:
            if self.probe_error:
                raise RuntimeError('Execution context was destroyed')
            if self.page is None:
                return {'result': {'type': 'undefined'}}
            return {'result': {'type': 'object', 'value': dict(self.page)}}
        if method == 'Runtime.evaluate' and expr == TEARDOWN_CALL:
            return {'result': {'type': 'object', 'value': {'stopped': 2, 'removed': 3, 'cleaned': 4, 'errors': 0}}}
        return {}

    def close(self):
        self.closed = True

    def methods(self):
        return [m for m, _ in self.calls]

    def index(self, method, expression=None):
        for i, (m, p) in enumerate(self.calls):
            if m == method and (expression is None or p.get('expression') == expression):
                return i
        return -1


TEARDOWN_CALL = '(' + ds.TEARDOWN_JS.strip() + ')()'


def attach_app(cdp):
    """A real DesktopApp attached (no spawn) to an already open Notion page on the DevTools port."""
    target = dict(type='page', id='T1', url='https://www.notion.so/workspace-abc',
                  webSocketDebuggerUrl='ws://127.0.0.1:%d/devtools/page/T1' % ds.DEFAULT_PORT)

    def http_get(url):
        return [target] if url.endswith('/json/list') else {'Browser': 'Notion'}

    return DesktopApp('attached', 'C:/prof', ds.DEFAULT_PORT, spawn=lambda argv: ds._Attached(),
                      http_get=http_get, connect=lambda ws: cdp, sleep=lambda s: None)


class DesktopReattachTests(unittest.TestCase):
    def attach(self, page=None, probe_error=False):
        cdp = FakeCdp(page, probe_error)
        shell = ds.DesktopShell(attach_app(cdp), 'acc', {})
        self.addCleanup(shell.close)
        shell.start(timeout=5, sleep=lambda s: None)
        return shell, cdp

    def assert_new_source_runs(self, shell, cdp):
        source = ds.build_source()
        add = cdp.index('Page.addScriptToEvaluateOnNewDocument')
        run = cdp.index('Runtime.evaluate', source)
        self.assertGreaterEqual(add, 0)
        self.assertEqual(cdp.calls[add][1], {'source': source})
        self.assertGreater(run, add)
        self.assertEqual(shell.script_id, 'script-1')
        for method in cdp.methods():
            self.assertNotIn(method, ('Page.reload', 'Page.navigate', 'Target.closeTarget'))
        return add, run

    def test_older_build_is_torn_down_then_new_source_runs(self):
        shell, cdp = self.attach({'installed': True, 'build': 'deadbeefdeadbeef'})
        probe = cdp.index('Runtime.evaluate', ds.PROBE_JS)
        teardown = cdp.index('Runtime.evaluate', TEARDOWN_CALL)
        add, run = self.assert_new_source_runs(shell, cdp)
        self.assertEqual(probe, 0)
        self.assertTrue(probe < teardown < add < run)
        self.assertEqual(shell.page_refresh, 'replaced')
        self.assertEqual(shell.teardown_result['stopped'], 2)
        self.assertEqual(shell.info()['page_refresh'], 'replaced')
        self.assertEqual(shell.info()['build'], ds.build_hash())

    def test_unmarked_old_overlay_is_replaced(self):
        shell, cdp = self.attach({'installed': True, 'build': ''})
        self.assertEqual(shell.page_refresh, 'replaced')
        self.assertGreaterEqual(cdp.index('Runtime.evaluate', TEARDOWN_CALL), 0)
        self.assert_new_source_runs(shell, cdp)

    def test_same_build_is_left_alone(self):
        shell, cdp = self.attach({'installed': True, 'build': ds.build_hash()})
        self.assertEqual(shell.page_refresh, 'current')
        self.assertEqual(cdp.index('Runtime.evaluate', TEARDOWN_CALL), -1)
        self.assert_new_source_runs(shell, cdp)

    def test_fresh_page_gets_overlay_without_teardown(self):
        shell, cdp = self.attach({'installed': False, 'build': ''})
        self.assertEqual(shell.page_refresh, 'fresh')
        self.assertEqual(cdp.index('Runtime.evaluate', TEARDOWN_CALL), -1)
        self.assert_new_source_runs(shell, cdp)

    def test_probe_failure_still_injects(self):
        shell, cdp = self.attach(probe_error=True)
        self.assertEqual(shell.page_refresh, 'unknown')
        self.assertEqual(cdp.index('Runtime.evaluate', TEARDOWN_CALL), -1)
        self.assert_new_source_runs(shell, cdp)

    def test_build_hash_follows_the_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'theme.js').write_text('window.__ntDesk.theme = function () {};', encoding='utf-8')
            first = ds.build_hash(tmp)
            source = ds.build_source(tmp)
            self.assertRegex(first, r'^[0-9a-f]{16}$')
            self.assertEqual(first, hashlib.sha256(ds._raw_source(tmp).encode('utf-8')).hexdigest()[:16])
            self.assertEqual(ds.source_build(source), first)
            self.assertEqual(ds.build_hash(tmp), first)
            self.assertIn("var BUILD = '%s';" % first, source)
            self.assertNotIn(ds.BUILD_MARK, source)
            self.assertNotIn(ds.TEARDOWN_MARK, source)
            (Path(tmp) / 'theme.css').write_text('.nt-desk-x{color:red}', encoding='utf-8')
            self.assertNotEqual(ds.build_hash(tmp), first)

    def test_boot_guards_build_and_keeps_part_handles(self):
        src = ds.build_source()
        self.assertIn('if (window.__ntDeskBuild === BUILD) return;', src)
        self.assertIn('window.__ntDeskBuild = BUILD;', src)
        self.assertIn('window.__ntDeskTeardown = function () {', src)
        self.assertIn('window.__ntDeskMounted[name] = handle;', src)
        self.assertIn('window.__ntDeskParts = window.__ntDeskMounted;', src)
        self.assertIn('[data-nt-desk-dead] { display: none !important; }', src)
        self.assertEqual(src.count('MOUNT_ORDER'), 0)
        self.assertEqual(src.count('MOUNT_ORDER.forEach'), 0)
        self.assertIn("old.getAttribute('data-nt-desk-build') !== BUILD", src)
        for text in ('reload', 'location.', 'removeChild', 'innerHTML'):
            self.assertNotIn(text, ds.TEARDOWN_JS)

    @unittest.skipUnless(NODE and os.path.isfile(NODE), 'node not installed')
    def test_teardown_removes_only_our_nodes(self):
        script = TEARDOWN_HARNESS.replace('__TEARDOWN__', ds.TEARDOWN_JS.strip())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'teardown.js'
            path.write_text(script, encoding='utf-8')
            res = subprocess.run([NODE, str(path)], capture_output=True, text=True, timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)
        got = json.loads(res.stdout)
        self.assertEqual(got['out'], {'stopped': 2, 'removed': 3, 'cleaned': 6, 'errors': 1})
        self.assertEqual(sorted(got['stopped']), ['a', 'd'])
        self.assertEqual(got['dead'], dict(style=True, panel=True, own=True, row=False, wrap=False))
        self.assertEqual(got['alive'], dict(style=False, row=True, bare=True, upsell=True, panel=False, own=False,
                                            wrap=True, kid=True, editor=True))
        self.assertEqual(got['classes'], dict(row=['notion-sidebar-item'], bare=[], wrap=[], html=[], upsell=[],
                                              kid=['notion-foo']))
        self.assertEqual(got['attrs'], dict(row=[], html=[], editor=['contenteditable']))
        self.assertEqual(got['win'], dict(installed=False, build='', desk=[], parts=[], mounted=[], same=True, teardown=None))


    @unittest.skipUnless(NODE and os.path.isfile(NODE), 'node not installed')
    def test_new_build_replaces_old_and_mounts_once(self):
        """Old build with handles is stopped, the new one mounts each part once; a second run is a no-op."""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'extras.js').write_text(
                "window.__ntDesk.extras = function (ctx) { mounts.push('extras'); "
                "return {stop: function () { stops.push('extras'); }}; };", encoding='utf-8')
            source = ds.build_source(tmp)
            build = ds.source_build(source)
            script = BOOT_HARNESS.replace('__SOURCE__', json.dumps(source))
            path = Path(tmp) / 'boot.js'
            path.write_text(script, encoding='utf-8')
            res = subprocess.run([NODE, str(path)], capture_output=True, text=True, timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)
        got = json.loads(res.stdout)
        self.assertEqual(got['stops'], ['old-live'])
        self.assertEqual(got['mounts'], ['extras'])
        self.assertEqual(got['build'], build)
        self.assertEqual(got['mounted'], ['extras'])
        self.assertEqual(got['styles'], 1)
        self.assertTrue(got['oldBarDead'])
        self.assertFalse(got['oldBarAttached'])
        self.assertEqual(got['pushTo'], 1)



BOOT_HARNESS = r"""
var mounts = [], stops = [];
function El(tag) { this.tagName = tag; this.id = ''; this.attrs = {}; this.children = []; this.parentNode = null;
  var list = []; list.add = function (t) { if (list.indexOf(t) < 0) list.push(t); };
  list.remove = function (t) { var i = list.indexOf(t); if (i >= 0) list.splice(i, 1); };
  list.toggle = function (t, on) { if (on) list.add(t); else list.remove(t); };
  list.contains = function (t) { return list.indexOf(t) >= 0; };
  this.classList = list; this.style = {}; this.textContent = ''; }
El.prototype.setAttribute = function (n, v) { this.attrs[n] = String(v); if (n === 'id') this.id = String(v); };
El.prototype.hasAttribute = function (n) { return Object.prototype.hasOwnProperty.call(this.attrs, n); };
El.prototype.getAttribute = function (n) { return this.hasAttribute(n) ? this.attrs[n] : null; };
El.prototype.removeAttribute = function (n) { delete this.attrs[n]; };
El.prototype.addEventListener = function () {};
El.prototype.appendChild = function (c) { if (c.parentNode) c.remove(); c.parentNode = this; this.children.push(c); return c; };
El.prototype.querySelectorAll = function () { var out = []; (function w(n) { n.children.forEach(function (c) { out.push(c); w(c); }); })(this); return out; };
El.prototype.remove = function () { var p = this.parentNode; if (!p) return; p.children.splice(p.children.indexOf(this), 1); this.parentNode = null; };
Object.defineProperty(El.prototype, 'className', { set: function (v) { var l = this.classList; l.length = 0; String(v).split(' ').filter(Boolean).forEach(function (t) { l.push(t); }); }, get: function () { return this.classList.join(' '); } });
Object.defineProperty(El.prototype, 'isConnected', { get: function () { var n = this; while (n.parentNode) n = n.parentNode; return n === html; } });
var html = new El('html'), head = new El('head'), body = new El('body');
html.appendChild(head); html.appendChild(body);
var oldStyle = new El('style'); oldStyle.id = 'nt-desk-style'; head.appendChild(oldStyle);
var oldBar = new El('div'); oldBar.className = 'nt-desk-bar'; body.appendChild(oldBar);
html.className = 'notion-html nt-theme nt-desk-chrome-on';
var document = { documentElement: html, head: head, body: body, readyState: 'complete',
  createElement: function (t) { return new El(t); }, createTextNode: function (t) { var n = new El('#text'); n.textContent = t; return n; },
  addEventListener: function () {},
  getElementById: function (id) { return [html].concat(html.querySelectorAll()).filter(function (n) { return n.id === id; })[0] || null; },
  querySelectorAll: function () { return [html].concat(html.querySelectorAll()); } };
var location = { hostname: 'www.notion.so' };
var pushTo = 0;
var window = { __ntDeskInstalled: true, __ntDeskBuild: 'oldbuild00000000', __ntDesk: {},
  __ntDeskMounted: { live: { stop: function () { stops.push('old-live'); } } },
  ntHarness: function (payload) { pushTo += 1; } };
window.top = window;
var localStorage = { getItem: function () { return null; } };
var console = globalThis.console;
var setTimeout = function () { return 0; };
var src = __SOURCE__;
(new Function('window', 'document', 'location', 'localStorage', 'setTimeout', 'mounts', 'stops', src))(window, document, location, localStorage, setTimeout, mounts, stops);
(new Function('window', 'document', 'location', 'localStorage', 'setTimeout', 'mounts', 'stops', src))(window, document, location, localStorage, setTimeout, mounts, stops);
console.log(JSON.stringify({ mounts: mounts, stops: stops, build: window.__ntDeskBuild, mounted: Object.keys(window.__ntDeskMounted),
  styles: document.querySelectorAll().filter(function (n) { return n.id === 'nt-desk-style'; }).length,
  oldBarDead: oldBar.hasAttribute('data-nt-desk-dead'), oldBarAttached: oldBar.isConnected, pushTo: pushTo }));
"""


TEARDOWN_HARNESS = r"""
var stopped = [];
var html = null;
function El(tag, opts) {
  opts = opts || {};
  this.tagName = tag; this.id = opts.id || ''; this.attrs = {}; this.children = []; this.parentNode = null;
  var a = opts.attrs || {};
  for (var k in a) this.attrs[k] = a[k];
  var list = String(opts.cls || '').split(' ').filter(Boolean);
  list.remove = function (t) { var i = list.indexOf(t); if (i >= 0) list.splice(i, 1); };
  this.classList = list;
  var self = this;
  (opts.kids || []).forEach(function (kid) { kid.parentNode = self; self.children.push(kid); });
}
El.prototype.setAttribute = function (n, v) { this.attrs[n] = String(v); };
El.prototype.hasAttribute = function (n) { return Object.prototype.hasOwnProperty.call(this.attrs, n); };
El.prototype.getAttribute = function (n) { return this.hasAttribute(n) ? this.attrs[n] : null; };
El.prototype.removeAttribute = function (n) { delete this.attrs[n]; };
El.prototype.querySelectorAll = function () {
  var out = [];
  (function walk(n) { n.children.forEach(function (c) { out.push(c); walk(c); }); })(this);
  return out;
};
El.prototype.remove = function () {
  var p = this.parentNode;
  if (!p) return;
  p.children.splice(p.children.indexOf(this), 1);
  this.parentNode = null;
};
Object.defineProperty(El.prototype, 'isConnected', { get: function () {
  var n = this; while (n.parentNode) n = n.parentNode; return n === html;
} });
var styleEl = new El('style', {id: 'nt-desk-style'});
var head = new El('head', {kids: [styleEl]});
var row = new El('div', {cls: 'notion-sidebar-item nt-desk-hidden nt-desk-main-row',
                         attrs: {'data-nt-desk': 'chat-row', 'data-nt-desk-thread': 'x'}});
var bare = new El('div', {cls: 'nt-desk-hidden'});
var upsell = new El('div', {cls: 'nt-desk-upsell'});
var panelTitle = new El('span', {cls: 'nt-desk-live-title'});
var panel = new El('div', {cls: 'nt-desk-live-panel', kids: [panelTitle]});
var ownBtn = new El('button', {attrs: {'data-nt-desk-own': '1'}});
var notionKid = new El('div', {cls: 'notion-foo'});
var wrap = new El('div', {cls: 'nt-desk-wrap', kids: [notionKid]});
var editor = new El('div', {cls: 'nt-desk-wrap2', attrs: {contenteditable: 'true'}});
var body = new El('body', {kids: [row, bare, upsell, panel, ownBtn, wrap, editor]});
html = new El('html', {cls: 'nt-theme nt-desk-chrome-on', attrs: {'data-nt-desk-theme': 'dark'}, kids: [head, body]});
var document = {
  documentElement: html, head: head, body: body,
  getElementById: function (id) { return [html].concat(html.querySelectorAll()).filter(function (n) { return n.id === id; })[0] || null; },
  querySelectorAll: function () { return [html].concat(html.querySelectorAll()); }
};
var partA = {stop: function () { stopped.push('a'); }};
var window = { __ntDeskInstalled: true, __ntDeskBuild: 'old', __ntDesk: {sidebar: function () {}},
  __ntDeskTeardown: function () {},
  __ntDeskParts: { a: partA, b: {stop: function () { throw new Error('x'); }}, c: {} },
  __ntDeskMounted: { a: partA, d: {stop: function () { stopped.push('d'); }} } };
var teardown = __TEARDOWN__;
var out = teardown();
function alive(n) { return n.isConnected; }
console.log(JSON.stringify({ out: out, stopped: stopped,
  alive: {style: alive(styleEl), row: alive(row), bare: alive(bare), upsell: alive(upsell), panel: alive(panel),
          own: alive(ownBtn), wrap: alive(wrap), kid: alive(notionKid), editor: alive(editor)},
  classes: {row: row.classList.slice(), bare: bare.classList.slice(), wrap: wrap.classList.slice(),
            html: html.classList.slice(), upsell: upsell.classList.slice(), kid: notionKid.classList.slice()},
  attrs: {row: Object.keys(row.attrs), html: Object.keys(html.attrs), editor: Object.keys(editor.attrs)},
  win: {installed: window.__ntDeskInstalled, build: window.__ntDeskBuild, desk: Object.keys(window.__ntDesk),
        parts: Object.keys(window.__ntDeskParts), mounted: Object.keys(window.__ntDeskMounted),
        same: window.__ntDeskParts === window.__ntDeskMounted, teardown: window.__ntDeskTeardown},
  dead: {style: styleEl.hasAttribute('data-nt-desk-dead'), panel: panel.hasAttribute('data-nt-desk-dead'),
         own: ownBtn.hasAttribute('data-nt-desk-dead'), row: row.hasAttribute('data-nt-desk-dead'),
         wrap: wrap.hasAttribute('data-nt-desk-dead')} }));
"""


SHELL_UI_HARNESS = r"""
const fs = require('fs'), vm = require('vm');
let doc;
function parse(sel) {
  return sel.split(',').map(function (part) {
    part = part.trim();
    const m = /^([a-zA-Z]*)((?:\.[\w-]+)*)((?:\[[^\]]+\])*)$/.exec(part);
    if (!m) throw new Error('selector ' + part);
    const attrs = []; const re = /\[([\w-]+)(?:="([^"]*)")?\]/g; let a;
    while ((a = re.exec(m[3]))) attrs.push([a[1], a[2]]);
    return { tag: m[1].toUpperCase(), cls: m[2].split('.').filter(Boolean), attrs: attrs };
  });
}
function El(tag) {
  this.tagName = String(tag).toUpperCase(); this.attrs = {}; this.children = []; this.parentNode = null;
  this.style = {}; this._text = ''; this.listeners = {}; this.rect = 0; this.innerHTML = '';
  const cl = [];
  cl.add = function () { for (const x of arguments) if (cl.indexOf(x) < 0) cl.push(x); };
  cl.remove = function () { for (const x of arguments) { const i = cl.indexOf(x); if (i >= 0) cl.splice(i, 1); } };
  cl.contains = function (x) { return cl.indexOf(x) >= 0; };
  this.classList = cl;
}
Object.defineProperty(El.prototype, 'className', { get() { return this.classList.join(' '); },
  set(v) { this.classList.length = 0; String(v).split(/\s+/).filter(Boolean).forEach((c) => this.classList.add(c)); } });
Object.defineProperty(El.prototype, 'textContent', { get() { return this._text + this.children.map((c) => c.textContent).join(''); },
  set(v) { this.children.forEach((c) => { c.parentNode = null; }); this.children = []; this._text = String(v == null ? '' : v); } });
Object.defineProperty(El.prototype, 'parentElement', { get() { return this.parentNode instanceof El ? this.parentNode : null; } });
Object.defineProperty(El.prototype, 'firstChild', { get() { return this.children[0] || null; } });
Object.defineProperty(El.prototype, 'lastChild', { get() { return this.children[this.children.length - 1] || null; } });
Object.defineProperty(El.prototype, 'nextSibling', { get() { const p = this.parentNode; return p ? p.children[p.children.indexOf(this) + 1] || null : null; } });
Object.defineProperty(El.prototype, 'previousSibling', { get() { const p = this.parentNode; return p ? p.children[p.children.indexOf(this) - 1] || null : null; } });
Object.defineProperty(El.prototype, 'isConnected', { get() { let n = this; while (n.parentNode) n = n.parentNode; return n === doc.documentElement; } });
El.prototype.setAttribute = function (n, v) { this.attrs[n] = String(v); };
El.prototype.hasAttribute = function (n) { return Object.prototype.hasOwnProperty.call(this.attrs, n); };
El.prototype.getAttribute = function (n) { return this.hasAttribute(n) ? this.attrs[n] : null; };
El.prototype.removeAttribute = function (n) { delete this.attrs[n]; };
El.prototype.matches = function (sel) {
  const el = this;
  return parse(sel).some(function (s) {
    if (s.tag && el.tagName !== s.tag) return false;
    if (!s.cls.every(function (c) { return el.classList.contains(c); })) return false;
    return s.attrs.every(function (p) { const v = el.getAttribute(p[0]); return p[1] === undefined ? v !== null : v === p[1]; });
  });
};
El.prototype.querySelectorAll = function (sel) {
  const out = [];
  (function walk(n) { n.children.forEach(function (c) { if (c.matches(sel)) out.push(c); walk(c); }); })(this);
  return out;
};
El.prototype.querySelector = function (sel) { return this.querySelectorAll(sel)[0] || null; };
El.prototype.closest = function (sel) { let n = this; while (n && n.matches) { if (n.matches(sel)) return n; n = n.parentNode; } return null; };
El.prototype.appendChild = function (c) { if (c.parentNode) c.parentNode.removeChild(c); c.parentNode = this; this.children.push(c); return c; };
El.prototype.insertBefore = function (c, ref) {
  if (!ref) return this.appendChild(c);
  if (c.parentNode) c.parentNode.removeChild(c);
  c.parentNode = this; this.children.splice(this.children.indexOf(ref), 0, c); return c;
};
El.prototype.removeChild = function (c) { const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); c.parentNode = null; return c; };
El.prototype.addEventListener = function (t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); };
El.prototype.removeEventListener = function (t, f) { const l = this.listeners[t] || []; const i = l.indexOf(f); if (i >= 0) l.splice(i, 1); };
El.prototype.fire = function (t, extra) {
  const ev = Object.assign({ target: this, defaultPrevented: false, stopped: false,
    preventDefault() { this.defaultPrevented = true; }, stopPropagation() { this.stopped = true; } }, extra || {});
  let n = this;
  while (n && !ev.stopped) { (n.listeners[t] || []).slice().forEach((f) => f(ev)); n = n.parentNode; }
  return ev;
};
El.prototype.click = function () { return this.fire('click'); };
El.prototype.focus = function () { doc.activeElement = this; };
El.prototype.getBoundingClientRect = function () { return { left: 0, top: 0, right: this.rect, bottom: 0, width: this.rect, height: 0 }; };

const html = new El('html'), body = new El('body'); html.appendChild(body);
doc = { documentElement: html, body: body, activeElement: null, createElement: (t) => new El(t),
  querySelector: (s) => html.querySelector(s), querySelectorAll: (s) => html.querySelectorAll(s) };
function mkEl(tag, attrs, text) {
  const e = new El(tag);
  Object.keys(attrs || {}).forEach((k) => { if (k === 'cls') e.className = attrs[k]; else e.setAttribute(k, attrs[k]); });
  if (text) e.textContent = text;
  return e;
}
let chatClicks = 0;
function buildSidebar() {
  const sb = mkEl('div', { cls: 'notion-sidebar' });
  const brand = mkEl('div', { cls: 'nt-desk-brand', 'data-nt-desk': 'brand', 'data-nt-desk-own': '1' });
  brand.appendChild(mkEl('span', { cls: 'nt-desk-brand-name' }, 'NotionTool'));
  sb.appendChild(brand);
  const wrap = mkEl('div', { cls: 'header-wrap' });
  const row = mkEl('div', { cls: 'top-row' });
  const items = [];
  const home = mkEl('div', { role: 'tab', 'aria-label': 'Home', 'aria-selected': 'true' }, 'Home');
  const chat = mkEl('div', { role: 'tab', 'aria-selected': 'false' }, 'Chat 3');
  const meet = mkEl('div', { role: 'tab', 'aria-label': 'Meetings', 'aria-selected': 'false' }, 'Meetings');
  [home, chat, meet].forEach((x) => { items.push(x); row.appendChild(x); });
  chat.addEventListener('click', function () { chatClicks++; items.forEach((x) => x.setAttribute('aria-selected', x === chat ? 'true' : 'false')); });
  row.appendChild(mkEl('div', { role: 'button', 'aria-label': 'Search' }, 'Search'));
  wrap.appendChild(row); sb.appendChild(wrap);
  sb.appendChild(mkEl('div', { cls: 'chat-list' }, 'Chats'));
  return { sb: sb, wrap: wrap, row: row, chat: chat };
}
const winL = {};
global.window = { __ntDesk: {},
  addEventListener(t, f) { (winL[t] = winL[t] || []).push(f); },
  removeEventListener(t, f) { const l = winL[t] || []; const i = l.indexOf(f); if (i >= 0) l.splice(i, 1); } };
global.document = doc;
console.warn = function () {};
vm.runInThisContext(fs.readFileSync(__SHELL__, 'utf8'));

const box = mkEl('div', { cls: 'notion-sidebar-container' }); box.rect = 260; body.appendChild(box);
const S = buildSidebar(); box.appendChild(S.sb);
const stale = mkEl('div', { 'data-nt-desk': 'tabbar', 'data-nt-desk-own': '1' });
S.wrap.insertBefore(stale, S.row);
const ctx = { getState: () => ({ connected: true, attention: [1, 2] }), onState: () => function () {} };
const out = {};
const util = window.__ntDesk.shellUtil;
out.nativeChat = util.nativeTab(S.row, 'chat') === S.chat;
out.step = [util.tabStep('chat', 'ArrowRight'), util.tabStep('chat', 'ArrowLeft'), util.tabStep('notes', 'Home'),
  util.tabStep('live', 'End'), util.tabStep('live', 'x')];
const calls = { live: 0, liveOff: 0, notes: 0 };
let handle = window.__ntDesk.shell(ctx);
const api = window.__ntShell;
api.register('live', { mount(b) { calls.live++; b.appendChild(mkEl('div', {}, 'Live view')); return () => { calls.liveOff++; }; } });
api.register('notes', { mount(b) { calls.notes++; b.appendChild(mkEl('div', {}, 'Notes view')); } });
function bars() { return doc.querySelectorAll('[data-nt-desk="tabbar"]'); }
function bar() { return bars()[0]; }
function btn(id) { return bar().querySelector('[data-nt-shell-tab="' + id + '"]'); }
function tabs() { return bar().children.map((b) => [b.getAttribute('data-nt-shell-tab'), b.getAttribute('aria-selected'),
  b.getAttribute('tabindex'), b.classList.contains('nt-shell-tab-active')]); }
function active() { return tabs().filter((t) => t[3]).map((t) => t[0]); }
function layerEl() { return doc.querySelector('[data-nt-desk="shell-layer"]'); }
function viewIds() { const l = layerEl(); return l ? l.children.map((c) => c.getAttribute('data-nt-desk-view')) : []; }
const isOpen = () => html.classList.contains('nt-shell-view-open');

out.mount = { bars: bars().length, stale: bar() === stale, staleGone: !stale.isConnected, inWrap: bar().parentNode === S.wrap,
  beforeRow: bar().nextSibling === S.row, tight: S.wrap.classList.contains('nt-shell-tight'),
  wrapMark: S.wrap.getAttribute('data-nt-shell'), chatBadge: btn('chat').lastChild.textContent, tabs: tabs() };
btn('live').click();
out.live = { current: api.current, tabs: tabs(), views: viewIds(), open: isOpen(), left: layerEl().style.left,
  chatListShown: !S.sb.querySelector('.chat-list').classList.contains('nt-shell-hide') };
btn('notes').click(); btn('settings').click(); btn('live').click(); btn('notes').click();
out.fast = { current: api.current, views: viewIds(), text: layerEl().textContent, calls: Object.assign({}, calls), active: active() };
let ev = btn('notes').fire('keydown', { key: 'ArrowRight' });
out.kRight = { current: api.current, focus: doc.activeElement === btn('settings'), prevented: ev.defaultPrevented, active: active() };
btn('settings').fire('keydown', { key: 'ArrowRight' });
out.kWrap = { current: api.current, clicks: chatClicks, open: isOpen(), views: viewIds(), active: active() };
btn('chat').fire('keydown', { key: 'End' }); out.kEnd = api.current;
btn('settings').fire('keydown', { key: 'Home' }); out.kHome = api.current;
ev = btn('live').fire('keydown', { key: 'Enter' });
out.kEnter = { current: api.current, prevented: ev.defaultPrevented, views: viewIds() };
btn('live').fire('keydown', { key: 'ArrowLeft' }); out.kLeft = api.current;
ev = btn('chat').fire('keydown', { key: 'a' }); out.kOther = { current: api.current, prevented: ev.defaultPrevented };
box.rect = 0; S.sb.rect = 0; api.open('notes'); out.leftKeep = layerEl().style.left;
S.sb.rect = 300; api.open('live'); out.leftSb = layerEl().style.left;
box.rect = 260;
let reCleanup = 0;
api.register('settings', { mount() { window.__ntShell.open('notes'); return () => { reCleanup++; }; } });
api.open('settings');
out.reenter = { current: api.current, views: viewIds(), cleanup: reCleanup, active: active() };
const S2 = buildSidebar(); box.removeChild(S.sb); box.appendChild(S2.sb);
api.refresh();
setTimeout(function () {
  out.rerender = { bars: bars().length, inWrap2: bar().parentNode === S2.wrap, beforeRow2: bar().nextSibling === S2.row,
    tight2: S2.wrap.classList.contains('nt-shell-tight'), row2Hidden: S2.row.classList.contains('nt-shell-hide'),
    current: api.current, active: active(), views: viewIds() };
  handle = window.__ntDesk.shell(ctx);
  out.remount = { bars: bars().length, layers: doc.querySelectorAll('[data-nt-desk="shell-layer"]').length,
    current: window.__ntShell.current, views: viewIds(), keyL: (winL.keydown || []).length, active: active() };
  btn('live').click();
  out.remountClick = { current: window.__ntShell.current, views: viewIds(), active: active() };
  handle.stop();
  out.stop = { bars: bars().length, tight: S2.wrap.classList.contains('nt-shell-tight'), mark: S2.wrap.getAttribute('data-nt-shell'),
    hidden: S2.row.classList.contains('nt-shell-hide'), rowKept: S2.row.isConnected, wrapKept: S2.wrap.isConnected };
  console.log(JSON.stringify(out));
}, 250);
"""


class ShellTabsUiTest(unittest.TestCase):
    """shell.js tab bar in a fake sidebar: one bar, one view, keyboard, re-render, no gap wrapper."""

    @classmethod
    def setUpClass(cls):
        src = Path(__file__).resolve().parents[1] / 'notiontool' / 'web' / 'desktop' / 'shell.js'
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'shell_ui.js'
            path.write_text(SHELL_UI_HARNESS.replace('__SHELL__', json.dumps(str(src))), encoding='utf-8')
            proc = subprocess.run([NODE, str(path)], capture_output=True, text=True, timeout=60)
        if proc.returncode != 0 or not proc.stdout.strip():
            raise AssertionError('harness failed: %s %s' % (proc.stdout[-2000:], proc.stderr[-2000:]))
        cls.out = json.loads(proc.stdout.strip().splitlines()[-1])

    def test_native_chat_tab_with_unread_count(self):
        self.assertTrue(self.out['nativeChat'])
        self.assertEqual(self.out['mount']['chatBadge'], '3')

    def test_tab_step_keys(self):
        self.assertEqual(self.out['step'], ['live', 'settings', 'chat', 'settings', None])

    def test_stale_bar_not_adopted(self):
        m = self.out['mount']
        self.assertEqual(m['bars'], 1)
        self.assertFalse(m['stale'])
        self.assertTrue(m['staleGone'])
        self.assertTrue(m['inWrap'])
        self.assertTrue(m['beforeRow'])

    def test_header_wrapper_collapsed_no_gap(self):
        self.assertTrue(self.out['mount']['tight'])
        self.assertEqual(self.out['mount']['wrapMark'], 'wrap')
        self.assertTrue(self.out['rerender']['tight2'])

    def test_click_shows_exactly_its_view_and_highlight_follows(self):
        live = self.out['live']
        self.assertEqual(live['current'], 'live')
        self.assertEqual(live['views'], ['live'])
        self.assertTrue(live['open'])
        self.assertEqual(live['left'], '260px')
        self.assertTrue(live['chatListShown'])
        self.assertEqual(live['tabs'], [['chat', 'false', '-1', False], ['live', 'true', '0', True],
                                        ['notes', 'false', '-1', False], ['settings', 'false', '-1', False]])
        self.assertEqual(self.out['mount']['tabs'][0], ['chat', 'true', '0', True])

    def test_fast_switching_leaves_one_view(self):
        fast = self.out['fast']
        self.assertEqual(fast['current'], 'notes')
        self.assertEqual(fast['views'], ['notes'])
        self.assertNotIn('Live view', fast['text'])
        self.assertEqual(fast['calls'], {'live': 2, 'liveOff': 2, 'notes': 2})
        self.assertEqual(fast['active'], ['notes'])

    def test_keyboard(self):
        out = self.out
        self.assertEqual(out['kRight'], {'current': 'settings', 'focus': True, 'prevented': True, 'active': ['settings']})
        self.assertEqual(out['kWrap'], {'current': 'chat', 'clicks': 1, 'open': False, 'views': [], 'active': ['chat']})
        self.assertEqual(out['kEnd'], 'settings')
        self.assertEqual(out['kHome'], 'chat')
        self.assertEqual(out['kEnter'], {'current': 'live', 'prevented': True, 'views': ['live']})
        self.assertEqual(out['kLeft'], 'chat')
        self.assertEqual(out['kOther'], {'current': 'chat', 'prevented': False})

    def test_layer_never_covers_sidebar(self):
        self.assertEqual(self.out['leftKeep'], '260px')
        self.assertEqual(self.out['leftSb'], '300px')

    def test_reentrant_mount_keeps_one_view(self):
        self.assertEqual(self.out['reenter'], {'current': 'notes', 'views': ['notes'], 'cleanup': 1, 'active': ['notes']})

    def test_sidebar_rerender_and_remount(self):
        r = self.out['rerender']
        self.assertEqual(r['bars'], 1)
        self.assertTrue(r['inWrap2'])
        self.assertTrue(r['beforeRow2'])
        self.assertTrue(r['row2Hidden'])
        self.assertEqual((r['current'], r['active'], r['views']), ('notes', ['notes'], ['notes']))
        m = self.out['remount']
        self.assertEqual((m['bars'], m['layers'], m['keyL']), (1, 1, 1))
        self.assertEqual((m['current'], m['views'], m['active']), ('notes', ['notes'], ['notes']))
        self.assertEqual(self.out['remountClick'], {'current': 'live', 'views': ['live'], 'active': ['live']})

    def test_stop_restores_wrapper(self):
        s = self.out['stop']
        self.assertEqual(s['bars'], 0)
        self.assertFalse(s['tight'])
        self.assertIsNone(s['mark'])
        self.assertFalse(s['hidden'])
        self.assertTrue(s['rowKept'])
        self.assertTrue(s['wrapKept'])


if __name__ == '__main__':
    unittest.main()


# ---- Sidebar 'Coworkers' section toggle (shell.js createCoworkerToggle) -------------------------------
import os as _os
import shutil as _shutil
import subprocess as _subprocess

_DESK = Path(__file__).resolve().parents[1] / 'notiontool' / 'web' / 'desktop'
_NODE = _shutil.which('node') or r'C:\Program Files\nodejs\node.exe'

_COWORKER_HARNESS = r"""
global.window = { __ntDesk: {} };
global.document = {};
require(process.argv[2]);
const U = window.__ntDesk.shellUtil;
class El {
  constructor(tag, attrs) {
    this.tagName = tag; this.attrs = Object.assign({}, attrs || {}); this.children = []; this.parentNode = null;
    this.nodeType = 1; this.listeners = {};
    const self = this;
    this.classList = {
      contains: (c) => self.cls().includes(c),
      toggle: (c, on) => { const s = self.cls().filter((x) => x !== c); if (on) s.push(c); self.attrs['class'] = s.join(' '); return on; }
    };
  }
  cls() { return (this.attrs['class'] || '').split(/\s+/).filter(Boolean); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  append(c) { c.parentNode = this; this.children.push(c); return c; }
  matches(sel) { const m = /^\[data-nt-desk="([^"]+)"\]$/.exec(sel); return !!m && this.attrs['data-nt-desk'] === m[1]; }
  closest(sel) { let n = this; while (n && n.matches) { if (n.matches(sel)) return n; n = n.parentNode; } return null; }
  all(sel, out) { out = out || []; this.children.forEach((c) => { if (c.matches(sel)) out.push(c); c.all(sel, out); }); return out; }
  querySelector(sel) { return this.all(sel)[0] || null; }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
}
function makeStore(broken) {
  const data = {};
  return {
    data: data,
    getItem: (k) => { if (broken) throw new Error('blocked'); return k in data ? data[k] : null; },
    setItem: (k, v) => { if (broken) throw new Error('blocked'); data[k] = String(v); }
  };
}
function makeWin(store) {
  return { localStorage: store, cap: {},
    addEventListener(t, f, c) { if (c) (this.cap[t] = this.cap[t] || []).push(f); },
    removeEventListener(t, f, c) { if (c && this.cap[t]) this.cap[t] = this.cap[t].filter((g) => g !== f); },
    count() { return Object.keys(this.cap).reduce((n, t) => n + this.cap[t].length, 0); } };
}
// Notion sidebar (not ours) holding our section; Notion's root swallows every click in its capture phase.
function build(root) {
  const section = root.append(new El('div', { 'data-nt-desk': 'coworkers', 'data-nt-desk-own': '1' }));
  const head = section.append(new El('button', { 'data-nt-desk': 'coworkers-head', 'data-nt-desk-own': '1', 'aria-expanded': 'true' }));
  const chevron = head.append(new El('span', { 'class': 'nt-desk-chevron', 'data-nt-desk-own': '1' }));
  section.append(new El('div', { 'data-nt-desk': 'coworker-list', 'data-nt-desk-own': '1' }));
  head.ownClicks = 0;
  head.addEventListener('click', () => { head.ownClicks++; });
  return { section, head, chevron };
}
function fire(win, target, type, extra) {
  const ev = Object.assign({ type: type, target: target, detail: 1, defaultPrevented: false, stopped: false,
    preventDefault() { this.defaultPrevented = true; }, stopPropagation() { this.stopped = true; },
    stopImmediatePropagation() { this.stopped = true; } }, extra || {});
  // keydown reaches the toggle through the shell's own window keydown listener (onKey).
  if (type === 'keydown' && win.onKey) win.onKey(ev);
  (win.cap[type] || []).slice().forEach((f) => { if (!ev.stopped) f(ev); });
  if (!ev.stopped && type === 'click') ev.notionSwallowed = true;   // Notion's capture handler
  else if (!ev.stopped) { let n = target; while (n) { (n.listeners[type] || []).forEach((f) => f(ev)); n = n.parentNode; } }
  return ev;
}
const out = {};
const store = makeStore(false);
const win = makeWin(store);
const root = new El('nav', { 'class': 'notion-sidebar' });
const notionRow = root.append(new El('div', { role: 'treeitem' }));
const doc = { querySelectorAll: (sel) => root.all(sel) };
let S = build(root);
const T = U.createCoworkerToggle(win, doc);
T.start();
win.onKey = (ev) => T.handle(ev);
const state = () => ({ collapsed: S.section.classList.contains('nt-desk-collapsed'), aria: S.head.getAttribute('aria-expanded'),
  title: S.head.getAttribute('title'), stored: store.data[U.COWORKERS_KEY] });
let ev = fire(win, S.chevron, 'click');
out.click1 = Object.assign(state(), { prevented: ev.defaultPrevented, ownClicks: S.head.ownClicks, swallowed: !!ev.notionSwallowed });
fire(win, S.head, 'click');
out.click2 = state();
out.down = fire(win, S.head, 'pointerdown').stopped;
fire(win, S.head, 'keydown', { key: 'Enter' });
const afterEnter = state();
fire(win, S.head, 'click', { detail: 0 });
fire(win, S.head, 'keyup', { key: 'Enter' });
out.enter = { after: afterEnter, settled: state() };
fire(win, S.head, 'keydown', { key: ' ' });
const up = fire(win, S.head, 'keyup', { key: ' ' });
out.space = Object.assign(state(), { upPrevented: up.defaultPrevented });
fire(win, S.head, 'keydown', { key: 'Enter', repeat: true });
out.repeat = state();
out.otherKey = fire(win, S.head, 'keydown', { key: 'a' }).defaultPrevented;
// Notion re-render: the old section is gone, a fresh one (expanded, no class) appears; state comes back.
fire(win, S.head, 'click');
root.children = root.children.filter((c) => c !== S.section);
S = build(root);
out.rebuilt = state();
T.syncAll();
out.synced = state();
fire(win, S.head, 'click');
out.afterRerender = state();
// Notion's own nodes: never handled, never removed.
const before = root.children.length;
const fake = root.append(new El('div', { 'data-nt-desk': 'coworkers-head' }));
out.notion = { handled: fire(win, fake, 'click').defaultPrevented, rowKept: root.children.includes(notionRow) };
root.children.pop();
out.notion.count = root.children.length === before;
// A second instance (e.g. an older build still listening) never toggles twice.
const T2 = U.createCoworkerToggle(win, doc);
T2.start();
const was = state().collapsed;
fire(win, S.head, 'click');
out.single = state().collapsed !== was;
T2.stop();
// Remembered: a fresh instance reads localStorage.
out.remembered = U.createCoworkerToggle(makeWin(store), doc).read() === (store.data[U.COWORKERS_KEY] === '1');
// Blocked localStorage: memory keeps the state.
const bw = makeWin(makeStore(true));
const B = U.createCoworkerToggle(bw, doc);
B.start();
const b0 = B.read();
fire(bw, S.head, 'click');
const b1 = B.read();
fire(bw, S.head, 'click');
out.blocked = [b0, b1, B.read()];
B.stop();
T.stop();
out.listenersLeft = win.count() + bw.count();
console.log(JSON.stringify(out));
"""


class CoworkerToggleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = (_DESK / 'shell.js').read_text(encoding='utf-8')
        cls.css = (_DESK / 'shell.css').read_text(encoding='utf-8')

    def run_harness(self):
        if not (_NODE and _os.path.isfile(_NODE)):
            self.skipTest('node not installed')
        with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as handle:
            handle.write(_COWORKER_HARNESS)
        try:
            res = _subprocess.run([_NODE, handle.name, str(_DESK / 'shell.js')], capture_output=True, text=True,
                                  timeout=30)
        finally:
            _os.unlink(handle.name)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout.strip().splitlines()[-1])

    def test_click_toggles_once_before_notion_and_remembers(self):
        out = self.run_harness()
        self.assertEqual(out['click1'], dict(collapsed=True, aria='false', title='Show coworkers', stored='1',
                                             prevented=True, ownClicks=0, swallowed=False))
        self.assertEqual(out['click2'], dict(collapsed=False, aria='true', title='Hide coworkers', stored='0'))
        self.assertTrue(out['down'])
        self.assertTrue(out['single'])
        self.assertTrue(out['remembered'])

    def test_enter_and_space_toggle_exactly_once(self):
        out = self.run_harness()
        self.assertTrue(out['enter']['after']['collapsed'])
        self.assertEqual(out['enter']['after'], out['enter']['settled'])  # no second toggle from click/keyup
        self.assertFalse(out['space']['collapsed'])
        self.assertTrue(out['space']['upPrevented'])
        self.assertFalse(out['repeat']['collapsed'])  # held key: no flicker
        self.assertFalse(out['otherKey'])

    def test_survives_sidebar_rerender(self):
        out = self.run_harness()
        self.assertFalse(out['rebuilt']['collapsed'])  # fresh node from the re-render
        self.assertEqual(out['synced']['collapsed'], True)
        self.assertEqual(out['synced']['aria'], 'false')
        self.assertEqual(out['afterRerender']['collapsed'], False)
        self.assertEqual(out['afterRerender']['stored'], '0')

    def test_notion_nodes_untouched_blocked_storage_and_stop(self):
        out = self.run_harness()
        self.assertEqual(out['notion'], dict(handled=False, rowKept=True, count=True))
        self.assertEqual(out['blocked'][1], not out['blocked'][0])
        self.assertEqual(out['blocked'][2], out['blocked'][0])
        self.assertEqual(out['listenersLeft'], 0)

    def test_wiring_css_and_line_endings(self):
        for needle in ('coworkers.start();', 'coworkers.stop();', 'coworkers.syncAll();',
                       'var coworkers = createCoworkerToggle(window, doc);', "'ntDesk.coworkersCollapsed'"):
            self.assertIn(needle, self.js)
        tog = self.js[self.js.index('function createCoworkerToggle'):self.js.index('window.__ntDesk.shellUtil = {')]
        for bad in ('removeChild', '.remove()', 'innerHTML'):
            self.assertNotIn(bad, tog)
        for rule in ('.nt-desk-coworkers .nt-desk-coworkers-head {', '.nt-desk-coworkers .nt-desk-chevron {',
                     'transform: rotate(-90deg)', '.nt-desk-coworkers.nt-desk-collapsed .nt-desk-coworker-list {'):
            self.assertIn(rule, self.css)
        for name in ('shell.js', 'shell.css'):
            self.assertNotIn(b'\r\n', (_DESK / name).read_bytes())

    def test_node_check(self):
        if not (_NODE and _os.path.isfile(_NODE)):
            self.skipTest('node not installed')
        res = _subprocess.run([_NODE, '--check', str(_DESK / 'shell.js')], capture_output=True, text=True, timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)
