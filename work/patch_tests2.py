"""Adapt screen tests to the new behaviour; screen hint; clean env for the gateway spawn."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
fails = []


def edit(rel, steps):
    path = ROOT / rel
    raw = path.read_bytes().decode('utf-8')
    crlf = '\r\n' in raw
    text = raw.replace('\r\n', '\n')
    for old, new in steps:
        if old not in text:
            fails.append(f'{rel}: missing {old[:70]!r}')
            continue
        text = text.replace(old, new, 1)
    if crlf:
        text = text.replace('\n', '\r\n')
    path.write_bytes(text.encode('utf-8'))


NEW_TESTS = '''

    def test_without_target_the_whole_screen_is_captured(self):
        def no_lister():
            raise AssertionError('window list must not be needed for a screen capture')
        screen = ScreenCapture(self.store, lister=no_lister, grabber=None,
                               screen_grabber=lambda: (picture(size=(2560, 1440)), 'screen'))
        result = screen.call('screen_capture', {})
        self.assertEqual((result['scope'], result['window']['process'], result['window_size']), ('screen', 'screen', [2560, 1440]))
        self.assertIn('_image', result)

    def test_old_stored_captures_are_deleted_and_nothing_new_is_written(self):
        old = Path(self.store.directory) / 'captures' / '2026-10-10'
        old.mkdir(parents=True)
        (old / 'x.png').write_bytes(b'png')
        self.screen.call('screen_capture', {'target': 'blender'})
        self.assertFalse((Path(self.store.directory) / 'captures').exists())
        self.assertEqual(list(Path(self.store.directory).rglob('*.png')), [])
'''

edit('tests/test_screen_capture.py', [
    ("    def test_missing_target_never_falls_back_to_the_whole_screen(self):\n        with self.assertRaises(CaptureError):\n            pick(WINDOWS, '')\n",
     "    def test_missing_target_means_the_whole_screen(self):\n        self.assertIsNone(pick(WINDOWS, ''))\n"),
    ("        self.assertTrue(Path(result['path']).is_file())\n",
     "        self.assertNotIn('path', result)\n        self.assertFalse(result['stored'])\n        self.assertEqual(list(Path(self.store.directory).rglob('*.png')), [])\n"),
    ("    def test_unchanged_window_reuses_the_file_and_can_skip_the_image(self):", "    def test_unchanged_window_can_skip_the_image(self):"),
    ("        self.assertEqual(second['path'], first['path'])\n", "        self.assertEqual(second['sha256'], first['sha256'])\n"),
    ("        self.assertNotEqual(third['path'], first['path'])\n", "        self.assertNotEqual(third['sha256'], first['sha256'])\n" + NEW_TESTS.lstrip('\n').join(['\n', ''])),
])

edit('notiontool/plain_protocol.py', [
    ("' (target \"roblox\" or \"blender\") as often as needed to see the current state before and after changes.')",
     "' (no target = the whole screen; target \"roblox\" or \"blender\" = only that window) as often as needed to see the current state'\n"
     "             ' before and after changes. Screen tools need no assignment token or code.')"),
])

edit('notiontool/server.py', [
    ("        options=dict(stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,close_fds=True,cwd=str(gw.runtime_dir(self.data_dir)))\n",
     "        env={k:v for k,v in os.environ.items() if not k.startswith('_PYI') and k!='_MEIPASS2'}\n"
     "        env['PYINSTALLER_RESET_ENVIRONMENT']='1'  # the gateway EXE unpacks its own copy, independent of this process\n"
     "        options=dict(stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,close_fds=True,cwd=str(gw.runtime_dir(self.data_dir)),env=env)\n"),
])

print('FAILS:', fails or 'none')
sys.exit(1 if fails else 0)
