"""devtools.ROOT for a frozen EXE: a dist folder inside the project uses the project folder."""
import sys, tempfile, unittest
from pathlib import Path
from unittest import mock

from notiontool import devtools

SOURCE_ROOT = Path(devtools.__file__).resolve().parent.parent


class DevtoolsRootTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.old = devtools.ROOT, devtools.MEMORY

    def tearDown(self):
        devtools.ROOT, devtools.MEMORY = self.old
        self.temp.cleanup()

    def layout(self, folder='dist', markers=('notiontool', 'project-memory')):
        project = self.base / 'project'
        project.mkdir(exist_ok=True)
        for name in markers:
            (project / name).mkdir(exist_ok=True)
        exe_dir = project / folder
        exe_dir.mkdir(exist_ok=True)
        return project, exe_dir, exe_dir / 'NotionTool.exe'

    def root(self, exe, frozen=True):
        with mock.patch.object(sys, 'frozen', frozen, create=True), \
                mock.patch.object(sys, 'executable', str(exe)):
            return devtools._root()

    def test_dist_with_both_markers_uses_the_parent(self):
        project, _, exe = self.layout()
        self.assertEqual(self.root(exe), project)

    def test_dist_name_is_case_insensitive(self):
        project, _, exe = self.layout(folder='DIST')
        self.assertEqual(self.root(exe), project)

    def test_dist_missing_a_marker_keeps_the_exe_folder(self):
        for markers in (('notiontool',), ('project-memory',), ()):
            with self.subTest(markers=markers):
                temp = tempfile.TemporaryDirectory()
                try:
                    project = Path(temp.name).resolve() / 'project'
                    for name in markers:
                        (project / name).mkdir(parents=True)
                    exe_dir = project / 'dist'
                    exe_dir.mkdir(parents=True)
                    self.assertEqual(self.root(exe_dir / 'NotionTool.exe'), exe_dir)
                finally:
                    temp.cleanup()

    def test_folder_not_named_dist_keeps_the_exe_folder(self):
        _, exe_dir, exe = self.layout(folder='build')
        self.assertEqual(self.root(exe), exe_dir)

    def test_not_frozen_uses_the_source_tree(self):
        _, _, exe = self.layout()
        self.assertEqual(self.root(exe, frozen=False), SOURCE_ROOT)

    def test_backup_and_memory_land_under_the_parent(self):
        project, exe_dir, exe = self.layout()
        root = self.root(exe)
        devtools.ROOT, devtools.MEMORY = root, root / 'project-memory'
        (project / 'a.py').write_bytes(b'x = 1\n')
        backup = Path(devtools.call('nt_write_file', {'path': 'a.py', 'content': 'x = 2\n'})['backup'])
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.relative_to(project / 'work' / 'backups').name, 'a.py')
        devtools.call('nt_memory_update', {'section': 'Status', 'content': 'root check'})
        memory = project / 'project-memory' / 'NotionTool.md'
        self.assertIn('root check', memory.read_text(encoding='utf-8'))
        self.assertFalse((exe_dir / 'work').exists())
        self.assertFalse((exe_dir / 'project-memory').exists())


if __name__ == '__main__':
    unittest.main()
