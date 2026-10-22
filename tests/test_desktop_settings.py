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
                self.assertEqual((entry['min'], entry['max']), SERVER_CLAMPS[entry['key']])
                self.assertTrue(entry['min'] <= entry['default'] <= entry['max'], entry['key'])

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
            known = set(SERVER_CLAMPS) | SERVER_BOOLS | set(SERVER_CHOICES)
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

    def test_files_use_lf(self):
        for rel in ('notiontool/desktop_settings.py', 'tests/test_desktop_settings.py'):
            self.assertNotIn(b'\r', (ROOT / rel).read_bytes(), rel)


if __name__ == '__main__':
    unittest.main()
