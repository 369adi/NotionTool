"""I50 UI Fix 2: the four open review points of the UI Fix 1 review (ta965876d).

1. A Main's old chat is archived by rename on a context switch, never deleted.
2. Exactly one switch button per sidebar row (rows with their own switch get no age switch).
3. A lock banner sits above the message box while the open chat is send-locked.
4. Every switch button is at least 24 x 24 px.
"""
import os
import re
import threading
import unittest

from notiontool import chat_context

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DESKTOP = os.path.join(ROOT, 'notiontool', 'web', 'desktop')


def read(name):
    with open(os.path.join(DESKTOP, name), encoding='utf-8', newline='') as handle:
        return handle.read()


class Store:
    def __init__(self, workers, settings=None):
        self.lock = threading.RLock()
        self.data = {'workers': {w['id']: dict(w) for w in workers}}
        self._settings = settings or {}
        self.events = []

    def settings(self):
        return dict(self._settings)

    def get(self, kind, ident, default=None):
        value = self.data.get(kind, {}).get(ident)
        return dict(value) if value is not None else default

    def put(self, kind, value):
        self.data.setdefault(kind, {})[value['id']] = dict(value)

    def event(self, kind, run_id='', **fields):
        self.events.append((kind, fields))


class Adapter:
    def __init__(self):
        self.deleted = []
        self.renamed = []

    def delete_thread(self, client, tid):
        self.deleted.append(tid)

    def rename_thread(self, client, tid, title):
        self.renamed.append((tid, title))


class MainArchiveTests(unittest.TestCase):
    def retire(self, purpose, settings=None):
        worker = {'id': 'w1', 'purpose': purpose, 'thread_id': 'new'}
        store = Store([worker], settings or {'coworker_delete_old_chat': True})
        adapter = Adapter()
        outcome = chat_context.ChatContext(store).retire_old(adapter, None, worker, {'run_id': 'r1'}, 'old1')
        return outcome, adapter, store

    def test_main_old_chat_is_renamed_never_deleted(self):
        for purpose in ('main', 'operator'):
            outcome, adapter, store = self.retire(purpose)
            self.assertEqual(outcome, 'archived')
            self.assertEqual(adapter.deleted, [], purpose)
            self.assertEqual(len(adapter.renamed), 1)
            tid, title = adapter.renamed[0]
            self.assertEqual(tid, 'old1')
            self.assertTrue(title.startswith(chat_context.MAIN_ARCHIVE_PREFIX), title)
            self.assertIn('expired', title)
            self.assertEqual(store.data['workers']['w1']['archived_threads'], ['old1'])
            self.assertEqual(store.events[-1][0], 'main_old_chat_archived')
            self.assertIn('never deleted', store.events[-1][1]['message'])

    def test_main_archive_survives_a_rename_failure_without_delete(self):
        worker = {'id': 'w1', 'purpose': 'main'}
        store = Store([worker], {'coworker_delete_old_chat': True})
        adapter = Adapter()

        def broken(client, tid, title):
            raise RuntimeError('offline')
        adapter.rename_thread = broken
        outcome = chat_context.ChatContext(store).retire_old(adapter, None, worker, {}, 'old1')
        self.assertEqual(outcome, 'archived')
        self.assertEqual(adapter.deleted, [])
        self.assertIn('rename failed', store.events[-1][1]['message'])

    def test_coworker_behaviour_is_unchanged(self):
        outcome, adapter, _ = self.retire('worker')
        self.assertEqual((outcome, adapter.deleted, adapter.renamed), ('deleted', ['old1'], []))
        outcome, adapter, _ = self.retire('worker', {'coworker_delete_old_chat': False})
        self.assertEqual((outcome, adapter.deleted, adapter.renamed), ('retired', [], [('old1', 'Cowork (retired)')]))


class OverlayFix2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = read('overlay.js')
        cls.css = read('overlay.css')

    def section(self, start, end):
        a = self.js.index(start)
        return self.js[a:self.js.index(end, a)]

    def test_one_switch_button_per_row(self):
        body = self.section('function styleChatAge(', 'function ageSwitchButton(')
        self.assertIn("var OWN_SWITCH_MARKS = ['switch', 'main-switch'];", self.js)
        self.assertIn('OWN_SWITCH_MARKS.indexOf(mark) >= 0) own = kid;', body)
        # With an own switch the age switch is dropped and never created.
        own_at = body.index('if (own) {')
        create_at = body.index('btn = ageSwitchButton(entry.thread)')
        self.assertLess(own_at, create_at)
        own_block = body[own_at:create_at]
        self.assertIn('if (btn) dropOwn(btn);', own_block)
        self.assertIn('return;', own_block)
        self.assertIn("own.setAttribute('data-nt-switch-level', level)", own_block)

    def test_lock_banner_above_the_message_box(self):
        body = self.section('function applyLockBanner(', '// ---- Named chats')
        self.assertIn("document.querySelector('.nt-desk-composer')", body)
        self.assertIn('composer.parentNode.insertBefore(lockBanner, composer)', body)
        self.assertIn("level !== 'locked'", body)
        self.assertIn('dropOwn(lockBanner)', body)
        self.assertIn("role: 'alert'", self.js)
        self.assertIn('ageContextSwitch(thread)', self.section('function lockBannerNode(', 'function applyLockBanner('))
        self.assertIn('try { applyLockBanner(); } catch (e)', self.js)
        self.assertIn('.nt-desk-lock-banner {', self.css)
        self.assertIn('.nt-desk-lock-banner[hidden] { display: none; }', self.css)

    def test_switch_buttons_are_at_least_24_px(self):
        rule = re.search(r'\.nt-desk-age-switch, \.nt-desk-switch, \.nt-desk-cw-switch, \.nt-desk-main-switch, '
                         r'\.nt-desk-lock-banner-switch \{([^}]*)\}', self.css)
        self.assertIsNotNone(rule)
        for decl in ('height: 24px', 'min-height: 24px', 'min-width: 24px'):
            self.assertIn(decl, rule.group(1))
        # The 24 px rule comes after the shared 18 px chip rule, so it wins.
        self.assertGreater(rule.start(), self.css.index('.nt-desk-chat-age, .nt-desk-age-switch {'))

    def test_safety_rules_and_line_endings(self):
        for bad in ('fetch(', 'XMLHttpRequest', 'location.reload', 'innerHTML', '.remove()'):
            self.assertNotIn(bad, self.js)
        self.assertEqual(self.js.count('removeChild'), 1)
        self.assertEqual(self.js.count('setInterval('), 1)
        self.assertNotIn('\r', self.js)
        self.assertNotIn('\r', self.css)


if __name__ == '__main__':
    unittest.main()
