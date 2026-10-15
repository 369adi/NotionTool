import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from notiontool.store import Store
from notiontool.account_pages import AccountPages


class AccountPageTests(unittest.TestCase):
    def setUp(self):
        base=Path(__file__).parent/'.test-data';base.mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=base)
        self.store=Store(self.temp.name)
        self.store.put('accounts',{'id':'a','state':'ready','email':'owner@example.test'})
        self.store.put('accounts',{'id':'b','state':'ready','email':'second@example.test'})
        self.adapter=SimpleNamespace(store=self.store,
            client=lambda ident:SimpleNamespace(identity=lambda:{'email':self.store.get('accounts',ident)['email']}),
            sessions=SimpleNamespace(load=lambda ident:{'test_account':ident}))
        self.gate=threading.Event();self.browsers=[]
        outer=self
        class Browser:
            def __init__(self,directory,session):
                self.profile=Path(directory)/session['test_account'];self.edge=Path('edge.exe')
                self.prepare=Mock();self.__exit__=Mock();outer.browsers.append(self)
        self.pages=AccountPages(self.adapter,Browser)

    def tearDown(self):
        self.pages.close();self.gate.set();self.store.close();self.temp.cleanup()

    def test_accounts_use_distinct_profiles_and_debug_port_is_not_exposed_in_visible_window(self):
        processes=[]
        def process(*args,**kwargs):
            result=Mock();result.wait.side_effect=lambda:self.gate.wait(5);processes.append(result);return result
        with patch('notiontool.account_pages.show_owned_window') as reveal, patch('notiontool.account_pages.subprocess.Popen',side_effect=process) as start:
            self.pages.open('a');self.pages.open('b');self.pages.open('a')
            self.assertEqual(start.call_count,2)
            reveal.assert_called()
            commands=[call.args[0] for call in start.call_args_list]
            self.assertNotEqual(commands[0][1],commands[1][1])
            self.assertTrue(all(command[-1]=='https://app.notion.com/' for command in commands))
            self.assertTrue(all('--window-position=80,80' in command and '--window-size=1280,900' in command for command in commands))
            self.assertTrue(all('creationflags' not in call.kwargs and 'startupinfo' not in call.kwargs for call in start.call_args_list))
            self.assertTrue(all(not any('remote-debugging' in arg or 'token' in arg for arg in command) for command in commands))

    def test_wrong_identity_is_rejected_before_any_browser_is_created(self):
        self.adapter.client=lambda _:SimpleNamespace(identity=lambda:{'email':'wrong@example.test'})
        with self.assertRaisesRegex(ValueError,'does not match'):
            self.pages.open('a')
        self.assertEqual(self.browsers,[])
