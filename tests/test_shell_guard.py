"""nt_shell guard: long inline code and hidden cmd launches are blocked before PowerShell runs."""
import unittest
from unittest import mock

from notiontool import devtools
from notiontool.devtools import INLINE_MAX, SHELL_BLOCKED, shell_guard

CODE = "import pathlib\n" + "pathlib.Path('out.txt').write_text('x')\n" * 12

BLOCKED = {
    'single-quoted here-string piped to python': "$code = @'\n" + CODE + "'@\n$code | python - 2>&1 | Out-String",
    'double-quoted here-string': '$t = @"\n' + 'x' * (INLINE_MAX + 50) + '\n"@\nSet-Content a.txt $t',
    'unclosed here-string': "@'\n" + 'y' * (INLINE_MAX + 10),
    'python -c with long code': 'python -c "' + 'import os; ' * 45 + '"',
    'py -3 -c with long code': "py -3 -c '" + 'print(1); ' * 45 + "'",
    'long string piped to python -': "'" + 'z' * (INLINE_MAX + 20) + "' | python -",
    'long string piped to python.exe path': "'" + 'z' * (INLINE_MAX + 20) + "' | & 'C:/Py312/python.exe' - 2>&1",
    'hidden cmd /c': "Start-Process -WindowStyle Hidden cmd.exe -ArgumentList '/c', 'python -m pytest -q > work/t.log 2>&1'",
    'hidden cmd /c short form': 'Start-Process cmd "/c python -m pytest" -WindowStyle Hidden',
    'hidden powershell -Command': "Start-Process powershell -WindowStyle Hidden -ArgumentList '-NoProfile','-Command','python -m pytest'",
    'hidden pwsh -EncodedCommand': 'Start-Process pwsh -WindowStyle Hidden -ArgumentList "-EncodedCommand AAAA"',
    'flagged Defender pattern': "$code = @'\n" + CODE + "'@\n$code | python -; Start-Process -WindowStyle Hidden cmd /c 'python -m pytest -q'",
}

ALLOWED = {
    'git': 'git status --short; git log --oneline -5',
    'pytest': 'python -m pytest -q tests/test_shell_guard.py tests/test_repo_eol.py',
    'select-string': "Select-String -Path notiontool/*.py -Pattern 'def call' | Select-Object -First 5",
    'hidden powershell -File script': "Start-Process powershell -WindowStyle Hidden -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','work/run-tests.ps1'",
    'hidden program without code': 'Start-Process -WindowStyle Hidden notepad.exe',
    'visible cmd /c': "Start-Process cmd -ArgumentList '/c', 'dir'",
    'plain cmd /c': 'cmd /c dir',
    'short here-string': "$m = @'\nfix: guard shell\n'@\ngit commit -m $m",
    'short python -c': 'python -c "import sys; print(sys.version)"',
    'short pipe into python -': "'print(1)' | python -",
    'long harmless command': 'git log --oneline ' + ' '.join(['--author=someone'] * 40),
    'mypy is not python': 'mypy -c ' + 'x' * (INLINE_MAX + 10),
}


class ShellGuardTest(unittest.TestCase):
    def test_blocked_examples(self):
        for label, command in BLOCKED.items():
            with self.subTest(label):
                self.assertEqual(shell_guard(command), SHELL_BLOCKED)

    def test_allowed_examples(self):
        for label, command in ALLOWED.items():
            with self.subTest(label):
                self.assertIsNone(shell_guard(command))

    def test_here_string_limit(self):
        self.assertIsNone(shell_guard("$t = @'\n" + 'a' * INLINE_MAX + "\n'@"))
        self.assertEqual(shell_guard("$t = @'\n" + 'a' * (INLINE_MAX + 1) + "\n'@"), SHELL_BLOCKED)

    def test_crlf_here_string(self):
        command = "$c = @'\r\n" + 'b' * (INLINE_MAX + 5) + "\r\n'@\r\n$c | python -"
        self.assertEqual(shell_guard(command), SHELL_BLOCKED)

    def test_empty_command(self):
        self.assertIsNone(shell_guard(''))
        self.assertIsNone(shell_guard(None))

    def test_message_text(self):
        self.assertEqual(SHELL_BLOCKED, 'Blocked: write files with nt_write_file and run tests with python -m pytest '
                                        'directly. Long inline code in shell commands triggers antivirus false alarms.')

    def test_description_points_to_nt_write_file(self):
        desc = next(t for t in devtools.TOOLS if t['name'] == 'nt_shell')['description']
        self.assertIn('Files must be written with nt_write_file', desc)

    def test_handler_rejects_before_running(self):
        with mock.patch.object(devtools.subprocess, 'run') as run:
            with self.assertRaises(ValueError) as cm:
                devtools.call('nt_shell', {'command': BLOCKED['hidden cmd /c']})
        self.assertEqual(str(cm.exception), SHELL_BLOCKED)
        run.assert_not_called()

    def test_handler_runs_short_command(self):
        done = mock.Mock(returncode=0, stdout=b'ok', stderr=b'')
        with mock.patch.object(devtools.subprocess, 'run', return_value=done) as run:
            out = devtools.call('nt_shell', {'command': 'git status --short'})
        self.assertEqual((out['exit_code'], out['stdout']), (0, 'ok'))
        run.assert_called_once()
        self.assertTrue(run.call_args[0][0][-1].endswith('git status --short'))


if __name__ == '__main__':
    unittest.main()
