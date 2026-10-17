import inspect
import unittest

from notiontool import capture_policy as cp
from notiontool.capture_policy import capture_allowed as _capture_allowed

# The old rules are reached with capture_all_coworkers False; the assertions below
# are unchanged from before the setting existed.
OLD = {'capture_all_coworkers': False}
ON = {'capture_enabled': True}


def capture_allowed(task_class, project_settings, already_taken):
    # Old-rule wrapper: non-dict settings were treated as {} before, so they become OLD.
    if isinstance(project_settings, dict):
        project_settings = dict(project_settings, **OLD)
    else:
        project_settings = dict(OLD)
    return _capture_allowed(task_class, project_settings, already_taken)


class CapturePolicyTests(unittest.TestCase):
    def test_visual_class_enabled_is_allowed(self):
        self.assertEqual(capture_allowed('ui', ON, 0), (True, 'allowed'))

    def test_class_match_ignores_case_and_spaces(self):
        for cls in (' UI ', 'Visual', '\tgame\n', 'DESIGN'):
            self.assertEqual(capture_allowed(cls, ON, 0), (True, 'allowed'), repr(cls))

    def test_non_visual_class_is_refused(self):
        for cls in ('code', 'docs', 'backtest'):
            self.assertEqual(capture_allowed(cls, ON, 0), (False, 'class_not_visual'))

    def test_missing_or_bad_task_class_is_refused(self):
        for cls in (None, '', '   ', 42, ['ui'], b'ui'):
            self.assertEqual(capture_allowed(cls, ON, 0), (False, 'class_not_visual'), repr(cls))

    def test_not_enabled_by_default(self):
        self.assertEqual(capture_allowed('ui', {}, 0), (False, 'not_enabled'))

    def test_missing_or_bad_settings_mean_not_enabled(self):
        for settings in (None, [], 'capture_enabled', 7):
            self.assertEqual(capture_allowed('ui', settings, 0), (False, 'not_enabled'), repr(settings))

    def test_capture_enabled_must_be_true(self):
        for value in (False, None, 'true', 1, 'yes'):
            self.assertEqual(capture_allowed('ui', {'capture_enabled': value}, 0), (False, 'not_enabled'), repr(value))

    def test_screenshot_class_needs_no_opt_in(self):
        self.assertEqual(capture_allowed(' Screenshot ', None, 0), (True, 'explicit_request'))

    def test_explicit_screenshot_setting_needs_no_opt_in(self):
        self.assertEqual(capture_allowed('ui', {'explicit_screenshot': True}, 0), (True, 'explicit_request'))
        self.assertEqual(capture_allowed('ui', {'explicit_screenshot': 'yes'}, 0), (False, 'not_enabled'))

    def test_explicit_screenshot_does_not_make_code_visual(self):
        self.assertEqual(capture_allowed('code', {'explicit_screenshot': True}, 0), (False, 'class_not_visual'))

    def test_enabled_and_explicit_reports_allowed(self):
        self.assertEqual(capture_allowed('screenshot', ON, 0), (True, 'allowed'))

    def test_capture_classes_override(self):
        settings = {'capture_enabled': True, 'capture_classes': [' Blender ', 'ROBLOX']}
        self.assertEqual(capture_allowed('blender', settings, 0), (True, 'allowed'))
        self.assertEqual(capture_allowed('roblox', settings, 0), (True, 'allowed'))
        self.assertEqual(capture_allowed('ui', settings, 0), (False, 'class_not_visual'))

    def test_capture_classes_single_string(self):
        settings = {'capture_enabled': True, 'capture_classes': 'Blender'}
        self.assertEqual(capture_allowed('blender', settings, 0), (True, 'allowed'))
        self.assertEqual(capture_allowed('ui', settings, 0), (False, 'class_not_visual'))

    def test_capture_classes_bad_type_falls_back_to_defaults(self):
        for raw in (None, 5, {'ui': True}):
            settings = {'capture_enabled': True, 'capture_classes': raw}
            self.assertEqual(capture_allowed('ui', settings, 0), (True, 'allowed'), repr(raw))
        self.assertEqual(cp.visual_classes(None), frozenset(cp.VISUAL_CLASSES))

    def test_empty_capture_classes_allows_no_class(self):
        settings = {'capture_enabled': True, 'capture_classes': []}
        self.assertEqual(capture_allowed('ui', settings, 0), (False, 'class_not_visual'))
        settings['capture_classes'] = ['', None, 3]
        self.assertEqual(capture_allowed('ui', settings, 0), (False, 'class_not_visual'))

    def test_limit_reached_at_default_max(self):
        self.assertEqual(cp.DEFAULT_MAX_CAPTURES, 4)
        for taken in (4, 5, 100):
            self.assertEqual(capture_allowed('ui', ON, taken), (False, 'limit_reached'))

    def test_max_captures_override(self):
        settings = {'capture_enabled': True, 'max_captures': 2}
        self.assertEqual(capture_allowed('ui', settings, 0), (True, 'allowed'))
        self.assertEqual(capture_allowed('ui', settings, 1), (True, 'allowed'))
        self.assertEqual(capture_allowed('ui', settings, 2), (False, 'limit_reached'))
        settings['max_captures'] = 0
        self.assertEqual(capture_allowed('ui', settings, 0), (False, 'limit_reached'))

    def test_bad_max_captures_falls_back_to_default(self):
        for raw in (None, '3', 2.0, True, -1):
            settings = {'capture_enabled': True, 'max_captures': raw}
            self.assertEqual(cp.max_captures(dict(settings, **OLD)), 4, repr(raw))
            self.assertEqual(capture_allowed('ui', settings, 3), (True, 'allowed'), repr(raw))
            self.assertEqual(capture_allowed('ui', settings, 4), (False, 'limit_reached'), repr(raw))

    def test_after_capture_is_due_when_count_is_odd(self):
        for taken in (1, 3):
            self.assertEqual(capture_allowed('ui', ON, taken), (True, 'allowed'))

    def test_before_capture_refused_when_it_would_be_the_last(self):
        settings = {'capture_enabled': True, 'max_captures': 3}
        self.assertEqual(capture_allowed('ui', settings, 0), (True, 'allowed'))
        self.assertEqual(capture_allowed('ui', settings, 1), (True, 'allowed'))
        self.assertEqual(capture_allowed('ui', settings, 2), (False, 'pair_incomplete'))
        self.assertEqual(capture_allowed('ui', {'capture_enabled': True, 'max_captures': 1}, 0), (False, 'pair_incomplete'))

    def test_full_default_sequence(self):
        results = [capture_allowed('ui', ON, n)[0] for n in range(6)]
        self.assertEqual(results, [True, True, True, True, False, False])

    def test_already_taken_none_counts_as_zero(self):
        self.assertEqual(capture_allowed('ui', ON, None), (True, 'allowed'))

    def test_bad_already_taken_is_refused(self):
        for taken in (-1, '2', 1.5, True, False, [1]):
            self.assertEqual(capture_allowed('ui', ON, taken), (False, 'invalid_count'), repr(taken))

    def test_invalid_count_is_checked_first(self):
        self.assertEqual(capture_allowed('code', None, -1), (False, 'invalid_count'))

    def test_inputs_are_not_changed(self):
        settings = {'capture_enabled': True, 'capture_classes': ['UI'], 'max_captures': 2, 'capture_all_coworkers': False}
        before = dict(settings)
        _capture_allowed('ui', settings, 1)
        self.assertEqual(settings, before)

    def test_result_types_and_reason_texts(self):
        allowed, reason = capture_allowed('ui', ON, 0)
        self.assertIs(type(allowed), bool)
        self.assertIsInstance(reason, str)
        for code in ('allowed', 'explicit_request', 'class_not_visual', 'not_enabled', 'limit_reached', 'pair_incomplete', 'invalid_count'):
            self.assertRegex(code, r'\A[a-z]+(_[a-z]+)*\Z')
            self.assertTrue(cp.reason_text(code).startswith('Screen capture'), code)
        self.assertIn('unknown', cp.reason_text('nope'))

    def test_module_uses_no_io(self):
        source = inspect.getsource(cp)
        for word in ('open(', 'import', 'requests', 'socket', 'urllib', 'store', 'pathlib'):
            self.assertNotIn(word, source)


class AllCoworkersCaptureTests(unittest.TestCase):
    """capture_all_coworkers: default True when the key is missing."""

    def test_missing_key_means_on(self):
        for settings in ({}, None, [], 'x', {'capture_all_coworkers': True}):
            self.assertTrue(cp.all_coworkers(settings), repr(settings))
        self.assertFalse(cp.all_coworkers({'capture_all_coworkers': False}))

    def test_true_allows_an_empty_class(self):
        for cls in ('', None, '   ', 42):
            self.assertEqual(_capture_allowed(cls, {}, 0), (True, 'allowed'), repr(cls))
            self.assertEqual(_capture_allowed(cls, {'capture_all_coworkers': True}, 0), (True, 'allowed'), repr(cls))

    def test_true_allows_every_class_and_ignores_capture_classes(self):
        for cls in ('code', 'docs', 'backtest', 'ui', 'screenshot'):
            self.assertEqual(_capture_allowed(cls, {'capture_classes': []}, 0), (True, 'allowed'), cls)

    def test_true_treats_capture_enabled_as_true(self):
        for value in (False, None, 'no'):
            self.assertEqual(_capture_allowed('code', {'capture_enabled': value}, 0), (True, 'allowed'), repr(value))

    def test_false_gives_exactly_the_old_reasons(self):
        self.assertEqual(_capture_allowed('', OLD, 0), (False, 'class_not_visual'))
        self.assertEqual(_capture_allowed('code', dict(OLD, capture_enabled=True), 0), (False, 'class_not_visual'))
        self.assertEqual(_capture_allowed('ui', OLD, 0), (False, 'not_enabled'))
        self.assertEqual(_capture_allowed('ui', dict(OLD, capture_enabled=True), 0), (True, 'allowed'))
        self.assertEqual(cp.max_captures(OLD), 4)

    def test_default_max_is_twelve(self):
        self.assertEqual(cp.ALL_COWORKERS_MAX_CAPTURES, 12)
        self.assertEqual(cp.max_captures({}), 12)
        self.assertEqual(cp.max_captures(None), 12)
        results = [_capture_allowed('', {}, n) for n in range(14)]
        self.assertEqual(results[:12], [(True, 'allowed')] * 12)
        self.assertEqual(results[12:], [(False, 'limit_reached')] * 2)

    def test_max_captures_overrides(self):
        settings = {'max_captures': 2}
        self.assertEqual(cp.max_captures(settings), 2)
        self.assertEqual(_capture_allowed('', settings, 1), (True, 'allowed'))
        self.assertEqual(_capture_allowed('', settings, 2), (False, 'limit_reached'))
        self.assertEqual(_capture_allowed('', {'max_captures': 0}, 0), (False, 'limit_reached'))
        for raw in ('3', -1, True, 2.0):
            self.assertEqual(cp.max_captures({'max_captures': raw}), 12, repr(raw))

    def test_pair_rule_is_skipped(self):
        # With the old rules these give pair_incomplete.
        self.assertEqual(_capture_allowed('', {'max_captures': 1}, 0), (True, 'allowed'))
        self.assertEqual(_capture_allowed('', {'max_captures': 3}, 2), (True, 'allowed'))
        self.assertEqual(_capture_allowed('', {}, 10), (True, 'allowed'))
        self.assertEqual(_capture_allowed('ui', dict(OLD, capture_enabled=True, max_captures=1), 0), (False, 'pair_incomplete'))

    def test_invalid_count_still_first(self):
        for taken in (-1, '2', True):
            self.assertEqual(_capture_allowed('', {}, taken), (False, 'invalid_count'), repr(taken))

    def test_inputs_are_not_changed(self):
        settings = {'max_captures': 5}
        _capture_allowed('', settings, 0)
        self.assertEqual(settings, {'max_captures': 5})


if __name__ == '__main__':
    unittest.main()
