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

    def screen_for(self, task_class, goal):
        task = {'role': 'executor', 'prompt': 'Do the part', 'acceptance': 'Check', 'files': ['a.txt'], 'task_class': task_class}
        # The visual SCREEN_HINT, not the screen tools line that every role now gets.
        return 'For visual work in desktop apps' in prompt(task, self.run_for(goal), '', 'lease-token', True)

    def test_code_class_gets_no_hint_even_with_keyword(self):
        self.assertFalse(self.screen_for('code', 'Fix the Roblox GUI script'))

    def test_docs_class_gets_no_hint_even_with_keyword(self):
        self.assertFalse(self.screen_for('docs', 'Document the screen capture in Blender'))
        self.assertFalse(self.screen_for(' Docs ', 'Document the screen capture in Blender'))

    def test_visual_class_keeps_hint_without_keyword(self):
        self.assertTrue(self.screen_for('visual', 'Calculate 7 squared'))

    def test_ui_class_keeps_hint_without_keyword(self):
        self.assertTrue(self.screen_for('UI', 'Calculate 7 squared'))

    def test_unknown_class_uses_keyword_match(self):
        self.assertTrue(self.screen_for('backtest', 'Check the Roblox island'))
        self.assertFalse(self.screen_for('backtest', 'Calculate 7 squared'))

    def test_missing_class_uses_keyword_match(self):
        self.assertTrue(self.screen_for('', 'Check the Roblox island'))
        self.assertFalse(self.screen_for(None, 'Calculate 7 squared'))

    def test_policy_classes_never_show_hint_without_tools(self):
        task = {'role': 'executor', 'prompt': 'x', 'acceptance': 'y', 'files': ['a.txt'], 'task_class': 'visual'}
        self.assertNotIn('screen_capture', prompt(task, self.run_for('Roblox'), '', 'lease-token', False))


if __name__ == '__main__':
    unittest.main()
