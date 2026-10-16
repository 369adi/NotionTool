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

patch('notiontool/mcp_servers.py', [
("""        return [{'id':'builtin_rea','name':'REA (Reverse Engineer Anything)','url':'Local stdio: npx rea-agents@6.5.0 mcp',
                 'builtin':True,'installed':self._node(),'enabled':bool(self.store.settings().get('rea_enabled',False)),
                 'toggle':'rea_enabled','read_auto':True,'write_auto':False}]+self.store.all('mcp_servers')""",
"""        from .local_mcp import public as local_public
        return local_public(self.store.settings())+self.store.all('mcp_servers')"""),
])

patch('notiontool/bridge.py', [
("READ_ONLY_ROLES = (", "from .local_mcp import SERVERS as LOCAL_SERVERS, BY_KEY as LOCAL_BY_KEY, TOOL_SERVER, tool_specs, enabled as local_enabled\nREAD_ONLY_ROLES = ("),
("""        if self.store.settings().get('rea_enabled', False):
            tools.update(REA_TOOLS)
        return tools
""",
"""        settings = self.store.settings()
        for server in LOCAL_SERVERS:
            if local_enabled(settings, server):
                tools.update(tool_specs(server))
        return tools

    def local_client(self, key):
        lock = self.__dict__.setdefault('_local_lock', threading.Lock())
        with lock:
            clients = self.__dict__.setdefault('_local_mcp', {})
            if key not in clients:
                from .rea import Rea
                server = LOCAL_BY_KEY[key]
                clients[key] = Rea(command=server['command'](), label=server['label'], prefix=key)
            return clients[key]

    def close_local(self, keys=None):
        lock = self.__dict__.setdefault('_local_lock', threading.Lock())
        with lock:
            clients = self.__dict__.setdefault('_local_mcp', {})
            closing = [clients.pop(k) for k in list(clients) if keys is None or k in keys]
        for client in closing:
            try:
                client.close()
            except Exception:
                pass
"""),
("""' (enable REA in Settings)' if name in REA_TOOLS else ''))
            if name in REA_TOOLS:
                if getattr(self, '_rea', None) is None:
                    from .rea import Rea
                    self._rea = Rea()
                rea = self._rea
                self.store.event('tool_started',run['id'],task_id=task['id'],tool=name,message='REA: '+str(args.get('name','discover'))[:100])""",
"""' (enable '+LOCAL_BY_KEY[TOOL_SERVER[name]]['label']+' in Settings)' if name in TOOL_SERVER else ''))
            if name in TOOL_SERVER:
                rea = self.local_client(TOOL_SERVER[name])
                self.store.event('tool_started',run['id'],task_id=task['id'],tool=name,message=LOCAL_BY_KEY[TOOL_SERVER[name]]['label']+': '+str(args.get('name','discover'))[:100])"""),
("""            if name in REA_TOOLS:
                pass""", """            if name in TOOL_SERVER:
                pass"""),
("""        if name in REA_TOOLS:
            return rea.tools() if name=='rea_tools' else rea.call(""",
"""        if name in TOOL_SERVER:
            return rea.tools() if name.endswith('_tools') else rea.call("""),
("""                    if self.store.settings().get('rea_enabled', False):
                        value.append({'id':'REA','tools':list(REA_TOOLS)})""",
"""                    settings = self.store.settings()
                    for server in LOCAL_SERVERS:
                        if local_enabled(settings, server):
                            value.append({'id':server['label'],'tools':list(tool_specs(server))})"""),
])

patch('notiontool/server.py', [
("""        rea = getattr(self.bridge, '_rea', None)
        if rea is not None:
            rea.close()
""", """        self.bridge.close_local()
"""),
("""                for key in ('harness_enabled', 'mcp_full_access', 'mcp_browser_access', 'rea_enabled'):""",
"""                from .local_mcp import SERVERS as LOCAL_SERVERS, SETTINGS as LOCAL_SETTINGS
                for key in ('harness_enabled', 'mcp_full_access', 'mcp_browser_access') + LOCAL_SETTINGS:"""),
("""                self.store.put('settings',settings)
                return {'ok':True}""",
"""                self.store.put('settings',settings)
                off = [s['key'] for s in LOCAL_SERVERS if not settings.get(s['setting'])]
                threading.Thread(target=self.bridge.close_local, args=(off,), daemon=True, name='local-mcp-close').start()
                return {'ok':True}"""),
])

patch('notiontool/web/app.js', [
("""${s.enabled?'checked':''}> ${s.enabled?'On':'Off'}</label> ${s.installed?'Node.js found; REA starts on first use.':'Install Node.js (npx) to use REA.'}`""",
"""data-label="${escape(s.label||s.name)}" ${s.enabled?'checked':''}> ${s.enabled?'On':'Off'}</label> ${escape(s.hint||'')}`"""),
("""el.checked?'REA on':'REA off')""", """(el.dataset.label||'MCP')+(el.checked?' on':' off'))"""),
])

patch('tests/test_rea.py', [
("""    def test_settings_offer_only_the_rea_builtin(self):
        store = SimpleNamespace(settings=lambda: {}, all=lambda kind: [])
        servers = McpServers(SimpleNamespace(store=store)).public()
        self.assertEqual([s['id'] for s in servers], ['builtin_rea'])
        self.assertFalse(servers[0]['enabled'])""",
"""    def test_settings_offer_the_local_builtins_disabled_by_default(self):
        store = SimpleNamespace(settings=lambda: {}, all=lambda kind: [])
        servers = McpServers(SimpleNamespace(store=store)).public()
        self.assertEqual([s['id'] for s in servers], ['builtin_rea', 'builtin_roblox', 'builtin_blender'])
        self.assertFalse(any(s['enabled'] for s in servers))
        self.assertEqual([s['toggle'] for s in servers], ['rea_enabled', 'roblox_enabled', 'blender_enabled'])

    def test_bridge_tools_follow_the_toggles(self):
        from notiontool.bridge import Bridge
        settings = {}
        bridge = Bridge(SimpleNamespace(settings=lambda: settings), None)
        self.assertNotIn('roblox_tools', bridge.tools())
        settings.update(roblox_enabled=True, blender_enabled=True)
        names = bridge.tools()
        for name in ('roblox_tools', 'roblox_call', 'blender_tools', 'blender_call'):
            self.assertIn(name, names)
        self.assertNotIn('rea_tools', names)
        settings['roblox_enabled'] = False
        self.assertNotIn('roblox_call', bridge.tools())"""),
])
