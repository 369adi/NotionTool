"""estimate_tokens lives in one place (chat_context.py) and the calibration helpers fit real token counts."""
import json
import math
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from notiontool import chat_context, context_archive, run_context  # noqa: E402
from notiontool.chat_context import (CONTENT_KINDS, TOKEN_DIVISOR, calibrated_tokens, calibration_error,  # noqa: E402
                                     content_kind, estimate_tokens, fit_divisors)


class TestSingleEstimateTokens(unittest.TestCase):
    def test_one_definition_for_every_module(self):
        self.assertIs(context_archive.estimate_tokens, chat_context.estimate_tokens)
        self.assertIs(run_context.estimate_tokens, chat_context.estimate_tokens)
        from bench import context_switch
        self.assertIs(context_switch.estimate_tokens, chat_context.estimate_tokens)
        self.assertEqual(chat_context.estimate_tokens.__module__, 'notiontool.context_archive')

    def test_string_rule_is_bytes_over_three(self):
        self.assertEqual(estimate_tokens(''), 0)
        self.assertEqual(estimate_tokens('abc'), 1)
        self.assertEqual(estimate_tokens('abcd'), 2)
        text = 'h\u00e4llo w\u00f6rld \u2713'
        self.assertEqual(estimate_tokens(text), math.ceil(len(text.encode('utf-8')) / 3))
        for mod in (chat_context, context_archive):
            self.assertEqual(mod.estimate_tokens(text), estimate_tokens(text))

    def test_other_values_use_their_json_text(self):
        value = dict(b=[1, 2, 'x'], a='\u00e4')
        canon = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        self.assertEqual(estimate_tokens(value), math.ceil(len(canon.encode('utf-8')) / 3))
        self.assertEqual(context_archive.estimate_tokens(value), estimate_tokens(value))
        # not JSON serializable: counted through str(), no TypeError
        self.assertGreater(estimate_tokens({'s': {1, 2}}), 0)

    def test_no_import_cycle_in_either_order(self):
        for first in ('notiontool.context_archive', 'notiontool.chat_context', 'notiontool.run_context'):
            code = 'import %s, notiontool.chat_context as h, notiontool.context_archive as c; ' % first
            code += 'assert c.estimate_tokens is h.estimate_tokens'
            done = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode, 0, first + ': ' + done.stderr)


class TestCalibration(unittest.TestCase):
    def test_content_kind(self):
        self.assertEqual(content_kind('{"a": [1, 2]}'), 'json')
        self.assertEqual(content_kind(dict(a=1)), 'json')
        self.assertEqual(content_kind('def f(x):\n    return {x: [x]}  # a_b;\n'), 'code')
        self.assertEqual(content_kind('Please summarise the meeting notes from last week for me.'), 'prose')
        self.assertEqual(content_kind('{not json at all, just words and words and words and words}'), 'prose')
        self.assertEqual(content_kind(''), 'prose')

    def test_default_divisor_equals_estimate_tokens(self):
        for value in ('hello world', '{"a": 1}', 'def f(): pass', dict(a=[1, 2])):
            self.assertEqual(calibrated_tokens(value), estimate_tokens(value))
            self.assertEqual(calibrated_tokens(value, {}), estimate_tokens(value))

    def test_fit_needs_min_samples_per_kind(self):
        prose = 'x' * 40
        few = fit_divisors([(prose, 10)] * 5, min_samples=30)
        self.assertEqual(few['fitted'], [])
        self.assertEqual(few['divisors'], {k: TOKEN_DIVISOR for k in CONTENT_KINDS})
        self.assertEqual(few['counts']['prose'], 5)
        enough = fit_divisors([(prose, 10)] * 30, min_samples=30)
        self.assertEqual(enough['fitted'], ['prose'])
        self.assertEqual(enough['divisors']['prose'], 4.0)
        self.assertEqual(enough['divisors']['code'], TOKEN_DIVISOR)

    def test_fit_ignores_unusable_pairs(self):
        fit = fit_divisors([('abc', 0), ('abc', -1), ('abc', None), ('abc', True), ('', 5), ('x' * 8, 2)], min_samples=1)
        self.assertEqual(fit['counts'], dict(code=0, prose=1, json=0))
        self.assertEqual(fit['divisors']['prose'], 4.0)

    def test_fit_lowers_the_error(self):
        # prose really costs 4 bytes per token, JSON 2: the default divisor 3 is off both ways
        prose = 'plain words in a row, nothing special here at all. ' * 4
        data = json.dumps([dict(id=i, v=i * 2) for i in range(8)])
        samples = [(prose, math.ceil(len(prose.encode()) / 4.0))] * 30 + [(data, math.ceil(len(data.encode()) / 2.0))] * 30
        before = calibration_error(samples)
        fit = fit_divisors(samples, min_samples=30)
        after = calibration_error(samples, fit['divisors'])
        self.assertEqual(sorted(fit['fitted']), ['json', 'prose'])
        self.assertGreater(before, 0.2)
        self.assertLess(after, 0.05)
        self.assertIsNone(calibration_error([]))
        self.assertIsNone(calibration_error([('abc', 0)]))


if __name__ == '__main__':
    unittest.main()
