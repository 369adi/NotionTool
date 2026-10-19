"""Tests for notiontool.task_specs. Every file operation happens in a tempfile folder."""
import hashlib
import os
import tempfile
import time
import unittest
from pathlib import Path

from notiontool import task_specs
from notiontool.task_specs import (check_spec_path, cleanup_specs, compose_prompt, load_rules, read_spec,
                                   save_spec, spec_file_path, spec_text, specs_dir)

LONG_TASK = 'Build the task spec module now. ' + 'Details follow here. ' * 120


class RulesTests(unittest.TestCase):
    def test_load_rules_hash_is_short_and_stable(self):
        text, short_hash = load_rules()
        self.assertEqual(len(short_hash), 12)
        int(short_hash, 16)
        data = (task_specs.RULES_DIR / 'standard_rules_v1.md').read_bytes()
        self.assertEqual(short_hash, hashlib.sha256(data).hexdigest()[:12])
        self.assertEqual(load_rules('v1'), (text, short_hash))

    def test_rules_file_lists_the_standard_rules(self):
        text, _ = load_rules('v1')
        for words in ('only the files listed', 'LF', 'BOM', 'git push', 'restart', 'live Notion',
                      'backup', 'Test-Path', 'line count', 'NOT DONE', 'CR=0', 'English', 'simple German'):
            self.assertIn(words, text)

    def test_unknown_rules_version_raises(self):
        with self.assertRaises(ValueError):
            load_rules('v9')
        with self.assertRaises(ValueError):
            compose_prompt('Short task.', rules_version='v9')


class ComposePromptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_short_text_inline_with_header_and_rules_once(self):
        rules, short_hash = load_rules()
        prompt = compose_prompt('Write a.py and test it.')
        header = 'Standard rules v1 apply (hash %s).' % short_hash
        self.assertTrue(prompt.startswith(header + '\n'))
        self.assertIn('Write a.py and test it.', prompt)
        self.assertEqual(prompt.count(rules.strip()), 1)
        self.assertEqual(prompt.count(header), 1)
        self.assertNotIn('Full details', prompt)

    def test_text_at_limit_stays_inline(self):
        prompt = compose_prompt('x' * 50, max_inline_chars=50)
        self.assertIn('x' * 50, prompt)
        self.assertNotIn('Full details', prompt)

    def test_long_text_saved_with_summary_and_path_line(self):
        rules, _ = load_rules()
        path = spec_file_path(self.root, 'run1', 't1')
        prompt = compose_prompt(LONG_TASK, spec_path=path)
        lines = prompt.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0], 'Build the task spec module now.')
        self.assertEqual(lines[1], 'Full details: read ' + str(path))
        self.assertEqual(lines[2], 'Standard rules v1 apply (hash %s).' % load_rules()[1])
        self.assertLess(len(prompt), 600)
        self.assertNotIn(rules.strip(), prompt)
        content = path.read_text(encoding='utf-8')
        self.assertEqual(content, spec_text(LONG_TASK))
        self.assertTrue(content.startswith(LONG_TASK.rstrip()))

    def test_long_summary_at_most_300_chars(self):
        path = spec_file_path(self.root, 'run1', 't2')
        prompt = compose_prompt('word ' * 1000, spec_path=path)
        self.assertLessEqual(len(prompt.splitlines()[0]), 300)
        self.assertIn('Full details: read ', prompt)

    def test_long_text_without_spec_path_raises(self):
        with self.assertRaises(ValueError):
            compose_prompt(LONG_TASK)

    def test_reading_never_deletes_the_spec(self):
        path = spec_file_path(self.root, 'run1', 't1')
        compose_prompt(LONG_TASK, spec_path=path)
        for _ in range(3):
            self.assertEqual(read_spec(self.root, 'run1', 't1'), spec_text(LONG_TASK))
        compose_prompt(LONG_TASK, spec_path=path)
        self.assertTrue(path.is_file())

    def test_spec_file_contains_task_then_rules_exactly_once(self):
        rules, _ = load_rules()
        path = spec_file_path(self.root, 'run1', 't1')
        compose_prompt(LONG_TASK, spec_path=path, root=self.root)
        content = path.read_text(encoding='utf-8')
        self.assertEqual(content.count(rules.strip()), 1)
        self.assertLess(content.index(LONG_TASK.rstrip()), content.index(rules.strip()))
        self.assertNotIn('\r', content)
        compose_prompt(LONG_TASK, spec_path=path, root=self.root)
        self.assertEqual(path.read_text(encoding='utf-8').count(rules.strip()), 1)

    def test_spec_path_outside_raises_and_writes_nothing(self):
        outside = Path(self.root) / 'elsewhere' / 't1.md'
        with self.assertRaises(ValueError):
            compose_prompt(LONG_TASK, spec_path=outside)
        with self.assertRaises(ValueError):
            compose_prompt(LONG_TASK, spec_path=outside, root=self.root)
        self.assertFalse(outside.exists())
        self.assertFalse(outside.parent.exists())

    def test_spec_path_under_other_root_raises_with_root(self):
        with tempfile.TemporaryDirectory() as other:
            foreign = spec_file_path(other, 'run1', 't1')
            with self.assertRaises(ValueError):
                compose_prompt(LONG_TASK, spec_path=foreign, root=self.root)
            self.assertFalse(foreign.exists())
            self.assertEqual(check_spec_path(foreign), foreign)

    def test_spec_path_with_dotdot_raises(self):
        base = specs_dir(self.root)
        for path in (base / 'run1' / '..' / '..' / 'escape' / 't1.md', base / 'run1' / '..' / 'run2' / 't1.md'):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    compose_prompt(LONG_TASK, spec_path=path)
                with self.assertRaises(ValueError):
                    compose_prompt(LONG_TASK, spec_path=path, root=self.root)
        self.assertFalse((Path(self.root) / 'data' / 'escape').exists())
        self.assertFalse((base / 'run2').exists())

    def test_spec_path_in_archive_raises(self):
        path = specs_dir(self.root) / '_archive' / 'run1' / 't1.md'
        with self.assertRaises(ValueError):
            compose_prompt(LONG_TASK, spec_path=path)
        with self.assertRaises(ValueError):
            compose_prompt(LONG_TASK, spec_path=path, root=self.root)
        self.assertFalse((specs_dir(self.root) / '_archive').exists())

    def test_spec_path_must_end_md_and_sit_in_run_folder(self):
        base = specs_dir(self.root)
        for path in (base / 'run1' / 't1.txt', base / 'run1' / 't1', base / 't1.md'):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    compose_prompt(LONG_TASK, spec_path=path, root=self.root)
                self.assertFalse(path.exists())


class SaveSpecTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_spec_atomic_leaves_no_temp_file(self):
        path = save_spec(self.root, 'run1', 't1', 'first\n')
        save_spec(self.root, 'run1', 't1', 'second\n')
        self.assertEqual(sorted(os.listdir(path.parent)), ['t1.md'])
        self.assertEqual(path.read_bytes(), b'second\n')

    def test_save_spec_idempotent_keeps_mtime(self):
        path = save_spec(self.root, 'run1', 't1', 'same text\n')
        os.utime(path, (1000000, 1000000))
        self.assertEqual(save_spec(self.root, 'run1', 't1', 'same text\n'), path)
        self.assertEqual(path.stat().st_mtime, 1000000)
        save_spec(self.root, 'run1', 't1', 'new text\n')
        self.assertNotEqual(path.stat().st_mtime, 1000000)

    def test_spec_path_layout_inside_task_specs(self):
        path = spec_file_path(self.root, 'run1', 't1')
        self.assertEqual(path, Path(self.root).resolve() / 'data' / 'task_specs' / 'run1' / 't1.md')

    def test_bad_ids_rejected(self):
        bad = ['', '   ', '..', '../x', 'a/b', 'a\\b', '/abs', 'C:x', 'C:\\x', 'run.', ' run', None, 7]
        for value in bad:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    spec_file_path(self.root, value, 't1')
                with self.assertRaises(ValueError):
                    spec_file_path(self.root, 'run1', value)
                with self.assertRaises(ValueError):
                    save_spec(self.root, value, 't1', 'x')
        with self.assertRaises(ValueError):
            spec_file_path(self.root, '_archive', 't1')
        self.assertFalse((Path(self.root) / 'x.md').exists())


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.base = specs_dir(self.root)
        self.archive = self.base / '_archive'
        self.now = time.time()

    def tearDown(self):
        self.tmp.cleanup()

    def test_cleanup_moves_only_given_runs(self):
        save_spec(self.root, 'run1', 't1', 'one')
        save_spec(self.root, 'run2', 't1', 'two')
        result = cleanup_specs(self.root, ['run1'], self.now)
        self.assertEqual(result['archived'], ['run1'])
        self.assertFalse((self.base / 'run1').exists())
        self.assertEqual((self.archive / 'run1' / 't1.md').read_text(encoding='utf-8'), 'one')
        self.assertEqual(read_spec(self.root, 'run2', 't1'), 'two')

    def test_cleanup_deletes_old_archives_keeps_new(self):
        for name, age in (('oldrun', 200000), ('newrun', 10)):
            folder = self.archive / name
            folder.mkdir(parents=True)
            (folder / 't1.md').write_text('x', encoding='utf-8')
            os.utime(folder, (self.now - age, self.now - age))
        result = cleanup_specs(self.root, [], self.now, keep_s=86400)
        self.assertEqual(result['deleted'], ['oldrun'])
        self.assertFalse((self.archive / 'oldrun').exists())
        self.assertTrue((self.archive / 'newrun' / 't1.md').is_file())

    def test_fresh_archive_is_not_deleted_in_same_call(self):
        save_spec(self.root, 'run1', 't1', 'one')
        result = cleanup_specs(self.root, ['run1'], self.now, keep_s=0)
        self.assertEqual(result['archived'], ['run1'])
        self.assertEqual(result['deleted'], [])

    def test_existing_archive_target_gets_timestamp(self):
        (self.archive / 'run1').mkdir(parents=True)
        save_spec(self.root, 'run1', 't1', 'again')
        result = cleanup_specs(self.root, ['run1'], self.now)
        name = 'run1.%d' % int(self.now)
        self.assertEqual(result['archived'], [name])
        self.assertEqual((self.archive / name / 't1.md').read_text(encoding='utf-8'), 'again')
        self.assertTrue((self.archive / 'run1').is_dir())

    def test_invalid_ids_skipped_and_nothing_outside_touched(self):
        outside = Path(self.root) / 'outside.txt'
        outside.write_text('keep', encoding='utf-8')
        other = Path(self.root) / 'data' / 'other'
        other.mkdir(parents=True)
        save_spec(self.root, 'run2', 't1', 'two')
        result = cleanup_specs(self.root, ['..', '../data', '/abs', '', '_archive', 'missing'], self.now)
        self.assertEqual(result['archived'], [])
        self.assertEqual(len(result['skipped']), 5)
        self.assertEqual(outside.read_text(encoding='utf-8'), 'keep')
        self.assertTrue(other.is_dir())
        self.assertEqual(read_spec(self.root, 'run2', 't1'), 'two')

    def test_cleanup_without_spec_folder(self):
        self.assertEqual(cleanup_specs(self.root, ['run1'], self.now),
                         dict(archived=[], deleted=[], skipped=[]))
        self.assertFalse(self.base.exists())


if __name__ == '__main__':
    unittest.main()
