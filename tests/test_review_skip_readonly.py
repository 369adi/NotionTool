import unittest

from notiontool.operator_roles import _read_only


class ReviewSkipReadOnlyTests(unittest.TestCase):
    def test_read_only_flag_without_changes_skips_review(self):
        self.assertTrue(_read_only([dict(read_only=True, proof=dict(files=[]))]))

    def test_read_only_prompt_skips_review(self):
        self.assertTrue(_read_only([dict(prompt='Read-only audit of engine.py')]))

    def test_changed_files_keep_review(self):
        self.assertFalse(_read_only([dict(read_only=True, proof=dict(files=[dict(path='a.py', change='changed')]))]))

    def test_normal_build_keeps_review(self):
        self.assertFalse(_read_only([dict(prompt='Fix the bug in engine.py')]))


if __name__ == '__main__':
    unittest.main()
