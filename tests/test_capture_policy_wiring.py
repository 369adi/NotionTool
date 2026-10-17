import json
import unittest
from unittest import mock

from notiontool import plain_protocol

# Texts as returned by capture_policy.reason_text for Part A's real reason codes.
TEXTS = {
    'not_enabled': 'Screen capture refused: screen capture is not enabled for this project.',
    'class_not_visual': 'Screen capture refused: this task class is not a visual task.',
    'invalid_count': 'Screen capture refused: the number of captures already taken is invalid.',
}
SUFFIX = ' No capture was taken.'


class CapturePolicyWiringTests(unittest.TestCase):
    def policy(self, allowed, why, texts=True):
        names = ['capture_allowed', 'reason_text'] if texts else ['capture_allowed']
        fake = mock.Mock(spec=names)
        fake.capture_allowed.return_value = (allowed, why)
        if texts:
            fake.reason_text.side_effect = lambda code: TEXTS.get(code, 'Screen capture refused: unknown policy reason.')
        return mock.patch.object(plain_protocol, 'capture_policy', fake), fake

    def task(self, task_class='ui'):
        return {'id': 't1', 'role': 'executor', 'task_class': task_class}

    def test_refused_takes_no_capture_and_returns_readable_text_and_reason(self):
        patch, fake = self.policy(False, 'not_enabled')
        capture = mock.Mock()
        with patch:
            result = plain_protocol.guarded_capture(capture, {'target': 'roblox'}, self.task('visual'), {}, 2)
        capture.assert_not_called()
        self.assertTrue(result['refused'])
        self.assertEqual(result['text'], TEXTS['not_enabled'] + SUFFIX)
        self.assertNotIn('not_enabled', result['text'])
        fake.reason_text.assert_called_once_with('not_enabled')
        self.assertEqual(result['reason'], {'reason': 'not_enabled', 'task_class': 'visual', 'already_taken': 2})
        json.dumps(result['reason'])

    def test_allowed_takes_the_capture(self):
        patch, fake = self.policy(True, 'allowed')
        capture = mock.Mock(return_value={'changed': True})
        with patch:
            result = plain_protocol.guarded_capture(capture, {'target': 'blender'}, self.task(), {'capture_enabled': True}, 0)
        capture.assert_called_once_with({'target': 'blender'})
        self.assertEqual(result, {'changed': True})
        fake.reason_text.assert_not_called()

    def test_operator_capture_is_not_gated(self):
        patch, fake = self.policy(False, 'class_not_visual')
        capture = mock.Mock(return_value='image')
        with patch:
            self.assertEqual(plain_protocol.guarded_capture(capture, {}, self.task('code'), {}, 99, operator=True), 'image')
            self.assertIsNone(plain_protocol.capture_gate(self.task('code'), {}, 99, operator=True))
        fake.capture_allowed.assert_not_called()
        capture.assert_called_once_with({})

    def test_chat_path_without_worker_task_is_not_gated(self):
        patch, fake = self.policy(False, 'not_enabled')
        capture = mock.Mock(return_value='image')
        with patch:
            self.assertEqual(plain_protocol.guarded_capture(capture, {'target': ''}), 'image')
        fake.capture_allowed.assert_not_called()

    def test_policy_receives_class_settings_and_count(self):
        patch, fake = self.policy(True, 'allowed')
        settings = {'capture_enabled': True, 'max_captures': 4}
        with patch:
            self.assertIsNone(plain_protocol.capture_gate(self.task(' UI '), settings, 3))
        fake.capture_allowed.assert_called_once_with('ui', settings, 3)

    def test_missing_values_are_normalized(self):
        patch, fake = self.policy(False, 'class_not_visual')
        with patch:
            result = plain_protocol.capture_gate({'role': 'executor'}, None, None)
        fake.capture_allowed.assert_called_once_with('', {}, 0)
        self.assertEqual(result['reason'], {'reason': 'class_not_visual', 'task_class': '', 'already_taken': 0})
        self.assertEqual(result['text'], TEXTS['class_not_visual'] + SUFFIX)

    def test_bad_count_is_passed_to_the_policy(self):
        for bad in ('3', -1, 2.5, True, ''):
            patch, fake = self.policy(False, 'invalid_count')
            with patch:
                result = plain_protocol.capture_gate(self.task(), {'capture_enabled': True}, bad)
            fake.capture_allowed.assert_called_once_with('ui', {'capture_enabled': True}, bad)
            self.assertEqual(result['reason']['reason'], 'invalid_count', repr(bad))
            self.assertEqual(result['text'], TEXTS['invalid_count'] + SUFFIX)
            json.dumps(result['reason'])
        patch, fake = self.policy(False, 'invalid_count')
        odd = object()
        with patch:
            result = plain_protocol.capture_gate(self.task(), {}, odd)
        fake.capture_allowed.assert_called_once_with('ui', {}, odd)
        self.assertEqual(result['reason']['already_taken'], repr(odd))
        json.dumps(result['reason'])

    def test_policy_without_reason_text_falls_back_to_the_code(self):
        patch, fake = self.policy(False, 'not_enabled', texts=False)
        with patch:
            result = plain_protocol.capture_gate(self.task(), {}, 0)
        self.assertEqual(result['text'], 'Screen capture refused for this task: not_enabled. No capture was taken.')

    def test_missing_policy_module_refuses_workers_only(self):
        capture = mock.Mock(return_value='image')
        with mock.patch.object(plain_protocol, 'capture_policy', None):
            refused = plain_protocol.guarded_capture(capture, {}, self.task(), {'capture_enabled': True}, 0)
            self.assertEqual(refused['reason']['reason'], 'capture_policy_unavailable')
            self.assertEqual(refused['text'], 'Screen capture refused for this task: the capture policy is not available. No capture was taken.')
            capture.assert_not_called()
            self.assertEqual(plain_protocol.guarded_capture(capture, {}, None, operator=True), 'image')


if __name__ == '__main__':
    unittest.main()
