"""Settings tab view (Studio v3): the save round trip of settings.js with a fake ctx.call."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

from notiontool import desktop_settings as ds
from notiontool import notion_shell
from notiontool.team_size import team_size, DEFAULT_PER_ACCOUNT

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / 'notiontool' / 'web' / 'desktop' / 'settings.js'
NODE = shutil.which('node') or (r'C:\\Program Files\\nodejs\\node.exe' if Path(r'C:\\Program Files\\nodejs\\node.exe').exists() else None)

# Fake ctx.call: mirrors the real routes. settings_set -> /api/settings (ints clamped like server.py,
# keys outside page_settings_keys refused like notion_shell.settings_set); app_settings_set only
# takes app-local keys and validates choices (desktop_shell.save_app_settings); settings_schema re-reads.
HARNESS = r"""
global.window = {};
global.document = {};
require(process.argv[2]);
var L = window.__ntDesk.settingsLogic;
var input = JSON.parse(process.argv[3]);
var state = input.state, entries = input.entries, serverKeys = input.server_keys;
var calls = [];
function schema() {
  return { harness: entries.map(function (e) { var c = Object.assign({}, e); c.value = (e.key in state) ? state[e.key] : e.default; return c; }),
    model_efforts: input.model_efforts || undefined };
}
function fakeCall(action, args) {
  calls.push([action, args]);
  if (input.fail) return Promise.reject(new Error(input.fail));
  if (action === 'settings_schema') return Promise.resolve(schema());
  var values = args.values || {};
  var keys = Object.keys(values);
  for (var i = 0; i < keys.length; i++) {
    var k = keys[i], e = entries.filter(function (x) { return x.key === k; })[0];
    if (action === 'settings_set' && serverKeys.indexOf(k) < 0) return Promise.reject(new Error('Setting not allowed: ' + k));
    if (action === 'app_settings_set' && !(e && e.app_local)) return Promise.reject(new Error('Unknown app setting: ' + k));
  }
  if (input.ignore) return Promise.resolve({ ok: true });
  keys.forEach(function (k) {
    var e = entries.filter(function (x) { return x.key === k; })[0];
    var v = values[k];
    if (e && e.type === 'int') v = Math.max(e.min, Math.min(input.clamp_max || e.max, v));
    state[k] = v;
  });
  return Promise.resolve({ ok: true });
}
var reports = [];
L.saveField(fakeCall, schema(), input.key, input.raw, function (kind, text) { reports.push([kind, text]); }).then(function (res) {
  var out = { ok: res.ok, error: res.error || null, reports: reports, calls: calls, state: state };
  if (input.key === 'app_effort_override') out.options = L.optionsOf(L.entriesOf(schema())[input.key], schema());
  out.team = L.teamSize(L.entriesOf(schema()));
  process.stdout.write(JSON.stringify(out));
});
"""


@unittest.skipUnless(NODE, 'node is not installed')
class SettingsTabRoundTrip(unittest.TestCase):
    def run_js(self, key, raw, state=None, **extra):
        entries = ds.settings_schema({})['harness']
        payload = dict(key=key, raw=raw, state=dict(state or {}), entries=entries,
                       server_keys=list(notion_shell.page_settings_keys()), **extra)
        harness = ROOT / 'tests' / '_settings_tab_harness.js'
        harness.write_text(HARNESS, encoding='utf-8')
        try:
            out = subprocess.run([NODE, str(harness), str(JS), json.dumps(payload)], capture_output=True, text=True,
                                 timeout=60, check=True)
        finally:
            harness.unlink()
        return json.loads(out.stdout)

    def test_coworkers_writes_per_account_and_shows_applied(self):
        res = self.run_js('per_account', '12', {'max_workers': 40, 'per_account': 10})
        self.assertTrue(res['ok'])
        self.assertEqual(res['calls'][0], ['settings_set', {'values': {'per_account': 12}}])
        self.assertEqual(res['calls'][1][0], 'settings_schema')
        self.assertEqual(res['reports'], [['saving', 'Saving...'], ['applied', 'Applied']])
        self.assertEqual(res['state']['per_account'], 12)
        self.assertEqual(res['team'], 12)
        self.assertEqual(team_size(res['state']), 12)  # the real team logic reads the same keys

    def test_coworkers_raises_a_lower_global_cap(self):
        res = self.run_js('per_account', 30, {'max_workers': 20, 'per_account': 10})
        self.assertEqual(res['calls'][0], ['settings_set', {'values': {'per_account': 30, 'max_workers': 30}}])
        self.assertTrue(res['ok'])
        self.assertEqual(team_size(res['state']), 30)

    def test_not_applied_when_the_server_keeps_another_value(self):
        res = self.run_js('coworker_max_age_hours', 48, {}, clamp_max=24)
        self.assertFalse(res['ok'])
        self.assertEqual(res['reports'][0], ['saving', 'Saving...'])
        self.assertEqual(res['reports'][-1], ['error', 'Not applied: the app kept 24.'])
        res = self.run_js('app_compact', True, {}, ignore=True)
        self.assertEqual(res['reports'][-1], ['error', 'Not applied: the app kept Off.'])

    def test_server_error_is_shown_inline(self):
        res = self.run_js('max_workers', 5, {}, fail='Server busy, try again')
        self.assertFalse(res['ok'])
        self.assertEqual(res['reports'], [['saving', 'Saving...'], ['error', 'Server busy, try again']])

    def test_invalid_values_never_call_the_server(self):
        for raw, msg in (('0', 'Use a number from 1 to 40.'), ('41', 'Use a number from 1 to 40.'),
                         ('abc', 'Enter a whole number.'), ('2.5', 'Enter a whole number.')):
            res = self.run_js('per_account', raw)
            self.assertEqual(res['calls'], [])
            self.assertEqual(res['reports'], [['error', msg]])
        res = self.run_js('app_theme_intensity', 'neon')
        self.assertEqual(res['calls'], [])

    def test_app_keys_use_the_app_route(self):
        for key, value in (('app_compact', True), ('app_theme_intensity', 'strong'), ('app_declutter', False),
                           ('app_notify_done', False), ('app_effort_override', 'high')):
            res = self.run_js(key, value)
            self.assertEqual(res['calls'][0], ['app_settings_set', {'values': {key: value}}], key)
            self.assertTrue(res['ok'], key)

    def test_effort_choices_follow_the_model_catalog(self):
        res = self.run_js('app_effort_override', 'medium', model_efforts=['low', 'high'])
        # Design g1: only Automatic, Medium, High; the catalog can only narrow that list.
        self.assertEqual(res['options'], ['auto', 'high'])
        self.assertEqual(res['calls'], [])
        res = self.run_js('app_effort_override', 'high', model_efforts=['low', 'high'])
        self.assertTrue(res['ok'])


class SettingsTabKeys(unittest.TestCase):
    def test_default_coworker_count_is_five_everywhere(self):
        # owner rule 2026-10-22: new accounts get at most 5 Coworkers by default
        self.assertEqual(DEFAULT_PER_ACCOUNT, 5)
        self.assertEqual(ds.DEFAULT_COWORKERS, 5)
        self.assertEqual(team_size({}), 5)
        entries = {e['key']: e for e in ds.settings_schema({})['harness']}
        self.assertEqual(entries['per_account']['value'], 5)
        # Rotation runs at 22 h on purpose (commit 49abeb2): 1 h safety margin before the hard 23 h limit (I32).
        self.assertEqual(entries['coworker_max_age_hours']['value'], 22)
        store = (ROOT / 'notiontool' / 'store.py').read_text(encoding='utf-8')
        self.assertIn('max_workers=40, per_account=5, coworker_max_age_hours=22', store)

    def test_team_keys_go_to_the_server_route(self):
        keys = notion_shell.page_settings_keys()
        for key in ('per_account', 'max_workers', 'coworker_max_age_hours'):
            self.assertIn(key, keys)
            self.assertFalse(ds.settings_schema({})['harness'][0]['app_local'])

    def test_new_keys_are_app_local(self):
        entries = {e['key']: e for e in ds.HARNESS_SETTINGS}
        for key in ('app_effort_override', 'app_theme_intensity', 'app_compact', 'app_declutter', 'app_notify_desktop',
                    'app_notify_done', 'app_notify_needs_you', 'app_notify_sound'):
            self.assertTrue(entries[key]['app_local'], key)
        self.assertEqual(entries['app_effort_override']['options'], ['auto', 'medium', 'high'])
        schema = ds.settings_schema({'model_efforts': ['high', 'low', 'bogus']})
        self.assertEqual(schema['model_efforts'], ['low', 'high'])
        self.assertNotIn('model_efforts', ds.settings_schema({}))

    def test_view_contract(self):
        js = JS.read_text(encoding='utf-8')
        for needle in ('window.__ntDesk.settingsTab', 'window.__ntShell', 'registerTab', "'nt-desk-shell-ready'",
                       "'Saving...'", "'Applied'", "call('settings_schema', {})", "'Team'", "'Models & effort'",
                       "'App look'", "'Notifications'", 'tab.shortcut()', "'data-nt-desk'"):
            self.assertIn(needle, js)
        self.assertNotIn('\\r\\n', js)
        self.assertNotIn('removeChild', js)
        self.assertNotIn('setInterval', js)
