"""End-to-end scenarios in the test app (port 8791) against the simulated Notion (fake_notion.py).

Each scenario starts a fresh fake-mode test app from this source tree, restarts it once and checks the
events and the simulated chats. The real NotionTool is never started, stopped or contacted.

Every call into the test app runs under a hard deadline (_with_deadline): a hang fails that test with the
stack of the stuck call instead of blocking the whole run. A faulthandler watchdog is the last resort for
the module as a whole.

Plain unittest: python -m unittest tests.test_test_app_scenarios
"""
import faulthandler
import json
import os
import sys
import threading
import time
import traceback
import unittest

from notiontool import test_app

RESULTS = {}
# Hard limit per scenario: its own wait, two boots (fresh start + restart) and the bounded stops around them.
STOP_TIMEOUT = 90
HARD_TIMEOUT = {name: seconds + 2 * test_app.BOOT_SECONDS + 3 * 45
                for name, seconds in test_app.SCENARIO_SECONDS.items()}
MODULE_WATCHDOG = 40 * 60  # dumps every stack and exits if the module itself ever blocks


def _with_deadline(label, seconds, func, *args, **kw):
    """Run func in a daemon thread; fail (AssertionError) with the stuck stack if it is not done in seconds."""
    box = {}

    def target():
        try:
            box['value'] = func(*args, **kw)
        except BaseException as exc:  # handed to the caller below
            box['error'] = exc

    worker = threading.Thread(target=target, name='deadline-' + label, daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        frame = sys._current_frames().get(worker.ident)
        stack = ''.join(traceback.format_stack(frame)) if frame is not None else '(no stack)'
        test_app.HALT.set()  # the stuck call may still run: start() now opens no process until setUp clears it
        if label != 'stop':
            try:
                _with_deadline('stop', STOP_TIMEOUT, test_app.stop)  # free port and processes for the next test
            except AssertionError:
                pass
        raise AssertionError('%s did not finish within %d s (hang). Stack of the stuck call:\n%s'
                             % (label, seconds, stack[-4000:]))
    if 'error' in box:
        raise box['error']
    return box.get('value')


def _stop():
    return _with_deadline('stop', STOP_TIMEOUT, test_app.stop)


def setUpModule():
    RESULTS.clear()
    try:
        faulthandler.dump_traceback_later(MODULE_WATCHDOG, exit=True)
    except (RuntimeError, ValueError, OSError):
        pass  # no real stderr (captured): the per-test deadlines still apply


def tearDownModule():
    try:
        _stop()
    finally:
        faulthandler.cancel_dump_traceback_later()
    if not os.environ.get('NT_SCENARIO_SUMMARY'):  # opt-in: a plain test run writes nothing into work/ or data/
        return
    root, _, _, _ = test_app._paths()
    summary = {name: dict(verdict=r['verdict'], reasons=r['reasons'], seconds=r.get('seconds'),
                          events=[e['kind'] for e in r['events']], details=r.get('details'))
               for name, r in RESULTS.items()}
    (root / 'work' / 'test-app-scenarios.json').write_text(json.dumps(summary, indent=1), encoding='utf-8')


class TestAppScenarios(unittest.TestCase):
    """One method per scenario in test_app.SCENARIOS; only the test app (port 8791) is used."""

    def setUp(self):
        _stop()  # only test-app processes (own handles + work/test-app.json); never the real app
        test_app.HALT.clear()  # a timeout in an earlier test set the stop flag

    def tearDown(self):
        _stop()

    def _scenario_passes(self, name):
        result = _with_deadline('scenario ' + name, HARD_TIMEOUT[name], test_app.scenario, name)
        RESULTS[name] = result
        self.assertEqual(result['scenario'], name)
        self.assertEqual(result['verdict'], 'passed', json.dumps(dict(
            reasons=result['reasons'], events=result['events'], details=result.get('details'),
            log_tail=result.get('log_tail', '')[-1500:]), indent=1))
        self.assertTrue(result['events'], 'a passed scenario must report the events it saw')
        self.assertEqual(test_app._PROCS, {}, 'scenario() must leave no test-app process behind')
        return result

    def test_scenario_list(self):
        self.assertEqual(tuple(test_app.SCENARIOS), ('restart_cut', 'too_large', 'deleted_chat', 'wake'))

    def test_restart_cut(self):
        self._scenario_passes('restart_cut')

    def test_too_large(self):
        result = self._scenario_passes('too_large')
        self.assertEqual(result['details'].get('circuit_open_check'), 'checked',
                         'the simulated connection must pass the real NetworkBudget')

    def test_deleted_chat(self):
        self._scenario_passes('deleted_chat')

    def test_wake(self):
        self._scenario_passes('wake')


class TestPlainTestInstance(unittest.TestCase):

    def setUp(self):
        _stop()
        test_app.HALT.clear()

    def tearDown(self):
        _stop()

    def test_plain_test_instance_has_no_simulated_notion(self):
        started = _with_deadline('start', test_app.BOOT_SECONDS + 60, test_app.start, fake=False)
        proc = test_app._PROCS.get(started.get('pid'))
        try:
            self.assertTrue(started['ok'], started)
            self.assertIs(started['fake_notion'], False)
            self.assertNotEqual(started['port'], test_app.REAL_PORT)
            time.sleep(3)
            _, data, _, _ = test_app._paths()
            kinds = [e['kind'] for e in test_app._events(data)]
            self.assertIn('test_instance_started', kinds)
            self.assertNotIn(test_app.FAKE_READY_EVENT, kinds)
            self.assertFalse((data / 'fake_notion.json').exists())
            self.assertIsNone(test_app._object(data, 'accounts', 'ready'))
        finally:
            stopped = _stop()
        # stop() ended and reaped every process it started, closed their pipes and freed the port.
        self.assertTrue(stopped['stopped'], stopped)
        self.assertTrue(stopped['port_free'], stopped)
        self.assertIn(started['pid'], stopped['pids'])
        self.assertEqual(test_app._PROCS, {})
        self.assertIsNotNone(proc)
        self.assertIsNotNone(proc.poll(), 'the test app process must be gone after stop()')

    def test_unknown_scenario_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'scenario name'):
            test_app.scenario('explode')


class BootGuard(unittest.TestCase):
    def test_simulated_mode_is_refused_outside_the_test_instance(self):
        class RealApp:
            test_instance = False
        with self.assertRaisesRegex(RuntimeError, 'only the test app'):
            test_app.boot_fake_notion(RealApp())

    def test_run_flags(self):
        self.assertIs(test_app._flag('false', True), False)
        self.assertIs(test_app._flag(None, True), True)
        self.assertIs(test_app._flag(1, False), True)


if __name__ == '__main__':
    unittest.main()
