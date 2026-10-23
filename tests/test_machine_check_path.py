import unittest

from notiontool.engine import machine_check_path


class MachineCheckPathTest(unittest.TestCase):
    def test_worktree_prefix_is_stripped(self):
        self.assertEqual(machine_check_path('work/worktrees/t2fba5b3d/notiontool/bridge.py'), 'notiontool/bridge.py')
        self.assertEqual(machine_check_path('work\\worktrees\\te7b0664b\\main.py'), 'main.py')

    def test_dot_folders_are_kept(self):
        self.assertEqual(machine_check_path('.github/workflows/release.yml'), '.github/workflows/release.yml')
        self.assertEqual(machine_check_path('work/worktrees/te7b/.github/workflows/release.yml'), '.github/workflows/release.yml')
        self.assertEqual(machine_check_path('./main.py'), 'main.py')

    def test_scratch_files_are_skipped(self):
        self.assertIsNone(machine_check_path('work/worktrees/t9c9b72d/work/_lane_p_patch.py'))
        self.assertIsNone(machine_check_path('data/notiontool.db'))
        self.assertIsNone(machine_check_path('notiontool/_tmp_probe.py'))
        self.assertIsNone(machine_check_path(''))

    def test_normal_paths_unchanged(self):
        self.assertEqual(machine_check_path('notiontool/engine.py'), 'notiontool/engine.py')
        self.assertEqual(machine_check_path('requirements-dev.txt'), 'requirements-dev.txt')


if __name__ == '__main__':
    unittest.main()
