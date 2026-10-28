"""Round 28 (user 2026-10-28 08:51): own chats from 'New chat' work with Switch context; new chats open without a page load."""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DESK = os.path.join(ROOT, 'notiontool', 'web', 'desktop')


def read(*parts):
    with open(os.path.join(*parts), encoding='utf-8') as f:
        return f.read()


class SwitchTargetTest(unittest.TestCase):
    def setUp(self):
        self.js = read(DESK, 'v6.js')
        start = self.js.index('function switchTarget(title) {')
        self.fn = self.js[start:self.js.index('function ageFor(threadId, title) {', start)]

    def test_own_chats_are_looked_up_in_mains_and_private_chats(self):
        self.assertIn('var w = currentWorker(title);', self.fn)
        self.assertIn('list(state.mains), list(state.private_chats)', self.fn)
        self.assertLess(self.fn.index('key(m.thread_id) === k'), self.fn.index('str(m.title).trim() === t'),
                        'the thread id wins over the title')

    def test_own_target_keeps_the_ids_and_the_role(self):
        self.assertIn("purpose: m.role === 'main' ? 'main' : 'private'", self.fn)
        self.assertIn('chat_id: str(m.chat_id)', self.fn)
        self.assertIn('pending: !!m.pending', self.fn)

    def test_switch_and_archive_use_the_own_chat_lookup(self):
        self.assertIn('aw = switchTarget(aTitle)', self.js)
        self.assertIn('var w = switchTarget(currentTitle());', self.js)
        self.assertNotIn('currentWorker(currentTitle())', self.js)

    def test_switch_sends_chat_and_thread(self):
        self.assertIn("call('chat_context_switch', { worker_id: str(w.id), chat_id: str(w.chat_id), "
                      "thread_id: str(w.thread_id) })", self.js)

    def test_private_and_pending_chats_get_a_clear_message(self):
        sw = self.js[self.js.index('var w = switchTarget(currentTitle());'):]
        sw = sw[:sw.index("call('chat_context_switch'")]
        self.assertIn('if (w && w.pending)', sw)
        self.assertIn("if (w.purpose === 'private')", sw)
        self.assertIn('Make this a Main', sw)


class OpenThreadTest(unittest.TestCase):
    def setUp(self):
        js = read(DESK, 'v6.js')
        start = js.index('window.__ntV6OpenThread = function (k) {')
        self.fn = js[start:js.index('    };', start)]

    def test_key_is_normalized(self):
        self.assertIn('k = key(k);', self.fn)
        self.assertIn('if (!k) return false;', self.fn)

    def test_falls_back_to_notion_rows(self):
        self.assertIn('notionRows()', self.fn)
        self.assertIn("key(a.getAttribute('href')).indexOf(k) >= 0", self.fn)


class NoPageLoadTest(unittest.TestCase):
    def test_overlay_opens_the_new_chat_row(self):
        js = read(DESK, 'overlay.js')
        self.assertNotIn('location.href = result.url', js)
        self.assertIn('openChat(threadKey(result.thread_id), result.url)', js)

    def test_extras_opens_the_new_chat_row(self):
        js = read(DESK, 'extras.js')
        self.assertNotIn('location.href = result.url', js)
        self.assertIn('var open = window.__ntV6OpenThread;', js)
        self.assertIn('open(result.thread_id)', js)


class GatewayHitPathTest(unittest.TestCase):
    def test_hit_path_is_defined_before_use(self):
        src = read(ROOT, 'notiontool', 'gateway.py')
        define = src.index("_HIT_PATH = re.compile(r'^(.*?):\\d+: ')")
        uses = [m.start() for m in re.finditer(r'_HIT_PATH\.', src)]
        self.assertTrue(uses)
        self.assertLess(define, min(uses))

    def test_hit_path_keeps_windows_drive_letters(self):
        hit = re.compile(r'^(.*?):\d+: ')
        self.assertEqual(hit.match('C:\\x\\v6.js:12: var a').group(1), 'C:\\x\\v6.js')


if __name__ == '__main__':
    unittest.main()
