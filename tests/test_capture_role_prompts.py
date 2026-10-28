import unittest

from notiontool import bridge, plain_protocol
from notiontool.plain_protocol import prompt

TOKEN = 'tok-ROLE-123'
LINE = 'Images: only via a capture tool that returns a view_url'
CHECK = 'open view_url with viewFileUrl'
NO_RIGHTS = 'no write or shell rights'


def run_for():
    return {'goal': 'Fix the parser in the project', 'project_path': 'C:/proj', 'capacity': 3, 'context': ''}


def task_for(role):
    return {'id': 't-' + role, 'role': role, 'prompt': 'Change parse.py only.', 'acceptance': 'tests pass',
            'files': ['parse.py'], 'attempts': 1, 'task_class': ''}


class CaptureRolePromptTests(unittest.TestCase):
    def build(self, role, tools=True):
        return prompt(task_for(role), run_for(), '', TOKEN, tools)

    def assert_screen_line(self, text):
        self.assertEqual(text.count(LINE), 1, text)
        self.assertIn(CHECK, text)

    def assert_adviser(self, role):
        text = self.build(role)
        self.assert_screen_line(text)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn('assignment_token', text)
        self.assertIn(NO_RIGHTS, text)
        self.assertIn(role, bridge.READ_ONLY_ROLES)

    def test_architect(self):
        self.assert_adviser('architect')

    def test_researcher(self):
        self.assert_adviser('researcher')

    def test_quality_planner(self):
        self.assert_adviser('quality_planner')

    def test_planner(self):
        self.assert_adviser('planner')

    def test_reviewer(self):
        text = self.build('reviewer')
        self.assert_screen_line(text)
        self.assertIn('assignment_token ' + TOKEN, text)
        self.assertNotIn(NO_RIGHTS, text)

    def test_executor(self):
        text = self.build('executor')
        self.assert_screen_line(text)
        self.assertIn('assignment_token ' + TOKEN, text)
        self.assertNotIn(NO_RIGHTS, text)
        self.assertNotIn('executor', bridge.READ_ONLY_ROLES)

    def test_integrator(self):
        text = self.build('integrator')
        self.assert_screen_line(text)
        self.assertNotIn(TOKEN, text)

    def test_existing_tool_text_is_kept(self):
        for role in ('reviewer', 'executor'):
            text = self.build(role)
            self.assertIn('\nCheck your actual tool list before using project tools. If a listed tool requires it, use assignment_token ' + TOKEN + '. This does not confirm any MCP connection.\n', text)

    def test_no_screen_line_without_connected_tools(self):
        for role in ('architect', 'researcher', 'quality_planner', 'planner', 'reviewer', 'executor', 'integrator'):
            text = self.build(role, tools=False)
            self.assertNotIn(LINE, text, role)
            self.assertNotIn(TOKEN, text, role)

    def test_line_constant_has_no_token(self):
        self.assertNotIn('assignment_token', plain_protocol.SCREEN_TOOLS_LINE)
        self.assertIn(LINE, plain_protocol.SCREEN_TOOLS_LINE)


if __name__ == '__main__':
    unittest.main()
