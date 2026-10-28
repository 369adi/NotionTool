"""Round 18 (user 2026-10-28 04:33): no counter on Live, calm colors, composer in the panel colors."""
import pathlib
import unittest

CSS = (pathlib.Path(__file__).resolve().parents[1] / "notiontool" / "web" / "desktop" / "v6.css").read_text(encoding="utf-8")
R18 = CSS[CSS.index("round 18 (user 2026-10-28 04:33)"):CSS.index("round 18b (user 2026-10-28 04:39")]


class CalmLookTest(unittest.TestCase):
    def test_live_counter_hidden_in_top_row(self):
        self.assertIn("nav.nt-rail .nt-v6-badge, html.nt-v6-on.nt-shell-on nav.nt-rail .nt-shell-tab-badge { display: none !important; }", R18)

    def test_new_chat_and_send_have_no_gradient(self):
        for sel in ("html.nt-v6-on .nt-v6-new {", 'html.nt-v6-on [data-nt-v6="send"][data-nt-v6="send"] {'):
            block = R18[R18.index(sel):]
            block = block[: block.index("}")]
            self.assertNotIn("gradient", block)
            self.assertIn("box-shadow: none", block)

    def test_composer_is_dark_blue_without_glow(self):
        self.assertIn("background: #0b111c !important; border: 1px solid #1b2433 !important;", R18)
        self.assertIn("background: #0b111c !important; border: 1px solid #2c3a52 !important;", R18)

    def test_round18_block_is_last_and_has_no_gradients(self):
        self.assertNotIn("gradient(", R18)


if __name__ == "__main__":
    unittest.main()
