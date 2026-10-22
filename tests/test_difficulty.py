import inspect
import socket
import unittest
from unittest import mock

from notiontool import difficulty
from notiontool.difficulty import TEAM_THRESHOLD, classify

LONG_RENAME = (
    'Hi, quick one before the weekend. In notiontool/engine.py the local variable tmp_val '
    'inside the function step() is badly named, please rename it to pending_value. '
    'Nothing else should change: keep the behaviour exactly the same, keep the existing '
    'tests as they are, and do not touch any other file. I know this message is long, '
    'but I wanted to be precise about it so there are no surprises. The variable is only '
    'used inside that one function, so a careful search and replace inside step() is enough. '
    'Please double check that you did not rename anything with a similar name elsewhere in '
    'that file, and tell me when you are finished. Thanks a lot for your help with this.'
)
FILLER = ' Thanks a lot for your help with this, I appreciate it.' * 40


class ShortButHard(unittest.TestCase):
    def test_ui_neu(self):
        self.assertEqual(classify('UI neu')['route'], 'team')

    def test_mach_mir_die_ui_neu(self):
        self.assertEqual(classify('mach mir die UI neu')['route'], 'team')

    def test_mach_die_app_schoener(self):
        self.assertEqual(classify('mach die app schöner')['route'], 'team')

    def test_redesign_settings_page(self):
        self.assertEqual(classify('redesign the settings page')['route'], 'team')

    def test_sieht_beschissen_aus(self):
        self.assertEqual(classify('die ganze App sieht beschissen aus')['route'], 'team')

    def test_security_and_migration(self):
        self.assertEqual(classify('security review of the whole system')['route'], 'team')
        self.assertEqual(classify('migrate the storage to sqlite')['route'], 'team')

    def test_many_files(self):
        result = classify('update a.py, b.py and c.py to use the logger')
        self.assertEqual(result['route'], 'team')
        self.assertTrue(any('many files' in r for r in result['reasons']))


class NarrowIsDirect(unittest.TestCase):
    def test_fix_typo_in_readme(self):
        self.assertEqual(classify('fix typo in README')['route'], 'direct')

    def test_long_but_easy_rename(self):
        self.assertGreater(len(LONG_RENAME), 600)
        result = classify(LONG_RENAME)
        self.assertEqual(result['route'], 'direct', result)

    def test_single_bug_with_error_message(self):
        text = "fix the KeyError: 'name' in notiontool/server.py line 120"
        self.assertEqual(classify(text)['route'], 'direct')

    def test_add_one_test(self):
        self.assertEqual(classify('add one test for parse_duration in tests/test_dur.py')['route'], 'direct')

    def test_bump_version(self):
        self.assertEqual(classify('bump version to 1.4.0')['route'], 'direct')

    def test_german_narrow(self):
        self.assertEqual(classify('Tippfehler in README korrigieren')['route'], 'direct')
        self.assertEqual(classify('benenne in engine.py die Variable x in count um')['route'], 'direct')


class ReviewFixes(unittest.TestCase):
    def _no_scope(self, result):
        return not any(r.startswith('scope:') for r in result['reasons'])

    def test_restart_neu_is_not_scope(self):
        for text in ('starte NotionTool neu', 'lade die Seite neu', 'installiere das Paket neu',
                     'verbinde den Server neu', 'melde dich neu an', 'bitte neu starten'):
            result = classify(text)
            self.assertEqual(result['route'], 'direct', (text, result))
            self.assertTrue(self._no_scope(result), (text, result))

    def test_real_neu_still_scope(self):
        self.assertEqual(classify('mach mir die UI neu')['route'], 'team')
        self.assertEqual(classify('die Einstellungsseite neu machen')['route'], 'team')

    def test_framework_name_is_not_a_file(self):
        result = classify('migrate the frontend to Next.js')
        self.assertEqual(result['route'], 'team', result)
        self.assertFalse(any('concrete target file' in r for r in result['reasons']), result)
        for name in ('Node.js', 'Vue.js', 'Three.js', 'Express.js', 'Chart.js'):
            reasons = classify('use %s here' % name)['reasons']
            self.assertFalse(any('file' in r for r in reasons if 'no named' not in r), (name, reasons))

    def test_real_js_file_still_counts(self):
        result = classify('fix the label in web/app.js')
        self.assertTrue(any('concrete target file: web/app.js' in r for r in result['reasons']), result)

    def test_one_file_does_not_cancel_big_change(self):
        self.assertEqual(classify('redesign settings.js')['route'], 'team')
        self.assertEqual(classify('security review of server.py')['route'], 'team')
        text = "fix the KeyError: 'name' in notiontool/server.py line 120"
        self.assertEqual(classify(text)['route'], 'direct')
        self.assertEqual(classify('add one test for parse_duration in tests/test_dur.py')['route'], 'direct')
        self.assertEqual(classify('benenne in engine.py die Variable x in count um')['route'], 'direct')

    def test_add_one_test_needs_verb_and_no_framework(self):
        result = classify('build a test framework for the whole app')
        self.assertEqual(result['route'], 'team', result)
        self.assertFalse(any('add one test' in r for r in result['reasons']), result)
        for text in ('add a test suite for everything', 'write a test plan', 'add a test-harness'):
            reasons = classify(text)['reasons']
            self.assertFalse(any('add one test' in r for r in reasons), (text, reasons))
        for text in ('add one test for parse_duration in tests/test_dur.py',
                     'schreibe einen Test für parse_duration', 'ergänze einen Testfall in tests/test_dur.py'):
            reasons = classify(text)['reasons']
            self.assertTrue(any('add one test' in r for r in reasons), (text, reasons))
        self.assertEqual(classify('add one test for parse_duration in tests/test_dur.py')['route'], 'direct')


class LengthIsNotASignal(unittest.TestCase):
    def test_padding_does_not_change_easy(self):
        a, b = classify('fix typo in README'), classify('fix typo in README.' + FILLER)
        self.assertEqual((a['route'], a['score']), (b['route'], b['score']))

    def test_padding_does_not_change_hard(self):
        a, b = classify('UI neu'), classify('UI neu.' + FILLER)
        self.assertEqual((a['route'], a['score']), (b['route'], b['score']))


class Contract(unittest.TestCase):
    def test_shape_and_range(self):
        for text in ('', 'UI neu', 'fix typo in README', LONG_RENAME, 'redesign ' * 30):
            result = classify(text)
            self.assertEqual(set(result), {'route', 'score', 'reasons'})
            self.assertIn(result['route'], ('team', 'direct'))
            self.assertIsInstance(result['score'], float)
            self.assertTrue(0.0 <= result['score'] <= 1.0)
            self.assertTrue(result['reasons'] and all(isinstance(r, str) for r in result['reasons']))
            self.assertEqual(result['route'] == 'team', result['score'] >= TEAM_THRESHOLD)

    def test_deterministic_and_context_untouched(self):
        context = {'files': ['web/app.js']}
        first = classify('rename the button label', context)
        self.assertEqual(first, classify('rename the button label', context))
        self.assertEqual(context, {'files': ['web/app.js']})
        self.assertEqual(classify('UI neu', None), classify('UI neu'))

    def test_context_files_count(self):
        many = {'files': ['a.py', 'b.py', 'c.py', 'd.py']}
        self.assertEqual(classify('use the new logger', many)['route'], 'team')

    def test_no_network_and_no_io(self):
        with mock.patch.object(socket, 'socket', side_effect=AssertionError('network used')), \
                mock.patch('builtins.open', side_effect=AssertionError('file opened')):
            classify('redesign the settings page')
        source = inspect.getsource(difficulty)
        for banned in ('import socket', 'urllib', 'requests', 'open(', 'import os', 'subprocess'):
            self.assertNotIn(banned, source)

    def test_type_errors(self):
        with self.assertRaises(TypeError):
            classify(42)
        with self.assertRaises(TypeError):
            classify('x', context=['a.py'])


class ReviewFixes2(unittest.TestCase):
    def _scope(self, result):
        return [r for r in result['reasons'] if r.startswith('scope:')]

    def test_restart_verbs_only_as_verbs(self):
        for text in ('die Startseite neu machen', 'den Ladebalken neu gestalten',
                     'die Installation neu bauen', 'die Meldungen neu schreiben'):
            result = classify(text)
            self.assertEqual(result['route'], 'team', (text, result))
            self.assertTrue(self._scope(result), (text, result))
            self.assertFalse(any('restart' in r for r in result['reasons']), (text, result))

    def test_neu_machen_after_restart_verb_still_scope(self):
        for text in ('Starte bitte: Einstellungen neu gestalten',
                     'Lade das Logo hoch und die Startseite neu machen'):
            result = classify(text)
            self.assertEqual(result['route'], 'team', (text, result))
            self.assertFalse(any('restart' in r for r in result['reasons']), (text, result))

    def test_real_restarts_still_direct(self):
        for text in ('starte die App neu', 'starten wir NotionTool neu', 'lade die Seite neu',
                     'installiere das Paket neu', 'verbinde den Server neu', 'melde dich neu an',
                     'bitte neu starten', 'die Seite neu laden'):
            result = classify(text)
            self.assertEqual(result['route'], 'direct', (text, result))
            self.assertFalse(self._scope(result), (text, result))

    def test_rework_words_are_scope(self):
        for text in ('\u00fcberarbeite die Einstellungen', 'die Sidebar umgestalten', 'verbessere die Suche',
                     'rework the onboarding', 'revamp the dashboard', 'improve the search',
                     'modernise the build', 'modernize the build'):
            result = classify(text)
            self.assertEqual(result['route'], 'team', (text, result))
            self.assertTrue(self._scope(result), (text, result))

    def test_short_hard_and_long_easy_unchanged(self):
        for text in ('UI neu', 'mach die app sch\u00f6ner', 'redesign the settings page'):
            self.assertEqual(classify(text)['route'], 'team')
        self.assertEqual(classify(LONG_RENAME)['route'], 'direct')


if __name__ == '__main__':
    unittest.main()
