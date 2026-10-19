"""Static and node-based checks for the desktop overlay Live view (notiontool/web/desktop/live.js + live.css).

The Live view was redesigned on 2026-10-19 (Simple Live view + live coworker progress): sections Problems,
Working now, Team, History (folded) and Details (folded) replace the old tiles / timeline / Main switches
dashboard. Hard rules that stay: no network calls, never remove Notion nodes, every class is nt-desk- prefixed.
"""
import json
import os
import re
import subprocess
import unittest

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DESK = os.path.join(ROOT, 'notiontool', 'web', 'desktop')
JS = os.path.join(DESK, 'live.js')
CSS = os.path.join(DESK, 'live.css')
NODE = r'C:\Program Files\nodejs\node.exe'


def read(path):
    with open(path, 'rb') as handle:
        return handle.read().decode('utf-8')


def css_text():
    return re.sub(r'/\*.*?\*/', '', read(CSS), flags=re.S)


def test_registers_live_part():
    js = read(JS)
    assert 'window.__ntDesk = window.__ntDesk || {}' in js
    assert 'window.__ntDesk' in js and '.live =' in js
    assert 'return { stop: stop, open: open, close: close }' in js
    assert js.lstrip().startswith('/*') and '(function () {' in js and js.rstrip().endswith('})();')


def test_open_triggers_and_close():
    js = read(JS)
    assert "'#nt-live'" in js
    assert 'event.ctrlKey && event.shiftKey' in js and "=== 'l'" in js
    assert "'Escape'" in js
    assert 'nt-desk-close' in js
    assert 'panel.hidden = true' in js


def test_state_contract_fields():
    # State fields the Simple Live view reads (the Main switches section moved to extras.js with the redesign).
    js = read(JS)
    for needle in ('onState', 'getState', 'runs', 'tasks', 'workers', 'events', 'attention', 'rate',
                   'used_per_min', 'limit_per_min', 'estimated_tokens', 'run_id', 'task_id', 'worker_id',
                   "purpose === 'worker'", 'goal', 'summary', 'remote_unknown', 'awaiting_main', 'proof',
                   'started_at', 'last_output_at'):
        assert needle in js, needle


def test_throttle_limits_and_meter_levels():
    # Redesign: at most one render per second, only changed sections repaint, timers stop while hidden.
    js = read(JS)
    assert 'RENDER_MS = 1000' in js and 'REFRESH_MS = 5000' in js
    assert 'EVENT_LIMIT = 10' in js and '.slice(-EVENT_LIMIT)' in js
    assert 'HISTORY_LIMIT = 20' in js and '.slice(0, HISTORY_LIMIT)' in js
    assert 'if (sigs[name] === key) return;' in js
    assert 'document.hidden' in js
    assert "text: value || '-'" in js  # missing detail values render as '-'


def test_no_inner_html():
    js = read(JS)
    assert 'innerHTML' not in js
    assert 'outerHTML' not in js and 'insertAdjacentHTML' not in js


def test_no_network_and_no_notion_node_removal():
    js = read(JS)
    for needle in ('fetch(', 'XMLHttpRequest', 'WebSocket', 'EventSource', 'sendBeacon', '.remove()'):
        assert needle not in js, needle
    # removeChild only lives in the two helpers, and the helpers only get the part's own nodes.
    assert js.count('removeChild') == 2
    assert 'function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }' in js
    assert 'function detach(node) { if (node && node.parentNode) node.parentNode.removeChild(node); }' in js
    assert set(re.findall(r'\bdetach\(([^)]*)\)', js)) <= {'node', 'root', 'panel', 'pill'}
    assert set(re.findall(r'\bclear\(([^)]*)\)', js)) <= {'node', 'confirmBox', 'slots[name]'}
    # detach(node) outside the helper is the sweep of stale copies of our own pill/panel/view.
    assert 'if (node !== panel && node !== pill && node !== root) detach(node);' in js
    assert "PILL_ID = 'nt-desk-" in js and "PANEL_ID = 'nt-desk-" in js
    stale = re.search(r'var STALE_SELECTORS = \[(.*?)\];', js, re.S)
    assert stale
    for selector in re.findall(r"'([^']*)'", stale.group(1)):
        assert selector == '#' or selector.startswith('[data-nt-desk="'), selector


EL_CLS = re.compile(r"el\('[a-z0-9]+', \{ cls: (.*?)(?:, '?[A-Za-z-]+'?: | \})")


def class_names(expr):
    for literal in re.findall(r"'([^']*)'", expr):
        for name in literal.split():
            yield name


def test_js_classes_prefixed():
    # Every class set on a DOM node (el(..., { cls }), .className =, classList) is nt-desk- prefixed.
    # Plain data fields like connection().cls ('on'/'off') are only used as suffixes of prefixed classes.
    js = read(JS)
    exprs = EL_CLS.findall(js) + re.findall(r'\.className = (.*?);', js)
    assert len(exprs) > 40
    names = [name for expr in exprs for name in class_names(expr)]
    assert names
    for name in names:
        assert name.startswith('nt-desk-'), name
    for name in re.findall(r"classList\.(?:add|remove|toggle|contains)\('([^']*)'", js):
        assert name.startswith('nt-desk-'), name


def test_css_selectors_prefixed():
    css = css_text()
    selectors = ''.join(re.findall(r'(?:^|})\s*([^{}@]+?)\s*{', css))
    classes = re.findall(r'\.([A-Za-z_-][\w-]*)', selectors)
    assert classes
    for name in classes:
        assert name.startswith('nt-desk-'), name
    # Colours come from the shared theme tokens (theme.css owns light/dark), always with a fallback.
    assert 'var(--nt-' in css


def test_dashboard_structure():
    js = read(JS)
    assert "text: 'Live'" in js
    assert "['needs', 'working', 'team', 'history', 'details']" in js
    for needle in ('nt-desk-lv-conn', 'nt-desk-lv-dot', 'nt-desk-lv-card', 'nt-desk-lv-progress', 'nt-desk-lv-bar-fill',
                   'nt-desk-lv-step', 'nt-desk-lv-task-head', 'nt-desk-lv-member', 'nt-desk-lv-fold',
                   'nt-desk-lv-events', 'nt-desk-lv-confirm', 'nt-desk-live-pill'):
        assert needle in js, needle
    for label in ("'Problems'", "'Working now'", "'Team'", "'History'", "'Details'", "'Nothing is running.'",
                  "'No problems right now.'", "'What to do: '"):
        assert label in js, label


def test_theme_tokens_with_fallbacks():
    css = css_text()
    for token, fallback in (('--nt-bg', '#0b0d12'), ('--nt-surface', '#12151d'), ('--nt-raised', '#181c26'),
                            ('--nt-success', '#22c55e'), ('--nt-warning', '#f59e0b'),
                            ('--nt-danger', '#ef4444'), ('--nt-radius', '12px')):
        assert 'var(%s, %s)' % (token, fallback) in css, token
    assert '#7c5cff' in css and '#22d3ee' in css and 'linear-gradient(' in css


def test_glass_and_animation():
    css = css_text()
    assert 'backdrop-filter: blur(' in css
    assert '.nt-desk-live.nt-desk-live-open' in css and 'translateX(' in css and 'transition:' in css
    assert 'prefers-reduced-motion: reduce' in css
    js = read(JS)
    assert "classList.add('nt-desk-live-open')" in js and "classList.remove('nt-desk-live-open')" in js
    assert 'ANIM_MS' in js


def test_lf_utf8():
    for path in (JS, CSS, os.path.abspath(__file__)):
        with open(path, 'rb') as handle:
            data = handle.read()
        assert b'\r' not in data, path
        data.decode('utf-8')


@pytest.mark.skipif(not os.path.exists(NODE), reason='node not installed')
def test_node_check():
    result = subprocess.run([NODE, '--check', JS], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


# Minimal fake DOM: enough for live.js to build, open, render, act and close the panel. Timers are global
# in a browser (live.js calls setTimeout/setInterval directly), so the sandbox provides them as globals.
NODE_DOM = r"""
const fs = require('fs'), vm = require('vm');
const code = fs.readFileSync(process.argv[1], 'utf8');
function Node(tag) {
  this.tagName = tag; this.children = []; this.attrs = {}; this.style = {}; this.className = '';
  this.textContent = ''; this.hidden = false; this.parentNode = null; this.listeners = {};
  const self = this;
  this.classList = {
    add(c) { const s = self.className.split(' ').filter(Boolean); if (s.indexOf(c) < 0) s.push(c); self.className = s.join(' '); },
    remove(c) { self.className = self.className.split(' ').filter(x => x && x !== c).join(' '); },
    contains(c) { return self.className.split(' ').indexOf(c) >= 0; }
  };
}
Node.prototype.appendChild = function (k) { if (k.parentNode) k.parentNode.removeChild(k); k.parentNode = this; this.children.push(k); return k; };
Node.prototype.removeChild = function (k) { this.children = this.children.filter(x => x !== k); k.parentNode = null; return k; };
Node.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
Node.prototype.addEventListener = function (t, f) { this.listeners[t] = f; };
Object.defineProperty(Node.prototype, 'firstChild', { get() { return this.children[0] || null; } });
Object.defineProperty(Node.prototype, 'offsetWidth', { get() { return 1; } });
function text(n) { return (n.textContent || '') + ' ' + n.children.map(text).join(' '); }
function find(n, cls, out) { out = out || []; if (n.className.split(' ').indexOf(cls) >= 0) out.push(n); n.children.forEach(k => find(k, cls, out)); return out; }
function buttons(n, label, out) { out = out || []; if (n.tagName === 'button' && text(n).indexOf(label) >= 0) out.push(n); n.children.forEach(k => buttons(k, label, out)); return out; }
function click(n) { n.listeners.click({ stopPropagation() {}, preventDefault() {} }); }
const body = new Node('body');
const notionNode = body.appendChild(new Node('div'));
let timers = [];
const listeners = {};
const win = {
  addEventListener(t, f) { listeners[t] = f; }, removeEventListener(t) { delete listeners[t]; },
  history: { replaceState() {} }
};
const sandbox = { window: win, document: { body: body, documentElement: body,
  createElement: t => new Node(t), createTextNode: t => { const n = new Node('#text'); n.textContent = t; return n; } },
  location: { hash: '', pathname: '/', search: '' }, history: win.history,
  setTimeout(f) { timers.push(f); return timers.length; }, clearTimeout() {},
  setInterval() { return 1; }, clearInterval() {}, console: { warn() {} } };
vm.runInNewContext(code, sandbox);
const now = Date.now();
const stateObj = {
  rate: { used_per_min: 3, limit_per_min: 10 },
  runs: [{ id: 'r1', state: 'executing', goal: 'Ship it. Then more words', created: now / 1000 - 300 },
         { id: 'r2', state: 'blocked', goal: 'Old fix', created: now / 1000 - 200 },
         { id: 'r3', state: 'done', goal: 'Finished one', created: now / 1000 - 900, finished: now / 1000 - 600 }],
  tasks: [{ id: 't1', run_id: 'r1', state: 'running', title: 'Build tiles', worker_id: 'w1', created: 1, started_at: (now - 125000) / 1000 },
          { id: 't2', run_id: 'r1', state: 'done', title: 'Plan', created: 2 },
          { id: 't3', run_id: 'r1', state: 'uncertain', title: 'Write docs', created: 3 }],
  workers: [{ id: 'w1', label: 'Cowork 1', purpose: 'worker', state: 'busy', task_id: 't1', context_usage: { estimated_tokens: 50000 } },
            { id: 'w2', label: 'Cowork 2', purpose: 'worker', state: 'ready' },
            { id: 'm', label: 'Main', purpose: 'main', state: 'busy' }],
  attention: [{ message: 'Check me' }],
  events: [{ ts: now / 1000 - 30, kind: 'notion_inference_error', message: 'boom' }, { ts: now / 1000, kind: 'task_done', message: 'ok' }]
};
const calls = [];
let unsubscribed = false;
const util = win.__ntDesk.liveUtil;
const part = win.__ntDesk.live({ getState: () => stateObj, onState: () => () => { unsubscribed = true; },
  call: (action, args) => { calls.push([action, args]); return Promise.resolve({}); } });
const panel = find(body, 'nt-desk-live')[0];
const out = { appended: body.children.length === 3 && body.children[0] === notionNode && !!panel, hiddenAtStart: panel.hidden };
listeners.keydown({ ctrlKey: true, shiftKey: true, altKey: false, key: 'L', preventDefault() {} });
out.openAfterKey = !panel.hidden && panel.classList.contains('nt-desk-live-open');
let t = text(panel);
out.texts = ['Live', 'Connected', 'Problems', 'Working now', 'Team', 'History', 'Details', 'Ship it.', 'Step 2 of 3',
  'Getting started', 'Build tiles', 'Working', 'Done', 'Problem', '2m', 'Cowork 1', 'Cowork 2', 'Busy', 'Idle', '1 of 2 busy',
  'Some steps of \u201cOld fix\u201d ran into a problem.', 'Try again',
  'A coworker finished \u201cWrite docs\u201d, but the answer got lost.', 'Check & accept', 'What to do: ', 'Pause', 'Stop'
].filter(x => t.indexOf(x) < 0);
const teamSlot = find(panel, 'nt-desk-lv-slot-team')[0];
out.teamMembers = find(teamSlot, 'nt-desk-lv-member').length;
out.mainHidden = text(teamSlot).indexOf('Main') < 0;
out.problemCount = find(panel, 'nt-desk-lv-problem').length;
out.progressWidth = find(panel, 'nt-desk-lv-bar-fill').map(n => n.style.width);
out.foldedClosed = t.indexOf('Finished one') < 0 && t.indexOf('Requests per minute') < 0;
click(buttons(panel, 'History')[0]);
click(buttons(panel, 'Details')[0]);
t = text(panel);
out.foldTexts = ['Finished one', 'Finished.', '10m ago', 'Requests per minute', '3 of 10', 'Coworkers busy', 'Open steps',
  'Chat size Cowork 1', '50k tokens', 'Check me', 'Notion inference error', 'boom', 'Task done', '30s ago', 'just now'
].filter(x => t.indexOf(x) < 0);
click(buttons(find(panel, 'nt-desk-lv-slot-needs')[0], 'Try again')[0]);
const confirmBox = find(panel, 'nt-desk-lv-confirm')[0];
out.askFirst = !confirmBox.hidden && text(confirmBox).indexOf('Try the failed steps again?') >= 0 && calls.length === 0;
click(buttons(confirmBox, 'Try again')[0]);
out.calls = calls;
out.confirmClosed = confirmBox.hidden;
listeners.keydown({ key: 'Escape', ctrlKey: false, shiftKey: false, preventDefault() {} });
out.closingClassRemoved = !panel.classList.contains('nt-desk-live-open');
out.hiddenBeforeTimer = panel.hidden;
timers.splice(0).forEach(f => f());
out.hiddenAfterTimer = panel.hidden;
out.util = {
  status: ['running', 'done', 'uncertain', 'cancelled', 'queued'].map(s => util.taskStatus({ state: s })),
  checked: util.taskStatus({ state: 'done', checked: true }),
  steps: [util.stepText(0, 0), util.stepText(1, 3), util.stepText(1, 1), util.stepText(3, 3)],
  progressEmpty: util.progressOf({}, []),
  initials: [util.initials('Cowork 12'), util.initials('Main'), util.initials('')],
  conn: [util.connection(null, now, 0).cls, util.connection({}, now, now - 20000).cls, util.connection({}, now, now).cls],
  span: [util.span(5), util.span(125), util.span(3700)],
  controls: [util.runControls({ id: 'x', state: 'blocked' }), util.runControls({ id: 'x', state: 'executing' }),
    util.runControls({ id: 'x', state: 'awaiting_main' })].map(list => list.map(d => d.key))
};
part.stop();
out.removedOnStop = body.children.length === 1 && body.children[0] === notionNode && !listeners.keydown &&
  unsubscribed && win.__ntDeskLiveOwner === null;
process.stdout.write(JSON.stringify(out));
"""


def run_dom():
    result = subprocess.run([NODE, '-e', NODE_DOM, JS], capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.skipif(not os.path.exists(NODE), reason='node not installed')
def test_node_dashboard_behaviour():
    out = run_dom()
    assert out['appended'] and out['hiddenAtStart']
    assert out['openAfterKey']
    assert out['texts'] == [], out['texts']
    assert out['teamMembers'] == 2 and out['mainHidden']  # the Main is not a coworker
    assert out['problemCount'] == 2
    assert out['progressWidth'] == ['33%', '0%']
    assert out['foldedClosed']
    assert out['foldTexts'] == [], out['foldTexts']
    assert out['askFirst']  # every action asks first, nothing is called before OK
    assert out['calls'] == [['run_retry', {'id': 'r2'}]]
    assert out['confirmClosed']
    assert out['closingClassRemoved'] and not out['hiddenBeforeTimer'] and out['hiddenAfterTimer']
    util = out['util']
    assert util['status'] == ['Working', 'Done', 'Problem', 'Stopped', 'Waiting']
    assert util['checked'] == 'Checked'
    assert util['steps'] == ['Getting started', 'Step 2 of 3', 'The step is done', 'All 3 steps done']
    assert util['progressEmpty'] == {'done': 0, 'total': 0, 'ratio': 0, 'text': 'Getting started'}
    assert util['initials'] == ['C12', 'M', '?']
    assert util['conn'] == ['off', 'warn', 'on']
    assert util['span'] == ['5s', '2m', '1h 1m']
    assert util['controls'] == [['cancel'], ['pause', 'cancel'], []]
    assert out['removedOnStop']  # only our own nodes go; the Notion node stays


class LiveDashboardTests(unittest.TestCase):
    """unittest entry point (python -m unittest discover) for the pytest-style checks above."""

    def test_static_checks(self):
        for check in (test_registers_live_part, test_open_triggers_and_close, test_state_contract_fields,
                      test_throttle_limits_and_meter_levels, test_no_inner_html,
                      test_no_network_and_no_notion_node_removal, test_js_classes_prefixed,
                      test_css_selectors_prefixed, test_dashboard_structure, test_theme_tokens_with_fallbacks,
                      test_glass_and_animation, test_lf_utf8):
            with self.subTest(check=check.__name__):
                check()

    @unittest.skipUnless(os.path.exists(NODE), 'node not installed')
    def test_node(self):
        test_node_check()
        test_node_dashboard_behaviour()


if __name__ == '__main__':
    unittest.main()
