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


if __name__ == '__main__':
    unittest.main()
