"""Tests for notiontool.role_split: free coworkers, role split and role sections."""
import itertools
import unittest

from notiontool.role_split import (free_coworkers, missing_roles, parse_role_sections, role_sections_prompt,
                                   split_roles)

ROLES = ['planner', 'critic', 'executor', 'tester', 'reviewer']
CHECKERS = {'tester', 'reviewer'}


def groups(split):
    return [part['roles'] for part in split]


def answer(*lines):
    return '\n'.join(lines) + '\n'


def worker(ident, purpose='worker', enabled=True, state='ready', **extra):
    doc = dict(id=ident, purpose=purpose, enabled=enabled, state=state)
    doc.update(extra)
    return doc


class SplitRolesTests(unittest.TestCase):
    def test_one_worker_gets_all_roles(self):
        self.assertEqual(split_roles(ROLES, ['w1']), [{'worker_id': 'w1', 'roles': ROLES}])

    def test_two_workers_builder_never_checker(self):
        split = split_roles(ROLES, ['w1', 'w2'])
        self.assertEqual(groups(split), [['planner', 'critic', 'executor'], ['tester', 'reviewer']])
        builder = next(part for part in split if 'executor' in part['roles'])
        self.assertFalse(CHECKERS & set(builder['roles']))

    def test_two_workers_checkers_moved_and_balanced(self):
        roles = ['executor', 'tester', 'reviewer', 'planner', 'critic']
        self.assertEqual(groups(split_roles(roles, ['w1', 'w2'])),
                         [['executor', 'planner'], ['tester', 'reviewer', 'critic']])

    def test_three_workers_even_split(self):
        split = split_roles(ROLES, ['w1', 'w2', 'w3'])
        self.assertEqual([part['worker_id'] for part in split], ['w1', 'w2', 'w3'])
        self.assertEqual(groups(split), [['planner', 'critic'], ['executor'], ['tester', 'reviewer']])

    def test_four_workers_even_split(self):
        self.assertEqual(groups(split_roles(ROLES, ['a', 'b', 'c', 'd'])),
                         [['planner', 'critic'], ['executor'], ['tester'], ['reviewer']])

    def test_five_workers_one_role_each(self):
        ids = ['w1', 'w2', 'w3', 'w4', 'w5']
        self.assertEqual(split_roles(ROLES, ids),
                         [{'worker_id': ident, 'roles': [role]} for ident, role in zip(ids, ROLES)])

    def test_more_workers_than_roles(self):
        split = split_roles(['planner', 'executor', 'reviewer'], ['w1', 'w2', 'w3', 'w4', 'w5'])
        self.assertEqual([part['worker_id'] for part in split], ['w1', 'w2', 'w3', 'w4', 'w5'])
        self.assertEqual(groups(split), [['planner'], ['executor'], ['reviewer'], [], []])

    def test_empty_worker_list_raises(self):
        with self.assertRaises(ValueError):
            split_roles(ROLES, [])
        with self.assertRaises(ValueError):
            split_roles(ROLES, None)

    def test_order_preserved(self):
        roles = ['reviewer', 'planner', 'executor', 'critic', 'tester']
        split = split_roles(roles, ['w1', 'w2'])
        self.assertEqual(groups(split), [['planner', 'executor'], ['reviewer', 'critic', 'tester']])
        for part in split:
            self.assertEqual(part['roles'], [role for role in roles if role in part['roles']])

    def test_hard_rule_for_every_role_order(self):
        for count in range(2, 7):
            ids = [f'w{n}' for n in range(count)]
            for order in itertools.permutations(ROLES):
                with self.subTest(count=count, order=order):
                    split = split_roles(list(order), ids)
                    self.assertEqual([part['worker_id'] for part in split], ids)
                    flat = [role for part in split for role in part['roles']]
                    self.assertEqual(sorted(flat), sorted(ROLES))
                    for part in split:
                        self.assertEqual(part['roles'], [role for role in order if role in part['roles']])
                        if 'executor' in part['roles']:
                            self.assertFalse(CHECKERS & set(part['roles']))

    def test_one_worker_may_hold_builder_and_checkers(self):
        split = split_roles(['executor', 'tester', 'reviewer'], ['solo'])
        self.assertEqual(groups(split), [['executor', 'tester', 'reviewer']])

    def test_duplicates_used_once_and_inputs_not_changed(self):
        roles = ['planner', 'planner', 'executor']
        ids = ['w1', 'w1', 'w2']
        split = split_roles(roles, ids)
        self.assertEqual(split, [{'worker_id': 'w1', 'roles': ['planner']}, {'worker_id': 'w2', 'roles': ['executor']}])
        self.assertEqual(roles, ['planner', 'planner', 'executor'])
        self.assertEqual(ids, ['w1', 'w1', 'w2'])
        single = list(ROLES)
        self.assertIsNot(split_roles(single, ['w1'])[0]['roles'], single)


class FreeCoworkersTests(unittest.TestCase):
    def test_main_and_non_ready_workers_ignored(self):
        workers = [
            worker('main', purpose='main'),
            worker('kind-main', kind='main'),
            worker('busy', state='busy'),
            worker('off', enabled=False),
            worker('w1'),
            worker('login', purpose='login'),
            'not a dict',
            None,
            worker('w2', enabled=1),
            worker('w1'),
            worker(''),
        ]
        self.assertEqual(free_coworkers(workers), ['w1', 'w2'])

    def test_no_workers(self):
        self.assertEqual(free_coworkers([]), [])
        self.assertEqual(free_coworkers(None), [])

    def test_free_coworkers_feed_split(self):
        workers = [worker('main', purpose='main'), worker('w1'), worker('w2'), worker('w3', state='busy')]
        split = split_roles(ROLES, free_coworkers(workers))
        self.assertEqual([part['worker_id'] for part in split], ['w1', 'w2'])
        self.assertNotIn('main', [part['worker_id'] for part in split])


class RoleSectionsTests(unittest.TestCase):
    def test_prompt_contains_every_heading_and_executor_note(self):
        prompt = role_sections_prompt(ROLES)
        lines = prompt.splitlines()
        for role in ROLES:
            self.assertIn(f'## As {role}:', lines)
        positions = [prompt.index(f'## As {role}:') for role in ROLES]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('nothing found', prompt)
        self.assertIn('hidden thinking', prompt)
        self.assertIn('visible', prompt)
        for words in ('Test-Path', 'line count', 'test output'):
            self.assertIn(words, prompt)

    def test_prompt_without_executor_has_no_proof_note(self):
        prompt = role_sections_prompt(['planner', 'reviewer'])
        self.assertIn('## As reviewer:', prompt)
        self.assertNotIn('Test-Path', prompt)
        self.assertEqual(role_sections_prompt([]), '')

    def test_parse_finds_sections(self):
        text = answer('Intro line.', '## As planner:', 'Plan: two steps.', '', '## As executor:', 'Wrote a.py.',
                      '### Proof', 'Test-Path True', '## As reviewer:', 'nothing found, because the diff is empty.',
                      '## Notes', 'Not a role.')
        parsed = parse_role_sections(text, ['planner', 'executor', 'reviewer', 'tester'])
        self.assertEqual(parsed, {
            'planner': 'Plan: two steps.',
            'executor': 'Wrote a.py.\n### Proof\nTest-Path True',
            'reviewer': 'nothing found, because the diff is empty.',
            'tester': None,
        })
        self.assertEqual(list(parsed), ['planner', 'executor', 'reviewer', 'tester'])

    def test_parse_crlf_and_first_section_wins(self):
        text = '## As planner:\r\nfirst\r\n## As planner:\r\nsecond\r\n'
        self.assertEqual(parse_role_sections(text, ['planner']), {'planner': 'first'})

    def test_heading_must_match_exactly(self):
        text = answer('## As Planner:', 'wrong case', '## As planner', 'no colon', '# As planner:', 'level one')
        self.assertEqual(parse_role_sections(text, ['planner']), {'planner': None})

    def test_missing_roles_reports_missing_and_empty(self):
        text = answer('## As planner:', 'Plan.', '## As critic:', '', '   ', '## As executor:', 'Done.')
        self.assertEqual(missing_roles(text, ROLES), ['critic', 'tester', 'reviewer'])

    def test_missing_roles_none_missing(self):
        text = answer(*[line for role in ROLES for line in (f'## As {role}:', f'Findings for {role}.')])
        self.assertEqual(missing_roles(text, ROLES), [])

    def test_empty_text(self):
        self.assertEqual(missing_roles('', ROLES), ROLES)
        self.assertEqual(parse_role_sections(None, ['planner']), {'planner': None})


if __name__ == '__main__':
    unittest.main()
