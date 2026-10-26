"""Desktop app settings schema (notiontool/desktop_settings.py)."""
import re
import unittest
from pathlib import Path

from notiontool import desktop_settings as ds

ROOT = Path(__file__).resolve().parent.parent

# The bounds the /api/settings route in server.py clamps to.
SERVER_CLAMPS = {'max_workers': (1, 40), 'per_account': (1, 40), 'coworker_max_age_hours': (1, 72), 'max_runs_per_chat': (1, 6),
                 'context_token_limit': (8000, 10000000), 'main_context_ask_tokens': (50000, 10000000),
                 'main_context_ask_turns': (20, 5000),
                 'token_budget_6h': (1000000, 500000000), 'burst_soft_15m': (100000, 100000000),
                 'burst_hard_15m': (100000, 200000000), 'burst_hard_60m': (100000, 500000000),
                 'burst_turns_5m': (5, 1000), 'burst_spacing_s': (1, 600)}
# The measured Coworker switch rule (SWITCH_SETTINGS) and its /api/settings clamps.
SWITCH_CLAMPS = {'switch_min_tokens': (0, 10000000), 'switch_base_tokens': (0, 1000000), 'switch_cost_s': (0, 3600),
                 'switch_slope_ms_per_100k': (0, 600000), 'switch_error_permille_per_100k': (0, 1000),
                 'switch_retry_s': (0, 3600), 'switch_remaining_tasks': (1, 50)}
SERVER_BOOLS = {'coworker_delete_old_chat', 'main_auto_continue', 'turn_watch', 'auto_continue_on_done'}
SERVER_CHOICES = {'coworker_fresh_context': ['threshold', 'every_task'], 'main_context_policy': ['hint', 'auto', 'ask']}


class DesktopSettingsTests(unittest.TestCase):
    def by_key(self, schema):
        return {entry['key']: entry for entry in schema['harness']}

    def test_values_come_from_the_sample(self):
        sample = dict(max_workers=12, per_account=3, main_context_policy='ask', turn_watch=False,
                      app_chat_filter='hide_coworkers', app_hide_inbox=True)
        entries = self.by_key(ds.settings_schema(sample))
        for key, value in sample.items():
            self.assertEqual(entries[key]['value'], value)

    def test_defaults_when_missing(self):
        entries = self.by_key(ds.settings_schema({}))
        self.assertEqual(entries['max_workers']['value'], 40)
        self.assertEqual(entries['per_account']['value'], 5)
        self.assertEqual(entries['max_runs_per_chat']['value'], 3)
        self.assertEqual(entries['main_context_policy']['value'], 'hint')
        self.assertIs(entries['main_auto_continue']['value'], True)
        self.assertEqual(entries['app_chat_filter']['value'], 'mains_only')
        self.assertIs(entries['app_hide_home']['value'], True)
        self.assertEqual(ds.settings_schema(None)['harness'][0]['value'], 40)

    def test_ints_have_server_bounds(self):
        for entry in ds.HARNESS_SETTINGS:
            if entry['type'] == 'int':
                self.assertLessEqual(entry['min'], entry['max'])
                if not entry['app_local']:  # app-local ints are clamped by the app, not by /api/settings
                    self.assertEqual((entry['min'], entry['max']), SERVER_CLAMPS[entry['key']])
                self.assertTrue(entry['min'] <= entry['default'] <= entry['max'], entry['key'])

    def test_models_and_roles_defaults_never_use_auto(self):
        entries = self.by_key(ds.settings_schema({}))
        self.assertNotIn('auto', ds.MODEL_OPTIONS)
        self.assertEqual(entries['app_default_model']['value'], 'opus-5.5')
        self.assertIs(entries['app_never_use_auto']['value'], True)
        for key in ('app_role_researcher', 'app_role_architect', 'app_role_quality_planner'):
            self.assertEqual(entries[key]['value'], 'opus-5.5', key)
        self.assertEqual(entries['app_role_worker']['value'], 'sonnet-5.5')
        self.assertEqual(entries['app_direct_model']['value'], 'sonnet-5.5')
        for key in ('app_default_model', 'app_direct_model') + ds.ROLE_MODEL_KEYS:
            self.assertTrue(entries[key]['app_local'], key)
            self.assertEqual(entries[key]['options'], list(ds.MODEL_OPTIONS), key)

    def test_limits_defaults(self):
        entries = self.by_key(ds.settings_schema({}))
        self.assertEqual(entries['app_send_block_hours']['value'], 23)
        self.assertEqual(entries['app_steps_per_turn']['value'], 450)
        self.assertIs(entries['app_auto_switch_context']['value'], True)
        self.assertEqual(entries['app_auto_switch_percent']['value'], 80)
        self.assertEqual(entries['app_load_last_messages']['value'], 5)
        self.assertIs(entries['app_restart_safety']['value'], True)
        # Server keys keep their owner-set defaults (22 h rotation, 5 coworkers); see the test in the tab tests.
        self.assertLessEqual(entries['coworker_max_age_hours']['value'], entries['app_send_block_hours']['value'])

    def test_choices_have_options_and_ask(self):
        for entry in ds.HARNESS_SETTINGS:
            if entry['type'] == 'choice':
                self.assertTrue(entry['options'])
                self.assertIn(entry['default'], entry['options'])
        policy = next(e for e in ds.HARNESS_SETTINGS if e['key'] == 'main_context_policy')
        self.assertIn('ask', policy['options'])

    def test_only_server_or_app_local_keys(self):
        for entry in ds.HARNESS_SETTINGS:
            self.assertIn(entry['type'], ('bool', 'int', 'choice', 'text'))
            self.assertIn(entry['group'], ('Coworkers', 'Main chat', 'App'))
            if entry['key'].startswith('app_'):
                self.assertTrue(entry['app_local'])
                continue
            self.assertFalse(entry['app_local'])
            known = set(SERVER_CLAMPS) | SERVER_BOOLS | set(SERVER_CHOICES) | {'mcp_full_access', 'mcp_browser_access'}
            self.assertIn(entry['key'], known)
            if entry['key'] in SERVER_CHOICES:
                self.assertEqual(entry['options'], SERVER_CHOICES[entry['key']])

    def test_secret_keys_never_appear(self):
        current = {'notion_token': 'x', 'api_secret': 'y', 'password': 'z', 'key_file': 'k',
                   'bearer': 'b', 'host_api_token': 't', 'max_workers': 5}
        schema = ds.settings_schema(current)
        text = repr(schema)
        for value in ('notion_token', 'api_secret', 'password', 'key_file', 'host_api_token'):
            self.assertNotIn(value, text)
        self.assertTrue(ds.is_secret_key('access_token'))
        self.assertTrue(ds.is_secret_key('Bearer'))
        self.assertFalse(ds.is_secret_key('main_context_ask_tokens'))
        self.assertFalse(ds.is_secret_key('context_token_limit'))

    def test_hidden_notion_sections_never_include_protected(self):
        protected = re.compile(r'account|security|billing|notion ai|connection|konto|sicherheit|abrechnung|verbindung', re.I)
        self.assertTrue(ds.HIDDEN_NOTION_SETTINGS)
        for section in ds.HIDDEN_NOTION_SETTINGS:
            self.assertTrue(section['label'] and section['label_de'])
            for value in section.values():
                self.assertIsNone(protected.search(value), value)
        self.assertEqual(ds.settings_schema({})['hidden_notion'], ds.HIDDEN_NOTION_SETTINGS)

    def test_effort_control_offers_only_auto_medium_high(self):
        # Design g1 / owner rule 2026-10-22: a 3-option control, Coworkers run on medium or high only.
        entry = self.by_key(ds.settings_schema({}))['app_effort_override']
        self.assertEqual(entry['options'], ['auto', 'medium', 'high'])
        self.assertEqual(entry['default'], 'auto')
        self.assertEqual(ds.STALE_EFFORTS, ('low', 'xhigh', 'max'))
        self.assertIn('medium for light work and high otherwise', entry['help'])
        self.assertIn('Main keeps its own pick', entry['help'])
        for stale in ds.STALE_EFFORTS:
            got = self.by_key(ds.settings_schema({'app_effort_override': stale}))['app_effort_override']
            self.assertEqual(got['value'], stale)  # kept as saved, so the UI can show the warning
            self.assertNotIn(stale, got['options'])

    def test_settings_js_effort_control_and_warning(self):
        js = (ROOT / 'notiontool' / 'web' / 'desktop' / 'settings.js').read_text(encoding='utf-8')
        self.assertIn("var EFFORT_OPTIONS = ['auto', 'medium', 'high'];", js)
        self.assertIn("var STALE_EFFORTS = ['low', 'xhigh', 'max'];", js)
        self.assertIn('coworkers use medium for light work and high otherwise. The Main keeps its own pick.', js)
        self.assertIn('nt-desk-stab-row-warn', js)
        self.assertIn('nt-desk-set-row-warn', js)
        self.assertNotIn('use light effort', js)
        self.assertNotIn('highest level the model supports', js)
        css = (ROOT / 'notiontool' / 'web' / 'desktop' / 'settings.css').read_text(encoding='utf-8')
        self.assertIn('#F5B5441A', css)
        self.assertIn('.nt-desk-stab-seg-btn', css)

    def test_switch_rule_entries_match_the_server(self):
        switch = {entry['key']: entry for entry in ds.settings_schema({})['switch']}
        self.assertEqual(set(switch), set(SWITCH_CLAMPS) | {'coworker_switch_policy'})
        self.assertEqual(switch['coworker_switch_policy']['options'], ['threshold', 'measured'])
        for key, bounds in SWITCH_CLAMPS.items():
            entry = switch[key]
            self.assertEqual(entry['type'], 'int')
            self.assertFalse(entry['app_local'])
            self.assertEqual((entry['min'], entry['max']), bounds, key)
            self.assertTrue(entry['min'] <= entry['default'] <= entry['max'], key)
        server = (ROOT / 'notiontool/server.py').read_text(encoding='utf-8')
        for key, (low, high) in SWITCH_CLAMPS.items():
            self.assertIn("('%s',%d,%d)" % (key, low, high), server)

    def test_switch_rule_card_in_team_section(self):
        js = (ROOT / 'notiontool/web/desktop/settings.js').read_text(encoding='utf-8')
        block = js[js.index('var SECTION_OF_KEY = {'):]
        mapping = dict(re.findall(r"(\w+): '(\w+)'", block[:block.index('};')]))
        for entry in ds.SWITCH_SETTINGS:
            self.assertEqual(mapping.get(entry['key']), 'team', entry['key'])
        self.assertIn("var SWITCH_CARD = { section: 'team', title: 'Context switch rule' };", js)
        self.assertIn('list(schema && schema.switch)', js)
        # Spec rows: no 'Allowed: x to y' line for the switch card.
        self.assertIn('rowFor(entry, section, true)', js)
        self.assertIn('if (!noRange && kind ===', js)
        # Saved through the existing save: server keys go into collect() server values -> settings_set.
        self.assertIn("if (isAppLocal(entry)) app[key] = value; else server[key] = value;", js)
        self.assertIn("call('settings_set', { values: data.server })", js)

    def switch_css(self):
        return (ROOT / 'notiontool/web/desktop/settings.css').read_text(encoding='utf-8')

    def test_switch_card_wraps_on_narrow_width(self):
        css = self.switch_css()
        start = css.index('@media (max-width: 720px) {\n')
        block = css[start:css.index('\n}', start)]
        self.assertIn('.nt-desk-set-card-spec .nt-desk-set-row { flex-wrap: wrap; gap: 10px; }', block)
        self.assertIn('.nt-desk-set-card-spec .nt-desk-set-control { flex: 1 1 100%; width: 100%; margin-top: 8px; '
                      'justify-content: flex-start; }', block)

    def test_switch_card_compact_mode(self):
        css = self.switch_css()
        compact = [line for line in css.splitlines() if line.startswith('html.nt-desk-compact ')]
        self.assertIn('html.nt-desk-compact .nt-desk-set-card-spec .nt-desk-set-row { min-height: 48px; padding: 8px 20px; }',
                      compact)
        # Cards 16 px apart in compact mode: the group header above the switch card moves up to 16 px.
        self.assertIn('html.nt-desk-compact .nt-desk-set-group { margin-top: 16px; }', compact)

    def test_files_use_lf(self):
        for rel in ('notiontool/desktop_settings.py', 'tests/test_desktop_settings.py'):
            self.assertNotIn(b'\r', (ROOT / rel).read_bytes(), rel)


if __name__ == '__main__':
    unittest.main()
