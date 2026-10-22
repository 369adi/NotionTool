import tempfile, unittest
from pathlib import Path
from notiontool.bridge import Bridge


class FakeStore:
    def __init__(self, tasks):
        self.tasks = tasks

    def get(self, kind, key, default=None):
        return self.tasks.get(key, default)


class ReviewPathTest(unittest.TestCase):
    def bridge(self, tasks):
        b = Bridge.__new__(Bridge)
        b.store = FakeStore(tasks)
        return b

    def test_review_of_merged_change_uses_main(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / '.git').write_text('gitdir: x')
            b = self.bridge({'e': {'patch_worktree': folder, 'patch_state': 'merged'}})
            self.assertIsNone(b.worktree({'review_of': 'e'}))

    def test_review_of_open_change_uses_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / '.git').write_text('gitdir: x')
            b = self.bridge({'e': {'patch_worktree': folder, 'patch_state': 'working'}})
            self.assertEqual(b.worktree({'review_of': 'e'}), Path(folder).resolve())


if __name__ == '__main__':
    unittest.main()
