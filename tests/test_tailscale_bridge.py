import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from notiontool.store import Store
from notiontool.tailscale_bridge import TailscaleBridge


class TailscaleTests(unittest.TestCase):
    def setUp(self):
        root=Path(__file__).parent/'.test-data';root.mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=root);self.store=Store(self.temp.name)
        self.commands=[];self.config={'TCP':{'443':{},'8443':{}},'Web':{}}
        def run(args,**kwargs):
            self.commands.append(args[1:])
            if args[1:]==['status','--json']:
                return SimpleNamespace(returncode=0,stdout=json.dumps({'BackendState':'Running','Self':{'DNSName':'my-pc.example.ts.net.'}}))
            if args[1:]==['funnel','status','--json']:
                return SimpleNamespace(returncode=0,stdout=json.dumps(self.config))
            return SimpleNamespace(returncode=0,stdout='')
        self.bridge=TailscaleBridge(self.store,runner=run)
    def tearDown(self):self.store.close();self.temp.cleanup()
    def test_dedicated_free_port_preserves_existing_refresher_routes(self):
        with patch.object(Path,'is_file',return_value=True):result=self.bridge.start(8873)
        self.assertEqual(result['url'],'https://my-pc.example.ts.net:10000/mcp')
        self.assertIn(['funnel','--bg','--https=10000','http://127.0.0.1:8873'],self.commands)
        self.assertFalse(any('reset' in command for command in self.commands))
    def test_no_free_port_does_not_overwrite_another_application(self):
        self.config['TCP']['10000']={}
        with patch.object(Path,'is_file',return_value=True):
            with self.assertRaisesRegex(ValueError,'occupied'):self.bridge.start(8873)
        self.assertFalse(any('--bg' in command for command in self.commands))
    def test_all_ports_used_by_web_apps_shares_listener_under_own_path(self):
        self.config={'TCP':{p:{'HTTPS':True} for p in ('443','8443','10000')},'Web':{f'my-pc.example.ts.net:{p}':{'Handlers':{'/':{'Proxy':f'http://127.0.0.1:{n}'}}} for p,n in (('443',1),('8443',2),('10000',3))}}
        settings=self.store.settings();settings.update(tailscale_port=10000,tailscale_backend='http://127.0.0.1:3');self.store.put('settings',settings)
        with patch.object(Path,'is_file',return_value=True):result=self.bridge.start(8874)
        self.assertEqual(result['url'],'https://my-pc.example.ts.net:10000/nt-harness/mcp')
        self.assertIn(['funnel','--bg','--https=10000','--set-path=/nt-harness','http://127.0.0.1:8874'],self.commands)
        self.assertFalse(any(c[:2]==['funnel','--bg'] and '--set-path=/nt-harness' not in c for c in self.commands))
    def test_shutdown_does_not_remove_route_if_another_app_replaced_it(self):
        self.bridge.owned_port=10000;self.bridge.target='http://127.0.0.1:8873'
        self.config['Web']['my-pc.example.ts.net:10000']={'Handlers':{'/':{'Proxy':'http://127.0.0.1:9999'}}}
        self.bridge.stop()
        self.assertFalse(any('off' in command for command in self.commands))
    def _shared_route(self, funnel=True, proxy='http://127.0.0.1:8874'):
        host='my-pc.example.ts.net:10000'
        self.config={'TCP':{p:{'HTTPS':True} for p in ('443','8443','10000')},
                     'Web':{host:{'Handlers':{'/':{'Proxy':'http://127.0.0.1:3'},'/nt-harness':{'Proxy':proxy}}}}}
        if funnel:self.config['AllowFunnel']={host:True}
        settings=self.store.settings();settings.update(tailscale_port=10000,tailscale_mount='/nt-harness');self.store.put('settings',settings)
    def test_live_shared_route_is_not_reapplied(self):
        self._shared_route()
        with patch.object(Path,'is_file',return_value=True):result=self.bridge.start(8874)
        self.assertEqual(result['url'],'https://my-pc.example.ts.net:10000/nt-harness/mcp')
        self.assertFalse(any(c[:2]==['funnel','--bg'] for c in self.commands))
    def test_shared_route_without_funnel_is_applied_again(self):
        self._shared_route(funnel=False)
        with patch.object(Path,'is_file',return_value=True):self.bridge.start(8874)
        self.assertIn(['funnel','--bg','--https=10000','--set-path=/nt-harness','http://127.0.0.1:8874'],self.commands)
    def test_changed_shared_target_is_applied_again(self):
        self._shared_route(proxy='http://127.0.0.1:9999')
        with patch.object(Path,'is_file',return_value=True):self.bridge.start(8874)
        self.assertIn(['funnel','--bg','--https=10000','--set-path=/nt-harness','http://127.0.0.1:8874'],self.commands)
    def test_live_dedicated_route_is_not_reapplied(self):
        host='my-pc.example.ts.net:10000'
        self.config={'TCP':{'443':{},'8443':{},'10000':{'HTTPS':True}},'Web':{host:{'Handlers':{'/':{'Proxy':'http://127.0.0.1:8873'}}}},'AllowFunnel':{host:True}}
        settings=self.store.settings();settings.update(tailscale_port=10000);self.store.put('settings',settings)
        with patch.object(Path,'is_file',return_value=True):result=self.bridge.start(8873)
        self.assertEqual(result['url'],'https://my-pc.example.ts.net:10000/mcp')
        self.assertFalse(any(c[:2]==['funnel','--bg'] for c in self.commands))
