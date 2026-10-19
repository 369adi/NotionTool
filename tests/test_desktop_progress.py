"""progress.js / progress.css: Team progress cards in the Live view and sidebar bars (fake DOM, fake timers, no network)."""
import json
import os
import re
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = os.path.join(ROOT, 'notiontool', 'web', 'desktop', 'progress.js')
CSS = os.path.join(ROOT, 'notiontool', 'web', 'desktop', 'progress.css')
NODE = r'C:\Program Files\nodejs\node.exe'
HAS_NODE = os.path.exists(NODE)

NODE_SCRIPT = r"""
const fs = require('fs'), vm = require('vm');
function N(tag) { this.tagName = String(tag).toUpperCase(); this.childNodes = []; this.parentNode = null; this.attrs = {};
  this.className = ''; this.style = {}; this.listeners = {}; this._t = ''; this.clicks = 0; }
Object.defineProperty(N.prototype, 'textContent', { get() { return this._t + this.childNodes.map(c => c.textContent).join(''); },
  set(v) { this._t = String(v); this.childNodes = []; } });
Object.defineProperty(N.prototype, 'nextSibling', { get() { if (!this.parentNode) return null;
  const s = this.parentNode.childNodes; return s[s.indexOf(this) + 1] || null; } });
N.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
N.prototype.getAttribute = function (k) { return k in this.attrs ? this.attrs[k] : null; };
N.prototype.removeAttribute = function (k) { delete this.attrs[k]; };
N.prototype.appendChild = function (c) { if (c.parentNode) c.parentNode.removeChild(c); c.parentNode = this; this.childNodes.push(c); return c; };
N.prototype.insertBefore = function (c, ref) { if (!ref) return this.appendChild(c); if (c.parentNode) c.parentNode.removeChild(c);
  c.parentNode = this; this.childNodes.splice(this.childNodes.indexOf(ref), 0, c); return c; };
N.prototype.removeChild = function (c) { const i = this.childNodes.indexOf(c); if (i >= 0) this.childNodes.splice(i, 1); c.parentNode = null; return c; };
N.prototype.addEventListener = function (t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); };
N.prototype.removeEventListener = function (t, f) { this.listeners[t] = (this.listeners[t] || []).filter(x => x !== f); };
N.prototype.dispatch = function (t, ev) { (this.listeners[t] || []).slice().forEach(f => f(ev)); };
N.prototype.click = function () { this.clicks += 1; };
function walk(n, out) { n.childNodes.forEach(c => { out.push(c); walk(c, out); }); return out; }
function hasCls(n, c) { return (' ' + n.className + ' ').indexOf(' ' + c + ' ') >= 0; }
function matches(n, sel) { const m = /^\[([^=\]]+)="([^"]*)"\]$/.exec(sel); if (m) return n.attrs[m[1]] === m[2];
  const p = sel.split('.'); if (p[0] && n.tagName !== p[0].toUpperCase()) return false; return p.slice(1).every(c => hasCls(n, c)); }
const document = new N('#document'); document.hidden = false;
document.createElement = t => new N(t);
document.querySelectorAll = sel => walk(document, []).filter(n => matches(n, sel));
const body = document.appendChild(new N('body'));
const liveRoot = body.appendChild(new N('div'));
const teamSection = liveRoot.appendChild(new N('section')); teamSection.className = 'nt-desk-lv-section nt-desk-lv-team';
const history = liveRoot.appendChild(new N('section')); history.className = 'nt-desk-lv-section nt-desk-lv-history';
const list = body.appendChild(new N('div'));
function row(th) { const r = list.appendChild(new N('a')); r.className = 'nt-desk-coworker'; r.setAttribute('data-nt-desk', 'coworker');
  r.setAttribute('data-nt-desk-thread', th); return r; }
const row1 = row('th1'), row2 = row('th2');
let NOW = 1800000000; Date.now = () => NOW * 1000;
const timers = {}; let tid = 0;
global.setInterval = (f, ms) => { tid += 1; timers[tid] = { f, ms }; return tid; };
global.clearInterval = id => { delete timers[id]; };
global.window = {}; global.document = document;
vm.runInThisContext(fs.readFileSync(process.argv[1], 'utf8'));
const core = window.__ntDesk.progressCore;
function st(prog) { return { workers: [
  { id: 'a:1', purpose: 'worker', label: 'Coworker 1', task_id: 't1', thread_id: 'th1', url: 'https://www.notion.so/chat?t=th1', state: 'busy' },
  { id: 'a:2', purpose: 'worker', label: 'Coworker 2', task_id: '', thread_id: 'th2', url: 'https://www.notion.so/chat?t=th2', state: 'ready' },
  { id: 'm', purpose: 'main', label: 'Main', thread_id: 'thm' }],
  tasks: [{ id: 't1', plain_summary: 'Fix the sidebar', title: 'raw title' }], progress: { t1: prog } }; }
let state = st({ task_id: 't1', worker_id: 'a:1', steps: 5, step: 'Writing file', target: 'overlay.js', last_activity: NOW - 20,
  progress: { percent: 40, source: 'phase', phase: 'editing', label: 'Editing' }, stalled: false });
let listener = null, unsubscribed = false;
const ctx = { getState: () => state, onState: cb => { listener = cb; return () => { unsubscribed = true; }; } };
const part = window.__ntDesk.progress(ctx);
const R = {};
const find = (n, c) => walk(n, []).find(x => hasCls(x, c));
const cards = () => document.querySelectorAll('[data-nt-desk="progress-card"]');
const grid = () => document.querySelectorAll('[data-nt-desk="progress-team"]')[0];
function card(i) { const c = cards()[i]; return { name: find(c, 'nt-desk-pg-name').textContent, label: find(c, 'nt-desk-pg-label').textContent,
  task: find(c, 'nt-desk-pg-task').textContent, step: find(c, 'nt-desk-pg-step').textContent, steps: find(c, 'nt-desk-pg-steps').textContent,
  ago: find(c, 'nt-desk-pg-ago').textContent, width: find(c, 'nt-desk-pg-fill').style.width, warn: find(c, 'nt-desk-pg-warn').textContent,
  warnHidden: !!find(c, 'nt-desk-pg-warn').hidden, stalled: hasCls(c, 'nt-desk-pg-stalled'), href: c.getAttribute('href') }; }
R.first = { count: cards().length, gridAfterTeam: teamSection.nextSibling === grid(), c1: card(0), c2: card(1),
  busyText: find(grid(), 'nt-desk-pg-count').textContent, timers: Object.keys(timers).length, renders: part.renderCount(),
  side1: !!find(row1, 'nt-desk-pg-side'), phase1: (find(row1, 'nt-desk-pg-phase') || {}).textContent, side1Width: find(row1, 'nt-desk-pg-side-fill').style.width,
  side2: !!find(row2, 'nt-desk-pg-side'), row1Cls: row1.className };
const node1 = cards()[0];
NOW += 1;
state = st({ task_id: 't1', worker_id: 'a:1', steps: 9, step: 'Running command', target: 'python -m unittest tests.test_x', last_activity: NOW - 401,
  progress: { percent: 75, source: 'steps', phase: '', label: 'Step 3 of 4' }, stalled: false });
listener(state);
R.noRenderOnState = part.renderCount() === R.first.renders && card(0).label === 'Editing';
const tick = () => Object.values(timers).forEach(t => t.f());
tick();
R.second = { c1: card(0), sameNode: cards()[0] === node1, renders: part.renderCount(), phase1: find(row1, 'nt-desk-pg-phase').textContent,
  sideStalled: hasCls(find(row1, 'nt-desk-pg-side'), 'nt-desk-pg-stalled') };
tick(); tick();
R.throttled = part.renderCount() === R.second.renders;
document.hidden = true; document.dispatch('visibilitychange', {});
R.hiddenTimers = Object.keys(timers).length;
NOW += 5; tick(); R.hiddenRenders = part.renderCount() === R.second.renders;
document.hidden = false; document.dispatch('visibilitychange', {});
R.visibleTimers = Object.keys(timers).length; R.visibleRendered = part.renderCount() === R.second.renders + 1;
let prevented = 0;
cards()[0].dispatch('click', { isTrusted: false, preventDefault() { prevented += 1; } });
R.untrusted = { prevented, rowClicks: row1.clicks };
cards()[0].dispatch('click', { isTrusted: true, preventDefault() { prevented += 1; } });
R.trusted = { prevented, rowClicks: row1.clicks };
liveRoot.removeChild(grid());
NOW += 1; tick();
R.reattached = teamSection.nextSibling === grid();
state = st({ task_id: 't1', worker_id: 'a:1', steps: 12, completed: true, last_activity: NOW, progress: { percent: 90, source: 'phase', phase: 'answering' } });
state.workers[0].task_id = ''; state.workers[0].state = 'ready';
listener(state); NOW += 1; tick();
R.idle = { side1: !!find(row1, 'nt-desk-pg-side'), row1Cls: row1.className, label: card(0).label };
part.stop();
R.stopped = { own: walk(document, []).filter(n => n.getAttribute('data-nt-desk-own') === '1').length, timers: Object.keys(timers).length,
  unsubscribed, rows: document.querySelectorAll('[data-nt-desk="coworker"]').length, team: teamSection.parentNode === liveRoot,
  listeners: (document.listeners.visibilitychange || []).length };
R.core = { ago: [core.agoText(NOW - 5, NOW), core.agoText(NOW - 125, NOW), core.agoText(NOW - 7300, NOW), core.agoText(0, NOW),
  core.agoText((NOW - 30) * 1000, NOW)], steps: [core.stepsText(1), core.stepsText(0), core.stepsText([1, 2, 3])],
  labels: ['exploring', 'editing', 'testing', 'answering'].map(p => core.label({ progress: { source: 'phase', phase: p } })),
  checklist: core.label({ progress: { source: 'checklist', label: '2 of 4 checked', percent: 50 } }),
  done: [core.label({ completed: true }), core.percent({ completed: true, progress: { percent: 10 } })],
  clamp: core.percent({ progress: { percent: 140 } }), noStep: core.stepLine({}), npm: core.stepLine({ step: 'Running command', target: 'npm test' }),
  cmd: core.stepLine({ step: 'Running command', target: 'git status' }) };
console.log(JSON.stringify(R));
"""


def read(path):
    with open(path, 'rb') as handle:
        return handle.read()


class Static(unittest.TestCase):
    def test_files_lf_only(self):
        for path in (JS, CSS):
            self.assertNotIn(b'\r', read(path), path)

    def test_no_network_no_send_no_programmatic_open(self):
        source = read(JS).decode('utf-8')
        for banned in ('fetch(', 'XMLHttpRequest', 'WebSocket', 'ctx.call', 'window.open', 'location.href =',
                       'location.assign', 'location.reload', 'innerHTML', 'sendBeacon'):
            self.assertNotIn(banned, source, banned)
        self.assertIn('ev.isTrusted !== true', source)

    def test_own_classes_and_nodes(self):
        source = read(JS).decode('utf-8')
        for cls in re.findall(r"el\('[a-z0-9]+', '([^']+)'", source):
            self.assertTrue(cls.startswith('nt-desk-pg-'), cls)
        css = read(CSS).decode('utf-8')
        for cls in re.findall(r'\.([a-zA-Z][\w-]*)', re.sub(r'/\*.*?\*/', '', css, flags=re.S)):
            if not re.match(r'^\d', cls):
                self.assertTrue(cls.startswith('nt-desk-'), cls)
        self.assertIn('transition: width', css)
        self.assertIn('prefers-reduced-motion', css)

    def test_plain_english_texts(self):
        source = read(JS).decode('utf-8')
        for phrase in ('Team progress', 'Exploring', 'Editing', 'Testing', 'Answering', 'Running tests',
                       'Last activity ', 'Stalled: no new step for '):
            self.assertIn(phrase, source)


@unittest.skipUnless(HAS_NODE, 'node not installed')
class Node(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = subprocess.run([NODE, '-e', NODE_SCRIPT, JS], capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise AssertionError(result.stderr[-2000:])
        cls.r = json.loads(result.stdout.strip().splitlines()[-1])

    def test_node_check(self):
        result = subprocess.run([NODE, '--check', JS], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cards_after_live_team_section(self):
        first = self.r['first']
        self.assertEqual(first['count'], 2)  # coworkers only, never the Main
        self.assertTrue(first['gridAfterTeam'])
        self.assertEqual(first['busyText'], '1 of 2 busy')
        c1 = first['c1']
        self.assertEqual((c1['name'], c1['label'], c1['task']), ('Coworker 1', 'Editing', 'Fix the sidebar'))
        self.assertEqual((c1['step'], c1['steps'], c1['ago']), ('Writing file overlay.js', '5 steps', 'Last activity 20 s ago'))
        self.assertEqual(c1['width'], '40%')
        self.assertTrue(c1['warnHidden'])
        self.assertFalse(c1['stalled'])
        self.assertEqual(c1['href'], 'https://www.notion.so/chat?t=th1')
        c2 = first['c2']
        self.assertEqual((c2['label'], c2['task'], c2['width']), ('Idle', 'No task', '0%'))
        self.assertEqual((first['timers'], first['renders']), (1, 1))

    def test_sidebar_bar_and_phase(self):
        first = self.r['first']
        self.assertTrue(first['side1'])
        self.assertEqual((first['phase1'], first['side1Width']), ('Editing', '40%'))
        self.assertFalse(first['side2'])
        self.assertIn('nt-desk-pg-row', first['row1Cls'])
        self.assertEqual(self.r['idle']['side1'], False)
        self.assertNotIn('nt-desk-pg-row', self.r['idle']['row1Cls'])
        self.assertEqual(self.r['idle']['label'], 'Done')  # last finished task, no sidebar bar

    def test_diff_render_steps_and_stall(self):
        self.assertTrue(self.r['noRenderOnState'])  # state changes wait for the next tick
        second = self.r['second']
        self.assertTrue(second['sameNode'])
        c1 = second['c1']
        self.assertEqual((c1['label'], c1['step'], c1['steps'], c1['width']), ('Step 3 of 4', 'Running tests', '9 steps', '75%'))
        self.assertEqual(c1['ago'], 'Last activity 6 min ago')
        self.assertTrue(c1['stalled'])
        self.assertEqual(c1['warn'], 'Stalled: no new step for 6 min')
        self.assertFalse(c1['warnHidden'])
        self.assertEqual(second['phase1'], 'Step 3 of 4')
        self.assertTrue(second['sideStalled'])

    def test_one_render_per_second_and_hidden_stops_timer(self):
        self.assertTrue(self.r['throttled'])
        self.assertEqual(self.r['hiddenTimers'], 0)
        self.assertTrue(self.r['hiddenRenders'])
        self.assertEqual(self.r['visibleTimers'], 1)
        self.assertTrue(self.r['visibleRendered'])

    def test_click_only_on_real_user_click(self):
        self.assertEqual(self.r['untrusted'], {'prevented': 1, 'rowClicks': 0})
        self.assertEqual(self.r['trusted'], {'prevented': 2, 'rowClicks': 1})

    def test_lazy_reattach_and_clean_stop(self):
        self.assertTrue(self.r['reattached'])
        stopped = self.r['stopped']
        self.assertEqual((stopped['own'], stopped['timers'], stopped['listeners']), (0, 0, 0))
        self.assertTrue(stopped['unsubscribed'])
        self.assertEqual(stopped['rows'], 2)  # foreign nodes stay
        self.assertTrue(stopped['team'])

    def test_core_texts(self):
        core = self.r['core']
        self.assertEqual(core['ago'], ['Last activity 5 s ago', 'Last activity 2 min ago', 'Last activity 2 h ago',
                                       'No activity yet', 'Last activity 30 s ago'])
        self.assertEqual(core['steps'], ['1 step', '0 steps', '3 steps'])
        self.assertEqual(core['labels'], ['Exploring', 'Editing', 'Testing', 'Answering'])
        self.assertEqual(core['checklist'], '2 of 4 checked')
        self.assertEqual(core['done'], ['Done', 100])
        self.assertEqual(core['clamp'], 100)
        self.assertEqual((core['noStep'], core['npm'], core['cmd']),
                         ('Waiting for the first step', 'Running tests', 'Running command git status'))


if __name__ == '__main__':
    unittest.main()
