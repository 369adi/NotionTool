import unittest

from notiontool.plain_protocol import prompt


class ScreenPromptTests(unittest.TestCase):
    def run_for(self, goal):
        return {'goal': goal, 'capacity': 5, 'project_path': '', 'context': ''}

    def test_visual_work_gets_token_and_screen_hint(self):
        run = self.run_for('Build a red obby tower in Roblox Studio')
        task = {'role': 'executor', 'prompt': 'Add the tower', 'acceptance': 'Tower visible', 'files': []}
        text = prompt(task, run, '', 'lease-token', True)
        self.assertIn('assignment_token lease-token', text)
        self.assertIn('screen_capture', text)
        reviewer = dict(task, role='reviewer')
        self.assertIn('screen_capture', prompt(reviewer, self.run_for('Check the Blender render'), '', 'lease-token', True))

    def test_non_visual_and_unavailable_tools_stay_without_token(self):
        task = {'role': 'executor', 'prompt': 'Calculate 7 squared', 'acceptance': 'Check', 'files': []}
        self.assertNotIn('lease-token', prompt(task, self.run_for('Calculate 7 squared'), '', 'lease-token', True))
        visual = self.run_for('Model a chair in Blender')
        self.assertNotIn('lease-token', prompt(task, visual, '', 'lease-token', False))


if __name__ == '__main__':
    unittest.main()
