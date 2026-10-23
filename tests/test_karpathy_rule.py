"""I52: the Karpathy guidelines (short form) go into every Main and Coworker prompt, once per chat.
Only the Main asks the user when unclear; Coworkers never ask or wait."""
import os
import unittest

from notiontool import agent_guard as g
from notiontool.agent_guard import KARPATHY_RULE, KARPATHY_MAIN_RULE

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN = dict(id='r1', goal='Fix the bug', context='', project_path='C:/p', allocation={}, capacity=2)
ROLES = ('architect', 'researcher', 'quality_planner', 'planner', 'reviewer', 'integrator', 'executor')


def expected(role):
    return KARPATHY_MAIN_RULE if role == 'planner' else KARPATHY_RULE


class KarpathyRuleTests(unittest.TestCase):
    def test_rules_are_short_and_have_four_principles(self):
        for rule in (KARPATHY_RULE, KARPATHY_MAIN_RULE):
            self.assertLess(len(rule), 800)  # A16: keep prompts light
            for word in ('Think first', 'Simplicity', 'Surgical changes', 'Goal-driven'):
                self.assertIn(word, rule)

    def test_only_main_asks(self):
        self.assertIn('ask the user', KARPATHY_MAIN_RULE)
        self.assertNotIn('ask the user', KARPATHY_RULE)
        self.assertIn('do not ask questions or wait', KARPATHY_RULE)

    def test_with_rule_adds_the_right_rule_once(self):
        for coworker, rule, other in ((False, KARPATHY_MAIN_RULE, KARPATHY_RULE), (True, KARPATHY_RULE, KARPATHY_MAIN_RULE)):
            text = g.with_rule(g.with_rule('x', coworker=coworker), coworker=coworker)
            self.assertEqual(text.count(rule), 1, coworker)
            self.assertNotIn(other, text)

    def test_plain_protocol_all_roles(self):
        from notiontool import plain_protocol
        for role in ROLES:
            text = plain_protocol.prompt(dict(id='t1', role=role, prompt='Do X', attempts=1, acceptance='ok'), dict(RUN), '', 'tok', False)
            self.assertIn(expected(role), text, role)
        direct = plain_protocol.prompt(dict(id='t1', role='executor', prompt=''), dict(RUN, allocation={'mode': 'direct'}), '', 'tok', False)
        self.assertTrue(KARPATHY_RULE in direct or KARPATHY_MAIN_RULE in direct)

    def test_json_protocol(self):
        from notiontool import protocol
        for role in ('planner', 'reviewer', 'executor'):
            text = protocol.prompt_for(dict(id='t1', role=role, prompt='Do X', acceptance='ok'), dict(RUN, capacity=2), '', 'tok')
            self.assertIn(expected(role), text, role)

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

    def test_chat_context_handoff(self):
        from notiontool import chat_context
        from test_agent_guard import FakeStore
        cls = next(v for v in vars(chat_context).values() if isinstance(v, type) and hasattr(v, 'handoff'))
        for purpose, rule in (('worker', KARPATHY_RULE), ('main', KARPATHY_MAIN_RULE)):
            ctx = cls.__new__(cls)
            ctx.store = FakeStore()
            ctx.store.docs['runs'] = {'r1': dict(RUN)}
            text = ctx.handoff(dict(id='w1', purpose=purpose, account_id='a', thread_id='th'), dict(id='t1', run_id='r1'))
            self.assertIn(rule, text, purpose)

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
