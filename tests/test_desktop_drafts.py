"""User 2026-10-28 04:49: typed chat text survives every restart (desktop_drafts.json + v6.js)."""
import tempfile
import unittest
from pathlib import Path

from notiontool import desktop_shell as ds

ROOT = Path(__file__).resolve().parents[1]
V6 = (ROOT / 'notiontool' / 'web' / 'desktop' / 'v6.js').read_text(encoding='utf-8')


class DraftStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip_and_tombstone(self):
        ds.save_draft(dict(thread='abc-1', text='Hallo\nWelt'), self.dir, now=lambda: 10.0)
        self.assertEqual(ds.load_draft(dict(thread='abc-1'), self.dir), dict(text='Hallo\nWelt', ts=10000))
        ds.save_draft(dict(thread='abc-1', text=''), self.dir, now=lambda: 11.0)
        self.assertEqual(ds.load_draft(dict(thread='abc-1'), self.dir), dict(text='', ts=11000))
        self.assertEqual(ds.load_draft(dict(thread='other'), self.dir), dict(text='', ts=0))

    def test_thread_is_cleaned_and_required(self):
        ds.save_draft(dict(thread='../x y', text='t'), self.dir, now=lambda: 1.0)
        self.assertEqual(ds.load_draft(dict(thread='xy'), self.dir)['text'], 't')
        with self.assertRaises(ValueError):
            ds.save_draft(dict(thread='', text='t'), self.dir)

    def test_prunes_oldest(self):
        for i in range(ds.DRAFT_MAX_CHATS + 5):
            ds.save_draft(dict(thread='t%d' % i, text='x'), self.dir, now=lambda i=i: float(i + 1))
        self.assertEqual(ds.load_draft(dict(thread='t0'), self.dir)['ts'], 0)
        self.assertEqual(ds.load_draft(dict(thread='t%d' % (ds.DRAFT_MAX_CHATS + 4)), self.dir)['text'], 'x')

    def test_broken_file_is_ignored(self):
        (Path(self.dir) / ds.DRAFTS_FILE).write_text('{broken', encoding='utf-8')
        self.assertEqual(ds.load_draft(dict(thread='a'), self.dir), dict(text='', ts=0))
        ds.save_draft(dict(thread='a', text='ok'), self.dir)
        self.assertEqual(ds.load_draft(dict(thread='a'), self.dir)['text'], 'ok')


class DraftWiringTest(unittest.TestCase):
    def test_actions_and_handlers(self):
        self.assertIn('draft_save', ds.DESK_ACTIONS)
        self.assertIn('draft_load', ds.DESK_ACTIONS)
        src = (ROOT / 'notiontool' / 'desktop_shell.py').read_text(encoding='utf-8')
        self.assertIn('draft_save=lambda args: save_draft(args, data_dir)', src)
        self.assertIn('draft_load=lambda args: load_draft(args, data_dir)', src)

    def test_overlay_saves_and_restores(self):
        for part in ("ctx.call('draft_save'", "ctx.call('draft_load'", "execCommand('insertText'",
                     'try { draftTick(); } catch', 'try { draftOff(); } catch', "'nt-v6-draft:'",
                     "addEventListener('input', onDraftInput, true)", "addEventListener('pagehide', draftSave)"):
            self.assertIn(part, V6)
        # an empty chat box alone never deletes a draft: only after a send (draftSent) or user input
        block = V6[V6.index('function draftTick()'):V6.index('function draftOff()')]
        self.assertIn('if (draftSent[t])', block)
        self.assertIn('draftShown[t] < 4', block)
        # the draft code is set up before the first tick runs
        self.assertLess(V6.index("var DRAFT_LS = 'nt-v6-draft:'"), V6.index("if (ctx && typeof ctx.onState === 'function')"))


if __name__ == '__main__':
    unittest.main()
