"""notiontool.result_files: path, write/read, summary, 24 h prune."""
import os
import tempfile
import time
import unittest
from unittest import mock

from notiontool import result_files as rf

REPLY = '''I added the parser and its tests. Everything else stays as it was. Extra detail here.

- benchmarks/dur_a.py (1936 bytes)
- tests/test_dur_a.py

```
Ran 16 tests in 0.002s
OK
```

| a | b |
Some long explanation that is not needed in the summary.

RESULT: DONE
'''

LONG = 'Word ' * 400 + '.\nRESULT: NOT DONE: tests failed'


class ResultFilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(os.path.join(self.temp.name, 'results'))
        patcher = mock.patch.object(rf, 'ROOT', self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.temp.cleanup)

    def test_root_default_is_next_to_handoffs(self):
        with mock.patch.object(rf, 'ROOT', os.path.join(rf.data_dir(), 'results')):
            self.assertTrue(rf.path_for('r', 't').endswith(os.path.join('data', 'results', 'r--t.md')))

    def test_path_for_is_inside_root_and_safe(self):
        plain = rf.path_for('r1', 't1')
        self.assertEqual(plain, os.path.join(self.root, 'r1--t1.md'))
        odd = rf.path_for('acc:thread/../x', '..\\evil')
        self.assertEqual(os.path.dirname(odd), self.root)
        self.assertNotEqual(rf.path_for('a:b', 'x'), rf.path_for('a_b', 'x'))

    def test_write_read_round_trip(self):
        path = rf.write('r1', 't1', 'line one\r\nline two\näöü')
        self.assertEqual(path, rf.path_for('r1', 't1'))
        with open(path, 'rb') as fh:
            self.assertNotIn(b'\r', fh.read())
        self.assertEqual(rf.read(path), 'line one\nline two\näöü')
        self.assertEqual(rf.read(path, max_chars=8), 'line one')
        rf.write('r1', 't1', 'second')
        self.assertEqual(rf.read(path), 'second')
        self.assertEqual([n for n in os.listdir(self.root) if n.endswith('.tmp')], [])

    def test_write_redacts_secrets_and_checks_type(self):
        path = rf.write('r', 't', 'token_v2=abcdef123 and Bearer xyz.abc')
        text = rf.read(path)
        self.assertNotIn('abcdef123', text)
        self.assertNotIn('xyz.abc', text)
        with self.assertRaises(TypeError):
            rf.write('r', 't', b'bytes')

    def test_read_never_raises(self):
        self.assertEqual(rf.read(os.path.join(self.root, 'missing.md')), '')
        self.assertEqual(rf.read(None), '')
        outside = os.path.join(self.temp.name, 'outside.md')
        with open(outside, 'w', encoding='utf-8') as fh:
            fh.write('secret')
        self.assertEqual(rf.read(outside), '')
        os.makedirs(self.root)
        self.assertEqual(rf.read(self.root), '')

    def test_summary_keeps_key_lines(self):
        text = rf.summary_of(REPLY)
        self.assertLessEqual(len(text), 600)
        self.assertTrue(text.startswith('I added the parser and its tests. Everything else stays as it was.'))
        self.assertNotIn('Extra detail', text)
        self.assertIn('benchmarks/dur_a.py', text)
        self.assertIn('tests/test_dur_a.py', text)
        self.assertIn('Ran 16 tests', text)
        self.assertTrue(text.endswith('RESULT: DONE'))
        self.assertNotIn('```', text)

    def test_summary_keeps_outcome_sentence_naming_files(self):
        reply = 'I created notiontool/a.py and tests/test_a.py. The tests pass.\n\nRESULT: DONE\n'
        text = rf.summary_of(reply)
        self.assertTrue(text.startswith('I created notiontool/a.py'), text)
        self.assertIn('The tests pass.', text)
        self.assertIn('Files: notiontool/a.py, tests/test_a.py', text)
        self.assertTrue(text.endswith('RESULT: DONE'))

    def test_summary_limit_keeps_status(self):
        for limit in (80, 200, 600):
            text = rf.summary_of(LONG, limit=limit)
            self.assertLessEqual(len(text), limit)
            self.assertTrue(text.endswith('RESULT: NOT DONE: tests failed'))
        self.assertEqual(rf.summary_of(''), '')
        self.assertEqual(rf.summary_of('just one line'), 'just one line')
        with self.assertRaises(TypeError):
            rf.summary_of(None)

    def test_summary_respects_very_small_limits(self):
        self.assertLessEqual(len(rf.summary_of(LONG, limit=10)), 10)
        for limit in (1, 2, 5, 31, 33, 40):
            self.assertLessEqual(len(rf.summary_of(LONG, limit=limit)), limit)
            self.assertLessEqual(len(rf.summary_of(REPLY, limit=limit)), limit)
        self.assertLessEqual(len(rf.summary_of(LONG, limit=0)), 1)

    def test_prune_deletes_old_keeps_young(self):
        now = time.time()
        old = rf.write('r', 'old', 'x')
        young = rf.write('r', 'young', 'y')
        edge = rf.write('r', 'edge', 'z')
        other = os.path.join(self.root, 'keep.json')
        with open(other, 'w') as fh:
            fh.write('{}')
        os.utime(old, (now - 86401, now - 86401))
        os.utime(young, (now - 3600, now - 3600))
        os.utime(edge, (now - 86000, now - 86000))
        os.utime(other, (now - 999999, now - 999999))
        deleted = rf.prune(now=now)
        self.assertEqual(deleted, [old])
        self.assertFalse(os.path.exists(old))
        for path in (young, edge, other):
            self.assertTrue(os.path.exists(path))
        self.assertEqual(rf.prune(now=now + 86400 * 2), sorted([edge, young]))

    def test_prune_without_root_and_stale_tmp(self):
        self.assertEqual(rf.prune(), [])
        os.makedirs(self.root)
        tmp = os.path.join(self.root, '.r--t.md.abc.tmp')
        with open(tmp, 'w') as fh:
            fh.write('x')
        os.utime(tmp, (time.time() - 90000,) * 2)
        self.assertEqual(rf.prune(), [tmp])


if __name__ == '__main__':
    unittest.main()
