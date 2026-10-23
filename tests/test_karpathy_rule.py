"""I52: the Karpathy guidelines (short form) go into every Main and Coworker prompt, once per chat."""
import os
import unittest

from notiontool import agent_guard as g
from notiontool.agent_guard import KARPATHY_RULE

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN = dict(id='r1', goal='Fix the bug', context='', project_path='C:/p', allocation={}, capacity=2)
ROLES = ('architect', 'researcher', 'quality_planner', 'planner', 'reviewer', 'integrator', 'executor')


class KarpathyRuleTests(unittest.TestCase):
    def test_rule_is_short_and_has_four_principles(self):
        self.assertLess(len(KARPATHY_RULE), 800)  # A16: keep prompts light
        for word in ('Think first', 'Simplicity', 'Surgical changes', 'Goal-driven'):
            self.assertIn(word, KARPATHY_RULE)

    def test_with_rule_adds_it_once_for_main_and_coworker(self):
        for coworker in (False, True):
            text = g.with_rule(g.with_rule('x', coworker=coworker), coworker=coworker)
            self.assertEqual(text.count(KARPATHY_RULE), 1, coworker)

    def test_plain_protocol_all_roles(self):
        from notiontool import plain_protocol
        for role in ROLES:
            text = plain_protocol.prompt(dict(id='t1', role=role, prompt='Do X', attempts=1, acceptance='ok'), dict(RUN), '', 'tok', False)
            self.assertIn(KARPATHY_RULE, text, role)
        direct = plain_protocol.prompt(dict(id='t1', role='executor', prompt=''), dict(RUN, allocation={'mode': 'direct'}), '', 'tok', False)
        self.assertIn(KARPATHY_RULE, direct)

    def test_json_protocol(self):
        from notiontool import protocol
        for role in ('planner', 'reviewer', 'executor'):
            text = protocol.prompt_for(dict(id='t1', role=role, prompt='Do X', acceptance='ok'), dict(RUN, capacity=2), '', 'tok')
            self.assertIn(KARPATHY_RULE, text, role)

    def test_prompt_once_sends_it_only_in_first_prompt(self):
        from notiontool import prompt_once
        worker = dict(thread_id='th1', context_base_seq=0)
        prompt = 'Role executor.\n' + KARPATHY_RULE + '\nTask: do X'
        first, state = prompt_once.compact(worker, dict(attempts=1), dict(RUN), prompt)
        self.assertIn(KARPATHY_RULE, first)
        worker['prompt_sent'] = state
        second, _ = prompt_once.compact(worker, dict(attempts=1), dict(RUN), prompt)
        self.assertNotIn(KARPATHY_RULE, second)
        self.assertIn(prompt_once.NOTE, second)
        retry, _ = prompt_once.compact(worker, dict(attempts=2), dict(RUN), prompt)
        self.assertIn(KARPATHY_RULE, retry)

    def test_skill_file_and_memory_rule(self):
        with open(os.path.join(ROOT, 'skills', 'karpathy-guidelines', 'SKILL.md'), encoding='utf-8') as handle:
            skill = handle.read()
        self.assertIn('license: MIT', skill)
        self.assertIn('multica-ai/andrej-karpathy-skills', skill)
        with open(os.path.join(ROOT, 'project-memory', 'NotionTool.md'), encoding='utf-8') as handle:
            memory = handle.read()
        self.assertIn('- I52 ', memory)


if __name__ == '__main__':
    unittest.main()
