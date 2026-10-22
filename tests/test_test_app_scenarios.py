"""End-to-end scenarios in the test app (port 8791) against the simulated Notion (fake_notion.py).

Each scenario starts a fresh fake-mode test app from this source tree, restarts it once and checks the
events and the simulated chats. The real NotionTool is never started, stopped or contacted.

Plain unittest: python -m unittest tests.test_test_app_scenarios
"""
import json
import time
import unittest

from notiontool import test_app

RESULTS = {}


def setUpModule():
    RESULTS.clear()


def tearDownModule():
    test_app.stop()
    root, _, _, _ = test_app._paths()
    summary = {name: dict(verdict=r['verdict'], reasons=r['reasons'], seconds=r.get('seconds'),
                          events=[e['kind'] for e in r['events']], details=r.get('details'))
               for name, r in RESULTS.items()}
    (root / 'work' / 'test-app-scenarios.json').write_text(json.dumps(summary, indent=1), encoding='utf-8')


class TestAppScenarios(unittest.TestCase):
    """One method per scenario in test_app.SCENARIOS; only the test app (port 8791) is used."""

    def setUp(self):
        test_app.stop()  # only the test app recorded in work/test-app.json; never the real app

    def tearDown(self):
        test_app.stop()

    def _scenario_passes(self, name):
        result = test_app.scenario(name)
        RESULTS[name] = result
        self.assertEqual(result['scenario'], name)
        self.assertEqual(result['verdict'], 'passed', json.dumps(dict(
            reasons=result['reasons'], events=result['events'], details=result.get('details'),
            log_tail=result.get('log_tail', '')[-1500:]), indent=1))
        self.assertTrue(result['events'], 'a passed scenario must report the events it saw')

    def test_scenario_list(self):
        self.assertEqual(tuple(test_app.SCENARIOS), ('restart_cut', 'too_large', 'deleted_chat', 'wake'))

    def test_restart_cut(self):
        self._scenario_passes('restart_cut')

    def test_too_large(self):
        self._scenario_passes('too_large')

    def test_deleted_chat(self):
        self._scenario_passes('deleted_chat')

    def test_wake(self):
        self._scenario_passes('wake')


class TestPlainTestInstance(unittest.TestCase):

    def setUp(self):
        test_app.stop()

    def tearDown(self):
        test_app.stop()

    def test_plain_test_instance_has_no_simulated_notion(self):
        started = test_app.start(fake=False)
        try:
            self.assertTrue(started['ok'], started)
            self.assertIs(started['fake_notion'], False)
            time.sleep(3)
            _, data, _, _ = test_app._paths()
            kinds = [e['kind'] for e in test_app._events(data)]
            self.assertIn('test_instance_started', kinds)
            self.assertNotIn(test_app.FAKE_READY_EVENT, kinds)
            self.assertFalse((data / 'fake_notion.json').exists())
            self.assertIsNone(test_app._object(data, 'accounts', 'ready'))
        finally:
            test_app.stop()

    def test_unknown_scenario_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'scenario name'):
            test_app.scenario('explode')


if __name__ == '__main__':
    unittest.main()
