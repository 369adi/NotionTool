"""Round 32 (user 2026-10-28 18:35): live feed, composer during voice recording, lag."""
import pathlib
import unittest

DESK = pathlib.Path(__file__).resolve().parents[1] / 'notiontool' / 'web' / 'desktop'
JS = (DESK / 'v6.js').read_text(encoding='utf-8', errors='replace')
CSS = (DESK / 'v6.css').read_text(encoding='utf-8', errors='replace')


def body(name):
    start = JS.index('    function ' + name + '(')
    end = JS.index('\n    }\n', start)
    return JS[start:end]


class FeedListTests(unittest.TestCase):
    def test_one_user_turn_still_finds_the_list(self):
        b = body('feedList').replace('\r\n', '\n')
        self.assertIn('fdDepth = i + 1; return e;', b)
        self.assertIn('i < fdDepth', b)
        self.assertIn("!e.querySelector('.nt-desk-composer,[data-nt-v6-comp]')", b)

    def test_kept_turns_are_not_shown_again_in_the_same_visit(self):
        self.assertIn('fdSnapTh !== th && feedSnapShow(host, th)', JS)
        self.assertIn('fdSnapTh = th;', body('feedSnapShow'))

    def test_finished_answer_rereads_every_row(self):
        b = body('feedSync')
        self.assertIn('busy !== fdBusy', b)
        self.assertIn('if (!busy) { fdAll = true;', b)


class ComposerTests(unittest.TestCase):
    def test_last_composer_keeps_an_own_mark(self):
        b = body('composerNow')
        self.assertIn("setAttribute('data-nt-v6-comp', '1')", b)
        self.assertIn("toggle('nt-v6-nocomp', none)", b)
        self.assertIn('cp = composerNow()', body('feedPlace'))
        self.assertIn('compTop ? compTop - 8', body('feedPlace'))

    def test_mask_shows_the_kept_composer_and_the_chat_without_box(self):
        self.assertIn('html.nt-v6-on.nt-v6-mask #notion-app [data-nt-v6-comp],', CSS)
        self.assertIn('html.nt-v6-on.nt-v6-mask.nt-v6-nocomp #notion-app .layout-chat { visibility: visible; }', CSS)

    def test_tick_runs_the_keeper(self):
        self.assertIn('try { composerNow(); }', body('tick'))


class LagTests(unittest.TestCase):
    def test_grown_row_skips_innerhtml(self):
        self.assertIn("var sig = grown ? 'c' + cs :", JS)

    def test_heavy_turn_syncs_less_often(self):
        self.assertIn('setTimeout(feedSync, Math.min(400, Math.max(80, Math.round(fdCost * 4))))', JS)
        self.assertIn('fdCost = performance.now() - t0;', JS)


if __name__ == '__main__':
    unittest.main()
