import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from notiontool.account_settings import account_handlers, open_main
from notiontool.store import Store


class AccountControlsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.store.put('settings', {'id': 'app', 'harness_account_id': 'a'})
        self.store.put('accounts', dict(id='a', email='owner@example.test', state='ready',
                                       identity={'user_name': 'Owner', 'space_id': 'space',
                                                 'spaces': [{'id': 'space', 'name': 'My workspace'}]}))
        self.store.put('chats', {'id': 'keep', 'title': 'Keep this chat'})
        self.store.put('projects', {'id': 'keep-project'})
        self.cdp = Mock()
        self.adapter = SimpleNamespace(lock=threading.RLock(), sessions=Mock(), transports={'a': Mock()},
                                       edge_transports={'a': Mock()}, network_budget=SimpleNamespace(paused=None))
        self.server = SimpleNamespace(store=self.store, adapter=self.adapter, engine=SimpleNamespace(lock=threading.RLock()),
                                      desktop=SimpleNamespace(shell=SimpleNamespace(app=SimpleNamespace(session=self.cdp))),
                                      action=Mock(), job=Mock(), first_login_setup=Mock(return_value={}))
        self.actions = account_handlers(self.server)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_status_reads_profile_and_reports_expired_session_without_secrets(self):
        self.adapter.network_budget.paused = {'account_id': 'a', 'reason': 'HTTP 401'}
        result = self.actions['account_status']({})
        self.assertEqual(result['harness_account_id'], 'a')
        self.assertEqual(result['accounts'][0]['name'], 'Owner')
        self.assertEqual(result['accounts'][0]['space_name'], 'My workspace')
        self.assertIn('401', result['accounts'][0]['error'])
        self.assertNotIn('identity', result['accounts'][0])

    def test_login_opens_only_the_selected_accounts_persisted_main(self):
        account=self.store.get('accounts','a')
        account['team_threads']={'main':{'id':'00000000-0000-4000-8000-000000000002'}}
        self.store.put('accounts',account)
        self.assertTrue(open_main(self.server,'a'))
        self.cdp.send.assert_any_call('Page.navigate',{
            'url':'https://app.notion.com/chat?t=4055ea8ee60a80b285db00a9b662ea8f'})
        self.assertFalse(open_main(self.server,'b'))

    def test_startup_does_not_reload_an_already_open_main(self):
        account=self.store.get('accounts','a')
        account['team_threads']={'main':{'id':'00000000-0000-4000-8000-000000000002'}}
        self.store.put('accounts',account)
        self.cdp.send.return_value={'result':{'value':'https://app.notion.com/chat?t=4055ea8ee60a80b285db00a9b662ea8f'}}
        self.assertTrue(open_main(self.server,'a'))
        self.assertNotIn('Page.navigate',[c.args[0] for c in self.cdp.send.call_args_list])

    def test_open_main_does_not_reopen_a_signed_out_account(self):
        account = self.store.get('accounts', 'a')
        account['team_threads'] = {'main': {'id': '00000000-0000-4000-8000-000000000002'}}
        account['state'] = 'signin'
        self.store.put('accounts', account)
        self.assertFalse(open_main(self.server, 'a'))
        self.cdp.send.assert_not_called()

    def test_desktop_startup_calls_main_routing_once(self):
        from notiontool import desktop_shell as ds
        controller=ds.DesktopController(self.server,exe_finder=lambda:'fake',runtime_check=False)
        self.server.desktop=controller
        controller._make_app=lambda:Mock()
        controller._ensure_protocol=lambda:None
        controller._start_login_assist=lambda:None
        controller._heal_error_page=lambda shell:None
        controller.stop_event.set()
        with patch.object(ds,'DesktopShell',return_value=Mock(alive=True)), \
             patch('notiontool.desktop_state_cache.DesktopStateCache'), \
             patch('notiontool.desktop_state_cache.StatePusher'), \
             patch('notiontool.account_settings.open_main') as route:
            controller._run()
        route.assert_called_once_with(self.server,'a')

    def test_sign_out_clears_only_local_sign_in_and_keeps_user_data(self):
        result = self.actions['account_sign_out']({'account_id': 'a'})
        self.cdp.send.assert_any_call('Network.clearBrowserCookies', {})
        self.cdp.send.assert_any_call('Page.navigate', {'url': 'https://app.notion.com/login'})
        self.assertEqual(self.adapter.sessions.delete.call_count, 2)
        self.assertEqual(self.store.get('accounts', 'a')['state'], 'signin')
        self.assertIsNotNone(self.store.get('chats', 'keep'))
        self.assertIsNotNone(self.store.get('projects', 'keep-project'))
        self.assertEqual(result['navigate'], 'https://app.notion.com/login')

    def test_sign_out_rejects_wrong_account_but_allows_active_work(self):
        with self.assertRaisesRegex(ValueError, 'active account'):
            self.actions['account_sign_out']({'account_id': 'other'})
        self.store.put('workers', {'id': 'w', 'account_id': 'a'})
        self.store.put('tasks', {'id': 't', 'worker_id': 'w', 'state': 'running'})
        result = self.actions['account_sign_out']({'account_id': 'a'})
        self.cdp.send.assert_any_call('Network.clearBrowserCookies', {})
        self.assertEqual(self.store.get('accounts', 'a')['state'], 'signin')
        self.assertEqual(result['navigate'], 'https://app.notion.com/login')

    def test_sign_out_proceeds_even_with_runs_present(self):
        self.store.put('runs', {'id': 'r1', 'account_id': 'a', 'state': 'planning'})
        self.store.put('runs', {'id': 'r2', 'account_id': 'a', 'state': 'executing'})
        cancel = Mock()
        self.server.engine.cancel = cancel
        result = self.actions['account_sign_out']({'account_id': 'a'})
        cancel.assert_not_called()
        self.assertEqual(self.store.get('accounts', 'a')['state'], 'signin')
        self.assertEqual(result['navigate'], 'https://app.notion.com/login')

    def test_switch_rejects_missing_login_without_touching_the_active_session(self):
        self.store.put('accounts', {'id': 'b', 'state': 'signin'})
        self.adapter.sessions.load.return_value = {}
        with self.assertRaisesRegex(ValueError, 'sign in again'):
            self.actions['account_select']({'account_id': 'b'})
        self.server.action.assert_not_called()
        self.cdp.send.assert_not_called()

    def test_sign_in_starts_existing_login_flow_as_a_background_job(self):
        self.actions['account_sign_in']({})
        self.server.job.assert_called_once()
        self.assertEqual(self.server.job.call_args.args[0], 'Sign in to Notion')
        self.cdp.send.assert_not_called()

    def test_switch_installs_selected_login_and_leaves_navigation_to_the_ui(self):
        self.store.put('accounts', {'id': 'b', 'state': 'ready'})
        self.adapter.sessions.load.return_value = {'cookies': [
            {'name': 'token_v2', 'value': 'test-session', 'domain': '.notion.so', 'path': '/'}]}
        result = self.actions['account_select']({'account_id': 'b'})
        self.server.action.assert_called_once_with('/api/account/select', {'account_id': 'b'})
        self.assertIn('Network.setCookies', [c.args[0] for c in self.cdp.send.call_args_list])
        self.assertNotIn('Page.navigate', [c.args[0] for c in self.cdp.send.call_args_list])
        self.server.first_login_setup.assert_called_once_with('b')

    def test_completed_login_uses_new_account_then_opens_its_workspace_once(self):
        self.adapter.browser_login = Mock(return_value={'account_id': 'b'})
        self.adapter.sessions.load.return_value = {'cookies': [
            {'name': 'token_v2', 'value': 'test-session', 'domain': '.notion.so', 'path': '/'}]}
        self.server.resume_after_sign_in = Mock()
        self.actions['account_sign_in']({})
        result = self.server.job.call_args.args[1]()
        self.assertEqual(result['account_id'], 'b')
        self.server.action.assert_called_once_with('/api/account/select', {'account_id': 'b'})
        self.server.resume_after_sign_in.assert_called_once_with('b')
        self.server.first_login_setup.assert_called_once_with('b')


class SettingsRegistrationTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'node is required')
    def test_private_chats_are_included_without_native_sidebar_rows(self):
        source=Path(__file__).resolve().parents[1]/'notiontool/web/desktop/v6.js'
        script=r'''
global.window={}; global.document={};require(process.argv[1]);
const merge=window.__ntDesk.v6Util.mergePrivateChats;
const rows=merge([],[{title:'My new chat',thread_id:'abc',role:'private'},
 {title:'Still creating',pending:true,role:'private'},{title:'Coworker 1',role:'worker'}]);
if(rows.length!==2||rows[1].thread_id!=='abc'||!rows[0].pending)throw Error('Private chat list missing');
if(merge([{title:'My new chat'}],[{title:'My new chat',thread_id:'abc',role:'private'}]).length!==1)throw Error('Duplicate row');
'''
        result=subprocess.run([shutil.which('node'),'-e',script,str(source)],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'node is required')
    def test_settings_uses_real_shell_register_contract_and_unregisters(self):
        source = Path(__file__).resolve().parents[1] / 'notiontool/web/desktop/settings.js'
        script = r'''
global.document = {documentElement:{classList:{toggle(){},remove(){}},setAttribute(){},removeAttribute(){}}};
let args, removed=false;
global.window = {__ntShell:{register(...a){args=a;return ()=>{removed=true}},open(){}},
  addEventListener(){},removeEventListener(){}};
require(process.argv[1]);
const handle=window.__ntDesk.settingsTab({call(){return Promise.resolve({harness:[]})}});
if(args.length!==2 || args[0]!=='settings' || typeof args[1].mount!=='function') throw Error('Settings did not register a view');
handle.stop();
if(!removed) throw Error('Settings did not unregister its view');
console.log('registered and cleaned up');
'''
        result = subprocess.run([shutil.which('node'), '-e', script, str(source)], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)


class PendingPrivateChatsTests(unittest.TestCase):
    setUp = AccountControlsTests.setUp
    tearDown = AccountControlsTests.tearDown
    def test_pending_normal_chat_is_listed_as_private_and_main_is_excluded(self):
        from notiontool.own_chats import private_chats
        self.store.put('app_chats',dict(id='new',account_id='a',name='New normal chat',main=False,state='pending'))
        self.store.put('app_chats',dict(id='main-new',account_id='a',name='Other Main',main=True,state='pending'))
        rows=private_chats(self.store,'a')
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['title'],'New normal chat')
        self.assertTrue(rows[0]['pending'])
