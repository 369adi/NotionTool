"""Tests for notiontool.cowork_names and chat_pool.apply_cowork_names (fakes only, no live Notion)."""
import os
import sys
import unittest
from unittest import mock

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
        self.assertEqual(cn.title_for(1), 'Coworker 1')
        self.assertEqual(cn.title_for(12), 'Coworker 12')
        for bad in (0, -1, True, '3', 1.0, None):
            with self.assertRaises(ValueError):
                cn.title_for(bad)

    def test_is_pool_title_true(self):
        for t in ('Welcome to Notion', 'Cowork joined', 'Cowork (retired)', 'Coworker (retired)', 'Cowork 1', 'Cowork 42',
                  'Coworker 1', 'Coworker 42'):
            self.assertTrue(cn.is_pool_title(t), t)

    def test_is_pool_title_false(self):
        for t in ('NotionTool', 'Sonstiges', 'Studio432', 'Roblox', 'Main', 'Main Chat', 'Cowork', 'Coworker', 'Cowork 1 x',
                  ' Cowork 1', 'cowork 1', 'Cowork one', 'welcome to notion', 'Coworker 1 x', '', None, 5):
            self.assertFalse(cn.is_pool_title(t), repr(t))

    def test_numbered_new_and_legacy(self):
        self.assertTrue(cn.is_numbered_title('Coworker 3')); self.assertFalse(cn.is_numbered_title('Cowork 3'))
        self.assertTrue(cn.is_legacy_numbered('Cowork 3')); self.assertFalse(cn.is_legacy_numbered('Coworker 3'))

    def test_unnamed_welcome(self):
        self.assertTrue(cn.is_unnamed_welcome('Welcome to Notion'))
        self.assertTrue(cn.is_unnamed_welcome('Welcome'))
        self.assertTrue(cn.is_unnamed_welcome('Cowork joined'))
        for t in ('Cowork 3', 'Coworker 3', 'Cowork (retired)', 'Coworker (retired)', 'Roblox'):
            self.assertFalse(cn.is_unnamed_welcome(t), t)

    def test_protected(self):
        for t in ('NotionTool', 'Sonstiges', 'Studio432', 'Roblox', 'Main', 'Main Chat', 'Main (Archive 1)', 'Main 2', 'Main2'):
            self.assertTrue(cn.is_protected_title(t), t)
        for t in ('Coworker 1', 'Maintenance', 'Mainframe'):
            self.assertFalse(cn.is_protected_title(t), t)

    def test_slot_number(self):
        self.assertEqual(cn.slot_number('worker7'), 7)
        for bad in ('main', 'worker', 'worker0', 'Worker3', None, 3):
            self.assertIsNone(cn.slot_number(bad), repr(bad))


class Numbers(unittest.TestCase):
    def test_new_slots_lowest_free(self):
        self.assertEqual(cn.assign_numbers([W('a'), W('b'), W('c')], None), {'a': 1, 'b': 2, 'c': 3})
        self.assertEqual(cn.assign_numbers([W('a'), W('b'), W('c')], {'a': 1, 'c': 3}), {'a': 1, 'b': 2, 'c': 3})

    def test_stable_and_replacement_keeps_number(self):
        saved = cn.assign_numbers([W('a'), W('b')], {})
        again = cn.assign_numbers([W('b', tid='new-thread'), W('a')], saved)
        self.assertEqual(again, {'a': 1, 'b': 2})

    def test_ledger_role_number_survives_rotation(self):
        # Slots 1 and 3 rotated, 2 was removed: the new record of worker3 still gets 3.
        ledger = {'worker1': {'id': 't-n1'}, 'worker3': {'id': 't-n3'}, 'main': {'id': 't-m'}}
        workers = [W('n3', tid='t-n3'), W('n1', tid='t-n1'), W('extra')]
        self.assertEqual(cn.assign_numbers(workers, {'n3': 1, 'extra': 3}, ledger), {'n1': 1, 'n3': 3, 'extra': 2})

    def test_ledger_dashless_ids_match(self):
        ledger = {'worker2': {'id': 'abcd1234'}}
        self.assertEqual(cn.assign_numbers([W('x', tid='abcd-1234')], {}, ledger), {'x': 2})

    def test_legacy_title_keeps_number(self):
        self.assertEqual(cn.assign_numbers([W('a', label='Cowork 5'), W('b')], None), {'a': 5, 'b': 1})

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
        self.assertEqual(plan, [('t-a', 'Coworker 1'), ('t-b', 'Coworker 2')])

    def test_legacy_names_migrated_same_number(self):
        plan = cn.rename_plan([W('a', label='Cowork 4'), W('b', label='Cowork 2')], {}, None)
        self.assertEqual(plan, [('t-b', 'Coworker 2'), ('t-a', 'Coworker 4')])

    def test_already_correct_no_rename(self):
        self.assertEqual(cn.rename_plan([W('a', label='Coworker 1')], {}, {'a': 1}), [])
        self.assertEqual(cn.rename_plan([W('a', label='Coworker 4')], {}, {'a': 1}), [('t-a', 'Coworker 1')])
        self.assertEqual(cn.rename_plan([W('a', label='Cowork 1')], {}, {'a': 1}), [('t-a', 'Coworker 1')])

    def test_user_set_title_skipped(self):
        chats = {'t-a': {'title': 'Welcome to Notion', 'data': {'user_set_title': True}}}
        self.assertEqual(cn.rename_plan([W('a')], chats, {}), [])
        chats = [{'id': 't-a', 'data': {'title': 'Welcome to Notion', 'user_set_title': True}}]
        self.assertEqual(cn.rename_plan([W('a')], chats, {}), [])

    def test_own_harness_title_is_not_user_rename(self):
        chats = {'t-a': {'title': 'Coworker 2', 'data': {'user_set_title': True}}}
        w = W('a', label='Coworker 2', harness_title='Coworker 2')
        self.assertEqual(cn.rename_plan([w], chats, {'a': 1}), [('t-a', 'Coworker 1')])
        self.assertFalse(cn.user_renamed(w, chats['t-a']))
        self.assertTrue(cn.user_renamed(W('a'), chats['t-a']))

    def test_main_private_and_user_chats_never_touched(self):
        workers = [W('m', purpose='main', label='Main'), W('x', label='Main Chat'), W('n', label='NotionTool'),
                   W('s', label='Sonstiges'), W('st', label='Studio432'), W('r', label='Roblox'), W('u', label='Mein Chat'),
                   W('p', purpose='private'), W('o', purpose='operator')]
        self.assertEqual(cn.rename_plan(workers, {}, {}), [])
        # Even a managed slot is never renamed when its title is protected or Main.
        ledger = {'worker1': {'id': 't-x'}, 'worker2': {'id': 't-r'}}
        self.assertEqual(cn.rename_plan([W('x', label='Main Chat'), W('r', label='Roblox')], {}, {}, ledger), [])
        # The live chat title wins over the stored label.
        chats = {'t-a': {'title': 'Roblox', 'data': {}}}
        self.assertEqual(cn.rename_plan([W('a')], chats, {}), [])

    def test_maintenance_label_still_named(self):
        plan = cn.rename_plan([W('a', label='Maintenance', pool_admitted=True)], {}, {})
        self.assertEqual(plan, [('t-a', 'Coworker 1')])
        ledger = {'worker2': {'id': 't-b'}}
        self.assertEqual(cn.rename_plan([W('b', label='Maintenance')], {}, {}, ledger), [('t-b', 'Coworker 2')])

    def test_managed_slot_renamed_whatever_title(self):
        ledger = {'worker2': {'id': 't-a'}}
        self.assertEqual(cn.rename_plan([W('a', label='Notion auto title')], {}, {}, ledger), [('t-a', 'Coworker 2')])
        self.assertEqual(cn.rename_plan([W('a', label='Notion auto title')], {}, {}), [])  # unmanaged: needs a pool title

    def test_pending_busy_disabled_retired_skipped(self):
        workers = [W('p', join_state='pending'), W('b', state='busy'), W('l', lease='x'), W('d', enabled=False),
                   W('r', label='Cowork (retired)'), W('rr', label='Coworker (retired)'), W('q', state='retired'), W('ok')]
        plan = cn.rename_plan(workers, {}, {})
        self.assertEqual(plan, [('t-ok', 'Coworker 8')])

    def test_busy_renamed_once_idle(self):
        self.assertEqual(cn.rename_plan([W('a', state='busy')], {}, {}), [])
        self.assertEqual(cn.rename_plan([W('a', state='ready')], {}, {}), [('t-a', 'Coworker 1')])

    def test_retry_at_respected(self):
        w = W('a', rename_retry_at=100.0)
        self.assertEqual(cn.rename_plan([w], {}, {}, now=50.0), [])
        self.assertEqual(cn.rename_plan([w], {}, {}, now=101.0), [('t-a', 'Coworker 1')])

    def test_replacement_chat_gets_slot_number(self):
        saved = {'a': 1, 'b': 2}
        plan = cn.rename_plan([W('a', label='Coworker 1'), W('b', tid='fresh', label='Welcome to Notion')], {}, saved)
        self.assertEqual(plan, [('fresh', 'Coworker 2')])

    def test_replacement_via_ledger_keeps_number(self):
        ledger = {'worker3': {'id': 't-new'}}
        workers = [W('old', tid='t-old', purpose='retired_worker', retired=True, enabled=False, label='Coworker 3'),
                   W('new', tid='t-new', pool_admitted=True)]
        self.assertEqual(cn.rename_plan(workers, {}, {}, ledger), [('t-new', 'Coworker 3')])


class Apply(unittest.TestCase):
    def test_apply_renames_via_adapter(self):
        ad = FakeAdapter()
        res = apply_cowork_names(ad, 'client', [W('a'), W('b', join_state='pending')], {}, None, {})
        self.assertEqual(ad.calls, [('client', 't-a', 'Coworker 1')])
        self.assertEqual(res['renamed'], [('t-a', 'Coworker 1')])
        self.assertEqual(res['numbers'], {'a': 1, 'b': 2})

    def test_setting_off_no_rename(self):
        ad = FakeAdapter()
        res = apply_cowork_names(ad, 'client', [W('a')], {}, None, {'cowork_names': False})
        self.assertEqual(ad.calls, [])
        self.assertFalse(res['enabled'])
        self.assertEqual(res['numbers'], {'a': 1})

    def test_budget_and_max_per_pass(self):
        ad = FakeAdapter()
        res = apply_cowork_names(ad, 'c', [W('a'), W('b')], {}, None, None, budget_ok=False)
        self.assertEqual(ad.calls, []); self.assertEqual(len(res['skipped']), 2)
        res = apply_cowork_names(ad, 'c', [W(str(i)) for i in range(7)], {}, None, None, max_per_pass=5)
        self.assertEqual(len(ad.calls), 5); self.assertEqual(len(res['skipped']), 2)

    def test_rename_failure_reported(self):
        ad = FakeAdapter(fail={'t-a'})
        res = apply_cowork_names(ad, 'c', [W('a'), W('b')], {}, {}, None)
        self.assertEqual(res['renamed'], [('t-b', 'Coworker 2')])
        self.assertEqual(res['failed'][0][:2], ('t-a', 'Coworker 1'))
        self.assertIn('rename refused t-a', res['failed'][0][2])

    def test_pool_decision_numbered_and_retired(self):
        for label in ('Cowork 3', 'Coworker 3'):
            ch, rn, ev = pool_decision({'label': label, 'join_state': 'joined', 'enabled': True}, DONE)
            self.assertTrue(ch['enabled']); self.assertIsNone(rn); self.assertIsNone(ev)
        for label in ('Cowork (retired)', 'Coworker (retired)'):
            ch, rn, ev = pool_decision({'label': label, 'join_state': 'joined'}, DONE)
            self.assertFalse(ch['enabled']); self.assertEqual(ch['join_state'], 'released')


class Wiring(unittest.TestCase):
    class Store:
        def __init__(self):
            self.events = []
        def all(self, kind):
            return [{'id': 'acc', 'state': 'ready'}, {'id': 'off', 'state': 'setup'}] if kind == 'accounts' else []
        def event(self, kind, **kw):
            self.events.append(kind)
        def settings(self):
            return {}

    class Adapter:
        def client(self, ident):
            return 'client-' + ident

    def test_periodic_check_calls_name_coworkers(self):
        from notiontool import team_rotation
        store = self.Store()
        rot = team_rotation.TeamRotator(store, self.Adapter(), lambda *a: None, clock=lambda: 1000.0)
        with mock.patch.object(team_rotation.TeamRotator, 'check', return_value=[]), \
                mock.patch('notiontool.chat_pool.name_coworkers', return_value={'renamed': [('t', 'Coworker 1')]}) as hook:
            rot.maybe_check()
            rot.maybe_check()  # within CHECK_S: no second pass
        hook.assert_called_once()
        self.assertEqual(hook.call_args[0][1:3], ('client-acc', 'acc'))

    def test_periodic_failure_records_event_and_never_raises(self):
        from notiontool import team_rotation
        store = self.Store()
        rot = team_rotation.TeamRotator(store, self.Adapter(), lambda *a: None, clock=lambda: 1000.0)
        with mock.patch.object(team_rotation.TeamRotator, 'check', return_value=[]), \
                mock.patch('notiontool.chat_pool.name_coworkers', side_effect=RuntimeError('x')):
            self.assertEqual(rot.maybe_check(), [])
        self.assertIn('coworker_rename_failed', store.events)

    def test_rotation_calls_naming_after_apply_chat_pool(self):
        import inspect
        from notiontool import team_rotation
        src = inspect.getsource(team_rotation.TeamRotator._rotate_once)
        self.assertLess(src.index('apply_chat_pool(ident, client)'), src.index('name_coworkers(adapter, client, ident'))


class Source(unittest.TestCase):
    def test_module_is_pure_and_lf(self):
        path = cn.__file__
        with open(path, 'rb') as handle:
            data = handle.read()
        self.assertNotIn(b'\r', data)
        text = data.decode('utf-8')
        for banned in ('import os', 'import socket', 'open(', 'requests', 'urllib', 'subprocess'):
            self.assertNotIn(banned, text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
