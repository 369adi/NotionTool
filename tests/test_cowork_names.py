"""Tests for notiontool.cowork_names and chat_pool.apply_cowork_names (fakes only, no live Notion)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from notiontool import cowork_names as cn
from notiontool.chat_pool import apply_cowork_names, pool_decision

DONE = {'alive': True, 'data': {'turn_outcomes': {'a': {'status': 'completed'}}}}


def W(ident, tid=None, label='Welcome to Notion', **kw):
    w = dict(id=ident, thread_id=tid or 't-' + ident, purpose='worker', enabled=True, join_state='joined', state='ready', lease='', label=label)
    w.update(kw)
    return w


class FakeAdapter:
    def __init__(self, fail=()):
        self.calls, self.fail = [], set(fail)

    def rename_thread(self, client, tid, title):
        self.calls.append((client, tid, title))
        if tid in self.fail:
            raise RuntimeError('rename refused ' + tid)


class Titles(unittest.TestCase):
    def test_title_for(self):
        self.assertEqual(cn.title_for(1), 'Cowork 1')
        self.assertEqual(cn.title_for(12), 'Cowork 12')
        for bad in (0, -1, True, '3', 1.0, None):
            with self.assertRaises(ValueError):
                cn.title_for(bad)

    def test_is_pool_title_true(self):
        for t in ('Welcome to Notion', 'Cowork joined', 'Cowork (retired)', 'Cowork 1', 'Cowork 42'):
            self.assertTrue(cn.is_pool_title(t), t)

    def test_is_pool_title_false(self):
        for t in ('NotionTool', 'Sonstiges', 'Studio432', 'Roblox', 'Main', 'Main Chat', 'Cowork', 'Cowork 1 x',
                  ' Cowork 1', 'cowork 1', 'Cowork one', 'welcome to notion', '', None, 5):
            self.assertFalse(cn.is_pool_title(t), repr(t))

    def test_unnamed_welcome(self):
        self.assertTrue(cn.is_unnamed_welcome('Welcome to Notion'))
        self.assertTrue(cn.is_unnamed_welcome('Welcome'))
        self.assertTrue(cn.is_unnamed_welcome('Cowork joined'))
        self.assertFalse(cn.is_unnamed_welcome('Cowork 3'))
        self.assertFalse(cn.is_unnamed_welcome('Cowork (retired)'))
        self.assertFalse(cn.is_unnamed_welcome('Roblox'))

    def test_protected(self):
        for t in ('NotionTool', 'Sonstiges', 'Studio432', 'Roblox', 'Main', 'Main Chat', 'Main (Archive 1)'):
            self.assertTrue(cn.is_protected_title(t), t)
        self.assertFalse(cn.is_protected_title('Cowork 1'))


class Numbers(unittest.TestCase):
    def test_new_slots_lowest_free(self):
        self.assertEqual(cn.assign_numbers([W('a'), W('b'), W('c')], None), {'a': 1, 'b': 2, 'c': 3})
        self.assertEqual(cn.assign_numbers([W('a'), W('b'), W('c')], {'a': 1, 'c': 3}), {'a': 1, 'b': 2, 'c': 3})

    def test_stable_and_replacement_keeps_number(self):
        saved = cn.assign_numbers([W('a'), W('b')], {})
        again = cn.assign_numbers([W('b', tid='new-thread'), W('a')], saved)
        self.assertEqual(again, {'a': 1, 'b': 2})

    def test_disabled_and_retired_keep_number(self):
        saved = {'a': 1, 'b': 2}
        workers = [W('a', enabled=False), W('b', state='retired', label='Cowork (retired)'), W('c')]
        self.assertEqual(cn.assign_numbers(workers, saved), {'a': 1, 'b': 2, 'c': 3})

    def test_removed_slot_frees_number(self):
        saved = {'a': 1, 'b': 2, 'c': 3}
        self.assertEqual(cn.assign_numbers([W('a'), W('c'), W('d')], saved), {'a': 1, 'c': 3, 'd': 2})

    def test_main_and_bad_saved_values_ignored(self):
        workers = [W('m', purpose='main', label='Main'), W('a'), W('b'), W('c')]
        numbers = cn.assign_numbers(workers, {'a': 2, 'b': 2, 'c': 'x', 'm': 1})
        self.assertNotIn('m', numbers)
        self.assertEqual(numbers, {'a': 2, 'b': 1, 'c': 3})


class Plan(unittest.TestCase):
    def test_joined_welcome_renamed(self):
        plan = cn.rename_plan([W('a'), W('b', label='Cowork joined')], {}, {})
        self.assertEqual(plan, [('t-a', 'Cowork 1'), ('t-b', 'Cowork 2')])

    def test_already_correct_no_rename(self):
        self.assertEqual(cn.rename_plan([W('a', label='Cowork 1')], {}, {'a': 1}), [])
        self.assertEqual(cn.rename_plan([W('a', label='Cowork 4')], {}, {'a': 1}), [('t-a', 'Cowork 1')])

    def test_user_set_title_skipped(self):
        chats = {'t-a': {'title': 'Welcome to Notion', 'data': {'user_set_title': True}}}
        self.assertEqual(cn.rename_plan([W('a')], chats, {}), [])
        chats = [{'id': 't-a', 'data': {'title': 'Welcome to Notion', 'user_set_title': True}}]
        self.assertEqual(cn.rename_plan([W('a')], chats, {}), [])

    def test_main_and_user_chats_never_touched(self):
        workers = [W('m', purpose='main', label='Main'), W('x', label='Main Chat'), W('n', label='NotionTool'),
                   W('s', label='Sonstiges'), W('st', label='Studio432'), W('r', label='Roblox'), W('u', label='Mein Chat')]
        self.assertEqual(cn.rename_plan(workers, {}, {}), [])
        # The live chat title wins over the stored label.
        chats = {'t-a': {'title': 'Roblox', 'data': {}}}
        self.assertEqual(cn.rename_plan([W('a')], chats, {}), [])

    def test_pending_busy_disabled_retired_skipped(self):
        workers = [W('p', join_state='pending'), W('b', state='busy'), W('l', lease='x'), W('d', enabled=False),
                   W('r', label='Cowork (retired)'), W('q', state='retired'), W('ok')]
        plan = cn.rename_plan(workers, {}, {})
        self.assertEqual(plan, [('t-ok', 'Cowork 7')])

    def test_replacement_chat_gets_slot_number(self):
        saved = {'a': 1, 'b': 2}
        plan = cn.rename_plan([W('a', label='Cowork 1'), W('b', tid='fresh', label='Welcome to Notion')], {}, saved)
        self.assertEqual(plan, [('fresh', 'Cowork 2')])


class Apply(unittest.TestCase):
    def test_apply_renames_via_adapter(self):
        ad = FakeAdapter()
        res = apply_cowork_names(ad, 'client', [W('a'), W('b', join_state='pending')], {}, None, {})
        self.assertEqual(ad.calls, [('client', 't-a', 'Cowork 1')])
        self.assertEqual(res['renamed'], [('t-a', 'Cowork 1')])
        self.assertEqual(res['numbers'], {'a': 1, 'b': 2})

    def test_setting_off_no_rename(self):
        ad = FakeAdapter()
        res = apply_cowork_names(ad, 'client', [W('a')], {}, None, {'cowork_names': False})
        self.assertEqual(ad.calls, [])
        self.assertFalse(res['enabled'])
        self.assertEqual(res['numbers'], {'a': 1})

    def test_rename_failure_reported(self):
        ad = FakeAdapter(fail={'t-a'})
        res = apply_cowork_names(ad, 'c', [W('a'), W('b')], {}, {}, None)
        self.assertEqual(res['renamed'], [('t-b', 'Cowork 2')])
        self.assertEqual(res['failed'][0][:2], ('t-a', 'Cowork 1'))
        self.assertIn('rename refused t-a', res['failed'][0][2])

    def test_pool_decision_numbered_and_retired(self):
        ch, rn, ev = pool_decision({'label': 'Cowork 3', 'join_state': 'joined', 'enabled': True}, DONE)
        self.assertTrue(ch['enabled']); self.assertIsNone(rn); self.assertIsNone(ev)
        ch, rn, ev = pool_decision({'label': 'Cowork (retired)', 'join_state': 'joined'}, DONE)
        self.assertFalse(ch['enabled']); self.assertEqual(ch['join_state'], 'released')


class Source(unittest.TestCase):
    def test_module_is_pure_and_lf(self):
        path = cn.__file__
        data = open(path, 'rb').read()
        self.assertNotIn(b'\r', data)
        text = data.decode('utf-8')
        for banned in ('import os', 'import socket', 'open(', 'requests', 'urllib', 'subprocess'):
            self.assertNotIn(banned, text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
