"""Coworker chat age in the desktop overlay: 'Created 19 Oct 12:04 - 16h old', amber >= 20h, red >= 23h."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'notiontool' / 'web' / 'desktop'
JS_PATH = ASSETS / 'overlay.js'
CSS_PATH = ASSETS / 'overlay.css'
NODE = 'C:/Program Files/nodejs/node.exe'

# A tiny fake DOM: enough for the overlay's el()/setText()/setCls()/dropOwn() helpers.
FAKE_DOM = r"""
function El(tag) { this.tagName = String(tag).toUpperCase(); this.attrs = {}; this.children = []; this.parentNode = null; this._text = ''; this.style = {}; }
El.prototype.getAttribute = function (k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; };
El.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
El.prototype.removeAttribute = function (k) { delete this.attrs[k]; };
Object.defineProperty(El.prototype, 'className', { get: function () { return this.attrs['class'] || ''; }, set: function (v) { this.attrs['class'] = String(v); } });
Object.defineProperty(El.prototype, 'textContent', {
  get: function () { return this._text + this.children.map(function (c) { return c.textContent; }).join(''); },
  set: function (v) { this._text = String(v); this.children = []; } });
El.prototype.appendChild = function (c) { if (c.parentNode) c.parentNode.removeChild(c); c.parentNode = this; this.children.push(c); return c; };
El.prototype.removeChild = function (c) { var i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); c.parentNode = null; return c; };
var window = { __ntDesk: {} };
var document = { createElement: function (t) { return new El(t); }, querySelector: function () { return null; }, querySelectorAll: function () { return []; } };
var location = { href: '' };
"""


class DesktopAgeLabelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = JS_PATH.read_text(encoding='utf-8')
        cls.css = CSS_PATH.read_text(encoding='utf-8')

    def section(self, start, end):
        a = self.js.index(start)
        return self.js[a:self.js.index(end, a)]

    def run_node(self, body):
        if not os.path.exists(NODE):
            self.skipTest('node is not installed at ' + NODE)
        source = '\n'.join([
            FAKE_DOM, self.js,
            # Closure helpers of the sidebar part (own nodes only), evaluated next to the module.
            'var ctx = {};',
            'var AGE_AMBER_H = 20, AGE_RED_H = 23;',
            self.section('function isOwn(node)', '// ---- Sidebar v2: pure helpers'),
            self.section('function el(tag, props, kids)', 'function sidebarRoot'),
            self.section('function setText(node, text)', 'function coworkersCollapsed'),
            'var U = window.__ntDesk.sidebarUtil;',
            'var T0 = Date.UTC(2026, 9, 19, 12, 4) / 1000;',
            body])
        with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as handle:
            handle.write(source)
        try:
            done = subprocess.run([NODE, handle.name], capture_output=True, text=True, timeout=40,
                                  env=dict(os.environ, TZ='UTC'))
        finally:
            os.unlink(handle.name)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        return json.loads(done.stdout)

    def test_created_text_and_levels(self):
        out = self.run_node(r"""
        function w(hours, extra) { var o = { purpose: 'worker', thread_id: 'aa-bb', created_at: T0 }; for (var k in extra || {}) o[k] = extra[k]; return [o, T0 + hours * 3600]; }
        function badge(hours, extra) { var a = w(hours, extra); return U.ageBadge(a[0], a[1]); }
        console.log(JSON.stringify({
          text: U.createdText(T0, T0 + 16 * 3600 + 120),
          ms: U.createdText(T0 * 1000, T0 + 16 * 3600),
          iso: U.createdText('2026-10-19T12:04:00Z', T0 + 3600),
          unknown: U.createdText(null, T0), future: U.createdText(T0 + 60, T0),
          b16: badge(16), b19: badge(19.9), b20: badge(20), b23: badge(23),
          b23busy: badge(23.5, { task_title: 'Fix login' }),
          b23state: badge(23.5, { state: 'running' }),
          b21busy: badge(21, { task_title: 'Fix login' }),
          none: U.ageBadge({ purpose: 'worker' }, T0),
          fromAge: U.ageBadge({ purpose: 'worker', created_at: null, age_s: 7200 }, T0 + 7200)
        }));""")
        self.assertEqual(out['text'], 'Created 19 Oct 12:04 - 16h old')
        self.assertEqual(out['ms'], 'Created 19 Oct 12:04 - 16h old')  # created_at in ms is accepted too
        self.assertEqual(out['iso'], 'Created 19 Oct 12:04 - 1h old')
        self.assertEqual(out['unknown'], '')
        self.assertEqual(out['future'], '')
        self.assertEqual((out['b16']['level'], out['b16']['text']), ('ok', '16h'))
        self.assertEqual(out['b16']['title'], 'Created 19 Oct 12:04 - 16h old')
        self.assertEqual(out['b19']['level'], 'ok')
        self.assertEqual(out['b20']['level'], 'amber')
        self.assertEqual(out['b23']['level'], 'red')
        self.assertFalse(out['b23']['rotating'])  # idle: rotation takes it right away, no 'soon' label
        self.assertEqual(out['b23']['text'], '23h')
        self.assertIn('a fresh chat takes over soon', out['b23']['title'])
        for key in ('b23busy', 'b23state'):
            self.assertTrue(out[key]['rotating'], key)
            self.assertEqual(out[key]['level'], 'red')
            self.assertEqual(out[key]['text'], '23h - rotating soon')
            self.assertTrue(out[key]['title'].startswith('Created 19 Oct 12:04 - 23h old - rotating soon'))
        self.assertFalse(out['b21busy']['rotating'])  # amber and busy: not yet
        self.assertIsNone(out['none'])
        self.assertEqual(out['fromAge']['text'], '2h')  # age_s is used when created_at is missing

    def test_only_coworkers_get_ages(self):
        out = self.run_node(r"""
        var state = { ts: T0 + 16 * 3600, mains: [{ thread_id: 'main-1' }], workers: [
          { id: 'w1', purpose: 'worker', thread_id: 'AB-CD', created_at: T0, label: 'Coworker 1' },
          { id: 'w2', purpose: 'worker', thread_id: 'EF', created_at: null },
          { id: 'm1', purpose: 'main', thread_id: 'main-1', created_at: T0 },
          { id: 'r1', purpose: 'retired_worker', thread_id: 'old-1', created_at: T0 - 86400 }] };
        var rows = U.coworkerRows(state);
        console.log(JSON.stringify({ ages: U.coworkerAges(state), rows: rows, none: U.coworkerAges(null) }));""")
        self.assertEqual(sorted(out['ages']), ['abcd'])  # dash-free thread key, Mains and retired chats left out
        self.assertEqual(out['ages']['abcd']['text'], '16h')
        self.assertEqual(out['none'], {})
        row = [r for r in out['rows'] if r['id'] == 'w1'][0]
        self.assertEqual(row['created'], 'Created 19 Oct 12:04 - 16h old')
        self.assertEqual(row['age'], '16h')
        self.assertEqual(row['ageCls'], 'ok')
        self.assertFalse(row['rotating'])
        unknown = [r for r in out['rows'] if r['id'] == 'w2'][0]
        self.assertEqual((unknown['created'], unknown['age']), ('', ''))

    def test_row_badge_added_updated_and_removed(self):
        out = self.run_node(r"""
        var row = new El('div'); row.setAttribute('role', 'menuitem');
        var title = new El('div'); title.textContent = 'Welcome'; row.appendChild(title);
        var info = U.ageBadge({ purpose: 'worker', created_at: T0 }, T0 + 16 * 3600);
        styleAge(row, info); styleAge(row, info);
        var first = row.children.slice();
        var badge = first[1];
        var firstState = { cls: badge.className, text: badge.textContent, aria: badge.getAttribute('aria-label') };
        var red = U.ageBadge({ purpose: 'worker', created_at: T0, task_title: 'x' }, T0 + 23.2 * 3600);
        styleAge(row, red);
        var after = { count: row.children.length, cls: badge.className, text: badge.textContent, title: badge.getAttribute('title') };
        styleAge(row, null);
        console.log(JSON.stringify({
          count: first.length, firstIsNotion: first[0] === title, mark: badge.getAttribute('data-nt-desk'),
          own: badge.getAttribute('data-nt-desk-own'), cls: firstState.cls, text: firstState.text,
          aria: firstState.aria, after: after,
          removed: row.children.length, kept: row.children[0] === title, titleText: title.textContent,
          rowAttrs: Object.keys(row.attrs).sort()
        }));""")
        self.assertEqual(out['count'], 2)  # idempotent: one badge after two runs
        self.assertTrue(out['firstIsNotion'])
        self.assertEqual(out['mark'], 'age')
        self.assertEqual(out['own'], '1')
        self.assertIn('nt-desk-age-ok', out['cls'])
        self.assertEqual(out['text'], '16h')
        self.assertEqual(out['aria'], 'Created 19 Oct 12:04 - 16h old')
        self.assertEqual(out['after']['count'], 2)
        self.assertIn('nt-desk-age-red', out['after']['cls'])
        self.assertIn('nt-desk-age-rotating', out['after']['cls'])
        self.assertEqual(out['after']['text'], '23h - rotating soon')
        self.assertEqual(out['removed'], 1)  # Main row / no age: only our badge goes
        self.assertTrue(out['kept'])
        self.assertEqual(out['titleText'], 'Welcome')
        self.assertEqual(out['rowAttrs'], ['role'])  # styleAge never marks the Notion row itself

    def test_wiring_main_rows_and_css(self):
        rows = self.section('function applyRows(state, root)', '// ---- Sidebar v2: Main cards')
        self.assertIn('var ages = coworkerAges(state);', rows)
        self.assertIn('styleAge(row, main ? null : (ages[key] || null));', rows)  # Main rows never get a badge
        style = self.section('function styleAge(row, info)', 'function coworkersCollapsed')
        self.assertIn('dropOwn(badge)', style)
        self.assertNotIn('removeChild', style)
        self.assertNotIn('.remove()', style)
        coworker = self.section('function coworkerRow(row)', 'function applyCoworkers')
        self.assertIn("'nt-desk-coworker-created'", coworker)
        self.assertIn('row.ageTitle', coworker)
        for rule in ('.nt-desk-age-amber', '.nt-desk-age-red', '.nt-desk-age-rotating', '.nt-desk-coworker-created',
                     '.nt-desk-row-age'):
            self.assertIn(rule + ' {', self.css)
        for text in ("'Created '", "' old'", "' - rotating soon'"):
            self.assertIn(text, self.js)

    def test_files_lf_and_node_check(self):
        for path in (JS_PATH, CSS_PATH, Path(__file__)):
            self.assertNotIn(b'\r', path.read_bytes(), path.name)
        if not os.path.exists(NODE):
            self.skipTest('node is not installed at ' + NODE)
        done = subprocess.run([NODE, '--check', str(JS_PATH)], capture_output=True, text=True, timeout=40)
        self.assertEqual(done.returncode, 0, done.stderr)


if __name__ == '__main__':
    unittest.main()
