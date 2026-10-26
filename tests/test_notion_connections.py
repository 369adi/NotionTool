import copy
import hashlib
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from notiontool.notion_connections import connections, desk_handlers, ensure_notiontool


class ConnectionsTests(unittest.TestCase):
    def setUp(self):
        self.account={'id':'a','state':'ready','bridge_connected':True}
        self.account['bridge_owner_fingerprint']=hashlib.sha256(b'owner-private').hexdigest()
        self.settings={'harness_account_id':'a','public_mcp_url':'https://owner.example/mcp'}
        self.data={
            'nt':{'alive':True,'data':{'name':'NotionTool','serverUrl':'https://owner.example/mcp',
                  'runReadToolsAutomatically':True,'runWriteToolsAutomatically':True,'authHeaders':['secret']}},
            'backup':{'alive':True,'data':{'name':'BackupMCP','serverUrl':'https://backup.example/mcp',
                      'runReadToolsAutomatically':False,'runWriteToolsAutomatically':False}}}
        self.view={'id':'view','space_id':'space','settings':{'agent_chat_modules':[
            {'pointer':{'table':'workflow_module','id':ident},'defaultEnabled':True} for ident in self.data]}}
        self.client=SimpleNamespace(info={'space_id':'space','records':{'space_view':{'view':{'value':self.view}}}},
                                    identity=Mock())
        self.client.call=lambda *args,**kwargs:{'recordMap':{'workflow_module':{
            key:{'value':copy.deepcopy(value)} for key,value in self.data.items()}}}
        self.store=SimpleNamespace(settings=lambda:self.settings,get=lambda *args:self.account,
                                   put=lambda table,value:self.account.update(value),all=lambda table:[self.account])
        self.adapter=SimpleNamespace(client=lambda ident:self.client,connect_bridge=Mock(),transaction=self.transaction)
        self.server=SimpleNamespace(store=self.store,adapter=self.adapter,bridge_token='private',host_api_key='owner-private',start_tunnel=Mock())

    def transaction(self,client,action,operations):
        for op in operations:
            self.data[op['pointer']['id']]['data'][op['path'][-1]]=op['args']

    def test_lists_the_actual_connected_mcps_without_credentials(self):
        result=desk_handlers(self.server)['notion_connections']({})
        self.assertEqual([r['name'] for r in result['connections']],['NotionTool','BackupMCP'])
        self.assertNotIn('authHeaders',repr(result))
        self.assertNotIn('secret',repr(result))

    def test_correct_connection_is_reused_without_creating_another(self):
        self.assertTrue(ensure_notiontool(self.server,'a')['write_auto'])
        self.adapter.connect_bridge.assert_not_called()

    def test_missing_connection_is_added_despite_stale_local_connected_flag(self):
        del self.data['nt']
        def connect(*args,**kwargs):
            self.data['nt']={'alive':True,'data':{'name':'NotionTool','serverUrl':self.settings['public_mcp_url'],
                             'runReadToolsAutomatically':True,'runWriteToolsAutomatically':True}}
        self.adapter.connect_bridge.side_effect=connect
        ensure_notiontool(self.server,'a')
        self.adapter.connect_bridge.assert_called_once_with('a','https://owner.example/mcp','owner-private',read_auto=True,write_auto=True,refresh_auth=False)

    def test_old_worker_connection_is_upgraded_to_the_authorized_owner_connection(self):
        self.account.pop('bridge_owner_fingerprint')
        ensure_notiontool(self.server,'a')
        self.adapter.connect_bridge.assert_called_once_with('a','https://owner.example/mcp','owner-private',
                                                            read_auto=True,write_auto=True,refresh_auth=True)
        self.assertNotIn('owner-private',repr(self.account))

    def test_wrong_write_permission_is_repaired_and_read_back(self):
        self.data['nt']['data']['runWriteToolsAutomatically']=False
        self.adapter.connect_bridge.side_effect=lambda *a,**kw:self.data['nt']['data'].update(runWriteToolsAutomatically=True)
        self.assertTrue(ensure_notiontool(self.server,'a')['connected'])
        self.adapter.connect_bridge.assert_called_once()

    def test_permission_changes_are_scoped_and_confirmed_by_read_back(self):
        action=desk_handlers(self.server)['notion_connection_permissions']
        with self.assertRaisesRegex(ValueError,'active account'):
            action({'id':'foreign','read_auto':True,'write_auto':True})
        result=action({'id':'backup','read_auto':True,'write_auto':True})
        self.assertTrue(next(r for r in result['connections'] if r['id']=='backup')['write_auto'])

    def test_failed_repair_is_not_reported_as_connected(self):
        self.data['nt']['data']['runWriteToolsAutomatically']=False
        with self.assertRaisesRegex(ValueError,'not confirmed'):
            ensure_notiontool(self.server,'a')


class OwnerLazyPcToolsTests(unittest.TestCase):
    def test_main_can_use_existing_lazy_shell_without_an_assignment_token(self):
        from notiontool.bridge import Bridge, TOOLS
        bridge=object.__new__(Bridge)
        bridge.tools=lambda:dict(TOOLS)
        bridge.operator_call=Mock(return_value={'ok':True})
        bridge.call=Mock(side_effect=ValueError('assignment required'))
        owner=object()
        result=bridge.tool_value('mcp_call',{'tool_name':'run_shell','arguments':{'command':'Get-Location'}},owner)
        self.assertTrue(result['ok'])
        bridge.operator_call.assert_called_once_with(owner,'nt_shell',{'command':'Get-Location'})
        with self.assertRaisesRegex(ValueError,'assignment required'):
            bridge.tool_value('mcp_call',{'tool_name':'run_shell','assignment_token':'invalid','arguments':{}},owner)
        with self.assertRaisesRegex(ValueError,'assignment required'):
            bridge.tool_value('mcp_call',{'tool_name':'run_shell','arguments':{}},None)

    def test_owner_lazy_discovery_includes_pc_tools(self):
        from notiontool.bridge import Bridge, TOOLS
        bridge=object.__new__(Bridge);bridge.tools=lambda:dict(TOOLS)
        result=bridge.tool_value('mcp_list_tools',{'tool_name':'nt_shell'},object())
        self.assertEqual(result[0]['name'],'nt_shell')
