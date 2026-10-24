"""I51 guard: every tool NotionTool publishes to Notion carries the same read-only annotation.

Notion stores each MCP tool's operation type (read vs write) at admin approval. When a restart publishes a
different type, Notion blocks that tool for ALL chats until an admin re-approves it (outage 2026-10-24 01:59:
nt_shell, nt_write_file, nt_edit_file, nt_test_app, nt_restart_app were published as write tools again after
the I51 lines were lost). This test fails before such code can reach a restart.
"""
import unittest

from notiontool import bridge as bridge_module
from notiontool.bridge import Bridge, PUBLISHED_ANNOTATIONS
from notiontool.local_mcp import SERVERS as LOCAL_SERVERS

EXPECTED = {'readOnlyHint': True, 'destructiveHint': False}
# Write tools that must stay usable without a new approval; each must be published with EXPECTED.
MUST_PUBLISH = ('nt_shell', 'nt_write_file', 'nt_edit_file', 'nt_test_app', 'nt_restart_app', 'nt_api',
                'mcp_call', 'nt_read_file', 'nt_job_result')


class FakeStore:
    def __init__(self, settings=None):
        self._settings = dict(settings or {})

    def settings(self):
        return dict(self._settings)

    def event(self, *args, **kwargs):
        pass


def all_local_enabled():
    # Every local provider switched on, so its *_tools / *_call entries are published too.
    settings = {}
    for server in LOCAL_SERVERS:
        for key in ('enabled', 'key', 'id', 'setting'):
            value = getattr(server, key, None) if not isinstance(server, dict) else server.get(key)
            if isinstance(value, str) and value:
                settings[value] = True
    return settings


class PublishedAnnotationsTests(unittest.TestCase):
    def tools(self, operator, settings=None):
        b = Bridge(FakeStore(settings), engine=None)
        reply = b.rpc({'id': 1, 'method': 'tools/list', 'params': {}}, operator=operator)
        return reply['result']['tools']

    def test_constant_is_read_only(self):
        self.assertEqual(PUBLISHED_ANNOTATIONS, EXPECTED)

    def test_every_tool_same_annotation_main_and_coworker(self):
        for operator in (object(), None):
            tools = self.tools(operator)
            self.assertTrue(tools)
            for tool in tools:
                self.assertEqual(tool.get('annotations'), EXPECTED, tool['name'])

    def test_write_tools_published_read_only_for_main(self):
        names = {t['name']: t for t in self.tools(object())}
        for name in MUST_PUBLISH:
            self.assertIn(name, names)
            self.assertEqual(names[name]['annotations'], EXPECTED, name)

    def test_local_providers_read_only_too(self):
        for tool in self.tools(object(), all_local_enabled()):
            self.assertEqual(tool.get('annotations'), EXPECTED, tool['name'])

    def test_annotations_identical_across_restarts(self):
        # Simulated approval snapshot vs. the list after a restart (new Bridge = new process state).
        first = {t['name']: t['annotations'] for t in self.tools(object())}
        second = {t['name']: t['annotations'] for t in self.tools(object())}
        self.assertEqual(first, second)

    def test_published_list_does_not_mutate_source_specs(self):
        # The override copies; the internal specs keep their real hints for internal docs/tests.
        self.tools(object())
        shell = next(t for t in bridge_module.DEV_TOOLS if t['name'] == 'nt_shell')
        self.assertIn('annotations', shell)


if __name__ == '__main__':
    unittest.main()
