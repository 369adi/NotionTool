import tempfile, unittest
from pathlib import Path
from notiontool import devtools


class DevToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.old = devtools.ROOT, devtools.MEMORY
        devtools.ROOT, devtools.MEMORY = root, root / 'project-memory'
        (root / 'a.py').write_text('class A:\n    def run(self):\n        return 1\n', encoding='utf-8')

    def tearDown(self):
        devtools.ROOT, devtools.MEMORY = self.old
        self.temp.cleanup()

    def test_read_write_with_sha_guard(self):
        r = devtools.call('nt_read_file', {'path': 'a.py'})
        devtools.call('nt_write_file', {'path': 'a.py', 'content': 'x = 1\n', 'expected_sha256': r['sha256']})
        with self.assertRaises(ValueError):
            devtools.call('nt_write_file', {'path': 'a.py', 'content': 'y', 'expected_sha256': r['sha256']})

    def test_edit_requires_exact_count(self):
        with self.assertRaises(ValueError):
            devtools.call('nt_edit_file', {'path': 'a.py', 'old_text': 'missing', 'new_text': 'z'})
        self.assertEqual(devtools.call('nt_edit_file', {'path': 'a.py', 'old_text': 'return 1', 'new_text': 'return 2'})['replacements'], 1)

    def test_search_and_outline(self):
        self.assertTrue(devtools.call('nt_search_code', {'pattern': 'def run'})['results'][0].startswith('a.py:2:'))
        self.assertIn('2: def run', devtools.call('nt_file_outline', {'path': 'a.py'})['outline'])

    def test_memory_sections_replace_and_append(self):
        devtools.call('nt_memory_update', {'section': 'Status', 'content': 'one'})
        devtools.call('nt_memory_update', {'section': 'Status', 'content': 'two', 'append': True})
        devtools.call('nt_memory_update', {'section': 'Decisions', 'content': 'd1'})
        text = devtools.call('nt_memory_read', {})['content']
        self.assertIn('## Status\none\ntwo', text)
        self.assertIn('## Decisions\nd1', text)
        self.assertEqual(text.count('_Last updated'), 1)

    def test_shell_runs(self):
        self.assertEqual(devtools.call('nt_shell', {'command': 'echo hi'})['exit_code'], 0)


if __name__ == '__main__':
    unittest.main()
