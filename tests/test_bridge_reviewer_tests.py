import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from notiontool import bridge as bridge_module
from notiontool.bridge import Bridge, read_only_test_modules, shell_command, unittest_python

TOKEN = 'tok-reviewer'

BYPASSES = [
    '',
    'whoami',
    'python -m unittest',
    'python -m unittest tests.test_a; whoami',
    'python -m unittest tests.test_a | Out-File x.txt',
    'python -m unittest tests.test_a > out.txt',
    'python -m unittest tests.test_a 2>&1',
    'python -m unittest tests.test_a < in.txt',
    'python -m unittest tests.test_a && del x',
    'python -m unittest tests.`whoami`',
    'python -m unittest $(calc)',
    'python -m unittest tests.test_a' + chr(10) + 'whoami',
    'python -m unittest -v tests.test_a',
    'python -m unittest tests.test_a -k x',
    'python -c print(1)',
    'python -m pip install requests',
    'C:/evil/python.exe -m unittest tests.test_a',
    'python -m unittest ' + chr(34) + 'tests.test_a' + chr(34),
    'python -m unittest tests/test_a.py',
    'python -m unittest tests.test_a.SomeTest',
    'python -m unittest ' + ' '.join('tests.test_%d' % i for i in range(21)),
    'python -m unittest tests.t' + chr(235) + 'st',
]


class FakeStore:
    def __init__(self, task, run, settings=None):
        self.task, self.run, self._settings = task, run, settings or {}
        self.events = []

    def settings(self):
        return self._settings

    def all(self, kind):
        return [self.task] if kind == 'tasks' else []

    def get(self, kind, ident, default=None):
        if kind == 'runs' and ident == self.run['id']:
            return self.run
        if kind == 'tasks' and ident == self.task['id']:
            return self.task
        return default

    def event(self, kind, *args, **fields):
        self.events.append((kind, args, fields))


class FakeEngine:
    def __init__(self):
        self.lock = threading.RLock()
        self.leases, self.files, self.absolute = [], [], []

    def check_lease(self, task_id, token):
        self.leases.append((task_id, token))

    def claim_file(self, task_id, token, path):
        self.files.append(path)

    def claim_absolute(self, task_id, token, path):
        self.absolute.append(path)


class ReviewerUnittestShellTests(unittest.TestCase):
    # Read-only roles may run exactly python -m unittest tests.<module> ...; Popen is stubbed.

    def setUp(self):
        base = Path(__file__).resolve().parent / '.test-data'
        base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.root = (Path(self.temp.name) / 'project').resolve()
        (self.root / 'tests').mkdir(parents=True)
        self.popen = mock.patch.object(bridge_module.subprocess, 'Popen').start()
        self.addCleanup(mock.patch.stopall)
        process = self.popen.return_value
        process.communicate.return_value = (b'Ran 1 test OK', None)
        process.returncode = 0
        process.pid = 4242
        self.make('reviewer')

    def tearDown(self):
        self.temp.cleanup()

    def make(self, role, settings=None):
        self.task = dict(id='t1', run_id='r1', role=role, state='running', lease=TOKEN, worker_id='w1', title='Review')
        self.run = dict(id='r1', chat_id='c1', state='reviewing', project_path=str(self.root), goal='g')
        self.store = FakeStore(self.task, self.run, settings)
        self.engine = FakeEngine()
        self.bridge = Bridge(store=self.store, engine=self.engine)

    def shell(self, command, **extra):
        return self.bridge.call('run_shell', dict(command=command, **extra), TOKEN)

    def test_exactly_22_distinct_bypass_cases(self):
        self.assertEqual(len(BYPASSES), 22)
        self.assertEqual(len(set(BYPASSES)), 22)

    def test_every_bypass_refused_before_any_process(self):
        for command in BYPASSES:
            with self.subTest(command=command):
                self.assertIsNone(read_only_test_modules(command))
                with self.assertRaisesRegex(ValueError, 'read-only PC access'):
                    self.shell(command)
        self.popen.assert_not_called()
        self.assertEqual(self.engine.leases, [])
        self.assertEqual(self.store.events, [])

    def test_non_string_command_refused(self):
        for command in (None, 5, ['python', '-m', 'unittest', 'tests.test_a']):
            with self.subTest(command=command):
                self.assertIsNone(read_only_test_modules(command))
                with self.assertRaisesRegex(ValueError, 'read-only PC access'):
                    self.bridge.call('run_shell', dict(command=command), TOKEN)
        self.popen.assert_not_called()

    def test_allowed_form_runs_without_shell_in_root(self):
        result = self.shell('python -m unittest tests.test_a tests.test_b', cwd='..', timeout=30)
        self.assertEqual(result['exit_code'], 0)
        self.assertIn('Ran 1 test', result['output'])
        self.popen.assert_called_once()
        args, kwargs = self.popen.call_args
        self.assertEqual(args[0], [unittest_python(), '-m', 'unittest', 'tests.test_a', 'tests.test_b'])
        self.assertIs(kwargs['shell'], False)
        self.assertEqual(kwargs['cwd'], str(self.root))
        self.assertEqual(kwargs['env']['PYTHONPATH'], os.pathsep.join([str(self.root), str(self.root / 'tests')]))
        self.popen.return_value.communicate.assert_called_once_with(timeout=30)
        self.assertEqual(self.engine.leases, [('t1', TOKEN)])
        self.assertEqual(self.engine.absolute, [])
        self.assertEqual([e[0] for e in self.store.events], ['tool_started', 'tool_finished'])

    def test_twenty_modules_allowed(self):
        modules = ['tests.test_%d' % i for i in range(20)]
        self.assertEqual(read_only_test_modules('python -m unittest ' + ' '.join(modules)), modules)

    def test_timeout_capped_at_120(self):
        self.shell('python -m unittest tests.test_a', timeout=999)
        self.popen.return_value.communicate.assert_called_once_with(timeout=120)

    def test_cap_and_root_stay_with_full_pc_access(self):
        self.make('reviewer', {'mcp_full_access': True})
        self.shell('python -m unittest tests.test_a', timeout=600, cwd='C:/')
        self.popen.return_value.communicate.assert_called_once_with(timeout=120)
        self.assertEqual(self.popen.call_args[1]['cwd'], str(self.root))
        self.assertEqual(self.engine.absolute, [])

    def test_write_file_still_refused(self):
        with self.assertRaisesRegex(ValueError, 'read-only PC access'):
            self.bridge.call('write_file', dict(path='x.txt', text='hi'), TOKEN)
        self.assertFalse((self.root / 'x.txt').exists())
        self.assertEqual(self.engine.files, [])
        self.popen.assert_not_called()

    def test_browser_action_still_refused(self):
        self.make('reviewer', {'mcp_browser_access': True})
        with self.assertRaisesRegex(ValueError, 'read-only PC access'):
            self.bridge.call('browser_open', dict(url='https://example.com'), TOKEN)
        self.popen.assert_not_called()

    def test_every_read_only_role_gets_the_same_gate(self):
        for role in bridge_module.READ_ONLY_ROLES:
            with self.subTest(role=role):
                self.make(role)
                with self.assertRaisesRegex(ValueError, 'read-only PC access'):
                    self.shell('whoami')
                self.shell('python -m unittest tests.test_a')
        self.assertEqual(self.popen.call_count, len(bridge_module.READ_ONLY_ROLES))

    def test_writing_role_keeps_shell_path(self):
        self.make('executor')
        command = 'Get-ChildItem | Select-Object -First 1'
        self.shell(command, timeout=999)
        args, kwargs = self.popen.call_args
        self.assertEqual(args[0], shell_command(command))
        self.assertEqual(set(kwargs), {'cwd', 'stdout', 'stderr', 'creationflags'})
        self.assertEqual(Path(kwargs['cwd']), self.root)
        self.popen.return_value.communicate.assert_called_once_with(timeout=120)

    def test_frozen_exe_uses_a_real_python(self):
        self.assertEqual(unittest_python(), sys.executable)
        with mock.patch.object(sys, 'frozen', True, create=True), \
                mock.patch.object(bridge_module.shutil, 'which', return_value='C:/Python/python.exe'):
            self.assertEqual(unittest_python(), 'C:/Python/python.exe')
        with mock.patch.object(sys, 'frozen', True, create=True), \
                mock.patch.object(bridge_module.shutil, 'which', return_value=None):
            with self.assertRaises(ValueError):
                unittest_python()


if __name__ == '__main__':
    unittest.main()
