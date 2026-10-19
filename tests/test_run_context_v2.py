"""Tests for notiontool.run_context: run isolation, briefing cap and prompt budget."""
import math
import unittest

from notiontool import context_archive, run_context
from notiontool.run_context import build_run_context, task_prompt_budget, trim_briefing

CODEWORD = 'ZEBRA-7731-QUOKKA'


def entry(task_id, finished, text, **extra):
    doc = dict(task_id=task_id, title=f'Task {task_id}', result=text)
    if finished is not None:
        doc['finished'] = finished
    doc.update(extra)
    return doc


def ids(entries):
    return [doc['task_id'] for doc in entries]


class BuildRunContextTests(unittest.TestCase):
    def test_only_goal_and_briefing_sections(self):
        out = build_run_context('Ship the snapshot module', 'Briefing text')
        self.assertEqual(out, 'Run goal: Ship the snapshot module\n\nProject briefing:\nBriefing text\n')

    def test_goal_dict_reads_only_id_and_title(self):
        goal = dict(id='g1', title='Own goal', messages=[f'chat {CODEWORD}'], reports=[CODEWORD], chat=CODEWORD)
        out = build_run_context(goal, 'Briefing')
        self.assertIn('Run goal: [goal g1] Own goal', out)
        self.assertNotIn(CODEWORD, out)

    def test_codeword_in_other_run_messages_never_appears(self):
        other_run = dict(id='run-other', messages=[f'secret {CODEWORD}'], report=f'report {CODEWORD}')
        goal = dict(id='run-own', title='Own goal', other_runs=[other_run])
        context = build_run_context(goal, 'Project briefing only')
        deps = [entry('t1', 10, 'own dependency result', messages=[CODEWORD], report=CODEWORD, run=other_run)]
        prompt = task_prompt_budget(deps, ['notiontool/run_context.py'], 5000)
        self.assertNotIn(CODEWORD, context)
        self.assertNotIn(CODEWORD, prompt['text'])
        self.assertIn('[result t1] Task t1\nown dependency result', prompt['text'])

    def test_short_briefing_kept(self):
        self.assertEqual(trim_briefing('Short briefing', 2000), 'Short briefing')

    def test_cap_respected_and_marker_present(self):
        briefing = 'START ' + 'x' * 5000 + ' END'
        trimmed = trim_briefing(briefing, 2000)
        self.assertLessEqual(len(trimmed), 2000)
        self.assertTrue(trimmed.startswith('START '))
        self.assertTrue(trimmed.endswith(run_context.TRUNCATION_MARKER))
        self.assertNotIn(' END', trimmed)
        out = build_run_context('Goal', briefing)
        section = out.split('Project briefing:\n', 1)[1][:-1]
        self.assertEqual(section, trimmed)

    def test_cap_respected_for_many_sizes_and_bad_caps(self):
        briefing = 'abc def ' * 1000
        for cap in (0, 1, 10, len(run_context.TRUNCATION_MARKER), 100, 2000, 7999):
            with self.subTest(cap=cap):
                self.assertLessEqual(len(trim_briefing(briefing, cap)), cap)
        for cap in (-5, None, 'bad', float('nan')):
            with self.subTest(cap=cap):
                self.assertEqual(trim_briefing(briefing, cap), '')

    def test_prompt_without_dependencies_under_8000_chars(self):
        goal = dict(id='g1', title='T' * 5000)
        context = build_run_context(goal, 'B' * 50000)
        files = [f'notiontool/module_{n:03d}.py' for n in range(100)]
        prompt = task_prompt_budget([], files, 100000)
        self.assertLess(len(context + prompt['text']), 8000)
        self.assertEqual(prompt['dropped'], [])


class TaskPromptBudgetTests(unittest.TestCase):
    def test_files_limited_with_more_note(self):
        files = [f'f{n:02d}.py' for n in range(25)] + ['f00.py', '']
        text = task_prompt_budget([], files, 1000)['text']
        self.assertEqual(text.count('\n- '), 20)
        self.assertIn('- f19.py', text)
        self.assertNotIn('- f20.py', text)
        self.assertIn('(+5 more)', text)

    def test_newest_first_by_time(self):
        deps = [entry('old', 1, 'old result'), entry('new', 3, 'new result'), entry('mid', 2, 'mid result')]
        text = task_prompt_budget(deps, [], 10000)['text']
        self.assertLess(text.index('new result'), text.index('mid result'))
        self.assertLess(text.index('mid result'), text.index('old result'))

    def test_newest_first_by_sequence(self):
        deps = [entry('s1', None, 'one', sequence=1), entry('s5', None, 'five', sequence=5),
                entry('s3', None, 'three', sequence=3)]
        text = task_prompt_budget(deps, [], 10000)['text']
        self.assertLess(text.index('[result s5]'), text.index('[result s3]'))
        self.assertLess(text.index('[result s3]'), text.index('[result s1]'))

    def test_stable_fallback_without_order_key(self):
        deps = [entry('x', None, 'rx'), entry('y', None, 'ry'), entry('z', None, 'rz')]
        text = task_prompt_budget(deps, [], 10000)['text']
        self.assertLess(text.index('[result x]'), text.index('[result y]'))
        self.assertLess(text.index('[result y]'), text.index('[result z]'))

    def test_oldest_dropped_first_and_whole(self):
        deps = [entry('old', 1, 'o' * 300), entry('new', 3, 'n' * 300), entry('mid', 2, 'm' * 300)]
        one = run_context.estimate_tokens(run_context.result_block(deps[1], 'new'))
        prompt = task_prompt_budget(deps, [], one + 10)
        self.assertEqual(ids(prompt['dropped']), ['old', 'mid'])
        self.assertIn('n' * 300, prompt['text'])
        self.assertNotIn('m', prompt['text'].replace('[result new] Task new', ''))
        self.assertNotIn('o' * 10, prompt['text'])
        self.assertLessEqual(prompt['tokens'], one + 10)
        self.assertEqual(prompt['dropped'][0], deps[0])
        self.assertIsNot(prompt['dropped'][0], deps[0])

    def test_stops_at_first_entry_over_budget(self):
        deps = [entry('big', 5, 'b' * 3000), entry('small', 4, 'tiny')]
        prompt = task_prompt_budget(deps, [], 50)
        self.assertEqual(ids(prompt['dropped']), ['small', 'big'])
        self.assertEqual(prompt['text'], '')
        self.assertEqual(prompt['tokens'], 0)

    def test_zero_budget_drops_all_oldest_first(self):
        deps = [entry('a', 1, 'x'), entry('b', 2, 'y'), entry('c', None, 'z')]
        self.assertEqual(ids(task_prompt_budget(deps, [], 0)['dropped']), ['c', 'a', 'b'])

    def test_bad_or_negative_budget_is_safe(self):
        deps = [entry('a', 1, 'x')]
        for budget in (-5, None, 'abc', float('nan'), float('inf'), True, [3]):
            with self.subTest(budget=budget):
                prompt = task_prompt_budget(deps, [], budget)
                self.assertEqual(prompt['text'], '')
                self.assertEqual(ids(prompt['dropped']), ['a'])
        self.assertEqual(task_prompt_budget(deps, [], '1000')['dropped'], [])

    def test_multibyte_text_counted_in_utf8_bytes(self):
        self.assertEqual(run_context.estimate_tokens('\u20ac\u20ac\u20ac'), 3)
        self.assertEqual(run_context.estimate_tokens('\u00e9'), 1)
        prompt = task_prompt_budget([entry('t1', 1, 'Gr\u00fc\u00dfe \u20ac')], [], 1000)
        raw = prompt['text'].encode('utf-8')
        self.assertIn('Gr\u00fc\u00dfe \u20ac', prompt['text'])
        self.assertEqual(prompt['tokens'], math.ceil(len(raw) / 3))
        self.assertGreater(prompt['tokens'], math.ceil(len(prompt['text']) / 3))

    def test_empty_dependency_list(self):
        self.assertEqual(task_prompt_budget([], [], 100), dict(text='', tokens=0, dropped=[]))
        self.assertEqual(task_prompt_budget(None, None, 100)['dropped'], [])
        prompt = task_prompt_budget([], ['a.py'], 100)
        self.assertEqual(prompt['text'], 'Files in scope:\n- a.py\n\n')

    def test_tokens_match_estimate_tokens(self):
        deps = [entry('t1', 2, 'first'), entry('t2', 1, 'second')]
        prompt = task_prompt_budget(deps, ['a.py', 'b.py'], 1000)
        self.assertEqual(prompt['tokens'], context_archive.estimate_tokens(prompt['text']))
        self.assertIs(run_context.estimate_tokens, context_archive.estimate_tokens)

    def test_non_dict_entries_ignored(self):
        prompt = task_prompt_budget(['raw ' + CODEWORD, None, entry('t1', 1, 'ok')], [], 1000)
        self.assertNotIn(CODEWORD, prompt['text'])
        self.assertEqual(prompt['dropped'], [])
        self.assertIn('[result t1]', prompt['text'])

    def test_inputs_not_changed(self):
        deps = [entry('t2', 1, 'r' * 900), entry('t1', 2, 'r')]
        before = [dict(doc) for doc in deps]
        files = ['a.py']
        task_prompt_budget(deps, files, 20)
        self.assertEqual(deps, before)
        self.assertEqual(files, ['a.py'])


if __name__ == '__main__':
    unittest.main()
