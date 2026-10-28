"""Round 17 (user 2026-10-28 04:18-04:24): rail becomes a top icon row; own chats switch instantly."""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
DESK = ROOT / "notiontool" / "web" / "desktop"
JS = (DESK / "v6.js").read_text(encoding="utf-8")
CSS = (DESK / "v6.css").read_text(encoding="utf-8")


class ChatCacheTest(unittest.TestCase):
    def test_switch_keeps_and_restores_turns(self):
        self.assertIn("function feedStash()", JS)
        self.assertIn("function feedSnapShow(host, th)", JS)
        self.assertIn("function feedSnapDrop()", JS)
        self.assertIn("if (th && fdThread && th !== fdThread) {", JS)
        # leaving a chat (also to Projects/Live) keeps its last turns
        self.assertRegex(JS, r"function feedOff\(\) \{\s*feedStash\(\); fdSnap = null;")

    def test_only_last_five_messages_are_kept(self):
        self.assertIn("FD_KEEP_TURNS = 5", JS)
        self.assertIn("keep = keep.slice(-FD_KEEP_TURNS);", JS)

    def test_coworker_chats_are_not_kept(self):
        self.assertIn("if (!fdIn || !fdThread || fdSnap || COWORKER_RE.test(fdTitle)) return;", JS)
        self.assertIn("fdTitle = currentTitle();", JS)

    def test_cache_is_bounded_and_drops_notion_nodes(self):
        self.assertRegex(JS, r"FD_CACHE_MAX = \d+")
        self.assertIn("while (fdCache.size > FD_CACHE_MAX)", JS)
        self.assertIn("keep[i].__ntOrig = null;", JS)

    def test_snapshot_gives_way_to_live_rows(self):
        self.assertIn("if (!fdSnap) fdIn.textContent = '';", JS)
        self.assertIn("pv < Math.min(fdSnap.length, 2) && now - fdSnapAt < FD_SNAP_MS", JS)


class TopIconRowTest(unittest.TestCase):
    def test_rail_no_longer_takes_width(self):
        self.assertIn("html.nt-v6-on { --nt-rail-w: 0px !important; }", CSS)
        self.assertIn("html.nt-v6-on.nt-shell-on .notion-app-inner { margin-left: 0 !important;", CSS)
        m = re.search(r"function railRight\(\) \{(.*?)\n    \}", JS, re.S)
        self.assertIsNotNone(m)
        self.assertIn("return 0;", m.group(1))

    def test_tabs_are_small_icons_without_labels(self):
        self.assertIn("nav.nt-rail .nt-shell-tab-label { display: none !important; }", CSS)
        self.assertIn("flex-direction: row !important", CSS)
        self.assertIn("html.nt-v6-on .nt-v6-sb { padding-top: 48px !important; }", CSS)


if __name__ == "__main__":
    unittest.main()
