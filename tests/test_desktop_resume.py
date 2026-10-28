"""Restart resume (user 2026-10-28 05:20): after a restart the same chat, the same scroll position and the
chat box text up to the very last letter are back."""
import json
import tempfile
import unittest
from pathlib import Path

from notiontool import desktop_shell

ROOT = Path(__file__).resolve().parents[1]
V6 = (ROOT / 'notiontool' / 'web' / 'desktop' / 'v6.js').read_text(encoding='utf-8')
DS = (ROOT / 'notiontool' / 'desktop_shell.py').read_text(encoding='utf-8')
PS = ROOT / 'work' / 'restart-harness.ps1'


class ResumeFileTests(unittest.TestCase):
    def test_save_read_take_once(self):
        with tempfile.TemporaryDirectory() as d:
            url = 'https://www.notion.so/chat?t=abc123'
            entry = desktop_shell.save_resume({'url': url, 'thread': 'abc-123', 'fromBottom': 312.6}, d,
                                              now=lambda: 5.0)
            self.assertEqual(entry, dict(url=url, thread='abc-123', from_bottom=313, ts=5000))
            self.assertEqual(desktop_shell.resume_url(d), url)
            self.assertEqual(desktop_shell.take_resume({}, d), dict(thread='abc-123', from_bottom=313))
            self.assertFalse((Path(d) / desktop_shell.RESUME_FILE).exists())
            self.assertEqual(desktop_shell.take_resume({}, d), dict(thread='', from_bottom=0))
            self.assertEqual(desktop_shell.resume_url(d), '')

    def test_only_notion_https_urls(self):
        for bad in ('http://www.notion.so/chat', 'file:///C:/x', 'https://evil.example/notion.so',
                    'https://notion.so.evil.example/', 'javascript:alert(1)', ''):
            self.assertEqual(desktop_shell._resume_url(bad), '', bad)
        self.assertTrue(desktop_shell._resume_url('https://app.notion.com/chat'))

    def test_draft_keeps_trailing_spaces(self):
        with tempfile.TemporaryDirectory() as d:
            desktop_shell.save_draft({'thread': 't1', 'text': 'hallo welt  '}, d)
            self.assertEqual(desktop_shell.load_draft({'thread': 't1'}, d)['text'], 'hallo welt  ')

    def test_action_and_handler(self):
        self.assertIn('resume_take', desktop_shell.DESK_ACTIONS)
        self.assertIn('resume_take=lambda args: take_resume(args, data_dir)', DS)
        self.assertIn('url=resume_url(self.data_dir)', DS)


class V6FreezeTests(unittest.TestCase):
    def test_draft_text_keeps_last_letter(self):
        line = [l for l in V6.splitlines() if 'function draftText(' in l][0]
        self.assertNotIn(r'\s+$', line)
        self.assertIn(r".replace(/\n$/, '')", line)

    def test_local_copy_at_every_keystroke(self):
        self.assertIn('function onDraftInput(e) { if (draftIn(e)) { draftKeep(); draftSoon(); } }', V6)

    def test_freeze_locks_box_and_reports_state(self):
        self.assertIn('window.__ntDeskFreeze = freezeState;', V6)
        start = V6.index('function freezeState()')
        body = V6[start:V6.index('function unfreeze()')]
        for part in ("setAttribute('contenteditable', 'false')", 'location.href', 'fromBottom', 'draftWait[t]'):
            self.assertIn(part, body)

    def test_scroll_restored_once_per_render(self):
        self.assertIn('      resumeTick();\n      maskSync();\n      ensureNodes();', V6.replace('\r\n', '\n'))
        self.assertIn("ctx.call('resume_take'", V6)


@unittest.skipUnless(PS.exists(), 'local harness script only')
class RestartOrderTests(unittest.TestCase):
    def test_freeze_before_window_stop(self):
        ps = PS.read_text(encoding='utf-8')
        self.assertLess(ps.index('stop-harness.ps1'), ps.index('freeze_window.py'))
        self.assertLess(ps.index('freeze_window.py'), ps.index('stop_window.py'))


if __name__ == '__main__':
    unittest.main()
