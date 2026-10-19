"""Rule I37: fixed user rules copied by code into the Main switch brief, open runs with built status."""
import copy
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from notiontool import main_switch as ms

T0 = 1_000_000.0

MEMORY = '\n'.join([
    '# NotionTool project memory',
    '- I0 before the rules heading is not a rule block line',
    '',
    '## Fixed user rules',
    'Intro text that is not a rule.',
    '- I1 First rule, single line.',
    '- I2 Second rule with',
    '  an indented continuation line',
    '\tand a tab continuation line.',
    '- note: plain bullet, not a rule',
    '  continuation of a non-rule (dropped)',
    '- R3 Rule R with  double  spaces and trailing space ',
    '',
    '  indented after a blank line (not a continuation)',
    '- PLAN keep the plan rule',
    '- PLANNED is not a PLAN rule',
    '- I12x is not a rule id',
    '### Sub heading',
    '- I4 rule under a sub heading | with pipe',
    '## Code and files',
    '- I40 rule inside Code and files',
    '    deep continuation',
    '### Code sub',
    '- R41 still inside Code and files',
    '## Later notes',
    '- I99 after the Code and files section is not copied',
]) + '\n'

EXPECTED = '\n'.join([
    '- I1 First rule, single line.',
    '- I2 Second rule with',
    '  an indented continuation line',
    '\tand a tab continuation line.',
    '- R3 Rule R with  double  spaces and trailing space ',
    '- PLAN keep the plan rule',
    '- I4 rule under a sub heading | with pipe',
    '- I40 rule inside Code and files',
    '    deep continuation',
    '- R41 still inside Code and files',
])
EXPECTED_RULES = 7

BRIEF = '\n'.join([
    '## Goal', 'Switch the Main.', '',
    '## Fixed user rules', '- coworker summary of the rules (must be replaced)', '',
    '## Current status', 'All fine.', '',
    '## Running and open tasks', '- t1 running', '',
    '## Next steps', '- continue', '',
    '## Key ids', '- r1',
]) + '\n'


class FakeStore:
    def __init__(self, directory, policy='ask'):
        self.directory = Path(directory)
        self.data, self.events = {}, []
        self._settings = dict(main_context_policy=policy)

    def settings(self):
        return dict(self._settings)

    def get(self, kind, ident, default=None):
        doc = self.data.get(kind, {}).get(str(ident))
        return copy.deepcopy(doc) if doc is not None else default

    def put(self, kind, doc):
        self.data.setdefault(kind, {})[str(doc['id'])] = copy.deepcopy(doc)

    def all(self, kind):
        return [copy.deepcopy(d) for d in self.data.get(kind, {}).values()]

    def event(self, kind, run_id='', **fields):
        self.events.append((kind, run_id, fields))


class GapOps:
    supports_operator = False

    def __init__(self):
        self.created = []

    def create_welcome(self, account_id, request_id):
        self.created.append((account_id, request_id))
        return {}


def task(run, state='done', tests='passed', role='executor', ident=None):
    return dict(id=ident or 't-%s-%s-%s' % (run, state, tests), run_id=run, role=role, state=state,
                proof=dict(tests_status=tests) if tests else {})


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as handle:
        handle.write(data)


class FixedRulesBlockTests(unittest.TestCase):
    def test_every_rule_line_verbatim_with_continuations_in_order(self):
        self.assertEqual(ms.fixed_rules_block(MEMORY), EXPECTED)

    def test_crlf_memory_gives_the_same_block(self):
        self.assertEqual(ms.fixed_rules_block(MEMORY.replace('\n', '\r\n')), EXPECTED)

    def test_rule_count_and_empty_memory(self):
        self.assertEqual(ms.rule_count(EXPECTED), EXPECTED_RULES)
        self.assertEqual(ms.fixed_rules_block(''), '')
        self.assertEqual(ms.fixed_rules_block('# Title\nno rules here\n'), '')

    def test_without_code_and_files_heading_runs_to_end(self):
        text = '## Rules\n- I1 a\n  b\n## Other\n- R2 c\n'
        self.assertEqual(ms.fixed_rules_block(text), '- I1 a\n  b\n- R2 c')


class ApplyToBriefTests(unittest.TestCase):
    def test_rules_section_replaced_by_exact_block(self):
        text, problem = ms.apply_to_brief(BRIEF, EXPECTED, [])
        self.assertEqual(problem, '')
        self.assertNotIn('coworker summary', text)
        self.assertIn(ms.RULES_HEADING + '\n' + EXPECTED + '\n', text)
        self.assertIn(ms.GUARD_RULE, text)
        self.assertNotIn('\r', text)
        self.assertEqual(text.count(ms.RULES_HEADING), 1)

    def test_open_runs_section_after_running_tasks_and_replaced_once(self):
        rows = ms.open_runs_status([dict(id='r9', state='executing', goal='Build X')], [task('r9')])
        text, _ = ms.apply_to_brief(BRIEF, EXPECTED, rows)
        again, _ = ms.apply_to_brief(text, EXPECTED, rows)
        self.assertEqual(again, text)  # deterministic and idempotent
        self.assertEqual(text.count(ms.RUNS_HEADING), 1)
        self.assertLess(text.index('## Running and open tasks'), text.index(ms.RUNS_HEADING))
        self.assertLess(text.index(ms.RUNS_HEADING), text.index('## Next steps'))
        self.assertIn('- r9 | executing | built: yes | Build X | 1/1 executor tasks done, proof tests passed 1/1', text)

    def test_missing_rules_section_is_inserted_after_goal(self):
        brief = '## Goal\nx\n\n## Next steps\n- y\n'
        text, _ = ms.apply_to_brief(brief, EXPECTED, [])
        self.assertLess(text.index('## Goal'), text.index(ms.RULES_HEADING))
        self.assertLess(text.index(ms.RULES_HEADING), text.index('## Next steps'))
        self.assertIn('- none', text)

    def test_no_memory_rules_keeps_coworker_text_with_note(self):
        text, _ = ms.apply_to_brief(BRIEF, '', [])
        self.assertIn(ms.MISSING_NOTE, text)
        self.assertIn('coworker summary', text)
        self.assertIn(ms.GUARD_RULE, text)

    def test_size_limit_cuts_other_sections_never_rules(self):
        big = BRIEF.replace('All fine.', '\n'.join('status line %04d with some padding text' % i for i in range(400)))
        self.assertGreater(len(big), ms.BRIEF_MAX_CHARS)
        text, problem = ms.apply_to_brief(big, EXPECTED, [])
        self.assertEqual(problem, '')
        self.assertLess(len(text), ms.BRIEF_MAX_CHARS)
        self.assertIn(ms.RULES_HEADING + '\n' + EXPECTED + '\n', text)
        self.assertIn(ms.CUT_NOTE, text)
        self.assertIn('## Next steps\n- continue', text)  # cut only as far as needed

    def test_rules_alone_over_limit_are_kept_and_reported(self):
        rules = '\n'.join('- I%d %s' % (i, 'x' * 60) for i in range(200))
        text, problem = ms.apply_to_brief(BRIEF, rules, [], limit=2000)
        self.assertIn(rules, text)
        self.assertIn('rules were not cut', problem)


class OpenRunsStatusTests(unittest.TestCase):
    def test_built_cases(self):
        runs = [dict(id='r_yes', state='executing', created=1, goal='yes'),
                dict(id='r_partly', state='blocked', created=2, goal='partly'),
                dict(id='r_untested', state='paused', created=3, goal='done but no passed tests'),
                dict(id='r_no', state='queued', created=4, goal='no'),
                dict(id='r_unknown', state='planning', created=5, goal='unknown'),
                dict(id='r_failed', state='failed', created=6, goal='failed runs are open')]
        tasks = [task('r_yes', ident='a'), task('r_yes', ident='b'),
                 task('r_yes', state='cancelled', tests='', ident='c'),
                 task('r_yes', role='reviewer', state='running', tests='', ident='d'),
                 task('r_partly', ident='e'), task('r_partly', state='running', tests='', ident='f'),
                 task('r_untested', tests='failed', ident='g'), task('r_untested', tests='', ident='h'),
                 task('r_no', state='queued', tests='', ident='i'),
                 task('r_failed', state='failed', tests='', ident='j')]
        rows = {r['id']: r for r in ms.open_runs_status(runs, tasks)}
        self.assertEqual({k: v['built'] for k, v in rows.items()},
                         dict(r_yes='yes', r_partly='partly', r_untested='partly', r_no='no',
                              r_unknown='unknown', r_failed='no'))
        self.assertEqual(rows['r_yes']['evidence'], '2/2 executor tasks done, proof tests passed 2/2')
        self.assertEqual(rows['r_untested']['evidence'], '2/2 executor tasks done, proof tests passed 0/2')
        self.assertEqual(rows['r_partly']['state'], 'blocked')

    def test_done_and_cancelled_runs_are_left_out_and_order_is_oldest_first(self):
        runs = [dict(id='b', state='executing', created=2), dict(id='x', state='done', created=0),
                dict(id='y', state='cancelled', created=0), dict(id='a', state='executing', created=1)]
        self.assertEqual([r['id'] for r in ms.open_runs_status(runs, [])], ['a', 'b'])

    def test_goal_is_short_single_line_without_pipes(self):
        run = dict(id='r', state='executing', goal='Line one | part\nline two ' + 'z' * 200)
        goal = ms.open_runs_status([run], [])[0]['goal']
        self.assertLessEqual(len(goal), ms.GOAL_CHARS)
        self.assertNotIn('|', goal)
        self.assertNotIn('\n', goal)
        self.assertTrue(goal.endswith('...'))


class FinalizeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.data = os.path.join(self.root, 'data')
        os.makedirs(self.data)
        write(os.path.join(self.root, 'project-memory', 'NotionTool.md'), MEMORY.encode('utf-8'))
        self.store = FakeStore(self.data)
        self.store.put('workers', dict(id='w_op', purpose='operator', thread_id='thread-old', account_id='acct'))
        self.store.put('chats', dict(id='c_op', operator=True, main_worker='w_op', state='active',
                                     project_path=self.root))
        self.store.put('runs', dict(id='r9', state='executing', created=5, goal='Open work'))
        self.store.put('tasks', task('r9', state='running', tests='', ident='t9'))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def read(self, path):
        with open(path, 'rb') as handle:
            return handle.read().decode('utf-8')

    def test_memory_path_is_project_root(self):
        self.assertEqual(ms.memory_path(self.store),
                         os.path.join(os.path.abspath(self.root), 'project-memory', 'NotionTool.md'))

    def test_advance_replaces_coworker_rules_and_adds_open_runs(self):
        record = ms.start(self.store, 'c_op', lambda body: dict(run_id='r1'), 'q1', now=T0)
        self.store.put('runs', dict(id='r1', state='done', created=9, goal='brief'))
        write(record['brief_path'], BRIEF.encode('utf-8'))
        out = ms.advance(self.store, record, GapOps(), now=T0)
        self.assertEqual(out['state'], 'brief_ready')
        self.assertTrue(out['brief_finalized'])
        self.assertEqual(out['brief_rules']['rules'], EXPECTED_RULES)
        text = self.read(record['brief_path'])
        self.assertIn(ms.RULES_HEADING + '\n' + EXPECTED + '\n', text)
        self.assertNotIn('coworker summary', text)
        self.assertIn('- r9 | executing | built: no | Open work', text)
        self.assertNotIn('- r1 |', text)  # the done brief run is not open
        self.assertEqual(ms.read_brief(record['brief_path'])[1], '')

    def test_seeded_brief_is_finalized_in_start(self):
        out = ms.start(self.store, 'c_op', lambda body: dict(run_id='r1'), 'q2', now=T0, brief_text=BRIEF)
        self.assertEqual(out['state'], 'brief_ready')
        self.assertTrue(out['brief_finalized'])
        self.assertIn(EXPECTED, self.read(out['brief_path']))
        self.assertIn(ms.RUNS_HEADING, self.read(out['brief_path']))

    def test_cr_brief_is_left_unchanged_and_fails_as_before(self):
        record = ms.start(self.store, 'c_op', lambda body: dict(run_id='r1'), 'q3', now=T0)
        self.store.put('runs', dict(id='r1', state='done'))
        write(record['brief_path'], b'x\r\ny\r\n')
        out = ms.advance(self.store, record, GapOps(), now=T0)
        self.assertEqual(out['state'], 'failed')
        self.assertIn('CR', out['reason'])

    def test_finalize_never_raises(self):
        info = ms.finalize_brief(self.store, os.path.join(self.data, 'missing.md'))
        self.assertFalse(info['ok'])
        self.assertIn('finalize failed', info['problem'])


class FileTests(unittest.TestCase):
    def test_files_are_lf(self):
        for path in (Path(ms.__file__), Path(__file__)):
            self.assertNotIn(b'\r', path.read_bytes())


if __name__ == '__main__':
    unittest.main()
