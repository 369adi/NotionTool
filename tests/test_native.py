import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from notiontool.store import Store
from notiontool.notion import NotionAdapter,NotionClient,apply_patch,stream_text
from notiontool.vendor.transport import NativeNotionTransport
from notiontool.server import Application


class FakeClient:
    def __init__(self):
        self.info=dict(space_id='440869f1-58c1-81c8-b3d1-0003ec9a0dfc',user_id='user',email='test@example.test',
            user_name='Test',spaces=[{'id':'440869f1-58c1-81c8-b3d1-0003ec9a0dfc','name':'Tests'}],records={})
        self.created=[];self.titles={};self.transport=self
    def identity(self):return self.info
    def model(self, selected='opus'):return 'verified-opus-codename'
    def headers(self):return {}
    def threads(self):return [dict(id=ident,title=title) for ident,title in self.titles.items()]
    def stream(self,path,body,headers,timeout):
        self.created.append(body)
        self.titles[body['threadId']]='Welcome to Notion'
        yield json.dumps({'type':'patch-start','data':{'s':[]}})
    def call(self,path,body,timeout=45):
        if path=='saveTransactionsFanout':
            for op in body['transactions'][0]['operations']:
                if op['pointer']['table']=='thread':self.titles[op['pointer']['id']]=op['args']
            return {}
        raise AssertionError(path)


class NativeTests(unittest.TestCase):
    def setUp(self):
        root=Path(__file__).parent/'.test-data';root.mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=root)
        self.store=Store(self.temp.name)
    def tearDown(self):
        self.store.close();self.temp.cleanup()
    def adapter(self):
        adapter=NotionAdapter(self.store)
        self.client=FakeClient()
        adapter.client=lambda *args,**kwargs:self.client
        self.store.put('accounts',dict(id='a',slot=1,email='test@example.test',label='Slot 1',state='ready'))
        return adapter
    def test_exactly_five_welcome_workers_and_one_welcome_main(self):
        adapter=self.adapter();result=adapter.ensure_team('a')
        self.assertEqual(result['welcome_chats'],6)
        self.assertEqual(len(self.client.created),6)
        self.assertTrue(all(b['agentMessageKey']=='unboxing_m5' for b in self.client.created))
        self.assertTrue(all(b['transcript'][0]['value']['oracleThreadType']=='onboarding' for b in self.client.created))
        self.assertTrue(all(b['transcript'][2]['value']=='Welcome to Notion' for b in self.client.created))
        workers=self.store.all('workers')
        self.assertEqual(sum(w['purpose']=='worker' and w['label']=='Cowork joined' for w in workers),5)
        self.assertEqual(sum(w['purpose']=='main' and w['label']=='Main Chat' for w in workers),1)
        self.assertEqual(len(self.store.all('chats')),1)
    def test_team_setup_is_idempotent(self):
        adapter=self.adapter();adapter.ensure_team('a');adapter.ensure_team('a')
        self.assertEqual(len(self.client.created),6)
        self.assertEqual(len(self.store.all('chats')),1)

    def test_all_inference_payloads_override_auto_with_verified_opus(self):
        adapter=self.adapter()
        normal=adapter.body(self.client,'thread','step','Question','verified-opus-codename')
        welcome=adapter.welcome_body(self.client,'thread','verified-opus-codename')
        for body in (normal,welcome):
            config=body['transcript'][0]['value']
            self.assertEqual(config['model'],'verified-opus-codename')
            self.assertIs(config['modelFromUser'],True)

    def test_mcp_connection_attaches_enabled_workspace_module_without_touching_other_modules(self):
        adapter=self.adapter();calls=[]
        view={'id':'view','space_id':self.client.info['space_id'],'settings':{'agent_chat_modules':[{'pointer':{'table':'workflow_module','id':'other'},'defaultEnabled':False}]}}
        self.client.info['records']={'space_view':{'view':{'value':view}},'space':{
            self.client.info['space_id']:{'settings':{'disallow_custom_mcp_servers':False}}}}
        original=self.client.call
        def call(path,body,timeout=45):
            calls.append((path,body))
            if path=='syncRecordValuesSpaceInitial':return {'recordMap':{'workflow_module':{}}}
            if path=='validateMcpConnection':return {'success':True,'tools':[]}
            if path=='postWorkflowsMcpServerConnect':return {'success':True}
            return original(path,body,timeout)
        self.client.call=call
        adapter.connect_bridge('a','https://own.example.ts.net:10000/mcp','secret-test-token')
        changes=[body for path,body in calls if path=='saveTransactionsFanout']
        modules=changes[0]['transactions'][0]['operations'][0]['args']['agent_chat_modules']
        self.assertEqual(modules[0],view['settings']['agent_chat_modules'][0])
        self.assertTrue(modules[1]['defaultEnabled'])
        self.assertTrue(self.store.get('accounts','a')['bridge_connected'])
        self.assertNotIn('secret-test-token',json.dumps(self.store.all('accounts')))
    def test_unknown_creation_is_not_repeated_after_restart(self):
        adapter=self.adapter()
        account=self.store.get('accounts','a');account['team_threads']={'main':{'id':'440869f1-58c1-81c8-b3d1-0003ec9a0dfc','pending':True}}
        self.store.put('accounts',account)
        with self.assertRaisesRegex(ValueError,'unconfirmed'):adapter.ensure_team('a')
        self.assertFalse(self.client.created)

    def test_malformed_welcome_creation_stays_pending_without_rename_or_resend(self):
        from unittest.mock import Mock
        adapter=self.adapter()
        self.client.stream=Mock(return_value=iter(['[']))
        self.client.call=Mock(side_effect=AssertionError('No rename or settings write allowed'))
        with self.assertRaisesRegex(ValueError, 'unconfirmed'):
            adapter.ensure_team('a')
        ledger=self.store.get('accounts','a')['team_threads']
        self.assertEqual(list(ledger), ['main'])
        self.assertTrue(ledger['main']['pending'])
        with self.assertRaisesRegex(ValueError, 'unconfirmed'):
            adapter.ensure_team('a')
        self.client.stream.assert_called_once()
        self.client.call.assert_not_called()

    def test_empty_successful_welcome_stream_does_not_confirm_creation(self):
        from unittest.mock import Mock
        adapter=self.adapter()
        self.client.stream=Mock(return_value=iter([]))
        with self.assertRaisesRegex(ValueError, 'unconfirmed'):
            adapter.ensure_team('a')
        self.assertTrue(self.store.get('accounts','a')['team_threads']['main']['pending'])

    def test_admin_disabled_ai_blocks_welcome_before_any_submission(self):
        adapter=self.adapter()
        self.client.info['records']={'space':{self.client.info['space_id']:{'value':{
            'settings':{'enable_ai_feature':False,'disable_ai_feature':True,'notion_admin_disabled_ai_feature':True}}}}}
        with self.assertRaisesRegex(ValueError,'AI is disabled'):
            adapter.ensure_team('a')
        self.assertFalse(self.client.created)
        self.assertNotIn('team_threads',self.store.get('accounts','a'))
        self.assertEqual(self.store.get('accounts','a')['state'],'ai_disabled')

    def test_missing_ai_flags_are_unknown_not_claimed_enabled(self):
        adapter=self.adapter()
        self.assertEqual(adapter.workspace_ai_access(self.client.info)['state'],'unknown')

    def test_add_worker_does_not_sign_in_again_when_ai_is_disabled(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        self.adapter()
        account=self.store.get('accounts','a')
        account.update(state='ai_disabled',ai_access={'state':'disabled'})
        self.store.put('accounts',account)
        fake=SimpleNamespace(store=self.store,adapter=Mock())
        with self.assertRaisesRegex(ValueError,'AI is disabled'):
            Application.add_worker(fake,'a')
        fake.adapter.automatic_account.assert_not_called()
        fake.adapter.provision.assert_not_called()
    def test_confirmed_creation_is_recovered_without_duplicate(self):
        adapter=self.adapter()
        ident='440869f1-58c1-81c8-b3d1-0003ec9a0dfc'
        account=self.store.get('accounts','a');account['team_threads']={'main':{'id':ident,'pending':True}}
        self.store.put('accounts',account);self.client.titles[ident]='Welcome to Notion'
        adapter.ensure_team('a')
        self.assertEqual(len(self.client.created),5)
        self.assertEqual(self.client.titles[ident],'Main Chat')
    def test_opus_unavailable_is_not_silently_substituted(self):
        class Transport:
            def request(self,*args,**kwargs):return 200,json.dumps({'models':[{'modelProvider':'anthropic','modelMessage':'Sonnet 5.5','model':'sonnet'}]})
        with self.assertRaisesRegex(ValueError,'No model substitution'):
            NotionClient(Transport(),{'space_id':'space'}).model()
    def test_stream_patch_decodes_incremental_response(self):
        steps=[{'type':'agent-inference','value':[{'type':'text','content':'hel'}]}]
        steps=apply_patch(steps,[{'p':'/s/0/value/0/content','o':'x','v':'lo'}])
        self.assertEqual(stream_text(steps),'hello')
    def test_transport_rejects_non_notion_destinations(self):
        with self.assertRaises(ValueError):NativeNotionTransport({},base_url='https://example.com')
        with self.assertRaises(ValueError):NativeNotionTransport({})._build_url('https://example.com/api')
    def test_local_snapshot_does_not_expose_capabilities(self):
        app=Application(self.temp.name)
        app.port=12345
        self.store.put('workers',dict(id='w',enabled=True,state='ready',purpose='worker',lease='secret-assignment'))
        self.store.put('tasks',dict(id='t',run_id='r',state='running',lease='secret-assignment',prompt='private prompt',step_id='step',result='report',partial='partial'))
        snapshot=json.dumps(app.snapshot())
        self.assertNotIn('secret-assignment',snapshot)
        self.assertNotIn('private prompt',snapshot)
        self.assertNotIn(app.bridge_token,snapshot)
        app.close();app.store.close()















    def test_recovery_uses_client_marker_and_stops_at_next_user_turn(self):
        import threading
        from unittest.mock import Mock
        adapter=self.adapter()
        adapter.verify_response_model=Mock(return_value=['verified-opus'])
        self.client.transcript=lambda thread:{'session':{'status':'idle'},'patches':[
            {'op':'put','entity':{'kind':'user_message','sequence':10,'client_message_id':'own-step'}},
            {'op':'put','entity':{'kind':'assistant_message','sequence':11,'content':[{'type':'text','text':'Own result'}]}},
            {'op':'put','entity':{'kind':'user_message','sequence':12,'client_message_id':'other-step'}},
            {'op':'put','entity':{'kind':'assistant_message','sequence':13,'content':[{'type':'text','text':'Other result'}]}}]}
        self.assertEqual(adapter.recover({'account_id':'a','thread_id':'w'},
            {'before_seq':9,'step_id':'own-step'},threading.Event()),'Own result')
        adapter.verify_response_model.assert_called_once()
    def test_recovery_does_not_take_a_later_users_answer(self):
        import threading
        from notiontool.engine import PendingRemote
        adapter=self.adapter()
        self.client.transcript=lambda thread:{'session':{'status':'idle'},'patches':[
            {'op':'put','entity':{'kind':'user_message','sequence':10,'client_message_id':'own-step'}},
            {'op':'put','entity':{'kind':'user_message','sequence':12,'client_message_id':'other-step'}},
            {'op':'put','entity':{'kind':'assistant_message','sequence':13,'content':[{'type':'text','text':'Other result'}]}}]}
        with self.assertRaises(PendingRemote):adapter.recover({'account_id':'a','thread_id':'w'},
            {'before_seq':9,'step_id':'own-step'},threading.Event())
    def test_idle_session_with_pending_server_work_is_not_completed(self):
        import threading
        from notiontool.engine import PendingRemote
        adapter=self.adapter()
        self.client.transcript=lambda thread:{'session':{'status':'idle'},'session_pending':True,'patches':[
            {'op':'put','entity':{'kind':'user_message','sequence':10,'client_message_id':'own-step'}},
            {'op':'put','entity':{'kind':'assistant_message','sequence':11,'content':[{'type':'text','text':'Draft answer'}]}}]}
        with self.assertRaises(PendingRemote):adapter.recover({'account_id':'a','thread_id':'w'},
            {'before_seq':9,'step_id':'own-step'},threading.Event())
    def test_empty_error_stream_reads_committed_reply_without_resubmission(self):
        import threading
        from unittest.mock import Mock
        adapter=self.adapter();account=self.store.get('accounts','a');account['model']='verified-opus';self.store.put('accounts',account)
        task={'id':'t','run_id':'r','role':'executor','lease':'assignment'};self.store.put('tasks',task)
        adapter.new_id=lambda *args:'own-step'
        adapter.ensure_thread_model=Mock(return_value={'explicit':True})
        adapter.verify_response_model=Mock(return_value=['verified-opus'])
        self.client.transcript=Mock(side_effect=[{'session':{'status':'idle','sequence':0}},
            {'session':{'status':'idle','sequence':2},'patches':[
                {'op':'put','entity':{'kind':'user_message','sequence':1,'client_message_id':'own-step'}},
                {'op':'put','entity':{'kind':'assistant_message','sequence':2,'content':[{'type':'text','text':'4, checked'}]}}]}])
        self.client.stream=Mock(return_value=iter([json.dumps({'type':'patch-start','data':{'s':[{'type':'error','value':''}]}})]))
        result=adapter.run({'account_id':'a','thread_id':'w'},task,'What is 2 squared?',threading.Event(),lambda text:None)
        self.assertEqual(result,'4, checked');self.assertEqual(self.client.stream.call_count,1)


if __name__=='__main__':unittest.main()
