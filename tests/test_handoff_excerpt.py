"""Setting handoff_excerpt_chars: coworkers get a checkpoint excerpt plus the full-file path (token saving)."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from notiontool import handoff_files
from notiontool import notion as notion_module
from notiontool.notion import (HANDOFF_EXCERPT_DEFAULT, NotionAdapter, handoff_excerpt, handoff_excerpt_block,
                               handoff_excerpt_limit)
from notiontool.store import Store

NOTION_PY = Path(notion_module.__file__)
THREAD = '401dac2a-b9fd-80d8-b80d-00a9bccea4a4'
READ_LINE = 'Read that file with the NotionTool read_file tool only if you need more than this excerpt.'


def checkpoint(lines=200):
    return '\n'.join('Line %03d: decision, file and status details for the checkpoint.' % i for i in range(lines))


class ExcerptFunctionTests(unittest.TestCase):
    def test_excerpt_is_at_most_limit(self):
        text = checkpoint()
        for limit in (50, 400, 1500, 1501):
            self.assertLessEqual(len(handoff_excerpt(text, limit)), limit)

    def test_cut_at_a_line_end(self):
        text = checkpoint()
        excerpt = handoff_excerpt(text, 1500)
        self.assertTrue(text.startswith(excerpt))
        self.assertEqual(text[len(excerpt)], '\n')
        self.assertGreater(len(excerpt), 1400)  # keeps nearly the whole budget
        self.assertIn(excerpt.split('\n')[-1], text.split('\n'))  # the last line is complete

    def test_without_line_end_falls_back_to_space_then_hard_cut(self):
        words = 'word ' * 100
        excerpt = handoff_excerpt(words, 42)
        self.assertLessEqual(len(excerpt), 42)
        self.assertTrue(excerpt.endswith('word'))
        self.assertEqual(handoff_excerpt('x' * 100, 30), 'x' * 30)

    def test_zero_or_short_text_is_unchanged(self):
        text = checkpoint()
        self.assertEqual(handoff_excerpt(text, 0), text)
        self.assertEqual(handoff_excerpt('short', 1500), 'short')

    def test_block_has_excerpt_path_and_one_read_line(self):
        text = checkpoint()
        block = handoff_excerpt_block(text, 1500, 'C:/data/handoffs/w_excerpt.json')
        self.assertTrue(block.startswith(handoff_excerpt(text, 1500)))
        self.assertIn('C:/data/handoffs/w_excerpt.json', block)
        self.assertEqual(block.count(READ_LINE), 1)
        self.assertTrue(block.endswith(READ_LINE))

    def test_setting_parse(self):
        self.assertEqual(HANDOFF_EXCERPT_DEFAULT, 1500)
        self.assertEqual(handoff_excerpt_limit({}), 1500)
        self.assertEqual(handoff_excerpt_limit({'handoff_excerpt_chars': 0}), 0)
        self.assertEqual(handoff_excerpt_limit({'handoff_excerpt_chars': '800'}), 800)
        self.assertEqual(handoff_excerpt_limit({'handoff_excerpt_chars': -5}), 0)
        self.assertEqual(handoff_excerpt_limit({'handoff_excerpt_chars': None}), 1500)


class AdapterExcerptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)  # Windows keeps the SQLite file locked
        self.store = Store(self.temp.name)
        self.adapter = NotionAdapter(self.store)
        self.text = checkpoint()
        self.task = dict(id='task1', run_id='run1')

    def tearDown(self):
        try:
            self.store.db.close()
        except Exception:
            pass
        self.temp.cleanup()

    def worker(self, purpose='worker', text=None):
        return dict(id='acc:' + THREAD, account_id='acc', thread_id=THREAD, purpose=purpose,
                    handoff=self.text if text is None else text, handoff_delivered=False, retired_threads=['old'])

    def setting(self, value):
        self.store.put('settings', dict(id='app', handoff_excerpt_chars=value))

    def folder(self):
        return str(Path(self.store.directory) / 'handoffs')

    def test_default_is_1500_for_coworkers(self):
        self.assertEqual(handoff_excerpt_limit(self.store.settings()), 1500)
        out = self.adapter._inline_handoff_text(self.worker(), self.task)
        excerpt = out.split('\n[Checkpoint excerpt:')[0]
        self.assertLessEqual(len(excerpt), 1500)
        self.assertEqual(self.text[len(excerpt)], '\n')
        self.assertLess(len(out), len(self.text) // 2)

    def test_coworker_gets_excerpt_and_full_file_path(self):
        self.setting(600)
        out = self.adapter._inline_handoff_text(self.worker(), self.task)
        excerpt = handoff_excerpt(self.text, 600)
        self.assertTrue(out.startswith(excerpt))
        self.assertLessEqual(len(excerpt), 600)
        path = handoff_files.handoff_path(self.folder(), handoff_files.file_key('acc:' + THREAD + ':excerpt'))
        self.assertIn(path, out)
        self.assertTrue(out.endswith(READ_LINE))
        with open(path, encoding='utf-8') as fh:
            record = json.load(fh)
        self.assertEqual(record['handoff'], self.text)  # the file keeps the full checkpoint
        self.assertEqual(record['new_thread'], THREAD)

    def test_zero_restores_full_text_and_writes_no_file(self):
        self.setting(0)
        self.assertEqual(self.adapter._inline_handoff_text(self.worker(), self.task), self.text)
        self.assertFalse(os.path.isdir(self.folder()) and os.listdir(self.folder()))

    def test_short_checkpoint_is_sent_in_full(self):
        self.assertEqual(self.adapter._inline_handoff_text(self.worker(text='short checkpoint'), self.task),
                         'short checkpoint')

    def test_main_and_other_chats_unchanged(self):
        self.setting(100)
        for purpose in ('main', 'planner', '', None):
            self.assertEqual(self.adapter._inline_handoff_text(self.worker(purpose=purpose), self.task), self.text)
        self.assertFalse(os.path.isdir(self.folder()) and os.listdir(self.folder()))

    def test_ack_token_file_is_not_touched(self):
        self.setting(600)
        self.adapter._inline_handoff_text(self.worker(), self.task)
        token_file = handoff_files.handoff_path(self.folder(), handoff_files.file_key('acc:' + THREAD))
        self.assertFalse(os.path.exists(token_file))

    def test_new_checkpoint_replaces_old_file(self):
        self.setting(600)
        self.adapter._inline_handoff_text(self.worker(), self.task)
        newer = checkpoint(150) + '\nNEWER'
        out = self.adapter._inline_handoff_text(self.worker(text=newer), self.task)
        path = handoff_files.handoff_path(self.folder(), handoff_files.file_key('acc:' + THREAD + ':excerpt'))
        with open(path, encoding='utf-8') as fh:
            self.assertEqual(json.load(fh)['handoff'], newer)
        self.assertIn(path, out)

    def test_file_failure_sends_full_text(self):
        self.setting(600)
        with mock.patch.object(handoff_files, 'write_handoff', side_effect=OSError('disk full')):
            self.assertEqual(self.adapter._inline_handoff_text(self.worker(), self.task), self.text)


class SourceTests(unittest.TestCase):
    def test_notion_py_is_still_crlf(self):
        data = NOTION_PY.read_bytes()
        self.assertGreater(data.count(b'\r\n'), 1000)
        self.assertEqual(data.count(b'\n'), data.count(b'\r\n'))

    def test_only_inline_text_changed_token_logic_kept(self):
        source = NOTION_PY.read_text(encoding='utf-8')
        self.assertIn("handoff_included = bool(worker.get('handoff') and not worker.get('handoff_delivered'))", source)
        self.assertIn("self.store.settings().get('handoff_mode', 'file') == 'file'", source)
        self.assertIn("prompt = self._inline_handoff_text(worker, task) + note + ", source)
        self.assertIn("fresh_worker['handoff_delivered'] = True", source)
        self.assertIn("saved['handoff_token'] = handoff_token", source)


if __name__ == '__main__':
    unittest.main()
