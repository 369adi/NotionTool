"""The backup MCP ('MCP Test') must never pause Coworker turns on an approval click (root cause 2026-10-19)."""
import unittest

from notiontool.notion import NotionAdapter
from test_stream_guard import SPACE, Base


class Client:
    info = {'space_id': SPACE}


def module(name, read=True, write=False, alive=True):
    return {'alive': alive, 'data': {'name': name, 'runReadToolsAutomatically': read, 'runWriteToolsAutomatically': write}}


class BackupAutorunTests(Base):
    def adapter_with_log(self):
        adapter = NotionAdapter(self.store)
        adapter.calls = []
        adapter.transaction = lambda client, name, ops: adapter.calls.append((name, ops))
        return adapter

    def test_ask_mode_backup_is_switched_to_automatic(self):
        adapter = self.adapter_with_log()
        changed = adapter.ensure_backup_autorun(Client(), {'m1': module('MCP Test')})
        self.assertEqual(changed, ['m1'])
        name, ops = adapter.calls[0]
        self.assertEqual(name, 'mcpServerSettings.updateToolPermissions')
        self.assertEqual({op['path'][1]: op['args'] for op in ops},
                         {'runReadToolsAutomatically': True, 'runWriteToolsAutomatically': True})

    def test_already_automatic_is_left_alone(self):
        adapter = self.adapter_with_log()
        self.assertEqual(adapter.ensure_backup_autorun(Client(), {'m1': module('MCP Test', write=True)}), [])
        self.assertEqual(adapter.calls, [])

    def test_other_servers_and_bridge_are_untouched(self):
        adapter = self.adapter_with_log()
        records = {'bridge': module('MCP Test'), 'other': module('Ramp'), 'dead': module('MCP Test', alive=False)}
        self.assertEqual(adapter.ensure_backup_autorun(Client(), records, skip='bridge'), [])
        self.assertEqual(adapter.calls, [])

    def test_names_from_settings(self):
        self.store.put('settings', dict(id='app', backup_mcp_names=['Spare MCP']))
        adapter = self.adapter_with_log()
        records = {'a': module('MCP Test'), 'b': module('spare mcp')}
        self.assertEqual(adapter.ensure_backup_autorun(Client(), records), ['b'])


if __name__ == '__main__':
    unittest.main()
