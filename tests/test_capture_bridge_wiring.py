import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from notiontool import capture_policy, plain_protocol
from notiontool.bridge import Bridge

ON = {'capture_enabled': True, 'capture_all_coworkers': False}
OFF = {'capture_all_coworkers': False}


class FakeStore:
    """In-memory store: tasks, runs, settings and events only (no disk, no Notion)."""

    def __init__(self, values=None):
        self.values = dict(values or {})
        self.tasks, self.runs, self.events = [], {}, []

    def settings(self):
        return self.values

    def all(self, kind):
        return list(self.tasks) if kind == 'tasks' else []

    def get(self, kind, key):
        if kind == 'runs':
            return self.runs[key]
        return {}

    def event(self, kind, run_id='', **fields):
        self.events.append((kind, run_id, fields))

    def add_task(self, task_id, token, task_class='ui', run_id='r1'):
        self.runs.setdefault(run_id, {'id': run_id, 'state': 'executing'})
        task = {'id': task_id, 'lease': token, 'state': 'running', 'role': 'executor',
                'run_id': run_id, 'worker_id': 'w1', 'task_class': task_class}
        self.tasks.append(task)
        return task


class CaptureBridgeWiringTests(unittest.TestCase):
    def make(self, values=None):
        store = FakeStore(values)
        bridge = Bridge(store, engine=SimpleNamespace(lock=threading.RLock(), adapter=None))
        screen = mock.Mock()
        screen.call.return_value = {'text': 'captured'}
        bridge._screen = screen
        return bridge, store, screen

    def refused_events(self, store):
        return [e for e in store.events if e[0] == 'capture_refused']

    def test_allowed_path_captures_and_counts(self):
        bridge, store, screen = self.make(ON)
        store.add_task('t1', 'tok1')
        with mock.patch.object(plain_protocol, 'capture_gate', wraps=plain_protocol.capture_gate) as gate:
            result = bridge.call('screen_capture', {'target': 'roblox'}, 'tok1')
        self.assertEqual(result, {'text': 'captured'})
        screen.call.assert_called_once_with('screen_capture', {'target': 'roblox'}, task_id='t1', run_id='r1', role='executor')
        gate.assert_called_once()
        task, settings, taken = gate.call_args.args
        self.assertEqual((task['id'], task['task_class'], settings.get('capture_enabled'), taken), ('t1', 'ui', True, 0))
        self.assertEqual(bridge.captures_taken('t1'), 1)
        self.assertEqual(self.refused_events(store), [])

    def test_refused_path_takes_no_capture_and_emits_capture_refused(self):
        bridge, store, screen = self.make(OFF)  # old rules: capture_enabled missing -> not_enabled
        store.add_task('t1', 'tok1')
        result = bridge.call('screen_capture', {}, 'tok1')
        screen.call.assert_not_called()
        self.assertTrue(result['refused'])
        self.assertEqual(result['reason'], 'not_enabled')
        self.assertIn('not enabled', result['text'])
        self.assertIn('No capture was taken.', result['text'])
        events = self.refused_events(store)
        self.assertEqual(len(events), 1)
        kind, run_id, fields = events[0]
        self.assertEqual((kind, run_id), ('capture_refused', 'r1'))
        self.assertEqual((fields['task_id'], fields['task_class'], fields['reason']), ('t1', 'ui', 'not_enabled'))
        self.assertEqual(bridge.captures_taken('t1'), 0)

    def test_non_visual_class_is_refused_with_reason(self):
        bridge, store, screen = self.make(ON)
        store.add_task('t1', 'tok1', task_class='code')
        result = bridge.call('screen_capture', {}, 'tok1')
        screen.call.assert_not_called()
        self.assertEqual(result['reason'], 'class_not_visual')
        self.assertEqual(self.refused_events(store)[0][2]['task_class'], 'code')

    def test_operator_path_unchanged_and_never_gated(self):
        bridge, store, screen = self.make({})
        with mock.patch.object(plain_protocol, 'capture_gate') as gate:
            result = bridge.operator_call(SimpleNamespace(), 'nt_screen_capture', {'target': 'blender'})
        self.assertEqual(result, {'text': 'captured'})
        screen.call.assert_called_once_with('screen_capture', {'target': 'blender'}, role='operator')
        gate.assert_not_called()
        self.assertEqual(self.refused_events(store), [])
        self.assertEqual(bridge.__dict__.get('_capture_count_map', {}), {})

    def test_chat_path_without_assignment_unchanged_and_never_gated(self):
        bridge, store, screen = self.make({})
        with mock.patch.object(plain_protocol, 'capture_gate') as gate:
            result = bridge.call('screen_capture', {}, '')
        self.assertEqual(result, {'text': 'captured'})
        screen.call.assert_called_once_with('screen_capture', {}, role='chat')
        gate.assert_not_called()
        self.assertEqual(self.refused_events(store), [])

    def test_counters_are_separate_per_task(self):
        bridge, store, screen = self.make(ON)
        store.add_task('t1', 'tok1')
        store.add_task('t2', 'tok2')
        with mock.patch.object(plain_protocol, 'capture_gate', wraps=plain_protocol.capture_gate) as gate:
            bridge.call('screen_capture', {}, 'tok1')
            bridge.call('screen_capture', {}, 'tok1')
            bridge.call('screen_capture', {}, 'tok2')
        self.assertEqual([(c.args[0]['id'], c.args[2]) for c in gate.call_args_list], [('t1', 0), ('t1', 1), ('t2', 0)])
        self.assertEqual((bridge.captures_taken('t1'), bridge.captures_taken('t2')), (2, 1))

    def test_limit_reached_after_counted_captures(self):
        bridge, store, screen = self.make({'capture_enabled': True, 'max_captures': 2})
        store.add_task('t1', 'tok1')
        bridge.call('screen_capture', {}, 'tok1')
        bridge.call('screen_capture', {}, 'tok1')
        result = bridge.call('screen_capture', {}, 'tok1')
        self.assertEqual(screen.call.call_count, 2)
        self.assertEqual(result['reason'], 'limit_reached')
        self.assertEqual(self.refused_events(store)[0][2]['reason'], 'limit_reached')

    def test_failed_capture_is_not_counted(self):
        bridge, store, screen = self.make(ON)
        store.add_task('t1', 'tok1')
        screen.call.side_effect = ValueError('window not found')
        with self.assertRaises(ValueError):
            bridge.call('screen_capture', {}, 'tok1')
        self.assertEqual(bridge.captures_taken('t1'), 0)

    def test_screen_windows_is_not_gated(self):
        bridge, store, screen = self.make({})
        store.add_task('t1', 'tok1')
        with mock.patch.object(plain_protocol, 'capture_gate') as gate:
            bridge.call('screen_windows', {}, 'tok1')
        gate.assert_not_called()
        screen.call.assert_called_once_with('screen_windows', {}, task_id='t1', run_id='r1', role='executor')

    # ---- capture_all_coworkers (default on) ------------------------------

    def test_default_settings_reach_the_policy_and_allow_an_empty_class(self):
        bridge, store, screen = self.make({})  # DB settings without any capture key
        store.add_task('t1', 'tok1', task_class='')
        with mock.patch.object(capture_policy, 'capture_allowed', wraps=capture_policy.capture_allowed) as policy:
            result = bridge.call('screen_capture', {}, 'tok1')
        self.assertEqual(result, {'text': 'captured'})
        policy.assert_called_once()
        cls, settings, taken = policy.call_args.args
        self.assertEqual((cls, taken), ('', 0))
        self.assertNotIn('capture_all_coworkers', settings)
        self.assertEqual(self.refused_events(store), [])

    def test_store_settings_are_passed_to_the_policy(self):
        bridge, store, screen = self.make({'capture_all_coworkers': True, 'max_captures': 5, 'theme': 'dark'})
        store.add_task('t1', 'tok1', task_class='code')
        with mock.patch.object(capture_policy, 'capture_allowed', wraps=capture_policy.capture_allowed) as policy:
            result = bridge.call('screen_capture', {}, 'tok1')
        self.assertEqual(result, {'text': 'captured'})
        settings = policy.call_args.args[1]
        self.assertEqual((settings['capture_all_coworkers'], settings['max_captures'], settings['theme']), (True, 5, 'dark'))

    def test_run_project_settings_can_switch_it_off(self):
        bridge, store, screen = self.make({})
        store.add_task('t1', 'tok1', task_class='')
        store.runs['r1']['project_settings'] = {'capture_all_coworkers': False}
        result = bridge.call('screen_capture', {}, 'tok1')
        screen.call.assert_not_called()
        self.assertEqual(result['reason'], 'class_not_visual')

    def test_default_limit_is_twelve_per_task(self):
        bridge, store, screen = self.make({})
        store.add_task('t1', 'tok1', task_class='')
        for _ in range(12):
            self.assertEqual(bridge.call('screen_capture', {}, 'tok1'), {'text': 'captured'})
        result = bridge.call('screen_capture', {}, 'tok1')
        self.assertEqual(screen.call.call_count, 12)
        self.assertEqual(result['reason'], 'limit_reached')
        self.assertEqual(bridge.captures_taken('t1'), 12)

    def test_refusal_text_is_readable_english_with_the_reason(self):
        bridge, store, screen = self.make({'max_captures': 0})
        store.add_task('t1', 'tok1', task_class='')
        result = bridge.call('screen_capture', {}, 'tok1')
        text = result['text']
        self.assertEqual(result['reason'], 'limit_reached')
        self.assertTrue(text.startswith('Screen capture refused:'), text)
        self.assertIn('maximum number of captures', text)
        self.assertTrue(text.endswith('No capture was taken.'), text)
        self.assertNotIn('_', text)
        self.assertEqual(self.refused_events(store)[0][2]['message'], text)


if __name__ == '__main__':
    unittest.main()
