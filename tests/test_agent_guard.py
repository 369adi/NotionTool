"""I12 agent guard: the rule is in every generated prompt; the detector flags agent actions only."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from notiontool import agent_guard as g
from notiontool.agent_guard import COWORKER_RULE, GUARD_RULE


class FakeStore:
    def __init__(self, tasks=()):
        self.docs = {'tasks': {t['id']: dict(t) for t in tasks}}
        self.events = []

    def get(self, kind, ident, default=None):
        return self.docs.get(kind, {}).get(ident, default)

    def all(self, kind):
        return list(self.docs.get(kind, {}).values())

    def put(self, kind, doc):
        self.docs.setdefault(kind, {})[doc['id']] = doc
        return doc

    def event(self, kind, run_id='', *, commit=True, **fields):
        self.events.append(dict(fields, kind=kind, run_id=run_id))

    def settings(self):
        return {}


def tool(seq, name, **args):
    return dict(kind='tool_use', sequence=seq, id='e%d' % seq, tool_name=name, input=args)


def transcript(*entities):
    user = dict(kind='user_message', sequence=1, id='u1', client_message_id='step-1')
    return dict(patches=[dict(op='put', entity=e) for e in (user,) + entities])


RUN = dict(id='r1', goal='Fix the bug', context='', project_path='C:/p', allocation={}, capacity=2)


class PromptTests(unittest.TestCase):
    def test_constant_text(self):
        self.assertIn('/api/operator/delegate (nt_api)', GUARD_RULE)
        self.assertIn('NEVER create, update or publish Notion Custom Agents', GUARD_RULE)
        self.assertEqual(COWORKER_RULE, 'You are a coworker: do the task yourself, do not delegate further.')
        once = g.with_rule(g.with_rule('x', coworker=True), coworker=True)
        self.assertEqual(once.count(GUARD_RULE), 1)
        self.assertEqual(once.count(COWORKER_RULE), 1)

    def test_plain_protocol_all_roles(self):
        from notiontool import plain_protocol
        for role in ('architect', 'researcher', 'quality_planner', 'planner', 'reviewer', 'integrator', 'executor'):
            text = plain_protocol.prompt(dict(id='t1', role=role, prompt='Do X', attempts=1, acceptance='ok'), dict(RUN), '', 'tok', False)
            self.assertIn(GUARD_RULE, text, role)
            if role == 'planner':
                self.assertNotIn(COWORKER_RULE, text)
            else:
                self.assertIn(COWORKER_RULE, text, role)
        direct = plain_protocol.prompt(dict(id='t1', role='executor', prompt=''), dict(RUN, allocation={'mode': 'direct'}), '', 'tok', False)
        self.assertIn(GUARD_RULE, direct)

    def test_json_protocol(self):
        from notiontool import protocol
        for role in ('planner', 'reviewer', 'executor'):
            text = protocol.prompt_for(dict(id='t1', role=role, prompt='Do X', acceptance='ok'), dict(RUN, capacity=2), '', 'tok')
            self.assertIn(GUARD_RULE, text, role)
            self.assertEqual(COWORKER_RULE in text, role != 'planner', role)

    def test_main_switch_and_handoff(self):
        from notiontool import main_switch, main_handoff_ops
        self.assertIn('Delegate work to the Coworkers. ' + GUARD_RULE, main_switch.RESUME_INSTRUCTIONS)
        self.assertIn(GUARD_RULE, main_switch.brief_task('C:/x/brief.md', 'work/brief.md')['prompt'])
        self.assertIn(GUARD_RULE, main_handoff_ops.OPERATOR_FIRST_LINES % 'brief.md')

    def test_operator_tool_description(self):
        from notiontool import bridge
        nt_api = next(t for t in bridge.OPERATOR_TOOLS if t['name'] == 'nt_api')
        self.assertIn(GUARD_RULE, nt_api['description'])

    def test_chat_context_handoff(self):
        from notiontool import chat_context
        cls = next(v for v in vars(chat_context).values() if isinstance(v, type) and hasattr(v, 'handoff'))
        for purpose, coworker in (('worker', True), ('main', False)):
            ctx = cls.__new__(cls)
            ctx.store = FakeStore()
            ctx.store.docs['runs'] = {'r1': dict(RUN)}
            text = ctx.handoff(dict(id='w1', purpose=purpose, account_id='a', thread_id='th'), dict(id='t1', run_id='r1'))
            self.assertIn(GUARD_RULE, text)
            self.assertEqual(COWORKER_RULE in text, coworker)

    def test_project_memory_invariant(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, 'project-memory', 'NotionTool.md'), encoding='utf-8') as handle:
            text = handle.read()
        self.assertIn('- I12 ', text)
        self.assertGreaterEqual(text.count(GUARD_RULE), 4)


class DetectorTests(unittest.TestCase):
    FORBIDDEN = ['createAgent', 'updateAgent', 'publishAgent', 'createAutofillAgent', 'spawnSession',
                 'enter-setup-mode', 'connections.notion.createAgent', 'create_trigger', 'createAutomation']

    def test_catches_direct_names(self):
        for name in self.FORBIDDEN:
            self.assertEqual([h['name'] for h in g.forbidden_calls([tool(2, name)])], [name], name)

    def test_catches_wrapped_calls(self):
        calls = [tool(2, 'callFunction', function='connections.notion.createAgent', args={'name': 'Helper'}),
                 tool(3, 'mcp_call', tool_name='runTool', arguments={'toolName': 'publishAgent'}),
                 dict(kind='tool_use', sequence=4, name='runScript', input='{"function": "connections.notion.spawnSession"}')]
        self.assertEqual([h['name'] for h in g.forbidden_calls(calls)],
                         ['connections.notion.createAgent', 'publishAgent', 'connections.notion.spawnSession'])

    def test_ignores_normal_calls(self):
        calls = [tool(2, 'read_file', path='notiontool/agent_guard.py'),
                 tool(3, 'mcp_call', tool_name='run_shell', arguments={'command': 'grep -n createAgent notiontool'}),
                 tool(4, 'callFunction', function='connections.notion.loadPage', args={'url': 'x'}),
                 tool(5, 'callFunction', function='connections.notion.createPage'),
                 dict(kind='assistant_message', sequence=6, text='Never call createAgent.'),
                 dict(kind='tool_result', sequence=7, tool_name='createAgent', output='old result')]
        self.assertEqual(g.forbidden_calls(calls), [])

    def test_report_flags_task_once_and_blocks_accept(self):
        task = dict(id='t1', run_id='r1', state='running', page_id='pg')
        store = FakeStore([task])
        from notiontool.progress_feed import guard_turn
        rec = transcript(tool(2, 'read_file'), tool(3, 'createAgent'))
        hits = guard_turn(store, dict(task, step_id='step-1'), dict(id='w1'), 'th-1', rec)
        self.assertEqual([h['name'] for h in hits], ['createAgent'])
        saved = store.get('tasks', 't1')
        self.assertTrue(g.blocks_accept(saved))
        self.assertTrue(saved['auto_accept_blocked'] and saved['requires_review'])
        self.assertEqual(store.events[0]['kind'], g.EVENT)
        self.assertEqual(store.events[0]['priority'], 'high')
        problem = store.get(g.PROBLEM_KIND, 'agent-t1')
        self.assertEqual(problem['title'], 'A chat tried to create a Notion agent instead of using coworkers')
        self.assertIn('th1', problem['url'].replace('-', ''))
        guard_turn(store, saved, dict(id='w1'), 'th-1', rec)
        self.assertEqual(len(store.events), 1)  # same tool is reported once

    def test_clean_turn_reports_nothing(self):
        store = FakeStore([dict(id='t1', run_id='r1')])
        from notiontool.progress_feed import guard_turn
        self.assertEqual(guard_turn(store, dict(id='t1'), dict(id='w1'), 'th', transcript(tool(2, 'run_shell'))), [])
        self.assertEqual(store.events, [])
        self.assertFalse(g.blocks_accept(store.get('tasks', 't1')))

    def test_engine_never_auto_accepts_flagged(self):
        from notiontool import engine
        with open(engine.__file__, encoding='utf-8') as handle:
            src = handle.read()
        self.assertIn("or t.get('agent_red_flag')]", src)
        self.assertIn('why.append(PROBLEM_TITLE)', src)


if __name__ == '__main__':
    unittest.main()
