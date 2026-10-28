"""Round 29 (user 2026-10-28 09:07): a sent message never comes back as a draft after a restart."""
import pathlib
import unittest

JS = pathlib.Path(__file__).resolve().parents[1] / 'notiontool' / 'web' / 'desktop' / 'v6.js'


class SentDraftTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = JS.read_bytes()
        cls.src = cls.raw.decode('utf-8')

    def block(self, start, end):
        i = self.src.index(start)
        return self.src[i:self.src.index(end, i)]

    def test_crlf_kept(self):
        self.assertEqual(self.raw.count(b'\n'), self.raw.count(b'\r\n'))

    def test_newest_user_message_is_compared_by_content(self):
        b = self.block('function draftLastSent()', 'function draftIsSent')
        self.assertIn('[data-agent-chat-user-step-id]', b)
        self.assertIn('us[us.length - 1]', b)
        self.assertIn('function draftNorm(s)', self.src)
        self.assertIn("n === draftLastSent()", self.src)

    def test_restore_skips_a_sent_draft_and_clears_it(self):
        b = self.block('function draftFetch(t)', 'function draftTick()')
        self.assertIn('if (draftIsSent(pick.text)) { draftLast[t] = null; draftSave(); return; }', b)
        self.assertLess(b.index('draftIsSent(pick.text)'), b.index('draftPut(ed, pick.text)'))

    def test_empty_box_after_any_send_clears_the_draft_before_any_re_put(self):
        b = self.block('function draftTick()', "document.addEventListener('input'")
        self.assertIn('if (draftSent[t])', b)
        i = b.index('draftLast[t] && draftIsSent(draftLast[t])')
        self.assertLess(i, b.index('draftPut(ed, draftLast[t])'))

    def test_restored_text_that_was_sent_is_taken_out(self):
        b = self.block('function draftTick()', "document.addEventListener('input'")
        self.assertIn('draftShown[t] && draftIsSent(draftText(ed))', b)
        self.assertIn('draftClear(ed)', b)

    def test_no_timers_added(self):
        b = self.block('function draftNorm(s)', 'function draftSave()')
        self.assertNotIn('setTimeout', b)
        self.assertNotIn('Date.now', b)


if __name__ == '__main__':
    unittest.main()
