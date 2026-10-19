import base64
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from notiontool.screen_capture import MAX_PER_TASK, CaptureError, ScreenCapture, pick


class Events:
    def __init__(self, directory):
        self.directory, self.events, self.values = directory, [], {}

    def event(self, kind, run_id='', **fields):
        self.events.append((kind, run_id, fields))

    def settings(self):
        return self.values


WINDOWS = [
    dict(hwnd=11, title='Roblox wiki - Microsoft Edge', process='msedge.exe', pid=1, width=1200, height=900, minimized=False, foreground=True),
    dict(hwnd=22, title='Place1 - Roblox Studio', process='RobloxStudioBeta.exe', pid=2, width=1920, height=1040, minimized=False, foreground=False),
    dict(hwnd=23, title='Output', process='RobloxStudioBeta.exe', pid=2, width=400, height=300, minimized=False, foreground=False),
    dict(hwnd=33, title='Blender [C:\\work\\ship.blend]', process='blender.exe', pid=3, width=1600, height=900, minimized=False, foreground=False),
]


def picture(size=(800, 600), bright_left=False):
    image = Image.new('RGB', size, (40, 90, 160))
    if bright_left:
        image.paste((250, 250, 250), (0, 0, size[0] // 2, size[1]))
    return image


class PickTests(unittest.TestCase):
    def test_alias_prefers_the_app_process_over_a_matching_title(self):
        self.assertEqual(pick(WINDOWS, 'roblox')['hwnd'], 22)
        self.assertEqual(pick(WINDOWS, 'Blender')['hwnd'], 33)

    def test_title_and_handle_select_a_specific_window(self):
        self.assertEqual(pick(WINDOWS, 'output')['hwnd'], 23)
        self.assertEqual(pick(WINDOWS, hwnd=23)['hwnd'], 23)

    def test_missing_target_means_the_whole_screen(self):
        self.assertIsNone(pick(WINDOWS, ''))
        with self.assertRaisesRegex(CaptureError, 'Open apps'):
            pick(WINDOWS, 'maya')


class CaptureTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Events(tmp.name)
        self.frames = [picture()]
        self.screen = ScreenCapture(self.store, lister=lambda: WINDOWS,
                                    grabber=lambda hwnd, restore=False: (self.frames[-1].copy(), 'print_window'))

    def test_capture_returns_an_image_and_logs_it_without_pixels(self):
        result = self.screen.call('screen_capture', {'target': 'roblox', 'reason': 'check build'}, task_id='t1', run_id='r1', role='executor')
        image = result.pop('_image')
        self.assertEqual(image['mimeType'], 'image/jpeg')
        self.assertEqual(Image.open(io.BytesIO(base64.b64decode(image['data']))).size, (800, 600))
        self.assertTrue(result['changed'])
        self.assertNotIn('path', result)
        self.assertFalse(result['stored'])
        self.assertEqual(list(Path(self.store.directory).rglob('*.png')), [])
        kind, run_id, fields = self.store.events[-1]
        self.assertEqual((kind, run_id, fields['task_id'], fields['reason'], fields['process']),
                         ('capture_taken', 'r1', 't1', 'check build', 'RobloxStudioBeta.exe'))
        self.assertLess(len(str(fields)), 2000)

    def test_unchanged_window_can_skip_the_image(self):
        first = self.screen.call('screen_capture', {'target': 'blender'})
        second = self.screen.call('screen_capture', {'target': 'blender', 'only_if_changed': True})
        self.assertFalse(second['changed'])
        self.assertEqual(second['sha256'], first['sha256'])
        self.assertNotIn('_image', second)
        self.frames.append(picture(bright_left=True))
        third = self.screen.call('screen_capture', {'target': 'blender', 'only_if_changed': True})
        self.assertTrue(third['changed'])
        self.assertIn('_image', third)
        self.assertNotEqual(third['sha256'], first['sha256'])

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

    def test_region_and_max_side_shape_the_returned_image(self):
        self.frames.append(picture(size=(3000, 2000)))
        result = self.screen.call('screen_capture', {'target': 'blender', 'max_side': 1000, 'format': 'png'})
        self.assertEqual((result['_image']['mimeType'], result['image']['width']), ('image/png', 1000))
        self.assertIn(result['image']['height'], (666, 667))
        zoom = self.screen.call('screen_capture', {'target': 'blender', 'region': {'x': 100, 'y': 50, 'width': 300, 'height': 200}})
        self.assertEqual((zoom['width'], zoom['height'], zoom['region']), (300, 200, [100, 50, 400, 250]))
        with self.assertRaises(CaptureError):
            self.screen.call('screen_capture', {'target': 'blender', 'region': {'x': 5000, 'y': 0, 'width': 10, 'height': 10}})

    def test_per_task_limit_stops_runaway_loops(self):
        self.screen.counts['t9'] = MAX_PER_TASK
        with self.assertRaisesRegex(CaptureError, 'limit'):
            self.screen.call('screen_capture', {'target': 'blender'}, task_id='t9')


class BridgeImageTests(unittest.TestCase):
    def test_operator_capture_returns_mcp_image_content(self):
        from notiontool.bridge import Bridge
        with tempfile.TemporaryDirectory() as tmp:
            store = Events(tmp)
            bridge = Bridge(store, engine=None)
            bridge._screen = ScreenCapture(store, lister=lambda: WINDOWS, grabber=lambda hwnd, restore=False: (picture(), 'print_window'))
            reply = bridge.rpc({'id': 1, 'method': 'tools/call',
                                'params': {'name': 'nt_screen_capture', 'arguments': {'target': 'roblox'}}}, operator=SimpleNamespace())
        content = reply['result']['content']
        self.assertEqual([c['type'] for c in content], ['text', 'image'])
        self.assertNotIn('_image', content[0]['text'])
        self.assertEqual(content[1]['mimeType'], 'image/jpeg')

    def test_workers_get_the_screen_tools_unless_switched_off(self):
        from notiontool.bridge import Bridge
        with tempfile.TemporaryDirectory() as tmp:
            store = Events(tmp)
            bridge = Bridge(store, engine=None)
            self.assertIn('screen_capture', bridge.tools())
            store.values['screen_capture'] = False
            self.assertNotIn('screen_capture', bridge.tools())


if __name__ == '__main__':
    unittest.main()
