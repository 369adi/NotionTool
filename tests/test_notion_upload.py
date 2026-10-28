import base64
import io
import json
import tempfile
import unittest
import urllib.parse
from unittest import mock

from PIL import Image

from notiontool import notion_upload, rea
from notiontool.screen_capture import CaptureError, ScreenCapture

THREAD = '00000000-0000-4000-8000-000000000001'
SPACE = '00000000-0000-4000-8000-000000000002'
SOURCE = 'attachment:11111111-2222-3333-4444-555555555555:nt-roblox.jpg'

HOST = 'https://app.notion' + '.com'
TARGETS = [
    dict(type='page', title='Notion', url=HOST + '/some-page', webSocketDebuggerUrl='ws://main'),
    dict(type='page', title='NotionTool \u00b7 Anims', url=HOST + '/chat?t=' + 'ab' * 16, webSocketDebuggerUrl='ws://chat'),
    dict(type='page', title='Tab Bar', url='file:///tabs.html', webSocketDebuggerUrl='ws://tabs'),
]


def b64_image(size=(64, 40), fmt='JPEG'):
    buf = io.BytesIO()
    Image.new('RGB', size, (30, 120, 200)).save(buf, fmt)
    return base64.b64encode(buf.getvalue()).decode('ascii')


class FakeSession:
    def __init__(self, value):
        self.value, self.sent, self.closed = value, [], False

    def send(self, method, params, timeout):
        self.sent.append((method, params, timeout))
        return {'result': {'value': self.value}}

    def close(self):
        self.closed = True


class FileUriTests(unittest.TestCase):
    def test_uri_is_the_viewer_format(self):
        uri = notion_upload.file_uri(SOURCE, THREAD, SPACE)
        self.assertTrue(uri.startswith('file://%7B%22source%22%3A%22attachment%3A'))
        payload = json.loads(urllib.parse.unquote(uri[len('file://'):]))
        self.assertEqual(payload, {'source': SOURCE, 'permissionRecord': {'table': 'thread', 'id': THREAD, 'spaceId': SPACE}})

    def test_the_open_chat_window_is_preferred(self):
        self.assertEqual(notion_upload.pick_target(TARGETS)['webSocketDebuggerUrl'], 'ws://chat')
        self.assertEqual(notion_upload.pick_target(TARGETS[:1])['webSocketDebuggerUrl'], 'ws://main')
        self.assertIsNone(notion_upload.pick_target(TARGETS[2:]))


class UploadTests(unittest.TestCase):
    def test_upload_runs_the_script_in_the_chat_window_and_returns_view_url(self):
        session = FakeSession({'source': SOURCE, 'thread': THREAD, 'space': SPACE})
        seen = []
        def connect(ws):
            seen.append(ws)
            return session
        out = notion_upload.upload(b64_image(), 'image/jpeg', 'nt roblox?.jpg', http_get=lambda url: TARGETS, connect=connect)
        self.assertEqual(seen, ['ws://chat'])
        self.assertTrue(session.closed)
        method, params, _ = session.sent[0]
        self.assertEqual(method, 'Runtime.evaluate')
        self.assertTrue(params['awaitPromise'])
        self.assertIn('nt-roblox-.jpg', params['expression'])
        self.assertEqual(out['view_url'], notion_upload.file_uri(SOURCE, THREAD, SPACE))
        self.assertEqual((out['thread'], out['space']), (THREAD, SPACE))

    def test_script_errors_and_missing_windows_raise(self):
        with self.assertRaisesRegex(RuntimeError, 'no Notion chat'):
            notion_upload.upload(b64_image(), http_get=lambda url: TARGETS,
                                 connect=lambda ws: FakeSession({'error': 'no Notion chat is open in the NotionTool window'}))
        with self.assertRaisesRegex(RuntimeError, 'not open'):
            notion_upload.upload(b64_image(), http_get=lambda url: [], connect=lambda ws: FakeSession({}))
        with self.assertRaises(ValueError):
            notion_upload.upload('', http_get=lambda url: TARGETS, connect=lambda ws: FakeSession({}))


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, notion_upload, 'ACTIVE', notion_upload.ACTIVE)

    def test_inactive_never_uploads(self):
        notion_upload.ACTIVE = False
        info = {}
        with mock.patch.object(notion_upload, 'upload', side_effect=AssertionError('must not upload')):
            self.assertFalse(notion_upload.publish(info, b64_image(), 'image/jpeg'))
        self.assertEqual(info, {})

    def test_failed_upload_keeps_the_inline_path_and_says_why(self):
        notion_upload.ACTIVE = True
        info = {}
        with mock.patch.object(notion_upload, 'upload', side_effect=RuntimeError('the NotionTool app window is not open')):
            self.assertFalse(notion_upload.publish(info, b64_image(), 'image/jpeg'))
        self.assertIn('not open', info['view_error'])
        self.assertNotIn('view_url', info)

    def test_success_sets_view_url_and_hint(self):
        notion_upload.ACTIVE = True
        info = {}
        with mock.patch.object(notion_upload, 'upload', return_value={'view_url': 'file://x'}) as up:
            self.assertTrue(notion_upload.publish(info, b64_image(), 'image/png', 'Roblox Studio'))
        self.assertEqual(info['view_url'], 'file://x')
        self.assertIn('viewFileUrl', info['view_hint'])
        self.assertRegex(up.call_args[0][2], r'^nt-roblox-studio-\d{8}-\d{6}\.png$')


class Events:
    def __init__(self, directory):
        self.directory, self.events, self.values = directory, [], {}

    def event(self, kind, run_id='', **fields):
        self.events.append((kind, run_id, fields))

    def settings(self):
        return self.values


class CaptureUploadTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, notion_upload, 'ACTIVE', notion_upload.ACTIVE)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        windows = [dict(hwnd=33, title='Blender [ship.blend]', process='blender.exe', pid=3, width=1920,
                        height=1040, minimized=False, foreground=False)]
        self.screen = ScreenCapture(Events(tmp.name), lister=lambda: windows,
                                    grabber=lambda hwnd, restore=False: (Image.new('RGB', (800, 600), (40, 90, 160)), 'print_window'))

    def test_uploaded_capture_carries_view_url_instead_of_base64(self):
        notion_upload.ACTIVE = True
        with mock.patch.object(notion_upload, 'upload', return_value={'view_url': 'file://cap'}) as up:
            result = self.screen.call('screen_capture', {'target': 'blender'})
        self.assertNotIn('_image', result)
        self.assertEqual(result['view_url'], 'file://cap')
        self.assertIn('viewFileUrl', result['view_hint'])
        self.assertIn('blender-exe', up.call_args[0][2])

    def test_capture_is_an_error_when_upload_is_off_or_fails(self):
        # Only method (owner 28.10.2026): view_url or an error, never inline base64.
        notion_upload.ACTIVE = False
        with self.assertRaisesRegex(CaptureError, 'upload is off'):
            self.screen.call('screen_capture', {'target': 'blender'})
        notion_upload.ACTIVE = True
        with mock.patch.object(notion_upload, 'upload', side_effect=RuntimeError('down')):
            with self.assertRaisesRegex(CaptureError, 'down'):
                self.screen.call('screen_capture', {'target': 'blender'})


class McpImageTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, notion_upload, 'ACTIVE', notion_upload.ACTIVE)

    def test_mcp_image_results_are_uploaded_too(self):
        notion_upload.ACTIVE = True
        result = {'content': [{'type': 'text', 'text': 'viewport'},
                              {'type': 'image', 'data': b64_image((2000, 1000), 'PNG'), 'mimeType': 'image/png'}]}
        with mock.patch.object(rea, 'save_capture', return_value=''), \
                mock.patch.object(notion_upload, 'upload', return_value={'view_url': 'file://mcp'}):
            out = rea.image_result(result)
        self.assertNotIn('_image', out)
        self.assertEqual(out['view_url'], 'file://mcp')
        self.assertFalse(any(it.get('type') == 'image' for it in out['content']))
        self.assertIn('file://mcp', out['content'][-1]['text'])


if __name__ == '__main__':
    unittest.main()
