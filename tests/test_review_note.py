import tempfile, unittest
from notiontool.engine import review_note


class ReviewNoteTest(unittest.TestCase):
    def test_merged_change_points_to_main(self):
        note = review_note({'patch_state': 'merged', 'patch_worktree': 'gone', 'patch_result': {'main_after': 'abc123def4567'}})
        self.assertIn('already merged', note)
        self.assertIn('abc123def456', note)
        self.assertIn('python -m unittest tests.', note)

    def test_open_copy_points_to_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            note = review_note({'patch_state': 'working', 'patch_worktree': folder})
            self.assertIn(folder, note)
            self.assertNotIn('already merged', note)

    def test_no_reviewed_task(self):
        self.assertIn('python -m unittest', review_note(None))


if __name__ == '__main__':
    unittest.main()
