"""Pinned private Notion runtime (notion_runtime.py) and its startup self-check. Fakes only."""
import shutil
import tempfile
import types
import unittest
from pathlib import Path

from notiontool import desktop_shell as ds
from notiontool import notion_runtime as nr

UPDATE_YML = ("provider: generic\nurl: 'https://desktop-release.notion-static.com'\nchannel: msix\n"
              "updaterCacheDirName: notion-updater\n")


def make_install(base, version):
    package = Path(base) / ('com.notion.app.desktop.notion_%s_x64' % version)
    notion = package / 'Notion'
    (notion / 'resources').mkdir(parents=True)
    (notion / 'Notion.exe').write_text('exe ' + version, encoding='utf-8')
    (notion / 'resources' / 'app-update.yml').write_text(UPDATE_YML, encoding='utf-8')
    return package


def fake_run(package, version):
    def run(argv, **kwargs):
        return types.SimpleNamespace(stdout='%s\r\n%s\r\n' % (version, package), returncode=0)
    return run


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / 'runtime'

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_first_start_pins_a_private_copy_with_updater_off(self):
        package = make_install(self.tmp / 'apps', '7.40.0.0')
        exe = nr.ensure(run=fake_run(package, '7.40.0.0'), root=self.root)
        self.assertEqual(Path(exe), self.root / '7.40.0.0' / 'Notion' / 'Notion.exe')
        self.assertTrue(Path(exe).exists())
        yml = (self.root / '7.40.0.0' / 'Notion' / 'resources' / 'app-update.yml').read_text(encoding='utf-8')
        self.assertIn(nr.DEAD_URL, yml)
        self.assertNotIn('notion-static.com', yml)
        self.assertTrue((self.root / '7.40.0.0' / 'Notion' / 'resources' / 'app-update.yml.orig').exists())
        # the system install itself is never changed
        self.assertEqual((package / 'Notion' / 'resources' / 'app-update.yml').read_text(encoding='utf-8'), UPDATE_YML)
        self.assertEqual(nr.load_state(self.root)['current'], '7.40.0.0')

    def test_system_update_does_not_change_the_pinned_window(self):
        old = make_install(self.tmp / 'apps', '7.40.0.0')
        first = nr.ensure(run=fake_run(old, '7.40.0.0'), root=self.root)
        new = make_install(self.tmp / 'apps', '7.41.0.0')
        self.assertEqual(nr.ensure(run=fake_run(new, '7.41.0.0'), root=self.root), first)
        self.assertTrue(nr.status(self.root, run=fake_run(new, '7.41.0.0'))['upgrade_available'])

    def test_find_exe_uses_the_pinned_copy(self):
        package = make_install(self.tmp / 'apps', '7.40.0.0')
        exe = ds.find_exe(run=fake_run(package, '7.40.0.0'), root=self.root)
        self.assertTrue(str(exe).startswith(str(self.root)))
        self.assertNotIn('apps', str(exe))

    def test_find_exe_falls_back_to_the_system_install_when_the_copy_fails(self):
        package = make_install(self.tmp / 'apps', '7.40.0.0')
        orig = nr.ensure

        def broken(**kwargs):
            raise OSError('disk full')
        nr.ensure = broken
        try:
            exe = ds.find_exe(run=fake_run(package, '7.40.0.0'), root=self.root)
        finally:
            nr.ensure = orig
        self.assertEqual(Path(exe), package / 'Notion' / 'Notion.exe')

    def test_upgrade_is_on_trial_until_marked_good_and_rolls_back(self):
        old = make_install(self.tmp / 'apps', '7.40.0.0')
        nr.ensure(run=fake_run(old, '7.40.0.0'), root=self.root)
        self.assertTrue(nr.mark_good('7.40.0.0', self.root))
        new = make_install(self.tmp / 'apps', '7.41.0.0')
        exe = nr.upgrade(run=fake_run(new, '7.41.0.0'), root=self.root)
        self.assertIn('7.41.0.0', exe)
        state = nr.load_state(self.root)
        self.assertEqual((state['current'], state['last_good']), ('7.41.0.0', '7.40.0.0'))
        self.assertIsNone(nr.upgrade(run=fake_run(new, '7.41.0.0'), root=self.root))
        self.assertTrue(nr.rollback(self.root))
        self.assertEqual(nr.load_state(self.root)['current'], '7.40.0.0')
        self.assertIn('7.40.0.0', nr.pinned_exe(self.root))
        self.assertFalse(nr.rollback(self.root))

    def test_keeps_at_most_two_versions(self):
        for version in ('7.40.0.0', '7.41.0.0', '7.42.0.0'):
            package = make_install(self.tmp / 'apps', version)
            nr.upgrade(run=fake_run(package, version), root=self.root) or nr.ensure(
                run=fake_run(package, version), root=self.root)
            nr.mark_good(version, self.root)
        state = nr.load_state(self.root)
        self.assertEqual(state['versions'], ['7.41.0.0', '7.42.0.0'])
        self.assertFalse((self.root / '7.40.0.0').exists())

    def test_ua_version_matching(self):
        ua = 'Mozilla/5.0 Notion/7.40.0 Chrome/152.0.7977.167 Electron/44.9.1'
        self.assertEqual(nr.ua_version(ua), '7.40.0')
        self.assertTrue(nr.matches('7.40.0.0', ua))
        self.assertFalse(nr.matches('7.41.0.0', ua))
        self.assertFalse(nr.matches('7.40.0.0', 'Chrome/1'))


class _Session:
    def __init__(self, value):
        self.value = value

    def send(self, method, params=None, timeout=None):
        return {'result': {'value': self.value}}


class _Store:
    def __init__(self):
        self.events = []

    def event(self, kind, **kwargs):
        self.events.append((kind, kwargs))


class SelfCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / 'runtime'
        for version in ('7.40.0.0', '7.41.0.0'):
            package = make_install(self.tmp / 'apps', version)
            nr.upgrade(run=fake_run(package, version), root=self.root)
            if version == '7.40.0.0':
                nr.mark_good(version, self.root)
        self.store = _Store()
        server = types.SimpleNamespace(store=self.store)
        self.ctl = ds.DesktopController(server, exe_finder=lambda: 'x', runtime_check=True,
                                        runtime_root=self.root, stopper=lambda profile: [])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def shell(self, value):
        return types.SimpleNamespace(app=types.SimpleNamespace(session=_Session(value)))

    def test_overlay_loaded_marks_the_trial_version_good(self):
        ok = self.ctl._verify_runtime(self.shell({'installed': True, 'ua': 'Notion/7.41.0 Chrome/1'}), tries=1, interval=0)
        self.assertTrue(ok)
        self.assertEqual(nr.load_state(self.root)['last_good'], '7.41.0.0')
        self.assertTrue(self.ctl.runtime['ok'])

    def test_overlay_missing_rolls_back_and_restarts(self):
        ok = self.ctl._verify_runtime(self.shell({'installed': False, 'ua': 'Notion/7.41.0 Chrome/1'}), tries=1, interval=0)
        self.assertFalse(ok)
        self.assertTrue(self.ctl._restart_after)
        self.assertEqual(nr.load_state(self.root)['current'], '7.40.0.0')
        self.assertIn('desktop_overlay_missing', [e[0] for e in self.store.events])

    def test_overlay_missing_on_a_window_not_from_the_trial_version_only_reports(self):
        ok = self.ctl._verify_runtime(self.shell({'installed': False, 'ua': 'Notion/9.0.0'}), tries=1, interval=0)
        self.assertTrue(ok)
        self.assertEqual(self.ctl.state, 'degraded')
        self.assertIn('did not load', self.ctl.error)
        self.assertEqual(nr.load_state(self.root)['current'], '7.41.0.0')

    def test_check_is_off_for_fake_finders_by_default(self):
        server = types.SimpleNamespace(store=self.store)
        self.assertFalse(ds.DesktopController(server, exe_finder=lambda: 'x').runtime_check)
        self.assertTrue(ds.DesktopController(server).runtime_check)


if __name__ == '__main__':
    unittest.main()
