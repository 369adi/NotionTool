from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent

def patch(rel, pairs):
    p = ROOT / rel
    raw = p.read_bytes().decode('utf-8')
    crlf = '\r\n' in raw
    text = raw.replace('\r\n', '\n')
    for old, new in pairs:
        if text.count(old) != 1:
            raise SystemExit(f'{rel}: expected 1 match, got {text.count(old)} for {old[:70]!r}')
        text = text.replace(old, new)
    p.write_bytes((text.replace('\n', '\r\n') if crlf else text).encode('utf-8'))
    print('patched', rel)

patch('notiontool/bridge.py', [
("]\nfrom .devtools import TOOLS as DEV_TOOLS", """    {'name':'nt_local_mcp','description':'Operator: list the local MCP servers (REA, Roblox, Blender, ...) and their current tools. refresh=true restarts the enabled servers and rediscovers their tools, so new tools work without reconnecting this MCP.',
     'annotations':{'readOnlyHint':True,'destructiveHint':False},
     'inputSchema':{'type':'object','properties':{'server':{'type':'string'},'refresh':{'type':'boolean'}}}},
    {'name':'nt_local_mcp_call','description':'Operator: call a tool of an enabled local MCP server (server = rea, roblox, blender). Use nt_local_mcp to see tools and schemas.',
     'annotations':{'readOnlyHint':False,'destructiveHint':True},
     'inputSchema':{'type':'object','properties':{'server':{'type':'string'},'name':{'type':'string'},'arguments':{'type':'object'}},'required':['server','name']}},
]
from .devtools import TOOLS as DEV_TOOLS"""),
("""    def close_local(self, keys=None):""", """    def local_refresh(self, key=None, restart=True, schemas=False):
        settings = self.store.settings()
        out = []
        for server in LOCAL_SERVERS:
            if key and server['key'] != key:
                continue
            entry = {'server': server['key'], 'name': server['name'], 'enabled': local_enabled(settings, server)}
            if entry['enabled']:
                if restart:
                    self.close_local([server['key']])
                try:
                    tools = self.local_client(server['key']).tools()
                    entry.update(ok=True, count=len(tools),
                                 tools=tools if schemas else [{'name': t.get('name'), 'description': str(t.get('description', ''))[:200]} for t in tools])
                except Exception as exc:
                    entry.update(ok=False, error=str(exc)[:300])
            out.append(entry)
        return out

    def close_local(self, keys=None):"""),
("""        if name in DEV_NAMES: return dev_call(name, args)
""", """        if name in DEV_NAMES: return dev_call(name, args)
        if name=='nt_local_mcp':
            return self.local_refresh(str(args.get('server') or '') or None, bool(args.get('refresh')), schemas=bool(args.get('server')))
        if name=='nt_local_mcp_call':
            server=LOCAL_BY_KEY.get(str(args.get('server','')))
            if server is None: raise ValueError('Unknown local MCP server; use nt_local_mcp')
            if not local_enabled(self.store.settings(), server): raise ValueError('Switch on '+server['label']+' in MCP servers first')
            return self.local_client(server['key']).call(str(args.get('name','')), args.get('arguments') or {})
"""),
])

patch('notiontool/server.py', [
("""        # Slow network calls run outside the coordinator lock.
""", """        # Slow network calls run outside the coordinator lock.
        if path=='/api/mcp/refresh':
            return {'ok':True,'servers':[{k:v for k,v in e.items() if k!='tools'} for e in self.bridge.local_refresh()]}
"""),
])

patch('notiontool/web/index.html', [
("""<div id="mcp-server-list"></div></div></main>""",
"""<div class="button-row"><button type="button" id="mcp-refresh" class="primary">↻ Refresh tools</button><span id="mcp-refresh-status" class="help">Restarts the switched-on local MCP servers and loads their current tools. No reconnect in Notion needed.</span></div><div id="mcp-server-list"></div></div></main>"""),
])

patch('notiontool/web/app.js', [
("""e.target.reset();}catch{}};""", """e.target.reset();}catch{}};
$('mcp-refresh').onclick=async()=>{const b=$('mcp-refresh'),st=$('mcp-refresh-status');b.disabled=true;st.textContent='Refreshing tools…';try{const r=await action('mcp/refresh',{},'MCP tools refreshed');const on=((r&&r.servers)||[]).filter(s=>s.enabled);st.textContent=on.map(s=>s.name+': '+(s.ok?s.count+' tools':'error – '+s.error)).join(' · ')||'No local MCP server is switched on.';}catch(e){st.textContent='Refresh failed';}finally{b.disabled=false;}};"""),
])

patch('tests/test_rea.py', [
("""        settings['roblox_enabled'] = False
        self.assertNotIn('roblox_call', bridge.tools())""", """        settings['roblox_enabled'] = False
        self.assertNotIn('roblox_call', bridge.tools())

    def test_refresh_restarts_enabled_servers_and_reports_tools(self):
        from notiontool.bridge import Bridge, OPERATOR_NAMES
        self.assertIn('nt_local_mcp', OPERATOR_NAMES)
        self.assertIn('nt_local_mcp_call', OPERATOR_NAMES)
        bridge = Bridge(SimpleNamespace(settings=lambda: {'blender_enabled': True}), None)
        fake = Mock()
        fake.tools.return_value = [{'name': 'get_scene_info', 'description': 'Scene'}]
        bridge.close_local = Mock()
        bridge.local_client = Mock(return_value=fake)
        result = {e['server']: e for e in bridge.local_refresh()}
        bridge.close_local.assert_called_once_with(['blender'])
        self.assertEqual(result['blender']['count'], 1)
        self.assertFalse(result['roblox']['enabled'])"""),
])
