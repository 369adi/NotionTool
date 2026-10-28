"""Round 21 (user 2026-10-28 06:14): a chat switch shows the chat at once - never blank, never Notion."""
import unittest
from pathlib import Path

JS = (Path(__file__).resolve().parents[1] / 'notiontool' / 'web' / 'desktop' / 'v6.js').read_text(encoding='utf-8')


class Round21Tests(unittest.TestCase):
    def test_kept_turns_survive_in_local_storage(self):
        self.assertIn('feedLsPut(fdThread, keep);', JS)
        self.assertIn('var keep = fdCache.get(th) || feedLsGet(th);', JS)
        self.assertIn('FD_LS_MAX = 400000', JS)

    def test_restored_html_is_cleaned(self):
        body = JS[JS.index('function feedLsGet('):JS.index('function feedHold(')]
        self.assertIn("querySelectorAll('script,iframe,object,embed')", body)
        self.assertIn('/^on/i.test(', body)

    def test_unknown_chat_holds_an_empty_feed_instead_of_notion(self):
        self.assertIn('fdThread = th; fdHoldAt = Date.now();', JS)
        self.assertIn('feedHold(host)) return;', JS)
        body = JS[JS.index('function feedHold('):]
        self.assertIn("host.querySelector('[data-agent-chat-user-step-id]')", body[:600])
        self.assertIn('fdGone = 0; fdHoldAt = 0;', JS)

    def test_snapshot_stays_until_rows_have_text(self):
        self.assertIn('/\\S/.test(list.children[q].textContent)) pv++;', JS)

    def test_freeze_stashes_open_chat(self):
        body = JS[JS.index('function freezeState()'):JS.index('function unfreeze()')]
        self.assertIn('feedStash();', body)


if __name__ == '__main__':
    unittest.main()
