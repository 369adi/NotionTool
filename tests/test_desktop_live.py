"""Static and node-based checks for the desktop overlay Live dashboard (notiontool/web/desktop/live.js + live.css)."""
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
    js = read(JS)
    for needle in ('onState', 'getState', 'estimated_tokens', 'attention', 'events', 'used_per_min',
                   'limit_per_min', 'runs', 'workers', 'tasks', 'switches', 'last_tool_at', 'needs_main',
                   "purpose !== 'main'", 'task_id', 'last_output_at', 'generation', 'operator', 'goal'):
        assert needle in js, needle


def test_throttle_limits_and_meter_levels():
    js = read(JS)
    assert 'RENDER_MS = 500' in js
    assert 'EVENT_LIMIT = 50' in js and '.slice(-EVENT_LIMIT)' in js
    assert 'ratio < 0.7' in js and 'ratio < 0.9' in js
    assert "return '-'" in js  # missing fields render as '-'


def test_no_inner_html():
    js = read(JS)
    assert 'innerHTML' not in js
    assert 'outerHTML' not in js and 'insertAdjacentHTML' not in js


def test_no_network_and_no_notion_node_removal():
    js = read(JS)
    for needle in ('fetch(', 'XMLHttpRequest', 'WebSocket', 'EventSource', 'sendBeacon', '.remove()'):
        assert needle not in js, needle
    # the only removeChild calls are on the part's own panel and its own body children
    assert js.count('removeChild') == 2
    assert 'panel.parentNode.removeChild(panel)' in js


def test_js_classes_prefixed():
    js = read(JS)
    for value in re.findall(r"cls: '([^']*)'", js):
        for name in value.split():
            assert name.startswith('nt-desk-'), name


def test_css_selectors_prefixed():
    css = css_text()
    selectors = ''.join(re.findall(r'(?:^|})\s*([^{}@]+?)\s*{', css))
    classes = re.findall(r'\.([A-Za-z_-][\w-]*)', selectors)
    assert classes
    for name in classes:
        assert name.startswith('nt-desk-'), name
    assert 'prefers-color-scheme: dark' in css and 'var(--nt-desk-' in css


def test_dashboard_structure():
    js = read(JS)
    assert "text: 'Live'" in js
    for needle in ('nt-desk-conn-dot', 'nt-desk-tiles', 'nt-desk-tile-', 'nt-desk-worker-elapsed',
                   'nt-desk-progress', 'nt-desk-timeline', 'kindClass(ev.kind)', 'age(ev.ts, now)'):
        assert needle in js, needle
    for label in ("'Running'", "'Ready'", "'Recovering'", "'Blocked'", "'Coworkers'", "'Runs'",
                  "'Event timeline'", "'Needs attention'", "'Main switches'"):
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


# Minimal fake DOM: enough for live.js to build, open, render and close the panel.
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
Node.prototype.appendChild = function (k) { k.parentNode = this; this.children.push(k); return k; };
Node.prototype.removeChild = function (k) { this.children = this.children.filter(x => x !== k); k.parentNode = null; return k; };
Node.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
Node.prototype.addEventListener = function (t, f) { this.listeners[t] = f; };
Object.defineProperty(Node.prototype, 'firstChild', { get() { return this.children[0] || null; } });
Object.defineProperty(Node.prototype, 'offsetWidth', { get() { return 1; } });
function text(n) { return (n.textContent || '') + ' ' + n.children.map(text).join(' '); }
function find(n, cls, out) { out = out || []; if (n.className.split(' ').indexOf(cls) >= 0) out.push(n); n.children.forEach(k => find(k, cls, out)); return out; }
const body = new Node('body');
const notionNode = body.appendChild(new Node('div'));
let timers = [];
const listeners = {};
const win = {
  setTimeout(f) { timers.push(f); return timers.length; }, clearTimeout() {},
  addEventListener(t, f) { listeners[t] = f; }, removeEventListener(t) { delete listeners[t]; },
  history: { replaceState() {} }
};
const sandbox = { window: win, document: { body: body, documentElement: body,
  createElement: t => new Node(t), createTextNode: t => { const n = new Node('#text'); n.textContent = t; return n; } },
  location: { hash: '', pathname: '/', search: '' }, history: win.history, Date, Math, JSON, Object, Array, String, Number, isFinite };
vm.runInNewContext(code, sandbox);
const now = Date.now();
const stateObj = {
  rate: { used_per_min: 3, limit_per_min: 10 },
  runs: [{ id: 'r1', state: 'executing', goal: 'Ship it', needs_main: [{ reason: 'review' }] }, { id: 'r2', state: 'blocked', goal: 'Old' }],
  tasks: [{ id: 't1', run_id: 'r1', state: 'running', title: 'Build tiles', started_at: (now - 125000) / 1000, last_tool_at: now / 1000 },
          { id: 't2', run_id: 'r1', state: 'done', title: 'Plan' }],
  workers: [{ id: 'w1', label: 'Cowork 1', purpose: 'worker', state: 'busy', task_id: 't1', context: { estimated_tokens: 50, limit: 100 } },
            { id: 'w2', label: 'Cowork 2', purpose: 'worker', state: 'ready' },
            { id: 'm', label: 'Main', purpose: 'main', state: 'busy' }],
  attention: [{ kind: 'held', message: 'Check me', id: 'a1' }],
  switches: [{ id: 's1', state: 'pending' }],
  events: [{ ts: now / 1000 - 30, kind: 'notion_inference_error', message: 'boom' }, { ts: now / 1000, kind: 'task_done', message: 'ok' }]
};
const util = win.__ntDesk.liveUtil;
const part = win.__ntDesk.live({ getState: () => stateObj, onState: () => () => {} });
const panel = body.children[1];
const out = { appended: body.children.length === 2 && body.children[0] === notionNode, hiddenAtStart: panel.hidden };
listeners.keydown({ ctrlKey: true, shiftKey: true, altKey: false, key: 'L', preventDefault() {} });
out.openAfterKey = !panel.hidden && panel.classList.contains('nt-desk-live-open');
const t = text(panel);
out.texts = ['Live', 'Connected', 'Running', 'Ready', 'Recovering', 'Blocked', 'Cowork 1', 'Build tiles', 'Elapsed 2m',
  '1/2 tasks', 'Needs the Main: review', 'Check me', 's1', 'notion_inference_error', 'just now', '30s ago',
  'Requests per minute: 3 / 10 (30%)', 'Context: 50 / 100 (50%)'].filter(x => t.indexOf(x) < 0);
out.mainHidden = t.indexOf('Main ') >= 0 && find(panel, 'nt-desk-worker').length === 2;
out.tileValues = find(panel, 'nt-desk-tile-value').map(n => n.textContent);
out.eventKinds = find(panel, 'nt-desk-event').map(n => n.className);
out.progressWidth = find(panel, 'nt-desk-progress-fill').map(n => n.style.width);
listeners.keydown({ key: 'Escape', ctrlKey: false, shiftKey: false, preventDefault() {} });
out.closingClassRemoved = !panel.classList.contains('nt-desk-live-open');
out.hiddenBeforeTimer = panel.hidden;
timers.splice(0).forEach(f => f());
out.hiddenAfterTimer = panel.hidden;
find(panel, 'nt-desk-close');
out.util = {
  bucket: [util.bucket('busy'), util.bucket('ready'), util.bucket('recovering'), util.bucket('blocked'), util.bucket('weird')],
  progress: util.progress(stateObj.tasks, 'r1'),
  progressEmpty: util.progress([], 'x'),
  kinds: [util.kindClass('notion_inference_error'), util.kindClass('task_held'), util.kindClass('task_done'), util.kindClass('tool_call'), util.kindClass('misc')],
  elapsedNone: util.elapsed({}, {}, now),
  conn: [util.connection(null).cls, util.connection({ connected: false }).cls, util.connection({}).cls]
};
part.stop();
out.removedOnStop = body.children.length === 1 && body.children[0] === notionNode && !listeners.keydown;
process.stdout.write(JSON.stringify(out));
"""


def run_dom():
    result = subprocess.run([NODE, '-e', NODE_DOM, JS], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.skipif(not os.path.exists(NODE), reason='node not installed')
def test_node_dashboard_behaviour():
    out = run_dom()
    assert out['appended'] and out['hiddenAtStart']
    assert out['openAfterKey']
    assert out['texts'] == [], out['texts']
    assert out['mainHidden']
    assert out['tileValues'] == ['1', '1', '0', '0']
    assert 'nt-desk-kind-bad' in out['eventKinds'][0] and 'nt-desk-kind-ok' in out['eventKinds'][1]
    assert out['progressWidth'] == ['50%', '0%']
    assert out['closingClassRemoved'] and not out['hiddenBeforeTimer'] and out['hiddenAfterTimer']
    util = out['util']
    assert util['bucket'] == ['running', 'ready', 'recovering', 'blocked', None]
    assert util['progress'] == {'done': 1, 'total': 2, 'ratio': 0.5}
    assert util['progressEmpty'] == {'done': 0, 'total': 0, 'ratio': 0}
    assert util['kinds'] == ['nt-desk-kind-bad', 'nt-desk-kind-warn', 'nt-desk-kind-ok', 'nt-desk-kind-info', 'nt-desk-kind-muted']
    assert util['elapsedNone'] == '-'
    assert util['conn'] == ['nt-desk-dot-off', 'nt-desk-dot-bad', 'nt-desk-dot-ok']
    assert out['removedOnStop']


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
