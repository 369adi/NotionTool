"""Import guard: every module of the notiontool package must import.

A module that is missing from the EXE (hand-typed hidden imports) or that breaks at import time is
caught here and, for the built EXE, by the hidden --self-test-imports flag run in release.yml.
"""

import importlib
import importlib.util
import pkgutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_main():
    spec = importlib.util.spec_from_file_location('notiontool_main_entry', ROOT / 'main.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ImportAllTests(unittest.TestCase):
    def test_every_module_imports(self):
        package = importlib.import_module('notiontool')
        names = [info.name for info in pkgutil.walk_packages(package.__path__, 'notiontool.')]
        self.assertGreater(len(names), 50)
        failed = []
        for name in names:
            try:
                importlib.import_module(name)
            except BaseException as exc:
                failed.append(f'{name}: {type(exc).__name__}: {exc}')
        self.assertEqual([], failed)

    def test_every_source_file_is_found_by_the_walk(self):
        walked = {info.name for info in pkgutil.walk_packages(importlib.import_module('notiontool').__path__, 'notiontool.')}
        files = set()
        for path in (ROOT / 'notiontool').rglob('*.py'):
            relative = path.relative_to(ROOT).with_suffix('')
            if '__pycache__' in relative.parts or relative.name == '__main__':
                continue
            parts = relative.parts[:-1] if relative.name == '__init__' else relative.parts
            if len(parts) > 1 and all((ROOT.joinpath(*parts[:i], '__init__.py')).exists() for i in range(1, len(parts))):
                files.add('.'.join(parts))
        self.assertEqual([], sorted(files - walked))

    def test_flag_exits_zero_from_source(self):
        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder) / 'report.txt'
            result = subprocess.run([sys.executable, str(ROOT / 'main.py'), '--self-test-imports', str(report)],
                                    cwd=ROOT, capture_output=True, text=True, timeout=180)
            text = report.read_text(encoding='utf-8')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotIn('FAILED', text)
        self.assertIn('imported', text)

    def test_flag_names_the_failing_module(self):
        main = load_main()
        real = importlib.import_module

        def broken(name):
            if name == 'notiontool.dag':
                raise ImportError('boom')
            return real(name)

        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder) / 'report.txt'
            code = main.self_test_imports(str(report), importer=broken)
            text = report.read_text(encoding='utf-8')
        self.assertNotEqual(0, code)
        self.assertIn('FAILED notiontool.dag: ImportError: boom', text)

    def test_spec_and_workflow_are_wired(self):
        spec = (ROOT / 'NotionTool.spec').read_text(encoding='utf-8')
        self.assertIn("collect_submodules('notiontool')", spec)
        self.assertNotIn("'notiontool.autopilot'", spec)
        for kept in ('webview.platforms.edgechromium', 'PIL.ImageGrab'):
            self.assertIn(kept, spec)
        workflow = (ROOT / '.github' / 'workflows' / 'release.yml').read_text(encoding='utf-8')
        self.assertIn('--self-test-imports', workflow)
        self.assertLess(workflow.index('Build EXE'), workflow.index('--self-test-imports'))
        self.assertLess(workflow.index('--self-test-imports'), workflow.index('Publish release'))


if __name__ == '__main__':
    unittest.main()
