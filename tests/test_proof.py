"""Proof record per finished task (notiontool/proof.py) plus its engine and Live view wiring."""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from notiontool import proof

ROOT = Path(__file__).resolve().parents[1]


class ParseTests(unittest.TestCase):
    def test_ok_run(self):
        text = 'Done.\n----------------------------------------------------------------------\nRan 17 tests in 0.412s\n\nOK\n'
        self.assertEqual(proof.parse_tests(text), [dict(ran=17, status='passed', failures=0, errors=0, skipped=0)])
        self.assertEqual(proof.tests_status(proof.parse_tests(text)), 'passed')

    def test_ok_with_skips(self):
        runs = proof.parse_tests('Ran 4 tests in 1.0s\n\nOK (skipped=2)')
        self.assertEqual(runs[0]['status'], 'passed')
        self.assertEqual(runs[0]['skipped'], 2)

    def test_failed_run_counts(self):
        runs = proof.parse_tests('Ran 180 tests in 9.1s\n\nFAILED (failures=13, errors=1)')
        self.assertEqual(runs, [dict(ran=180, status='failed', failures=13, errors=1, skipped=0)])
        self.assertEqual(proof.tests_status(runs), 'failed')

    def test_several_runs_any_failure_is_red(self):
        text = 'Ran 3 tests in 0.1s\nOK\nthen\nRan 5 tests in 0.2s\nFAILED (errors=2)\n'
        runs = proof.parse_tests(text)
        self.assertEqual([r['status'] for r in runs], ['passed', 'failed'])
        self.assertEqual(proof.tests_status(runs), 'failed')

    def test_escaped_newlines_in_result_text(self):
        runs = proof.parse_tests('{"summary": "Ran 2 tests in 0.01s\\n\\nOK"}')
        self.assertEqual(runs[0]['status'], 'passed')

    def test_unclear_and_missing(self):
        runs = proof.parse_tests('Ran 9 tests in 2s and then the output stopped')
        self.assertEqual(runs[0]['status'], 'unknown')
        self.assertEqual(proof.tests_status(runs), 'not_checked')
        self.assertEqual(proof.parse_tests('No tests were run.'), [])
        self.assertEqual(proof.tests_status([]), 'not_checked')
        self.assertEqual(proof.parse_tests(None), [])


class ScreenshotTests(unittest.TestCase):
    def test_windows_path(self):
        text = 'All good.\nScreenshot: C:\\Users\\me\\work\\capture-check\\live.png\n'
        self.assertEqual(proof.parse_screenshot(text), 'C:\\Users\\me\\work\\capture-check\\live.png')

    def test_relative_path(self):
        self.assertEqual(proof.parse_screenshot('Saved a screen capture to work/shots/proof.jpg.'), 'work/shots/proof.jpg')

    def test_image_without_screenshot_word_is_ignored(self):
        self.assertEqual(proof.parse_screenshot('Changed icons/logo.png and tests.'), '')
        self.assertEqual(proof.parse_screenshot(''), '')


class LineCountTests(unittest.TestCase):
    def test_counts(self):
        self.assertEqual(proof.line_counts(b'a\nb\nc\n', b'a\nB\nc\nd\n'), (2, 1))
        self.assertEqual(proof.line_counts(None, b'x\ny\n'), (2, 0))
        self.assertEqual(proof.line_counts(b'x\ny\n', None), (0, 2))

    def test_line_ending_change_alone_counts_nothing(self):
        self.assertEqual(proof.line_counts(b'a\r\nb\r\n', b'a\nb\n'), (0, 0))

    def test_binary(self):
        self.assertIsNone(proof.line_counts(b'\x89PNG\0\0', b'\x89PNG\0\1'))


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='nt-proof-')
        self.store = os.path.join(self.root, 'work', 'proof-snapshots')
        self.write('a.py', 'one\ntwo\nthree\n')
        self.write('gone.py', 'x\ny\n')
        self.write('outside.py', 'keep\n')
        self.write('same.py', 'same\n')

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, rel, text):
        path = os.path.join(self.root, *rel.split('/'))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8', newline='') as handle:
            handle.write(text)

    def test_full_record(self):
        task = dict(id='t1', files=['a.py', 'gone.py', 'new/b.py', 'same.py'])
        self.assertTrue(proof.start(task['id'], task['files'], self.root, store_dir=self.store))
        self.write('a.py', 'one\nTWO\nthree\nfour\n')
        os.remove(os.path.join(self.root, 'gone.py'))
        self.write('new/b.py', 'b1\nb2\nb3\n')
        self.write('outside.py', 'changed but not in scope\n')
        # A file claimed later gets its own base before it is written.
        task['files'].append('later.py')
        self.write('later.py', 'l1\n')
        self.assertTrue(proof.extend(task['id'], 'later.py', self.root, store_dir=self.store))
        self.write('later.py', 'l1\nl2\n')
        # A file added to the scope without a base is reported as not checked.
        task['files'].append('nobase.py')
        self.write('nobase.py', 'n\n')
        text = 'Ran 6 tests in 0.2s\n\nOK\nScreenshot: work/capture-check/live.png'
        rec = proof.record(task, text, self.root, store_dir=self.store, clock=lambda: 123.0)
        self.assertTrue(rec['files_checked'])
        files = {f['path']: f for f in rec['files']}
        self.assertEqual(sorted(files), ['a.py', 'gone.py', 'later.py', 'new/b.py'])
        self.assertEqual((files['a.py']['change'], files['a.py']['added'], files['a.py']['removed']), ('changed', 2, 1))
        self.assertEqual((files['gone.py']['change'], files['gone.py']['removed']), ('removed', 2))
        self.assertEqual((files['new/b.py']['change'], files['new/b.py']['added']), ('added', 3))
        self.assertEqual((files['later.py']['added'], files['later.py']['removed']), (1, 0))
        self.assertNotIn('outside.py', files)
        self.assertNotIn('same.py', files)
        self.assertEqual(rec['unknown'], ['nobase.py'])
        self.assertEqual(rec['tests_status'], 'passed')
        self.assertEqual(rec['screenshot'], 'work/capture-check/live.png')
        self.assertEqual(rec['created'], 123.0)

    def test_base_is_not_retaken(self):
        proof.start('t2', ['a.py'], self.root, store_dir=self.store)
        self.write('a.py', 'one\n')
        self.assertTrue(proof.extend('t2', 'a.py', self.root, store_dir=self.store))  # already in base: kept
        rec = proof.record(dict(id='t2', files=['a.py']), '', self.root, store_dir=self.store)
        self.assertEqual(rec['files'][0]['removed'], 2)

    def test_no_snapshot_means_not_checked(self):
        rec = proof.record(dict(id='never-started', files=['a.py']), 'no tests', self.root, store_dir=self.store)
        self.assertFalse(rec['files_checked'])
        self.assertEqual(rec['files'], [])
        self.assertEqual(rec['tests_status'], 'not_checked')
        self.assertEqual(rec['screenshot'], '')

    def test_paths_outside_root_are_skipped(self):
        self.assertTrue(proof.start('t3', ['../evil.py', 'a.py'], self.root, store_dir=self.store))
        rec = proof.record(dict(id='t3', files=['../evil.py', 'a.py']), '', self.root, store_dir=self.store)
        self.assertTrue(rec['files_checked'])
        self.assertEqual(rec['files'], [])

    def test_never_raises(self):
        for task, text, root in [(None, None, None), ('x', 42, None), (dict(id='t4', files=None), b'bytes', self.root)]:
            rec = proof.record(task, text, root, store_dir=self.store)
            self.assertEqual(rec['tests_status'], 'not_checked')
        self.assertFalse(proof.extend('t5', '../x', self.root, store_dir=self.store))


class WiringTests(unittest.TestCase):
    def source(self, rel):
        return (ROOT / rel).read_text(encoding='utf-8')

    def test_engine_takes_base_and_stores_record(self):
        src = self.source('notiontool/engine.py')
        self.assertIn('from . import proof as task_proof', src)
        dispatch = src[src.index('def _dispatch('):]
        dispatch = dispatch[:dispatch.index('\n    def ')]
        self.assertIn('task_proof.start(', dispatch)
        self.assertLess(dispatch.index('task_proof.start('), dispatch.index('Commit the submission marker'))
        self.assertIn('task_proof.extend(', src)
        complete = src[src.index('def _complete('):]
        complete = complete[:complete.index('\n    def ')]
        self.assertIn("task['proof'] = task_proof.record(", complete)

    def test_proof_reaches_state(self):
        from notiontool import desktop_state_cache
        self.assertNotIn('proof', desktop_state_cache.HEAVY_TASK_KEYS)
        server = self.source('notiontool/server.py')
        hidden = re.search(r"if k not in \(([^)]*)\)", server[server.index('def safe_task'):]).group(1)
        self.assertNotIn("'proof'", hidden)

    def test_live_view_shows_proof(self):
        js = self.source('notiontool/web/desktop/live.js')
        for text in ("text: 'Proof'", "'Not checked'", "'No changes'", 'proofDetail(t.proof)', "' (new)'", "' (deleted)'",
                     "'Screenshot: '", 'nt-desk-lv-proof-'):
            self.assertIn(text, js)
        css = self.source('notiontool/web/desktop/live.css')
        for cls in ('.nt-desk-lv-proof-pass', '.nt-desk-lv-proof-fail', '.nt-desk-lv-proof-none'):
            self.assertIn(cls, css)

    def test_live_js_syntax(self):
        node = shutil.which('node') or r'C:\Program Files\nodejs\node.exe'
        if not os.path.exists(node):
            self.skipTest('node not found')
        result = subprocess.run([node, '--check', str(ROOT / 'notiontool/web/desktop/live.js')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_line_endings_lf(self):
        for rel in ('notiontool/proof.py', 'notiontool/engine.py', 'notiontool/web/desktop/live.js', 'notiontool/web/desktop/live.css'):
            self.assertNotIn(b'\r', (ROOT / rel).read_bytes(), rel)


if __name__ == '__main__':
    unittest.main()
