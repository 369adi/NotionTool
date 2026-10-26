"""Desktop shell: nav rail, router (window.__ntShell), Ctrl+1..4, Esc goes back, idempotent mount/stop.

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
// An element node as the browser reports it (nodeType 1), optionally with a visible box.
function node(tag, attrs) { const n = mk(tag, attrs); n.nodeType = 1; return n; }
function visible(n) { n.getBoundingClientRect = () => ({ left: 0, top: 0, right: 300, bottom: 200, width: 300, height: 200 }); return n; }
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
  out.keyTabAll = ['1', '2', '3', '4', '5'].map(k => U.keyTab({ ctrlKey: true, key: k }));
  out.liveBadge = [U.liveBadge(state), U.liveBadge(null), U.liveBadge({ connected: false, attention: [1] }), U.liveBadge({ attention: [] })];
  out.badgeText = [U.badgeText(0), U.badgeText(3), U.badgeText(150)];
  out.tabStep = [U.tabStep('chat', 'ArrowDown'), U.tabStep('settings', 'ArrowRight'), U.tabStep('chat', 'ArrowUp'), U.tabStep('live', 'End')];

  // isBackKey: a plain Escape only
  out.isBackKey = [U.isBackKey({ key: 'Escape' }), U.isBackKey({ key: 'Escape', ctrlKey: true }), U.isBackKey({ key: 'Escape', shiftKey: true }),
    U.isBackKey({ key: 'Escape', altKey: true }), U.isBackKey({ key: 'Escape', metaKey: true }), U.isBackKey({ key: 'Escape', defaultPrevented: true }),
    U.isBackKey({ key: 'Escape', isComposing: true }), U.isBackKey({ key: 'a' }), U.isBackKey({}), U.isBackKey(null)];

  // isTyping: text fields only
  out.isTyping = [U.isTyping(node('input')), U.isTyping(node('textarea')), U.isTyping(node('select')),
    U.isTyping(node('div', { contenteditable: 'true' })), U.isTyping(node('div', { contenteditable: '' })),
    U.isTyping(node('div', { role: 'textbox' })), U.isTyping(node('div', { contenteditable: 'false' })),
    U.isTyping(node('div')), U.isTyping(node('button')), U.isTyping(null), U.isTyping(mk('input'))];

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

  // router history: router.back()
  const quiet = Object.assign({}, host, { clearView() {}, showView() {}, hideView() {}, showChat() {}, placeholder() {} });
  const r2 = U.createRouter(quiet);
  out.back = { empty: [r2.back(), r2.depth, r2.current] };
  r2.open('live'); r2.open('live'); r2.open('settings');
  out.back.depth = r2.depth;
  out.back.unknown = [r2.open('nope'), r2.depth, r2.current];
  out.back.steps = [r2.back(), r2.current, r2.depth, r2.back(), r2.current, r2.depth, r2.back(), r2.current, r2.depth];
  r2.open('notes'); r2.open('projects');
  out.back.notes = [r2.depth, r2.back(), r2.current, r2.back(), r2.current];
  r2.destroy();
  out.back.dead = r2.back();
  const r3 = U.createRouter(quiet);
  for (let i = 0; i < 30; i++) r3.open(i % 2 ? 'projects' : 'live');
  out.back.max = r3.depth;
  let n = 0; while (r3.back() && n < 100) n++;
  out.back.maxBacks = n;

  // mount
  const h = win.__ntDesk.shell(ctx);
  const rail1 = q('[data-nt-desk="rail"]');
  out.m1 = { rails: rail1.length, railOnBody: rail1[0].parentNode === doc.body, railClass: rail1[0].classList.contains('nt-rail'),
    bars: q('[data-nt-desk="tabbar"]').length, barInRail: q('[data-nt-desk="tabbar"]')[0].parentNode === rail1[0],
    logo: q('.nt-rail-logo').map(d => d.textContent),
    rowHidden: row.classList.contains('nt-shell-hide'), rowMark: row.getAttribute('data-nt-shell'), tabs: tabInfo(),
    conn: q('[data-nt-desk="conn"]').map(d => [d.getAttribute('data-level'), d.textContent, d.parentNode === rail1[0], d.getAttribute('aria-label')]),
    brandDots: q('[data-nt-desk="brand"] [data-nt-desk="conn"]').length,
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
  out.key3State = { current: win.__ntShell.current, calls: Object.assign({}, calls) };
  win.__ntShell.open('notes');
  out.notes = { current: win.__ntShell.current, calls: Object.assign({}, calls), text: layer().textContent,
    active: tabInfo().filter(t => t[3]).map(t => t[0]) };
  out.keyShift = key({ ctrlKey: true, shiftKey: true, key: '2' });
  key({ ctrlKey: true, key: '1' });
  const chatTab = row.querySelector('[aria-label="Chat"]');
  out.chat1 = { current: win.__ntShell.current, open: doc.documentElement.classList.contains('nt-shell-view-open'), clicks: chatTab.clicks };
  chatTab.setAttribute('aria-selected', 'false');
  q('[data-nt-desk="tabbar"]')[0].children[0].click();
  out.chat2 = { clicks: chatTab.clicks };
  key({ ctrlKey: true, key: '2' });
  out.projects = { current: win.__ntShell.current, text: layer().textContent };
  key({ ctrlKey: true, key: '3' });
  out.live3 = { current: win.__ntShell.current, calls: Object.assign({}, calls) };

  // idempotent: mount again without stop, and Notion re-renders its row
  const h2 = win.__ntDesk.shell(ctx);
  const row2 = topRow(); wrap.replaceChild(row2, row);
  win.__ntShell.refresh(); await sleep(200);
  out.m2 = { rails: q('[data-nt-desk="rail"]').length, bars: q('[data-nt-desk="tabbar"]').length, layers: q('[data-nt-desk="shell-layer"]').length,
    conns: q('[data-nt-desk="conn"]').length, row2Hidden: row2.classList.contains('nt-shell-hide'),
    oldRowDetached: !row.isConnected, keyListeners: (winListeners.keydown || []).length,
    carried: tabInfo().filter(t => t[0] === 'live')[0][2], calls: Object.assign({}, calls), current: win.__ntShell.current };
  h.stop();
  out.m2.afterOldStop = q('[data-nt-desk="tabbar"]').length;

  // stop
  win.__ntShell.open('live');
  h2.stop(); h2.stop();
  out.stop = { rails: q('[data-nt-desk="rail"]').length, bars: q('[data-nt-desk="tabbar"]').length, layers: q('[data-nt-desk="shell-layer"]').length,
    conns: q('[data-nt-desk="conn"]').length,
    marked: q('[data-nt-shell]').length, hidden: q('.nt-shell-hide').length, api: typeof win.__ntShell,
    html: doc.documentElement.className, brandCls: brand.classList.contains('nt-shell-brand'), keyListeners: (winListeners.keydown || []).length,
    subs: subs.length, calls: Object.assign({}, calls), brandKept: brand.isConnected, rowKept: row2.isConnected,
    keyAfter: key({ ctrlKey: true, key: '2' }) };

  // offline connection text
  state = null;
  const h3 = win.__ntDesk.shell(ctx);
  out.offline = q('[data-nt-desk="conn"]').map(d => [d.getAttribute('data-level'), d.textContent, d.getAttribute('aria-label')]);
  h3.stop();

  // Esc goes straight back to Chat
  state = { connected: true, attention: [] };
  const h4 = win.__ntDesk.shell(ctx);
  const sh = win.__ntShell;
  const esc = o => key(Object.assign({ key: 'Escape' }, o || {}));
  const settle = () => { sh.open('live'); sh.open('settings'); };
  const e = {};
  settle();
  e.chain = [esc(), sh.current, esc(), sh.current, esc(), sh.current];
  // typing keeps Esc
  settle();
  const inp = node('input'); doc.body.appendChild(inp);
  doc.activeElement = inp; e.input = [esc(), sh.current];
  doc.activeElement = null; e.inputTarget = [esc({ target: inp }), sh.current];
  doc.activeElement = node('textarea'); e.textarea = [esc(), sh.current];
  doc.activeElement = node('div', { contenteditable: 'true' }); e.editable = [esc(), sh.current];
  doc.activeElement = node('div', { role: 'textbox' }); e.textbox = [esc(), sh.current];
  doc.activeElement = node('button'); e.button = [esc(), sh.current];
  doc.activeElement = null;
  settle();
  // modifiers and already handled keys
  e.mods = [esc({ ctrlKey: true }), esc({ shiftKey: true }), esc({ altKey: true }), esc({ defaultPrevented: true }), esc({ isComposing: true }), sh.current];
  // popups: a visible dialog / menu inside Notion's overlay container, or aria-modal anywhere
  const ov = mk('div', { class: 'notion-overlay-container' }); doc.body.appendChild(ov);
  const dlg = visible(node('div', { role: 'dialog' })); ov.appendChild(dlg);
  e.popupDialog = [U.popupOpen(doc), esc(), sh.current];
  ov.removeChild(dlg);
  const menu = visible(node('div', { role: 'menu' })); ov.appendChild(menu);
  e.popupMenu = [U.popupOpen(doc), esc(), sh.current];
  ov.removeChild(menu);
  const lb = visible(node('div', { role: 'listbox' })); ov.appendChild(lb);
  e.popupListbox = [U.popupOpen(doc), esc(), sh.current];
  ov.removeChild(lb);
  const hid = visible(node('div', { role: 'dialog', 'aria-hidden': 'true' })); ov.appendChild(hid);
  e.popupHidden = U.popupOpen(doc);
  ov.removeChild(hid);
  const flat = node('div', { role: 'dialog' }); ov.appendChild(flat);
  e.popupZeroBox = U.popupOpen(doc);
  ov.removeChild(flat);
  e.noPopup = [U.popupOpen(doc), esc(), sh.current];
  settle();
  const modal = visible(node('div', { 'aria-modal': 'true' })); doc.body.appendChild(modal);
  e.popupModal = [U.popupOpen(doc), esc(), sh.current];
  doc.body.removeChild(modal);
  // menus outside the overlay container, in our rail or in Notion's sidebar never block Esc
  settle();
  const loose = visible(node('div', { role: 'menu' })); doc.body.appendChild(loose);
  e.looseMenu = U.popupOpen(doc);
  doc.body.removeChild(loose);
  const railEl = q('[data-nt-desk="rail"]')[0];
  const inRail = visible(node('div', { role: 'menu' })); railEl.appendChild(inRail);
  const inRailModal = visible(node('div', { role: 'dialog', 'aria-modal': 'true' })); railEl.appendChild(inRailModal);
  e.railMenu = [U.popupOpen(doc), esc(), sh.current];
  railEl.removeChild(inRail); railEl.removeChild(inRailModal);
  settle();
  const inSb = visible(node('div', { role: 'listbox' })); sb.appendChild(inSb);
  e.sidebarMenu = [U.popupOpen(doc), esc(), sh.current];
  sb.removeChild(inSb);
  e.noDoc = [U.popupOpen(null), U.popupOpen({})];
  // api.back and stop
  settle();
  e.apiBack = [sh.back(), sh.current];
  h4.stop();
  e.afterStop = [esc(), typeof win.__ntShell];
  out.esc = e;

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
        # Ctrl+1..4 are the four rail pages; Notes is routable but has no key and no rail item.
        self.assertEqual(self.out['keyTab'], ['chat', 'settings', None, None, 'live'])
        self.assertEqual(self.out['keyTabAll'], ['chat', 'projects', 'live', 'settings', None])
        self.assertEqual(self.out['liveBadge'], [3, 0, 0, 0])
        self.assertEqual(self.out['badgeText'], ['', '3', '99+'])
        self.assertEqual(self.out['tabStep'], ['projects', 'chat', 'settings', 'settings'])

    def test_is_back_key(self):
        self.assertEqual(self.out['isBackKey'], [True, False, False, False, False, False, False, False, False, False])

    def test_is_typing(self):
        self.assertEqual(self.out['isTyping'], [True, True, True, True, True, True, False, False, False, False, False])

    def test_router(self):
        out = self.out
        self.assertEqual(out['r0'], 'chat')
        self.assertEqual(out['rBadge'], [7, 4, 0])
        self.assertFalse(out['rBad'])
        self.assertEqual(out['rAfterUnregister'], 3)
        self.assertEqual(out['rCurrent'], 'chat')
        self.assertEqual(out['rLog'], ['show:live', 'ph:live', 'clear:live', 'mount:live', 'unmount', 'cleanup',
                                       'clear:live', 'show:notes', 'ph:notes', 'clear:notes', 'hide', 'chat'])

    def test_router_back_and_history(self):
        back = self.out['back']
        self.assertEqual(back['empty'], [False, 0, 'chat'])
        # chat -> live (re-open of live adds nothing) -> settings
        self.assertEqual(back['depth'], 2)
        # an unknown page changes nothing
        self.assertEqual(back['unknown'], [False, 2, 'settings'])
        self.assertEqual(back['steps'], [True, 'live', 1, True, 'chat', 0, False, 'chat', 0])
        # Notes is routable and takes part in the history
        self.assertEqual(back['notes'], [2, True, 'notes', True, 'chat'])
        self.assertFalse(back['dead'])
        # the history is capped (20) and back never adds to it
        self.assertEqual(back['max'], 20)
        self.assertEqual(back['maxBacks'], 20)

    def test_mount_builds_the_rail(self):
        m1 = self.out['m1']
        self.assertEqual(m1['rails'], 1)
        self.assertTrue(m1['railOnBody'])
        self.assertTrue(m1['railClass'])
        self.assertEqual(m1['bars'], 1)
        self.assertTrue(m1['barInRail'])
        self.assertEqual(m1['logo'], ['N'])
        # Notion's own top row stays in the DOM, only hidden
        self.assertTrue(m1['rowHidden'])
        self.assertTrue(m1['rowKept'])
        self.assertEqual(m1['rowMark'], 'toprow')
        # rail tabs: chat / projects / live / settings. Notes has no rail item.
        self.assertEqual(m1['tabs'], [['chat', 'Chats', '4', True, True], ['projects', 'Projects', '', False, True],
                                      ['live', 'Live', '3', False, True], ['settings', 'Settings', '', False, True]])
        # the rail footer is the one connection status; nothing in the brand header
        self.assertEqual(m1['conn'], [['on', 'Online', True, 'Connected. NotionTool is running.']])
        self.assertEqual(m1['brandDots'], 0)
        self.assertTrue(m1['brandCls'])
        self.assertTrue(m1['on'])
        self.assertEqual(m1['api'], 'chat')
        self.assertEqual(self.out['offline'], [['wait', 'Connecting', 'Connecting to NotionTool...']])

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
        # Ctrl+3 is Live: already open, so it is handled but nothing is mounted again
        self.assertTrue(out['key3'])
        self.assertEqual(out['key3State'], {'current': 'live', 'calls': {'mount': 1, 'unmount': 0}})
        # Notes is still routable, but no rail tab is highlighted for it
        self.assertEqual(out['notes']['current'], 'notes')
        self.assertEqual(out['notes']['calls'], {'mount': 1, 'unmount': 1})
        self.assertNotIn('Live view', out['notes']['text'])
        self.assertEqual(out['notes']['active'], [])
        self.assertFalse(out['keyShift'])
        self.assertEqual(out['chat1'], {'current': 'chat', 'open': False, 'clicks': 0})
        self.assertEqual(out['chat2'], {'clicks': 1})
        self.assertEqual(out['projects']['current'], 'projects')
        self.assertIn('projects', out['projects']['text'].lower())
        self.assertEqual(out['live3'], {'current': 'live', 'calls': {'mount': 2, 'unmount': 1}})

    def test_idempotent_mount(self):
        m2 = self.out['m2']
        self.assertEqual((m2['rails'], m2['bars'], m2['layers'], m2['conns']), (1, 1, 1, 1))
        self.assertTrue(m2['row2Hidden'])
        self.assertTrue(m2['oldRowDetached'])
        self.assertEqual(m2['keyListeners'], 1)
        self.assertEqual(m2['carried'], '5')
        # live: mounted, left for Notes, mounted again; the re-mount carries the open Live view over
        self.assertEqual(m2['calls'], {'mount': 3, 'unmount': 2})
        self.assertEqual(m2['current'], 'live')
        self.assertEqual(m2['afterOldStop'], 1)

    def test_stop_restores_everything(self):
        s = self.out['stop']
        self.assertEqual((s['rails'], s['bars'], s['layers'], s['conns'], s['marked'], s['hidden']), (0, 0, 0, 0, 0, 0))
        self.assertEqual(s['api'], 'undefined')
        self.assertNotIn('nt-shell', s['html'])
        self.assertFalse(s['brandCls'])
        self.assertEqual((s['keyListeners'], s['subs']), (0, 0))
        self.assertEqual(s['calls'], {'mount': 3, 'unmount': 3})
        self.assertTrue(s['brandKept'])
        self.assertTrue(s['rowKept'])
        self.assertFalse(s['keyAfter'])

    def test_esc_goes_back(self):
        e = self.out['esc']
        # settings -> straight to chat; on chat Esc stays with Notion
        self.assertEqual(e['chain'], [True, 'chat', False, 'chat', False, 'chat'])
        self.assertEqual(e['apiBack'], [True, 'live'])
        self.assertEqual(e['afterStop'], [False, 'undefined'])

    def test_esc_ignored_while_typing(self):
        e = self.out['esc']
        for name in ('input', 'inputTarget', 'textarea', 'editable', 'textbox'):
            self.assertEqual(e[name], [False, 'settings'], name)
        # a focused button is not typing
        self.assertEqual(e['button'], [True, 'chat'])

    def test_esc_needs_a_plain_key(self):
        self.assertEqual(self.out['esc']['mods'], [False, False, False, False, False, 'settings'])

    def test_esc_closes_popups_first(self):
        e = self.out['esc']
        for name in ('popupDialog', 'popupMenu', 'popupListbox', 'popupModal'):
            self.assertEqual(e[name], [True, False, 'settings'], name)
        # hidden or zero-size popups do not count
        self.assertFalse(e['popupHidden'])
        self.assertFalse(e['popupZeroBox'])
        self.assertEqual(e['noPopup'], [False, True, 'chat'])

    def test_permanent_menus_do_not_count_as_popups(self):
        e = self.out['esc']
        self.assertFalse(e['looseMenu'])
        self.assertEqual(e['railMenu'], [False, True, 'chat'])
        self.assertEqual(e['sidebarMenu'], [False, True, 'chat'])
        self.assertEqual(e['noDoc'], [False, False])

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

    def test_popup_check_is_narrow(self):
        self.assertIn("OVERLAY_SELECTOR = '.notion-overlay-container'", self.js)
        self.assertIn("POPUP_IGNORE_SELECTOR = '.nt-rail,.notion-sidebar'", self.js)

    def test_css(self):
        self.assertIn('.nt-shell-hide { display: none !important; }', self.css)
        self.assertIn('html.nt-shell-on .nt-desk-composer-empty::after', self.css)
        self.assertIn('html.nt-shell-view-open .nt-shell-layer { display: block; }', self.css)
        self.assertIn('#8b5cf6', self.css)
        self.assertNotIn('#7c5cff', self.css)
        self.assertNotIn('#0b0d12', self.css)

    def test_css_rail(self):
        self.assertIn('.nt-rail {', self.css)
        self.assertIn('width: var(--nt-rail-w, 84px);', self.css)
        self.assertIn('z-index: 95;', self.css)
        self.assertIn('html.nt-shell-on .notion-app-inner {', self.css)
        self.assertIn('margin-left: var(--nt-rail-w, 84px);', self.css)
        self.assertIn('width: calc(100% - var(--nt-rail-w, 84px));', self.css)

    def test_second_status_dot_hidden(self):
        # The rail footer is the only connection status; the dot extras.js puts in the brand header is hidden.
        self.assertIn('html.nt-shell-on .nt-shell-brand .nt-desk-brand-dot { display: none !important; }', self.css)
        self.assertNotIn('.nt-shell-conn', self.css)

    def test_line_endings(self):
        for name in ('shell.js', 'shell.css'):
            with open(os.path.join(DESK, name), 'rb') as fh:
                self.assertNotIn(b'\r', fh.read())


if __name__ == '__main__':
    unittest.main()
