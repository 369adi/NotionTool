import copy
import unittest

from notiontool.done_definition import (
    MAX_CRITERIA,
    METHODS,
    build_done_definition,
    validate_criterion,
)


class DoneDefinitionTests(unittest.TestCase):
    def assertAllValid(self, criteria):
        self.assertTrue(criteria)
        for criterion in criteria:
            ok, problems = validate_criterion(criterion)
            self.assertTrue(ok, problems)
        ids = [criterion['id'] for criterion in criteria]
        self.assertEqual(len(ids), len(set(ids)))

    def test_nothing_usable_gives_one_required_fallback(self):
        for bad in (None, '', '   ', [], {}, 42, True, [None, 3, [], {}], {'criteria': None}):
            with self.subTest(bad=bad):
                out = build_done_definition('t1', bad)
                self.assertEqual(len(out), 1)
                self.assertEqual(out[0]['id'], 'c1')
                self.assertEqual(out[0]['method'], 'requirements')
                self.assertIs(out[0]['required'], True)
                self.assertEqual(out[0]['target'], [])
                self.assertIn('t1', out[0]['text'])
                self.assertAllValid(out)

    def test_plain_string_becomes_requirements_criterion(self):
        out = build_done_definition('t1', '  All   tests pass ')
        self.assertEqual(out, [{'id': 'c1', 'text': 'All tests pass', 'method': 'requirements', 'target': [], 'required': True}])

    def test_multiline_string_strips_bullets(self):
        out = build_done_definition('t1', '- first\n2. second\n\n* third\n1.5 s limit')
        self.assertEqual([c['text'] for c in out], ['first', 'second', 'third', '1.5 s limit'])
        self.assertEqual([c['id'] for c in out], ['c1', 'c2', 'c3', 'c4'])
        self.assertAllValid(out)

    def test_single_dict_is_kept(self):
        item = {'text': 'Run unit tests', 'method': 'tests', 'target': ['python -m unittest tests.test_x'], 'required': False}
        out = build_done_definition('t1', item)
        self.assertEqual(out, [{'id': 'c1', 'text': 'Run unit tests', 'method': 'tests', 'target': ['python -m unittest tests.test_x'], 'required': False}])

    def test_wrapper_dict_with_criteria_key(self):
        out = build_done_definition('t1', {'criteria': [{'text': 'a'}, {'description': 'b', 'method': 'diff'}]})
        self.assertEqual([(c['text'], c['method']) for c in out], [('a', 'requirements'), ('b', 'diff')])
        self.assertAllValid(out)

    def test_list_with_mixed_types(self):
        raw = ['plain text', {'text': 'dict item', 'method': 'static'}, None, 7, ['nested'], {'method': 'tests'}, {'target': 'tests/test_a.py', 'method': 'tests'}]
        out = build_done_definition('t1', raw)
        self.assertEqual([c['text'] for c in out], ['plain text', 'dict item', 'tests check: tests/test_a.py'])
        self.assertEqual([c['method'] for c in out], ['requirements', 'static', 'tests'])
        self.assertAllValid(out)

    def test_unknown_method_is_mapped_and_noted(self):
        out = build_done_definition('t1', [{'text': 'Looks nice', 'method': 'Vibes'}, {'text': 'x', 'method': ' TESTS '}])
        self.assertEqual(out[0]['method'], 'requirements')
        self.assertIn("unknown method 'vibes' mapped to requirements", out[0]['text'])
        self.assertEqual(out[1]['method'], 'tests')
        self.assertNotIn('unknown method', out[1]['text'])
        self.assertAllValid(out)

    def test_every_known_method_is_kept(self):
        out = build_done_definition('t1', [{'text': m, 'method': m} for m in METHODS])
        self.assertEqual([c['method'] for c in out], list(METHODS))

    def test_missing_invalid_and_duplicate_ids(self):
        raw = [
            {'id': 'c2', 'text': 'a'},
            {'text': 'b'},
            {'id': 'c2', 'text': 'c'},
            {'id': 'bad id!', 'text': 'd'},
            {'id': 'x9', 'text': 'e'},
            {'id': 'c1\n', 'text': 'f'},
        ]
        out = build_done_definition('t1', raw)
        self.assertEqual([c['id'] for c in out], ['c2', 'c3', 'c4', 'c5', 'x9', 'c1'])
        self.assertAllValid(out)

    def test_target_string_and_messy_list(self):
        cases = [
            ('tests/a.py', ['tests/a.py']),
            ('a.py\nb.py', ['a.py', 'b.py']),
            (['a.py', 'a.py', 3, '', ' b.py ', None], ['a.py', 'b.py']),
            (None, []),
            (5, []),
        ]
        for target, expected in cases:
            with self.subTest(target=target):
                out = build_done_definition('t1', {'text': 't', 'target': target})
                self.assertEqual(out[0]['target'], expected)
        out = build_done_definition('t1', {'text': 't', 'targets': 'x.py'})
        self.assertEqual(out[0]['target'], ['x.py'])

    def test_required_is_coerced_to_bool(self):
        cases = [(True, True), (False, False), ('no', False), ('Optional', False), ('yes', True), (0, False), (1, True), (None, True), ('maybe', True), ([], True)]
        for value, expected in cases:
            with self.subTest(value=value):
                out = build_done_definition('t1', {'text': 't', 'required': value})
                self.assertIs(out[0]['required'], expected)

    def test_stable_output_and_input_not_mutated(self):
        raw = [{'text': 'a', 'target': ['x']}, {'id': 'c1', 'text': 'b'}]
        before = copy.deepcopy(raw)
        first = build_done_definition('t1', raw)
        second = build_done_definition('t1', raw)
        self.assertEqual(first, second)
        self.assertEqual(raw, before)
        self.assertIsNot(first[0]['target'], raw[0]['target'])
        self.assertEqual([c['id'] for c in first], ['c2', 'c1'])

    def test_criteria_count_is_capped(self):
        out = build_done_definition('t1', ['item %d' % i for i in range(80)])
        self.assertEqual(len(out), MAX_CRITERIA)
        self.assertAllValid(out)

    def test_validate_criterion_reports_problems(self):
        self.assertEqual(validate_criterion('nope'), (False, ['criterion must be a dict']))
        good = {'id': 'c1', 'text': 'ok', 'method': 'tests', 'target': ['a.py'], 'required': True}
        self.assertEqual(validate_criterion(good), (True, []))
        ok, problems = validate_criterion({})
        self.assertFalse(ok)
        self.assertEqual(len(problems), 5)
        bad = {'id': '', 'text': ' ', 'method': 'magic', 'target': 'a.py', 'required': 'yes', 'extra': 1}
        ok, problems = validate_criterion(bad)
        self.assertFalse(ok)
        self.assertEqual(len(problems), 6)
        self.assertFalse(validate_criterion(dict(good, id='c1\n'))[0])
        self.assertFalse(validate_criterion(dict(good, required=1))[0])
        self.assertFalse(validate_criterion(dict(good, target=['a.py', '']))[0])


if __name__ == '__main__':
    unittest.main()
