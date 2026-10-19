"""Studio v3 shell: tab bar, router (window.__ntShell), Ctrl+1..4, idempotent mount/stop.

Runs notiontool/web/desktop/shell.js (and overlay.js for the Mains heading) in node with a small
fake DOM shaped like the live Notion sidebar (top row of [role="tab"] items + search button).
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from notiontool import desktop_shell

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DESK = os.path.join(ROOT, 'notiontool', 'web', 'desktop')
NODE = shutil.which('node') or r'C:\Program Files\nodejs\node.exe'

HARNESS = r'''
const fs = require('fs'); const vm = require('vm');
const [shellPath, overlayPath] = process.argv.slice(2);
function simple(n, s) {
  if (!n || !n.tagName || !s) return false;
  const re = /(\.[\w-]+)|(\[([\w-]+)(?:="([^"]*)")?\])|^([a-zA-Z]+)/g; let m, any = false;
  while ((m = re.exec(s))) {
    any = true;
    if (m[1]) { if (!n.classList.contains(m[1].slice(1))) return false; }
    else if (m[2]) { const v = n.getAttribute(m[3]); if (v === null) return false; if (m[4] !== undefined && v !== m[4]) return false; }
    else if (m[5]) { if (n.tagName !== m[5].toUpperCase()) return false; }
  }
  return any;
}
function matches(n, sel) { return sel.split(',').some(s => simple(n, s.trim())); }
let doc;
class El {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.attrs = {}; this.children = []; this.parentNode = null;
    this.listeners = {}; this.style = {}; this._text = ''; this.clicks = 0; const s = [];
    this.classList = { _s: s, add: c => { if (s.indexOf(c) < 0) s.push(c); }, remove: c => { const i = s.indexOf(c); if (i >= 0) s.splice(i, 1); },
      contains: c => s.indexOf(c) >= 0, toggle: (c, on) => { if (on) this.classList.add(c); else this.classList.remove(c); } }; }
  get className() { return this.classList._s.join(' '); }
  set className(v) { this.classList._s.splice(0); String(v).split(/\s+/).filter(Boolean).forEach(c => this.classList._s.push(c)); }
  setAttribute(k, v) { if (k === 'class') this.className = v; else this.attrs[k] = String(v); }
  getAttribute(k) { if (k === 'class') return this.className; return k in this.attrs ? this.attrs[k] : null; }
  hasAttribute(k) { return this.getAttribute(k) !== null; }
  removeAttribute(k) { delete this.attrs[k]; }
  appendChild(c) { if (c.parentNode) c.parentNode.removeChild(c); c.parentNode = this; this.children.push(c); return c; }
  insertBefore(c, ref) { if (!ref) return this.appendChild(c); if (c.parentNode) c.parentNode.removeChild(c);
    const i = this.children.indexOf(ref); c.parentNode = this; this.children.splice(i, 0, c); return c; }
  removeChild(c) { const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); c.parentNode = null; return c; }
  replaceChild(n, o) { this.insertBefore(n, o); this.removeChild(o); return o; }
  get parentElement() { return this.parentNode && this.parentNode !== doc ? this.parentNode : null; }
  get childNodes() { return this.children; }
  get firstChild() { return this.children[0] || null; }
  get lastChild() { return this.children[this.children.length - 1] || null; }
  get firstElementChild() { return this.firstChild; }
  get nextSibling() { const p = this.parentNode; if (!p) return null; return p.children[p.children.indexOf(this) + 1] || null; }
  get previousSibling() { const p = this.parentNode; if (!p) return null; return p.children[p.children.indexOf(this) - 1] || null; }
  get nextElementSibling() { return this.nextSibling; }
  get previousElementSibling() { return this.previousSibling; }
  get isConnected() { let p = this; while (p.parentNode) p = p.parentNode; return p === doc; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  set textContent(v) { this.children.forEach(c => { c.parentNode = null; }); this.children = []; this._text = String(v); }
  get innerHTML() { return this._html || ''; }
  set innerHTML(v) { this._html = String(v); }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  removeEventListener(t, f) { const a = this.listeners[t] || []; const i = a.indexOf(f); if (i >= 0) a.splice(i, 1); }
  click() { this.clicks++; (this.listeners.click || []).slice().forEach(f => f({ preventDefault() {}, stopPropagation() {} })); }
  contains(n) { while (n) { if (n === this) return true; n = n.parentNode; } return false; }
  closest(sel) { let p = this; while (p && p !== doc) { if (matches(p, sel)) return p; p = p.parentNode; } return null; }
  querySelectorAll(sel) { const out = []; (function walk(n) { n.children.forEach(k => { if (matches(k, sel)) out.push(k); walk(k); }); })(this); return out; }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  getBoundingClientRect() { const r = this.rectRight || 0; return { left: 0, top: 0, right: r, bottom: 0, width: r, height: 0 }; }
}
doc = new El('#document');
doc.documentElement = doc.appendChild(new El('html'));
doc.body = doc.documentElement.appendChild(new El('body'));
doc.createElement = t => new El(t);
doc.createTextNode = t => { const n = new El('#text'); n._text = String(t); return n; };
doc.addEventListener = () => {}; doc.removeEventListener = () => {};
function mk(tag, attrs, text) { const n = new El(tag); Object.keys(attrs || {}).forEach(k => n.setAttribute(k, attrs[k])); if (text) n._text = text; return n; }
function topRow() {
  const row = mk('div', { class: 'x78zum5' }); const tabs = mk('div');
  ['Home', 'Chat', 'Meetings', 'Posteingang'].forEach(l => tabs.appendChild(mk('div', { role: 'tab', 'aria-label': l,
    'aria-selected': l === 'Chat' ? 'true' : 'false' }, l === 'Chat' ? '4Chat' : l)));
  const s = mk('div'); s.appendChild(mk('div', { role: 'button', 'aria-label': 'Suchen' }));
  row.appendChild(tabs); row.appendChild(s); return row;
}
const cont = mk('nav', { class: 'notion-sidebar-container' }); cont.rectRight = 270;
const sb = mk('div', { class: 'notion-sidebar nt-desk-sidebar', 'data-nt-desk': 'sidebar' });
const brand = mk('div', { class: 'nt-desk-brand', 'data-nt-desk': 'brand', 'data-nt-desk-own': '1' });
brand.appendChild(mk('span', { class: 'nt-desk-brand-mark' })); brand.appendChild(mk('span', { class: 'nt-desk-brand-name' }, 'NotionTool'));
const wrap = mk('div'); let row = topRow(); wrap.appendChild(row);
sb.appendChild(brand); sb.appendChild(wrap); cont.appendChild(sb); doc.body.appendChild(cont);

const winListeners = {};
const win = { __ntDesk: {}, __ntDeskMounted: {},
  addEventListener(t, f) { (winListeners[t] = winListeners[t] || []).push(f); },
  removeEventListener(t, f) { const a = winListeners[t] || []; const i = a.indexOf(f); if (i >= 0) a.splice(i, 1); } };
function key(o) { const ev = Object.assign({ prevented: false, preventDefault() { this.prevented = true; }, stopPropagation() {} }, o);
  (winListeners.keydown || []).slice().forEach(f => f(ev)); return ev.prevented; }
const sandbox = { window: win, document: doc, console, setTimeout, clearTimeout, setInterval, clearInterval, Date, Math, JSON };
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(shellPath, 'utf8'), sandbox);
vm.runInContext(fs.readFileSync(overlayPath, 'utf8'), sandbox);
let state = { connected: true, attention: [{ kind: 'awaiting_main' }, { kind: 'stalled' }], context_ask: { q: 'x' } };
const subs = [];
const ctx = { getState: () => state, onState: f => { subs.push(f); return () => { const i = subs.indexOf(f); if (i >= 0) subs.splice(i, 1); }; } };
const sleep = ms => new Promise(r => setTimeout(r, ms));
const q = s => doc.querySelectorAll(s);
function tabInfo() { const bar = q('[data-nt-desk="tabbar"]')[0]; if (!bar) return null;
  return bar.children.map(b => [b.getAttribute('data-nt-shell-tab'), b.children[1].textContent, b.lastChild.textContent,
    b.classList.contains('nt-shell-tab-active'), !!b.children[0].innerHTML]); }
(async () => {
  const out = {};
  const U = win.__ntDesk.shellUtil;
  out.keyTab = [U.keyTab({ ctrlKey: true, key: '1' }), U.keyTab({ ctrlKey: true, key: '4' }), U.keyTab({ ctrlKey: true, shiftKey: true, key: '2' }),
    U.keyTab({ key: '2' }), U.keyTab({ ctrlKey: true, code: 'Digit3', key: '#' })];
  out.liveBadge = [U.liveBadge(state), U.liveBadge(null), U.liveBadge({ connected: false, attention: [1] }), U.liveBadge({ attention: [] })];
  out.badgeText = [U.badgeText(0), U.badgeText(3), U.badgeText(150)];

  // pure router
  const log = [];
  const host = { ctx: {}, viewBox: t => ({ tab: t }), clearView: t => log.push('clear:' + t), showView: t => log.push('show:' + t),
    hideView: () => log.push('hide'), showChat: () => log.push('chat'), placeholder: t => log.push('ph:' + t), chatBadge: () => 4, changed: () => {} };
  const r = U.createRouter(host);
  out.r0 = r.current;
  r.open('live'); r.open('live');
  const un = r.register('live', { mount: b => { log.push('mount:' + b.tab); return () => log.push('cleanup'); }, unmount: () => log.push('unmount'), badge: () => 7 });
  out.rBadge = [r.badge('live', state), r.badge('chat', state), r.badge('notes', state)];
  r.open('notes'); r.open('chat'); out.rBad = r.open('nope'); un(); out.rAfterUnregister = r.badge('live', state);
  out.rLog = log; out.rCurrent = r.current;

  // mount
  const h = win.__ntDesk.shell(ctx);
  out.m1 = { bars: q('[data-nt-desk="tabbar"]').length, barBeforeRow: q('[data-nt-desk="tabbar"]')[0].nextSibling === row,
    rowHidden: row.classList.contains('nt-shell-hide'), rowMark: row.getAttribute('data-nt-shell'), tabs: tabInfo(),
    conn: q('[data-nt-desk="conn"]').map(d => [d.getAttribute('data-level'), d.textContent, d.parentNode === brand]),
    brandCls: brand.classList.contains('nt-shell-brand'), on: doc.documentElement.classList.contains('nt-shell-on'),
    api: !!win.__ntShell && win.__ntShell.current, rowKept: row.isConnected };

  // router through the DOM
  win.__ntShell.open('live');
  const layer = () => q('[data-nt-desk="shell-layer"]')[0];
  out.live1 = { open: doc.documentElement.classList.contains('nt-shell-view-open'), left: layer().style.left,
    text: layer().textContent, current: win.__ntShell.current };
  const calls = { mount: 0, unmount: 0 };
  const unreg = win.__ntShell.register('live', { mount: box => { calls.mount++; box.appendChild(mk('div', { class: 'my-live' }, 'Live view')); },
    unmount: () => { calls.unmount++; }, badge: () => 5 });
  win.__ntShell.open('live');
  out.live2 = { calls: Object.assign({}, calls), views: layer().children.length, text: layer().textContent };
  await sleep(200);
  out.live2.tabs = tabInfo();
  out.key3 = key({ ctrlKey: true, key: '3' });
  out.notes = { current: win.__ntShell.current, calls: Object.assign({}, calls), text: layer().textContent };
  out.keyShift = key({ ctrlKey: true, shiftKey: true, key: '2' });
  key({ ctrlKey: true, key: '1' });
  const chatTab = row.querySelector('[aria-label="Chat"]');
  out.chat1 = { current: win.__ntShell.current, open: doc.documentElement.classList.contains('nt-shell-view-open'), clicks: chatTab.clicks };
  chatTab.setAttribute('aria-selected', 'false');
  q('[data-nt-desk="tabbar"]')[0].children[0].click();
  out.chat2 = { clicks: chatTab.clicks };
  key({ ctrlKey: true, key: '2' });
  out.live3 = { current: win.__ntShell.current, calls: Object.assign({}, calls) };

  // idempotent: mount again without stop, and Notion re-renders its row
  const h2 = win.__ntDesk.shell(ctx);
  const row2 = topRow(); wrap.replaceChild(row2, row);
  win.__ntShell.refresh(); await sleep(200);
  out.m2 = { bars: q('[data-nt-desk="tabbar"]').length, layers: q('[data-nt-desk="shell-layer"]').length, conns: q('[data-nt-desk="conn"]').length,
    barBeforeRow2: q('[data-nt-desk="tabbar"]')[0].nextSibling === row2, row2Hidden: row2.classList.contains('nt-shell-hide'),
    oldRowDetached: !row.isConnected, keyListeners: (winListeners.keydown || []).length,
    carried: tabInfo().filter(t => t[0] === 'live')[0][2], calls: Object.assign({}, calls), current: win.__ntShell.current };
  h.stop();
  out.m2.afterOldStop = q('[data-nt-desk="tabbar"]').length;

  // stop
  win.__ntShell.open('live');
  h2.stop(); h2.stop();
  out.stop = { bars: q('[data-nt-desk="tabbar"]').length, layers: q('[data-nt-desk="shell-layer"]').length, conns: q('[data-nt-desk="conn"]').length,
    marked: q('[data-nt-shell]').length, hidden: q('.nt-shell-hide').length, api: typeof win.__ntShell,
    html: doc.documentElement.className, brandCls: brand.classList.contains('nt-shell-brand'), keyListeners: (winListeners.keydown || []).length,
    subs: subs.length, calls: Object.assign({}, calls), brandKept: brand.isConnected, rowKept: row2.isConnected,
    keyAfter: key({ ctrlKey: true, key: '2' }) };

  // offline connection text
  state = null;
  const h3 = win.__ntDesk.shell(ctx);
  out.offline = q('[data-nt-desk="conn"]').map(d => [d.getAttribute('data-level'), d.textContent]);
  h3.stop();

  // Mains heading stays single (overlay.js sidebarUtil)
  const S = win.__ntDesk.sidebarUtil || {};
  out.hasEnsure = typeof S.ensureHeading;
  if (typeof S.ensureHeading === 'function') {
    const box = mk('div'); const anchor = mk('div', {}, 'Notion AI'); box.appendChild(anchor); doc.body.appendChild(box);
    const make = () => mk('div', { class: 'nt-desk-heading', 'data-nt-desk': 'heading', 'data-nt-desk-own': '1' }, 'Mains');
    for (let i = 0; i < 3; i++) S.ensureHeading(doc, anchor, make);
    box.appendChild(make());
    S.ensureHeading(doc, anchor, make);
    out.headings = q('[data-nt-desk="heading"]').length;
  }
  console.log(JSON.stringify(out));
  process.exit(0);
})().catch(e => { console.log(JSON.stringify({ error: String(e && e.stack || e) })); process.exit(1); });
'''


def run_harness():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'shell_nav_harness.js')
        with open(path, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(HARNESS)
        proc = subprocess.run([NODE, path, os.path.join(DESK, 'shell.js'), os.path.join(DESK, 'overlay.js')],
                              capture_output=True, text=True, timeout=60)
    lines = [l for l in proc.stdout.splitlines() if l.startswith('{')]
    if not lines:
        raise AssertionError('harness failed: %s %s' % (proc.stdout[-2000:], proc.stderr[-2000:]))
    return json.loads(lines[-1])


@unittest.skipUnless(os.path.exists(NODE), 'node is not installed')
class ShellNavTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_harness()
        if 'error' in cls.out:
            raise AssertionError(cls.out['error'])

    def test_helpers(self):
        self.assertEqual(self.out['keyTab'], ['chat', 'settings', None, None, 'notes'])
        self.assertEqual(self.out['liveBadge'], [3, 0, 0, 0])
        self.assertEqual(self.out['badgeText'], ['', '3', '99+'])

    def test_router(self):
        out = self.out
        self.assertEqual(out['r0'], 'chat')
        self.assertEqual(out['rBadge'], [7, 4, 0])
        self.assertFalse(out['rBad'])
        self.assertEqual(out['rAfterUnregister'], 3)
        self.assertEqual(out['rCurrent'], 'chat')
        self.assertEqual(out['rLog'], ['show:live', 'ph:live', 'clear:live', 'mount:live', 'unmount', 'cleanup',
                                       'clear:live', 'show:notes', 'ph:notes', 'clear:notes', 'hide', 'chat'])

    def test_mount_builds_tab_bar_in_place_of_notion_row(self):
        m1 = self.out['m1']
        self.assertEqual(m1['bars'], 1)
        self.assertTrue(m1['barBeforeRow'])
        self.assertTrue(m1['rowHidden'])
        self.assertTrue(m1['rowKept'])
        self.assertEqual(m1['rowMark'], 'toprow')
        self.assertEqual(m1['tabs'], [['chat', 'Chat', '4', True, True], ['live', 'Live', '3', False, True],
                                      ['notes', 'Notes', '', False, True], ['settings', 'Settings', '', False, True]])
        self.assertEqual(m1['conn'], [['on', 'Connected. NotionTool is running.', True]])
        self.assertTrue(m1['brandCls'])
        self.assertTrue(m1['on'])
        self.assertEqual(m1['api'], 'chat')
        self.assertEqual(self.out['offline'], [['wait', 'Connecting to NotionTool...']])

    def test_views_and_tab_switch(self):
        out = self.out
        self.assertTrue(out['live1']['open'])
        self.assertEqual(out['live1']['left'], '270px')
        self.assertIn('Live', out['live1']['text'])
        self.assertEqual(out['live1']['current'], 'live')
        self.assertEqual(out['live2']['calls'], {'mount': 1, 'unmount': 0})
        self.assertEqual(out['live2']['views'], 1)
        self.assertEqual(out['live2']['text'], 'Live view')
        live_tab = [t for t in out['live2']['tabs'] if t[0] == 'live'][0]
        self.assertEqual(live_tab[2:4], ['5', True])
        self.assertTrue(out['key3'])
        self.assertEqual(out['notes']['current'], 'notes')
        self.assertEqual(out['notes']['calls'], {'mount': 1, 'unmount': 1})
        self.assertNotIn('Live view', out['notes']['text'])
        self.assertFalse(out['keyShift'])
        self.assertEqual(out['chat1'], {'current': 'chat', 'open': False, 'clicks': 0})
        self.assertEqual(out['chat2'], {'clicks': 1})
        self.assertEqual(out['live3'], {'current': 'live', 'calls': {'mount': 2, 'unmount': 1}})

    def test_idempotent_mount(self):
        m2 = self.out['m2']
        self.assertEqual((m2['bars'], m2['layers'], m2['conns']), (1, 1, 1))
        self.assertTrue(m2['barBeforeRow2'])
        self.assertTrue(m2['row2Hidden'])
        self.assertTrue(m2['oldRowDetached'])
        self.assertEqual(m2['keyListeners'], 1)
        self.assertEqual(m2['carried'], '5')
        self.assertEqual(m2['calls'], {'mount': 3, 'unmount': 2})
        self.assertEqual(m2['current'], 'live')
        self.assertEqual(m2['afterOldStop'], 1)

    def test_stop_restores_everything(self):
        s = self.out['stop']
        self.assertEqual((s['bars'], s['layers'], s['conns'], s['marked'], s['hidden']), (0, 0, 0, 0, 0))
        self.assertEqual(s['api'], 'undefined')
        self.assertNotIn('nt-shell', s['html'])
        self.assertFalse(s['brandCls'])
        self.assertEqual((s['keyListeners'], s['subs']), (0, 0))
        self.assertEqual(s['calls'], {'mount': 3, 'unmount': 3})
        self.assertTrue(s['brandKept'])
        self.assertTrue(s['rowKept'])
        self.assertFalse(s['keyAfter'])

    def test_single_mains_heading(self):
        self.assertEqual(self.out['hasEnsure'], 'function')
        self.assertEqual(self.out['headings'], 1)


class ShellWiringTest(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(DESK, 'shell.js'), encoding='utf-8') as fh:
            self.js = fh.read()
        with open(os.path.join(DESK, 'shell.css'), encoding='utf-8') as fh:
            self.css = fh.read()

    def test_part_order(self):
        for seq in (desktop_shell.PART_FILES, desktop_shell.MOUNT_ORDER):
            self.assertIn('shell', seq)
            for later in ('live', 'settings', 'notes'):
                if later in seq:
                    self.assertLess(seq.index('shell'), seq.index(later))
        self.assertLess(desktop_shell.MOUNT_ORDER.index('sidebar'), desktop_shell.MOUNT_ORDER.index('shell'))

    def test_never_removes_notion_nodes(self):
        # removeChild only on own nodes (data-nt-desk-own) or own view boxes.
        self.assertEqual(self.js.count('removeChild'), 2)
        self.assertIn("isOwn(nodes[i]) && nodes[i].parentNode) nodes[i].parentNode.removeChild", self.js)
        self.assertIn("if (box && box.parentNode) box.parentNode.removeChild(box);", self.js)
        self.assertNotIn('.remove()', self.js)
        self.assertNotIn('location.reload', self.js)

    def test_css(self):
        self.assertIn('.nt-shell-hide { display: none !important; }', self.css)
        self.assertIn('html.nt-shell-on .nt-desk-composer-empty::after', self.css)
        self.assertIn('html.nt-shell-view-open .nt-shell-layer { display: block; }', self.css)
        self.assertIn('#7c5cff', self.css)

    def test_line_endings(self):
        for name in ('shell.js', 'shell.css'):
            with open(os.path.join(DESK, name), 'rb') as fh:
                self.assertNotIn(b'\r', fh.read())


if __name__ == '__main__':
    unittest.main()
