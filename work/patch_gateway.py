import pathlib, re
ROOT = pathlib.Path(r'C:\Users\liket\Downloads\NotionTool-harness\NotionTool')

def patch(rel, edits):
    path = ROOT / rel
    raw = path.read_bytes().decode('utf-8')
    crlf = '\r\n' in raw
    text = raw.replace('\r\n', '\n')
    for old, new in edits:
        n = text.count(old)
        assert n == 1, (rel, n, old[:80])
        text = text.replace(old, new)
    if crlf:
        text = text.replace('\n', '\r\n')
    path.write_bytes(text.encode('utf-8'))
    print('patched', rel, 'crlf' if crlf else 'lf')

# ---------------- notion.py ----------------
notion = [
("""                self.flag_bridge_unavailable(worker['account_id'])
""", """                self.note_worker_mcp_failure(worker)
"""),
("""    def flag_bridge_unavailable(self, account_id):
        account = self.store.get('accounts', account_id)
        if not account:
            return
        account.update(bridge_connected=False, bridge_next_at=0, bridge_checked_at=0,
                       bridge_error='A worker reported the NotionTool MCP connection as unavailable')
        self.store.put('accounts', account)
        self.store.event('mcp_unavailable', message='NotionTool MCP missing in a worker chat; reconnecting automatically', slot=account.get('slot'))
""", """    def note_worker_mcp_failure(self, worker):
        \"\"\"A worker chat could not use the MCP. Never reconnect from here: a reconnect could disturb the
        other Coworkers. Only schedule a read-only check of the account's MCP module for the next minute.\"\"\"
        account = self.store.get('accounts', worker['account_id'])
        if not account:
            return
        account.update(bridge_checked_at=0, bridge_suspect_at=time.time())
        self.store.put('accounts', account)
        self.store.event('worker_mcp_unavailable', message='NotionTool MCP was unavailable in ' + str(worker.get('label') or 'a worker chat')
                         + '; checking the account connection without interrupting other Coworkers', slot=account.get('slot'))
"""),
("""        for module in modules:
            if (module.get('pointer') or {}).get('id')==integration:module['defaultEnabled']=True
        self.transaction(client,'agentPersistenceHelpers.addAgentChatModule',[
            {'pointer':{'table':'space_view','id':view['id'],'spaceId':client.info['space_id']},'path':['settings'],
             'command':'update','args':{'agent_chat_modules':modules}}])
        self.transaction(client,'mcpServerSettings.updateToolPermissions',[
            {'pointer':{'table':'workflow_module','id':integration,'spaceId':client.info['space_id']},
             'path':['data',key],'command':'set','args':value} for key,value in
             (('runReadToolsAutomatically',read_auto),('runWriteToolsAutomatically',write_auto))])
""", """        # Idempotent: an existing, enabled module with the right permissions is left untouched, so a
        # reconnect never rewrites the account's chat modules while Coworkers are working.
        changed = integration not in records
        for module in modules:
            if (module.get('pointer') or {}).get('id')==integration and module.get('defaultEnabled') is not True:
                module['defaultEnabled']=True;changed=True
        if changed:
            self.transaction(client,'agentPersistenceHelpers.addAgentChatModule',[
                {'pointer':{'table':'space_view','id':view['id'],'spaceId':client.info['space_id']},'path':['settings'],
                 'command':'update','args':{'agent_chat_modules':modules}}])
        current=(records.get(integration) or {}).get('data') or {}
        if integration not in records or current.get('runReadToolsAutomatically')!=read_auto or current.get('runWriteToolsAutomatically')!=write_auto:
            self.transaction(client,'mcpServerSettings.updateToolPermissions',[
                {'pointer':{'table':'workflow_module','id':integration,'spaceId':client.info['space_id']},
                 'path':['data',key],'command':'set','args':value} for key,value in
                 (('runReadToolsAutomatically',read_auto),('runWriteToolsAutomatically',write_auto))])
"""),
]

# ---------------- server.py ----------------
src = (ROOT / 'notiontool/server.py').read_bytes().decode('utf-8').replace('\r\n', '\n')
start = src.index("        if not self.mcp_http:\n", src.index('    def start_tunnel(self):'))
end_marker = "        result=self.tailscale.start(self.mcp_http.server_port)\n"
end = src.index(end_marker, start) + len(end_marker)
old_block = src[start:end]
listener_lines = old_block.split("        self.tailscale=self.tailscale or TailscaleBridge(self.store)\n")[0]
assert listener_lines.count('ExclusiveHTTPServer') == 2, listener_lines
listener_body = ''.join('    ' + line + '\n' if line.strip() else '\n' for line in listener_lines.rstrip('\n').split('\n'))

new_methods = ("""    def ensure_mcp_listener(self):
""" + listener_body.replace('        if not self.mcp_http:', 'if not self.mcp_http:', 0) + """        from .gateway import publish_core
        publish_core(self.data_dir,self.mcp_http.server_port)
        return self.mcp_http.server_port

    @staticmethod
    def gateway_health(port):
        import urllib.request
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/__gateway',timeout=2) as response:
                data=json.loads(response.read())
            return data if data.get('service')=='notiontool-gateway' else None
        except Exception:
            return None

    @staticmethod
    def port_busy(port):
        import socket
        with socket.socket() as probe:
            probe.settimeout(1)
            return probe.connect_ex(('127.0.0.1',port))==0

    def ensure_gateway(self):
        \"\"\"Process 2: the stable MCP gateway Tailscale points to. It survives every restart of this app,
        holds MCP requests while the app is down and answers as backup MCP if the restart takes longer.\"\"\"
        from . import gateway as gw
        import sys
        settings=self.store.settings()
        if settings.get('mcp_gateway') is False:return 0
        port=int(settings.get('mcp_gateway_port') or gw.DEFAULT_PORT)
        health=self.gateway_health(port)
        if health and health.get('version')==gw.VERSION:return port
        if health:  # an older gateway version: replace it once
            import urllib.request
            control=gw.read_json(gw.runtime_dir(self.data_dir)/'gateway.json').get('control','')
            try:urllib.request.urlopen(urllib.request.Request(f'http://127.0.0.1:{port}/__gateway/stop',data=b'{}',method='POST',headers={'X-Gateway-Control':control}),timeout=5).read()
            except Exception:pass
            for _ in range(20):
                if not self.gateway_health(port):break
                time.sleep(0.5)
        elif self.port_busy(port):
            self.store.event('mcp_gateway_port_busy',message=f'Port {port} belongs to another program; the MCP runs without gateway')
            return 0
        breakaway=0x01000000  # CREATE_BREAKAWAY_FROM_JOB
        flags=(subprocess.DETACHED_PROCESS|subprocess.CREATE_NEW_PROCESS_GROUP|breakaway) if os.name=='nt' else 0
        if getattr(sys,'frozen',False):
            exe=gw.runtime_dir(self.data_dir)/f'NotionTool-gateway-v{gw.VERSION}.exe'
            if not exe.is_file():shutil.copy2(sys.executable,exe)  # own file: the app EXE stays replaceable
            command=[str(exe),'--gateway','--data-dir',self.data_dir,'--gateway-port',str(port)]
        else:
            python=Path(sys.executable);pythonw=python.with_name('pythonw.exe')
            command=[str(pythonw if pythonw.is_file() else python),str(Path(__file__).resolve().parent.parent/'main.py'),
                     '--gateway','--data-dir',self.data_dir,'--gateway-port',str(port)]
        options=dict(stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,close_fds=True,cwd=str(gw.runtime_dir(self.data_dir)))
        try:subprocess.Popen(command,creationflags=flags,**options)
        except OSError:subprocess.Popen(command,creationflags=flags&~breakaway,**options)
        for _ in range(60):
            health=self.gateway_health(port)
            if health and health.get('version')==gw.VERSION:
                self.store.event('mcp_gateway_started',message=f'MCP gateway (process 2) running on port {port}')
                return port
            time.sleep(0.5)
        self.store.event('mcp_gateway_failed',message='MCP gateway did not start; Tailscale points to the app directly')
        return 0

    def start_tunnel(self):
""")

server = [
("""        self.store = Store(directory)
""", """        self.store = Store(directory)
        self.data_dir = str(directory)
        self.gateway_port = 0
"""),
("""        threading.Thread(target=self.http.serve_forever,daemon=True,name='local-http').start()
""", """        threading.Thread(target=self.http.serve_forever,daemon=True,name='local-http').start()
        try:self.ensure_mcp_listener()  # MCP is reachable right after a restart, before the first maintenance run
        except Exception as exc:self.store.event('mcp_listener_failed',message=str(exc)[:200])
"""),
("    def start_tunnel(self):\n", new_methods),
(old_block, """        self.ensure_mcp_listener()
        self.gateway_port=self.ensure_gateway()
        self.tailscale=self.tailscale or TailscaleBridge(self.store)
        result=self.tailscale.start(self.gateway_port or self.mcp_http.server_port)
"""),
("""                if alive:
                    connected+=1;continue
                self.store.event('mcp_missing',message='NotionTool MCP is no longer active in the account; reconnecting',slot=account.get('slot'))
""", """                fresh=self.store.get('accounts',account['id'])
                if alive:
                    if fresh.get('bridge_misses'):
                        fresh.update(bridge_misses=0,bridge_missing_since=0);self.store.put('accounts',fresh)
                    connected+=1;continue
                # Reconnect only when the module is confirmed missing twice, and not while Coworkers of this
                # account are working (max. 10 minutes) - a confirmed-missing MCP is broken for them anyway.
                misses=fresh.get('bridge_misses',0)+1
                fresh.update(bridge_misses=misses,bridge_missing_since=fresh.get('bridge_missing_since') or time.time())
                if misses<2:fresh['bridge_checked_at']=time.time()-240  # confirm in about a minute
                self.store.put('accounts',fresh)
                busy=any(w['account_id']==account['id'] and w.get('state') in ('busy','recovering') for w in self.store.all('workers'))
                if misses<2 or (busy and time.time()-fresh['bridge_missing_since']<600):
                    if misses>=2:
                        self.store.event('mcp_reconnect_deferred',message='NotionTool MCP missing; reconnect waits until the Coworkers are idle',slot=account.get('slot'))
                    connected+=1;continue
                self.store.event('mcp_missing',message='NotionTool MCP is no longer active in the account; reconnecting',slot=account.get('slot'))
"""),
("""                    self.store.event('mcp_reconnected',message='NotionTool MCP reconnected automatically',slot=account.get('slot'))
                fresh=self.store.get('accounts',account['id']);fresh['bridge_checked_at']=time.time();self.store.put('accounts',fresh)
""", """                    self.store.event('mcp_reconnected',message='NotionTool MCP reconnected automatically',slot=account.get('slot'))
                fresh=self.store.get('accounts',account['id']);fresh.update(bridge_checked_at=time.time(),bridge_misses=0,bridge_missing_since=0);self.store.put('accounts',fresh)
"""),
("""        if self.tailscale:
            try:self.tailscale.stop()
""", """        if self.tailscale and not self.gateway_port:  # with the gateway the route stays; process 2 bridges the restart
            try:self.tailscale.stop()
"""),
]

main = [
("""    parser = argparse.ArgumentParser()
""", """    if '--gateway' in sys.argv:  # process 2: MCP gateway / backup MCP, no window, no instance lock
        from notiontool.gateway import main as gateway_main
        sys.exit(gateway_main(sys.argv))
    parser = argparse.ArgumentParser()
"""),
]

stop = [
("""        $_.ProcessId -ne $PID -and (
""", """        $_.ProcessId -ne $PID -and -not ($_.CommandLine -and $_.CommandLine.Contains('--gateway')) -and (
"""),
]

patch('notiontool/notion.py', notion)
patch('notiontool/server.py', server)
patch('main.py', main)
patch('work/stop-harness.ps1', stop)
