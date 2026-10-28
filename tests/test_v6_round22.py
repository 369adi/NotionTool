"""Round 22 (user 2026-10-28 06:26/06:27): instant chat switch, bottom kept, purple taskbar icon."""
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / 'notiontool' / 'web' / 'desktop' / 'v6.js').read_text(encoding='utf-8')


class SwitchTests(unittest.TestCase):
    def test_row_click_switches_before_notion_navigates(self):
        self.assertIn("document.addEventListener('click', feedRowClick, true);", JS)
        self.assertIn("document.removeEventListener('click', feedRowClick, true);", JS)
        body = JS[JS.index('function feedSwitch('):]
        body = body[:body.index('\n    }\n')]
        self.assertIn('feedStash();', body)
        self.assertIn('fdPend = from;', body)
        self.assertIn('if (!feedSnapShow(host, k)) feedHold(host);', body)

    def test_row_menu_click_is_no_switch(self):
        self.assertIn('if (b && b !== r && r.contains(b)) return;', JS)

    def test_old_url_is_not_a_switch_back(self):
        self.assertIn('if (th === fdPend && Date.now() - fdPendAt < 3000)', JS)

    def test_old_chat_list_never_mirrored_into_new_chat(self):
        self.assertIn('if (list && fdOld) { if (feedSig(list) === fdOld) list = null; else fdOld = \'\'; }', JS)
        self.assertIn("if (!fdOld && host.querySelector('[data-agent-chat-user-step-id]')) return false;", JS)

    def test_host_remount_moves_feed_not_rebuilds(self):
        body = JS[JS.index('function feedEnsure(host) {'):]
        body = body[:body.index('if (fd) fd.remove();')]
        self.assertIn('host.appendChild(fd);', body)
        self.assertIn('fdSc.scrollTop = fdStick ? fdSc.scrollHeight', body)

    def test_only_user_scroll_unpins_bottom(self):
        self.assertIn('else if (fdUserDrag || Date.now() - fdUserAt < 700) fdStick = false;', JS)
        self.assertIn('else if (fdStick) fdSc.scrollTop = fdSc.scrollHeight;', JS)
        self.assertNotIn('fdStick = fdSc.scrollHeight - fdSc.scrollTop - fdSc.clientHeight < 40;', JS)

    def test_mid_switch_never_blank(self):
        self.assertIn('if (!off && th && fd && fdHoldAt && Date.now() - fdHoldAt < FEED_GRACE_MS) {', JS)


class IconTests(unittest.TestCase):
    def test_icon_source_exists(self):
        from notiontool import desktop_icon as di
        kind, path = di.icon_source()
        self.assertEqual(kind, 'ico')
        self.assertTrue(Path(path).is_file())

    def test_hung_window_never_blocks(self):
        src = (ROOT / 'notiontool' / 'desktop_icon.py').read_text(encoding='utf-8')
        self.assertNotIn('SendMessageW(', src)
        self.assertIn('SMTO_ABORTIFHUNG', src)

    def test_guard_applies_icon_to_our_pids(self):
        from notiontool import desktop_guard as g

        class FakeIcon:
            def __init__(self):
                self.calls = []

            def apply(self, pids):
                self.calls.append(list(pids))
                return 1
        icon = FakeIcon()
        t = [0.0]
        guard = g.DesktopGuard(object(), lambda: [11, 12], threading.Event(), clock=lambda: t[0], icon=icon)
        guard.step()
        self.assertEqual(icon.calls, [[11, 12]])
        self.assertTrue(guard.icon_ok)

    def test_guard_retries_pids_while_icon_missing(self):
        from notiontool import desktop_guard as g

        class NoWindow:
            def apply(self, pids):
                return 0
        n = []
        t = [0.0]
        guard = g.DesktopGuard(object(), lambda: n.append(1) or [5], threading.Event(), clock=lambda: t[0], icon=NoWindow())
        guard.step()
        t[0] = 10
        guard.step()
        t[0] = 31
        guard.step()
        self.assertEqual(len(n), 2)


if __name__ == '__main__':
    unittest.main()
