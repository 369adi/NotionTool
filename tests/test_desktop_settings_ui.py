"""Static checks for the desktop overlay part 'settings' (notiontool/web/desktop/settings.js/.css)."""
import json
import os
import re
import subprocess
import unittest
from pathlib import Path

from notiontool import desktop_settings as ds

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / 'notiontool' / 'web' / 'desktop' / 'settings.js'
CSS = ROOT / 'notiontool' / 'web' / 'desktop' / 'settings.css'
NODE = r'C:\Program Files\nodejs\node.exe'
SECTION_IDS = ('general', 'team', 'app', 'advanced', 'mcp')

# Minimal DOM for running the MCP section of settings.js in node.
MCP_DOM = r'''
function El(tag) { this.tagName = tag; this.children = []; this.attrs = {}; this.listeners = {}; this._text = '';
  this.value = ''; this.checked = false; this.disabled = false; this.className = ''; this.style = {}; }
Object.defineProperty(El.prototype, 'textContent', {
  get: function () { return [this._text].concat(this.children.map(function (c) { return c.textContent; })).join(' '); },
  set: function (v) { this._text = String(v); this.children = []; } });
El.prototype.appendChild = function (c) { this.children.push(c); return c; };
El.prototype.setAttribute = function (k, v) { this.attrs[k] = v; };
El.prototype.addEventListener = function (t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); };
El.prototype.fire = function (t) { (this.listeners[t] || []).forEach(function (f) {
  f({ preventDefault: function () {}, stopPropagation: function () {} }); }); };
var document = { head: new El('head'), createElement: function (t) { return new El(t); },
  getElementById: function () { return null; } };
var confirms = 0, confirmAnswer = false;
var window = { __ntDesk: {}, confirm: function () { confirms += 1; return confirmAnswer; } };
var clip = [];
// Newer node versions have a read-only global navigator; replace it for the test.
Object.defineProperty(globalThis, 'navigator', { configurable: true, writable: true,
  value: { clipboard: { writeText: function (t) { clip.push(t); return Promise.resolve(); } } } });
'''

MCP_DRIVER = r'''
var calls = [], toasts = [], failAdd = true;
function fake(action, args) {
  calls.push([action, JSON.parse(JSON.stringify(args || {}))]);
  if (action === 'mcp_list') return Promise.resolve([
    { id: 'rea', name: 'REA', builtin: true, enabled: true, toggle: 'rea_enabled', tool_count: 3 },
    { id: 'ramp', name: 'Ramp', url: 'https://ramp.example/mcp', enabled: true, error: 'boom' }]);
  if (action === 'mcp_bridge') return Promise.resolve({ local_url: 'http://host:1/mcp', public_url: 'https://pub.example/mcp' });
  if (action === 'mcp_refresh') return Promise.resolve({ ok: true, servers: [{ label: 'REA', enabled: true, tool_count: 3 }] });
  if (action === 'mcp_add') {
    if (failAdd) { failAdd = false; return Promise.resolve({ ok: false, error: 'bad url' }); }
    return Promise.resolve({ ok: true });
  }
  if (action === 'host_key') return Promise.resolve({ masked: '\u2022\u2022\u2022\u2022abcd', has_key: true,
    rotated: !!args.rotate, copied: !!args.copy });
  return Promise.resolve({ ok: true });
}
function walk(n, out) { out.push(n); n.children.forEach(function (c) { walk(c, out); }); return out; }
var box = new El('div');
function by(cls) { return walk(box, []).filter(function (n) { return (' ' + n.className + ' ').indexOf(' ' + cls + ' ') >= 0; }); }
function attr(k, v) { return walk(box, []).filter(function (n) { return n.attrs[k] === v; })[0]; }
function button(t) { return walk(box, []).filter(function (n) { return n.tagName === 'button' && n._text === t; })[0]; }
function tick() { var p = Promise.resolve(); for (var i = 0; i < 20; i++) p = p.then(function () {
  return new Promise(function (r) { setImmediate(r); }); }); return p; }
window.__ntDesk.settingsMcp({ call: fake, toast: function (t, k) { toasts.push([t, k]); } }).mount(box);
(async function () {
  await tick();
  var rows = by('nt-desk-set-mcp-row');
  var out = { rows: rows.map(function (r) { return r.textContent; }),
    tones: rows.map(function (r) { return r.className.split(' ')[1]; }),
    summary: by('nt-desk-set-mcp-summary')[0]._text,
    local: attr('aria-label', 'Local MCP URL').value, public: attr('aria-label', 'Public MCP URL').value };
  var sw = attr('data-toggle', 'rea_enabled'); sw.checked = false; sw.fire('change'); await tick();
  button('\u21bb Refresh tools').fire('click'); await tick();
  out.status = by('nt-desk-set-mcp-status')[0]._text;
  attr('name', 'name').value = 'Ramp'; attr('name', 'mcp_url').value = 'https://ramp.example/mcp';
  attr('name', 'mcp_bearer_token').value = 'tok'; attr('name', 'write_auto').value = 'true';
  var form = by('nt-desk-set-mcp-form')[0];
  form.fire('submit'); await tick();
  form.fire('submit'); await tick();
  button('Show').fire('click'); await tick();
  out.key = attr('aria-label', 'API token (masked)').value;
  button('Copy token').fire('click'); await tick();
  confirmAnswer = false; button('Rotate token').fire('click'); await tick();
  confirmAnswer = true; button('Rotate token').fire('click'); await tick();
  button('Copy').fire('click'); await tick();
  button('Start remote access').fire('click'); await tick();
  out.calls = calls; out.toasts = toasts; out.confirms = confirms; out.clip = clip;
  console.log(JSON.stringify(out));
  process.exit(0);
})().catch(function (e) { console.error(e && e.stack || e); process.exit(1); });
'''


class DesktopSettingsUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js_bytes = JS.read_bytes()
        cls.css_bytes = CSS.read_bytes()
        cls.js = cls.js_bytes.decode('utf-8')
        cls.css = cls.css_bytes.decode('utf-8')

    def block(self, start, end='};'):
        i = self.js.index(start)
        return self.js[i:self.js.index(end, i)]

    def test_registers_part(self):
        self.assertIn('window.__ntDesk = window.__ntDesk || {}', self.js)
        self.assertIn('.settings =', self.js)
        self.assertIn('open: open', self.js)
        self.assertIn('stop:', self.js)
        self.assertTrue(self.js.lstrip().startswith('/*'))
        self.assertTrue(self.js.rstrip().endswith('})();'))

    def test_actions(self):
        self.assertIn("call('settings_schema', {})", self.js)
        self.assertIn("call('settings_set', { values: data.server })", self.js)
        self.assertIn("call('app_settings_set', { values: data.app })", self.js)
        self.assertIn("indexOf('app_') === 0", self.js)
        self.assertIn('app_local', self.js)
        self.assertIn("'Settings saved'", self.js)
        self.assertIn("'No changes to save'", self.js)
        self.assertIn("'error'", self.js)

    def test_dialog_entry_and_panel(self):
        for text in ('NotionTool', 'Einstellungen', "'settings'", "'einstellungen'", "'my account'", "'mein konto'",
                     '[role="dialog"]', 'nt-desk-set-nav', 'nt-desk-set-panel', 'nt-desk-set-modal', 'insertBefore',
                     'hidden_notion', 'label_de', 'nt-desk-hidden', "'Comma'", 'ctrlKey', 'shiftKey'):
            self.assertIn(text, self.js)
        for kind in ("'checkbox'", "'number'", "'select'", "'text'"):
            self.assertIn(kind, self.js)

    def test_never_hidden_sections(self):
        never = self.block('var NEVER_HIDE = [', '];')
        for label in ('account', 'security', 'billing', 'notion ai', 'connections', 'sicherheit', 'abrechnung',
                      'verbindungen'):
            self.assertIn("'%s'" % label, never)

    def test_sections_in_order(self):
        sections = self.block('var SECTIONS = [', '];')
        labels = re.findall(r"label: '([^']+)'", sections)
        self.assertEqual(labels, ['General', 'Mains & Coworkers', 'Desktop app', 'Advanced', 'MCP'])
        self.assertEqual(tuple(re.findall(r"id: '([^']+)'", sections)), SECTION_IDS)
        self.assertEqual(sections.count('desc:'), 5)

    def mcp_block(self):
        return self.block('// ---- MCP section', '// ---- end MCP section')

    def test_mcp_section_is_wired(self):
        mcp = self.mcp_block()
        for text in ("call('mcp_list', {})", "call('mcp_bridge', {})", "call('mcp_refresh', {})",
                     "call('mcp_add', body)", "call('host_key', { rotate: true })", "call('host_key', { rotate: false })",
                     "call('host_key', { rotate: false, copy: true })", "call('bridge_tunnel', {})",
                     "call('settings_set', { values: values })", 'window.confirm(MCP_ROTATE_CONFIRM)',
                     "'Rotate the token? Existing MCP clients lose access.'", "'Loading MCP servers...'",
                     "'No MCP servers configured.'", "'MCP servers could not be loaded: '", "'Add MCP server'",
                     "'\\u21bb Refresh tools'", "'Remote MCP access'", "'Local MCP URL'", "'Public MCP URL'",
                     "'Start remote access'", "'Rotate token'", "'Copy token'", "'Copy URL'", "'data-toggle'",
                     "name: 'mcp_url'", "name: 'mcp_bearer_token'", "name: 'write_auto'", "'error'", "'success'"):
            self.assertIn(text, mcp)
        # Settings window section and the Settings tab card both use the same part.
        self.assertIn("mcpSection({ call: call, toast: showToast })", self.js)
        self.assertIn("if (id === 'mcp' && mcpPart) mcpPart.load();", self.js)
        self.assertIn("card(view, 'mcp', 'Local MCP servers'", self.js)
        self.assertIn("call('notion_connections', {})", self.js)
        self.assertIn("call('notion_connection_permissions', values)", self.js)
        # The key itself is never rendered: only the masked form comes back.
        self.assertNotIn('api_key', mcp)

    @unittest.skipUnless(os.path.isfile(NODE), 'node not installed')
    def test_mcp_section_renders_and_acts(self):
        script = MCP_DOM + self.block('function pretty(option) {', '\n  }') + '\n  }\n' + self.mcp_block() + MCP_DRIVER
        res = subprocess.run([NODE, '-'], input=script, capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout.strip().splitlines()[-1])
        self.assertEqual(len(out['rows']), 2)
        self.assertIn('REA', out['rows'][0])
        self.assertIn('Built-in', out['rows'][0])
        self.assertIn('3 tools', out['rows'][0])
        self.assertIn('Enabled', out['rows'][0])
        self.assertIn('Ramp', out['rows'][1])
        self.assertIn('https://ramp.example/mcp', out['rows'][1])
        self.assertIn('boom', out['rows'][1])
        self.assertEqual(out['tones'], ['nt-desk-set-mcp-on', 'nt-desk-set-mcp-error'])
        self.assertEqual(out['summary'], '2 servers \u00b7 2 enabled \u00b7 1 error')
        self.assertEqual(out['local'], 'http://host:1/mcp')
        self.assertEqual(out['public'], 'https://pub.example/mcp')
        actions = [c[0] for c in out['calls']]
        for action in ('mcp_list', 'mcp_bridge', 'mcp_refresh', 'mcp_add', 'host_key', 'bridge_tunnel', 'settings_set'):
            self.assertIn(action, actions)
        add = [c[1] for c in out['calls'] if c[0] == 'mcp_add'][0]
        self.assertEqual(add, {'name': 'Ramp', 'url': 'https://ramp.example/mcp', 'token': 'tok', 'write_auto': 'true'})
        keys = [c[1] for c in out['calls'] if c[0] == 'host_key']
        # The first rotate click is cancelled in confirm(); only the second one rotates.
        self.assertEqual(out['confirms'], 2)
        self.assertEqual(sum(1 for k in keys if k.get('rotate')), 1)
        self.assertIn({'rotate': False, 'copy': True}, keys)
        self.assertEqual([c[1] for c in out['calls'] if c[0] == 'settings_set'], [{'values': {'rea_enabled': False}}])
        self.assertEqual(out['key'], '\u2022\u2022\u2022\u2022abcd')
        toasts = [t[0] for t in out['toasts']]
        for text in ('MCP tools refreshed', 'MCP server added', 'Token rotated', 'Remote access is starting',
                     'Token copied to the clipboard of this PC', 'Local URL copied'):
            self.assertIn(text, toasts)
        self.assertIn('error', [t[1] for t in out['toasts']])  # failing add shows an error toast
        self.assertEqual(out['status'], 'REA: 3 tools')

    def test_every_setting_has_a_section(self):
        mapping = dict(re.findall(r"(\w+): '(\w+)'", self.block('var SECTION_OF_KEY = {')))
        for section in mapping.values():
            self.assertIn(section, SECTION_IDS)
        for entry in ds.settings_schema({})['harness']:
            key = entry['key']
            if key.startswith('app_') or entry.get('app_local'):
                continue  # sectionOf() sends unmapped app-local keys to 'app'
            self.assertIn(key, mapping, key)
        self.assertIn("if (isAppLocal(entry)) return 'app';", self.js)
        self.assertIn("return 'advanced';", self.js)

    def test_modern_controls(self):
        for text in ("role: 'switch'", "role: 'radiogroup'", "role: 'radio'", "role: 'tablist'", "role: 'tab'",
                     "role: 'tabpanel'", 'SEGMENT_MAX = 4', 'nt-desk-set-help', 'nt-desk-set-toast',
                     "'aria-live': 'polite'", 'ArrowDown', 'ArrowUp', "'aria-modal': 'true'"):
            self.assertIn(text, self.js)
        # Segmented controls keep the raw option value; only the label is prettified.
        self.assertIn("'data-value': String(option)", self.js)
        self.assertIn('node.value = String(option);', self.js)

    def test_keys_close_and_save(self):
        self.assertIn("ev.key === 'Escape' && modal", self.js)
        self.assertIn('if (modal) closeModal(); else open();', self.js)
        self.assertIn("(ev.key === 's' || ev.key === 'S')", self.js)
        self.assertIn('panel.contains(ev.target)', self.js)
        self.assertIn('if (ev.target === modal) closeModal();', self.js)

    def test_failed_save_keeps_edits(self):
        i = self.js.index('function doSave()')
        body = self.js[i:self.js.index('function showInDialog()', i)]
        fail = body[body.index('function (err)'):]
        self.assertIn('setSaving(false)', fail)
        self.assertNotIn('refreshPanel()', fail)

    def test_observer_and_safety(self):
        self.assertEqual(self.js.count('new MutationObserver'), 1)
        self.assertIn('DEBOUNCE_MS = 250', self.js)
        self.assertNotIn('innerHTML', self.js)
        self.assertNotIn('outerHTML', self.js)
        self.assertNotIn('insertAdjacentHTML', self.js)
        self.assertNotIn('removeChild', self.js)
        for text in ('fetch(', 'XMLHttpRequest', 'WebSocket', 'setInterval', '127.0.0.1', 'Bearer', 'localhost'):
            self.assertNotIn(text, self.js)

    def test_css_only_own_classes(self):
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        classes = re.findall(r'\.(-?[A-Za-z_][\w-]*)', css)
        self.assertTrue(classes)
        # Only exception: Notion's own top bar is hidden while our settings are open (it is only read, never changed).
        for name in classes:
            # nt-shell-* is our own shell part (html.nt-shell-view-open), also allowed.
            # Settings hides our own v6 chat sidebar while its navigation is displayed.
            if name == 'notion-topbar' or name.startswith(('nt-shell-', 'nt-v6-')):
                continue
            self.assertTrue(name.startswith('nt-desk-'), name)
        self.assertIn('.nt-desk-set-panel', css)
        self.assertIn('.nt-desk-set-modal', css)
        self.assertNotIn('notion-', css.replace('.notion-topbar', ''))
        for name in re.findall(r"'(nt-[\w-]+)'", self.js):
            self.assertTrue(name.startswith('nt-desk-'), name)

    def test_css_theme_tokens(self):
        for text in ('var(--nt-bg, #0b0d12)', 'var(--nt-surface, #12151d)', 'var(--nt-raised, #181c26)',
                     'var(--nt-radius, 12px)', '#7c5cff', '#22d3ee', 'backdrop-filter: blur(',
                     '.nt-desk-set-switch', '.nt-desk-set-seg', '.nt-desk-set-toast-show', '.nt-desk-set-side',
                     'prefers-reduced-motion'):
            self.assertIn(text, self.css)
        # Every custom property use has a fallback.
        for use in re.findall(r'var\((--nt-[\w-]+)\s*([,)])', self.css):
            if not use[0].startswith('--nt-set-'):
                self.assertEqual(use[1], ',', use[0])

    def test_lf_utf8(self):
        for data in (self.js_bytes, self.css_bytes, Path(__file__).read_bytes()):
            self.assertNotIn(b'\r', data)
            self.assertFalse(data.startswith(b'\xef\xbb\xbf'))
            data.decode('utf-8')

    @unittest.skipUnless(os.path.isfile(NODE), 'node not installed')
    def test_node_check(self):
        res = subprocess.run([NODE, '--check', str(JS)], capture_output=True, text=True, timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)


if __name__ == '__main__':
    unittest.main()
