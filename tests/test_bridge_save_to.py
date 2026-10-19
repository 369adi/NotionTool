import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from notiontool.bridge import Bridge, OPERATOR_TOOLS


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def call(self, method, body):
        self.calls.append((method, body))
        return self.response


class FakeAdapter:
    def __init__(self, client):
        self._client = client

    def client(self, account):
        return self._client


class FakeApp:
    def __init__(self, client):
        self.adapter = FakeAdapter(client)


class BridgeSaveToTests(unittest.TestCase):
    """nt_notion_call save_to, with a mocked Notion client only (no network)."""

    def setUp(self):
        base = Path(__file__).resolve().parent / '.test-data'
        base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.top = Path(self.temp.name)
        self.work = self.top / 'project' / 'work'
        self.work.mkdir(parents=True)
        self.response = {'recordMap': {'block': {'a': {'value': {'id': 'a'}}}}, 'cursor': {'stack': []}, 'big': 'x' * 5000}
        self.client = FakeClient(self.response)
        self.app = FakeApp(self.client)
        self.bridge = Bridge(store=None, engine=None)
        patcher = mock.patch.object(Bridge, 'save_root', lambda bridge: self.work)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.temp.cleanup()

    def call(self, **extra):
        args = {'method': 'loadPageChunk', 'body': {'pageId': 'p'}, 'account_id': 'acc'}
        args.update(extra)
        return self.bridge.operator_call(self.app, 'nt_notion_call', args)

    def files(self):
        return sorted(p.relative_to(self.top).as_posix() for p in self.top.rglob('*') if p.is_file())

    def test_schema_has_optional_save_to(self):
        tool = next(t for t in OPERATOR_TOOLS if t['name'] == 'nt_notion_call')
        self.assertIn('save_to', tool['inputSchema']['properties'])
        self.assertNotIn('save_to', tool['inputSchema']['required'])

    def test_without_save_to_returns_the_full_response_unchanged(self):
        for extra in ({}, {'save_to': None}, {'save_to': ''}):
            with self.subTest(extra=extra):
                self.assertEqual(self.call(**extra), self.response)
        self.assertEqual(len(self.client.calls), 3)
        self.assertEqual(self.files(), [])

    def test_valid_path_writes_the_file_and_returns_only_the_summary(self):
        result = self.call(save_to='notion/page.json')
        target = self.work / 'notion' / 'page.json'
        data = target.read_bytes()
        self.assertEqual(set(result), {'path', 'bytes', 'sha256', 'keys'})
        self.assertEqual(result['path'], 'work/notion/page.json')
        self.assertEqual(result['bytes'], len(data))
        self.assertEqual(result['sha256'], hashlib.sha256(data).hexdigest())
        self.assertEqual(result['keys'], ['recordMap', 'cursor', 'big'])
        self.assertEqual(json.loads(data.decode('utf-8')), self.response)
        self.assertNotIn('x' * 100, json.dumps(result))
        self.assertEqual(self.files(), ['project/work/notion/page.json'])

    def test_work_prefix_is_accepted(self):
        result = self.call(save_to='work/a.json')
        self.assertEqual(result['path'], 'work/a.json')
        self.assertEqual(self.files(), ['project/work/a.json'])

    def test_rejected_paths_write_nothing_and_do_not_call_notion(self):
        rejected = ['../outside.json', '..\\outside.json', 'notion/../../outside.json', 'work/../x.json',
                    str(self.top / 'project' / 'x.json'), '/tmp/x.json', 'C:/x.json', 'C:x.json', 'work/', '.']
        for path in rejected:
            with self.subTest(save_to=path):
                with self.assertRaisesRegex(ValueError, 'save_to'):
                    self.call(save_to=path)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.files(), [])
        self.assertFalse((self.top / 'project' / 'outside.json').exists())
        self.assertFalse((self.top / 'outside.json').exists())


if __name__ == '__main__':
    unittest.main()
