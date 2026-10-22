"""Backtest notiontool.difficulty.classify on the labelled cases, and check the route wiring."""
import json
import unittest
from pathlib import Path

from notiontool import difficulty
from notiontool import effort_policy as ep

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'tests' / 'data' / 'difficulty_cases.json'
MIN_CASES = 40
MIN_OVERALL = 0.85
MIN_SHORT_HARD = 0.9
SHORT_WORDS = 6
ROUTES = ('team', 'direct')


def load_cases(path=DATA):
    """Load every case strictly: a missing text or an unknown route fails, nothing is dropped."""
    raw = json.loads(Path(path).read_text(encoding='utf-8'))
    cases = raw.get('cases') if isinstance(raw, dict) else raw
    if not isinstance(cases, list):
        raise AssertionError('%s: expected a list of cases (or {"cases": [...]})' % path)
    for i, case in enumerate(cases):
        if not isinstance(case, dict):
            raise AssertionError('case %d is not an object: %r' % (i, case))
        text = case.get('text')
        if not isinstance(text, str) or not text.strip():
            raise AssertionError('case %d has no text: %r' % (i, case))
        if case.get('route') not in ROUTES:
            raise AssertionError('case %d has route %r, expected team or direct: %r' % (i, case.get('route'), text))
    return cases


def run(cases):
    wrong = []
    for case in cases:
        got = difficulty.classify(case['text'])
        if got['route'] != case['route']:
            wrong.append((case, got))
    return wrong


def report(title, cases, wrong):
    acc = 1.0 - len(wrong) / len(cases) if cases else 0.0
    print('\n%s: %d/%d correct (%.3f)' % (title, len(cases) - len(wrong), len(cases), acc))
    for case, got in wrong:
        print('  MISS want=%s got=%s score=%.3f text=%r reasons=%s'
              % (case['route'], got['route'], got['score'], case['text'], '; '.join(got['reasons'])))
    return acc


class DifficultyBacktest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not DATA.exists():
            raise AssertionError('labelled cases missing: %s (the dataset task must be merged first)' % DATA)
        cls.cases = load_cases()

    def test_case_count(self):
        self.assertGreaterEqual(len(self.cases), MIN_CASES)

    def test_overall_accuracy(self):
        acc = report('overall', self.cases, run(self.cases))
        self.assertGreaterEqual(acc, MIN_OVERALL)

    def test_short_but_hard_accuracy(self):
        subset = [c for c in self.cases if c['route'] == 'team' and len(c['text'].split()) <= SHORT_WORDS]
        self.assertGreater(len(subset), 0)
        acc = report('short-but-hard', subset, run(subset))
        self.assertGreaterEqual(acc, MIN_SHORT_HARD)


class StrictLoadingTest(unittest.TestCase):
    def _load(self, cases):
        import os
        import tempfile
        fd, path = tempfile.mkstemp(suffix='.json')
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(cases, f)
        try:
            return load_cases(path)
        finally:
            os.remove(path)

    def test_missing_text_fails(self):
        with self.assertRaisesRegex(AssertionError, 'no text'):
            self._load([dict(route='team')])

    def test_unknown_route_fails(self):
        with self.assertRaisesRegex(AssertionError, 'expected team or direct'):
            self._load([dict(text='x', route='maybe')])

    def test_valid_cases_kept(self):
        self.assertEqual(len(self._load([dict(text='a', route='team'), dict(text='b', route='direct')])), 2)


class RouteWiringTest(unittest.TestCase):
    def test_team_result_raises_route(self):
        result = ep.route_for_request('mach mir die UI neu')
        self.assertEqual(result['route'], 'team')
        self.assertTrue(result['raised'])
        self.assertIn('raised direct -> team', result['reason'])

    def test_direct_result_keeps_route(self):
        result = ep.route_for_request('fix typo in README')
        self.assertEqual(result['route'], 'direct')
        self.assertFalse(result['raised'])

    def test_never_lowers_team(self):
        result = ep.route_for_request('fix typo in README', current='team')
        self.assertEqual(result['route'], 'team')
        self.assertFalse(result['raised'])
        self.assertIn('already team', result['reason'])

    def test_matches_classify(self):
        for text in ('redesign the settings page', 'bump version to 1.2', 'mach die app sch\u00f6ner'):
            want = difficulty.classify(text)
            got = ep.route_for_request(text)
            self.assertEqual(got['score'], want['score'])
            self.assertEqual(got['route'], want['route'])

    def test_bad_input(self):
        with self.assertRaises(ep.EffortPolicyError):
            ep.route_for_request('x', current='big')
        with self.assertRaises(TypeError):
            ep.route_for_request(5)

    def test_existing_api_unchanged(self):
        self.assertEqual(ep.initial()['level'], 'low')
        self.assertEqual(ep.escalate('low', True, 'r')['level'], 'standard')


if __name__ == '__main__':
    unittest.main()
