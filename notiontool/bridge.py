"""Compact MCP facade inspired by SecretScript's lazy tool discovery.

Assignment capabilities replace join/session guessing. By default file access is
confined to the selected project; writes inside the project claim a coordinator
lock before touching the file. Two explicit opt-in settings widen the reach:

* ``mcp_full_access``    - absolute paths anywhere on the PC, shell in any folder
                           (home folder when the chat has no project), longer timeouts.
* ``mcp_browser_access`` - browser tools (Edge/Chrome via DevTools protocol, own profile).

Planning/review roles always stay read-only. Every write, shell command and browser
action is written to the activity log.
"""
from pathlib import Path
import json
import os
import shutil
import subprocess
import threading


TOOLS = {
    'context': ('Read source reports, memory, or archived native thread context; paginated',
                {'task_id':{'type':'string'}, 'thread_id':{'type':'string'}, 'offset':{'type':'integer'}}),
    'list_files': ('List files in a directory (project-relative; absolute paths with full PC access)', {'path':{'type':'string'}}),
    'read_file': ('Read a UTF-8 text file (project-relative; absolute paths with full PC access)', {'path':{'type':'string'}}),
    'write_file': ('Write a text file (project-relative; absolute paths with full PC access)', {'path':{'type':'string'},'text':{'type':'string'}}),
    'run_shell': ('Run a bounded PowerShell command in the project folder (any folder via cwd with full PC access)',
                  {'command':{'type':'string'},'timeout':{'type':'integer'},'cwd':{'type':'string'}}),
    'memory': ('Persist a durable project finding', {'text':{'type':'string'}}),
}

BROWSER_TOOLS = {
    'browser_tabs': ('List open browser tabs (id, title, url)', {}),
    'browser_snapshot': ('Read the visible text, links, buttons and inputs of a tab', {'tab_id':{'type':'string'}}),
    'browser_screenshot': ('Save a PNG screenshot of a tab on the PC and return its path', {'tab_id':{'type':'string'}}),
    'browser_open': ('Open a URL in the current tab or a new tab and wait for it to load',
                     {'url':{'type':'string'},'new_tab':{'type':'boolean'},'tab_id':{'type':'string'}}),
    'browser_click': ('Click an element by CSS selector or by its visible text',
                      {'selector':{'type':'string'},'text':{'type':'string'},'tab_id':{'type':'string'}}),
    'browser_type': ('Type text into an element (CSS selector); submit=true presses Enter',
                     {'selector':{'type':'string'},'text':{'type':'string'},'submit':{'type':'boolean'},'tab_id':{'type':'string'}}),
    'browser_eval': ('Evaluate a JavaScript expression in a tab and return the JSON result',
                     {'expression':{'type':'string'},'tab_id':{'type':'string'}}),
    'browser_close': ('Close a browser tab', {'tab_id':{'type':'string'}}),
}
BROWSER_ACTIONS = {'browser_open','browser_click','browser_type','browser_eval','browser_close'}
REA_TOOLS = {
    'rea_tools': ('Discover REA (Reverse Engineer Anything) tools and their argument schemas', {}),
    'rea_call': ('Call a discovered REA tool', {'name':{'type':'string'},'arguments':{'type':'object'}}),
}
from .local_mcp import SERVERS as LOCAL_SERVERS, BY_KEY as LOCAL_BY_KEY, TOOL_SERVER, tool_specs, enabled as local_enabled
READ_ONLY_ROLES = ('architect','researcher','quality_planner','planner','reviewer')
from .screen_capture import SCREEN_TOOLS, SCREEN_OPERATOR_TOOLS, SCREEN_OPERATOR_NAMES, ScreenCapture


def shell_command(command):
    """PowerShell on Windows; pwsh or sh elsewhere (development/tests)."""
    if os.name=='nt':
        return ['powershell.exe','-NoProfile','-NonInteractive','-Command',command]
    if shutil.which('pwsh'):
        return ['pwsh','-NoProfile','-NonInteractive','-Command',command]
    return ['sh','-c',command]



# Operator scope: only with the owner Host API key (never the worker bridge token).
OPERATOR_TOOLS = [
    {'name':'nt_api','description':'Operator: call a NotionTool Host API route on the owner PC. GET or POST /api/... (e.g. /api/health, /api/state, /api/chats, /api/send, /api/main/create, /api/operations).',
     'annotations':{'readOnlyHint':False,'destructiveHint':True},
     'inputSchema':{'type':'object','properties':{'method':{'type':'string','enum':['GET','POST']},'path':{'type':'string'},'body':{'type':'object'},'timeout':{'type':'number'}},'required':['path']}},
    {'name':'nt_notion_call','description':'Operator: raw internal Notion API call (POST /api/v3/<method>) as the selected harness account. For verified reverse-engineering tests.',
     'annotations':{'readOnlyHint':False,'destructiveHint':True},
     'inputSchema':{'type':'object','properties':{'method':{'type':'string'},'body':{'type':'object'},'account_id':{'type':'string'}},'required':['method','body']}},
    {'name':'nt_inference_body','description':'Operator: build (not send) the standard NotionTool runInferenceTranscript body for a thread and prompt. Edit it, then send with nt_notion_stream.',
     'annotations':{'readOnlyHint':True,'destructiveHint':False},
     'inputSchema':{'type':'object','properties':{'thread_id':{'type':'string'},'prompt':{'type':'string'},'model':{'type':'string'},'create':{'type':'boolean'},'account_id':{'type':'string'}},'required':['thread_id','prompt']}},
    {'name':'nt_notion_stream','description':'Operator: send a full runInferenceTranscript body and return the streamed NDJSON lines (capped).',
     'annotations':{'readOnlyHint':False,'destructiveHint':True},
     'inputSchema':{'type':'object','properties':{'body':{'type':'object'},'account_id':{'type':'string'},'timeout':{'type':'number'},'max_lines':{'type':'integer'}},'required':['body']}},
    {'name':'nt_local_mcp','description':'Operator: list the local MCP servers (REA, Roblox, Blender, ...) and their current tools. refresh=true restarts the enabled servers and rediscovers their tools, so new tools work without reconnecting this MCP.',
     'annotations':{'readOnlyHint':True,'destructiveHint':False},
     'inputSchema':{'type':'object','properties':{'server':{'type':'string'},'refresh':{'type':'boolean'}}}},
    {'name':'nt_local_mcp_call','description':'Operator: call a tool of an enabled local MCP server (server = rea, roblox, blender). Use nt_local_mcp to see tools and schemas.',
     'annotations':{'readOnlyHint':False,'destructiveHint':True},
     'inputSchema':{'type':'object','properties':{'server':{'type':'string'},'name':{'type':'string'},'arguments':{'type':'object'}},'required':['server','name']}},
]
from .devtools import TOOLS as DEV_TOOLS, NAMES as DEV_NAMES, call as dev_call
OPERATOR_TOOLS = OPERATOR_TOOLS + DEV_TOOLS + SCREEN_OPERATOR_TOOLS
OPERATOR_NAMES = {t['name'] for t in OPERATOR_TOOLS}

class Bridge:
    def __init__(self, store, engine, browser=None):
        self.store,self.engine = store,engine
        self.shell_gate = threading.BoundedSemaphore(4)
        self._browser = browser

    # ---- settings ---------------------------------------------------------
    def full_access(self):
        return bool(self.store.settings().get('mcp_full_access', False))

    def browser_access(self):
        return bool(self.store.settings().get('mcp_browser_access', False))

    def screen_access(self):
        # Window capture is read-only and on by default; Settings can switch it off.
        return bool(self.store.settings().get('screen_capture', True))

    def screen(self):
        if getattr(self, '_screen', None) is None:
            self._screen = ScreenCapture(self.store)
        return self._screen

    def tools(self):
        tools = dict(TOOLS)
        if self.browser_access():
            tools.update(BROWSER_TOOLS)
        if self.screen_access():
            tools.update(SCREEN_TOOLS)
        settings = self.store.settings()
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

    def local_refresh(self, key=None, restart=True, schemas=False):
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

    @property
    def browser(self):
        if self._browser is None:
            from .browser import Browser
            self._browser = Browser(self.store)
        return self._browser

    # ---- capability -------------------------------------------------------
    def assignment(self, token):
        # Capability is revoked as soon as an attempt ends or is superseded.
        task = next((t for t in self.store.all('tasks') if t.get('lease')==token and t['state']=='running'),None)
        if not task:
            raise ValueError('Assignment expired or invalid; do not join again')
        run = self.store.get('runs',task['run_id'])
        if run['state'] not in ('approach','planning','executing','reviewing','fixing','integrating','paused'):
            raise ValueError('Run is no longer active')
        return task,run

    @staticmethod
    def project(run):
        if run.get('project_path') and Path(run['project_path']).is_dir():
            return Path(run['project_path']).resolve()
        return None

    def root(self, run):
        """Project folder, or the user's home folder when full PC access allows it."""
        project = self.project(run)
        if project:
            return project
        if self.full_access():
            return Path.home().resolve()
        if not run.get('project_path'):
            raise ValueError('No PC project folder configured for this chat')
        raise ValueError('The configured project folder does not exist')

    def path(self, run, relative='.'):
        root = self.root(run)
        path = (root/os.path.expanduser(str(relative))).resolve()
        if self.full_access():
            # Full PC access (explicit opt-in in Settings): absolute paths anywhere.
            return path
        if not path.is_relative_to(root):
            raise ValueError('File is outside the configured project folder (enable full PC access in Settings)')
        return path

    # ---- tools ------------------------------------------------------------
    def call(self, name, args, token):
        cwd = None
        verifier=getattr(getattr(self.engine,'adapter',None),'verify_response_model',None)
        if verifier is not None:
            # A wrongly routed model must not touch project tools before its
            # final response is rejected. Do network reads outside the engine
            # lock, then recheck the capability below before any side effect.
            with self.engine.lock:
                pending,_=self.assignment(token)
                worker=self.store.get('workers',pending['worker_id'])
            verifier(worker,pending)
        with self.engine.lock:
            task,run = self.assignment(token)
            if name not in self.tools():
                raise ValueError('Unknown or disabled tool' + (' (enable browser access in Settings)' if name in BROWSER_TOOLS else ' (enable '+LOCAL_BY_KEY[TOOL_SERVER[name]]['label']+' in Settings)' if name in TOOL_SERVER else ''))
            if name in TOOL_SERVER:
                rea = self.local_client(TOOL_SERVER[name])
                self.store.event('tool_started',run['id'],task_id=task['id'],tool=name,message=LOCAL_BY_KEY[TOOL_SERVER[name]]['label']+': '+str(args.get('name','discover'))[:100])
            if task['role'] in READ_ONLY_ROLES and (name in ('write_file','run_shell') or name in BROWSER_ACTIONS):
                raise ValueError('This planning/review role has read-only PC access')
            if name in TOOL_SERVER:
                pass
            elif name in SCREEN_TOOLS:
                pass
            elif name in BROWSER_TOOLS:
                if name in BROWSER_ACTIONS:
                    detail = args.get('url') or args.get('selector') or args.get('text') or args.get('expression') or ''
                    self.store.event('tool_started',run['id'],task_id=task['id'],tool=name,message='Browser: '+name+' '+str(detail)[:200])
            elif name=='context':
                wanted = args.get('task_id')
                if args.get('thread_id'):
                    worker = self.store.get('workers', task['worker_id'])
                    thread = str(args['thread_id'])
                    archive = self.store.get('thread_context', worker['account_id'] + ':' + thread)
                    if not archive:
                        raise ValueError('No observed transcript for this account and thread')
                    text = json.dumps(archive, ensure_ascii=False)
                    offset = max(0, int(args.get('offset', 0)))
                    return dict(text=text[offset:offset+16000], next_offset=offset+16000 if len(text)>offset+16000 else None)
                records = [t for t in self.engine.tasks(run['id']) if not wanted or t['id']==wanted]
                if wanted:
                    source = self.store.get('tasks', wanted)
                    previous = self.store.get('runs', (source or {}).get('run_id'), {})
                    if source and previous.get('chat_id') == run['chat_id']:
                        records = [source]
                memory_id = run.get('folder_id') or run['chat_id']
                payload = dict(goal=run['goal'],conversation=[m for m in self.store.all('messages') if m['chat_id']==run['chat_id']],
                            memory=self.store.get('memory', memory_id, {}),
                            memory_baseline=self.store.get('memory_baselines', memory_id, {}),
                            memory_entries=[m for m in self.store.all('memory_entries') if m['memory_id']==memory_id],
                            reports=[dict(id=t['id'],title=t['title'],state=t['state'],result=t['result']) for t in records])
                text = json.dumps(payload, ensure_ascii=False)
                if len(text) > 16000 or args.get('offset'):
                    offset = max(0, int(args.get('offset', 0)))
                    return dict(text=text[offset:offset+16000], next_offset=offset+16000 if len(text)>offset+16000 else None)
                return payload
            elif name=='memory':
                ident = run.get('folder_id') or run['chat_id']
                self.store.remember(ident, str(args.get('text','')), task['id'])
                return {'saved':True}
            elif name=='run_shell':
                cwd = self.path(run,str(args.get('cwd') or '.'))
                if not cwd.is_dir():
                    raise ValueError('Shell folder does not exist')
                # Shell is a powerful tool, not a filesystem sandbox. Serialize it
                # against ALL edits in the project root, even across main chats.
                if self.full_access():
                    self.engine.claim_absolute(task['id'],token,Path(cwd).anchor)
                elif self.project(run):
                    self.engine.claim_file(task['id'],token,'.')
                self.store.event('tool_started',run['id'],task_id=task['id'],tool=name,
                                 message='Shell in '+str(cwd)[:200]+': '+str(args.get('command',''))[:300])
            else:
                path = self.path(run,str(args.get('path','.')))
                if name=='list_files':
                    return [dict(name=p.name,directory=p.is_dir()) for p in sorted(path.iterdir())][:500]
                if name=='read_file':
                    if path.stat().st_size>2*1024*1024:
                        raise ValueError('File is too large; choose a smaller artifact')
                    return path.read_text(encoding='utf-8',errors='replace')
                if name=='write_file':
                    if 'text' not in args and isinstance(args.get('content'),str):
                        args={**args,'text':args['content']}
                    if 'text' in args and 'content' in args and args['text'] != args['content']:
                        raise ValueError('Conflicting text/content values; no file was changed')
                    if not isinstance(args.get('text'),str):
                        raise ValueError('write_file requires a text string; no file was changed')
                    project = self.project(run)
                    if project and path.is_relative_to(project):
                        self.engine.claim_file(task['id'],token,str(path.relative_to(project)))
                    else:
                        self.engine.claim_absolute(task['id'],token,path)
                    text = str(args.get('text',''))
                    if len(text)>2*1024*1024:
                        raise ValueError('Write is too large')
                    self.store.event('tool_started',run['id'],task_id=task['id'],tool=name,message='Write: '+str(path)[:300])
                    path.parent.mkdir(parents=True,exist_ok=True)
                    path.write_text(text,encoding='utf-8')
                    return {'written':str(path),'bytes':len(text.encode())}
        if name in TOOL_SERVER:
            return rea.tools() if name.endswith('_tools') else rea.call(str(args.get('name','')), args.get('arguments') or {})
        if name in SCREEN_TOOLS:
            # Window capture is read-only and runs outside the coordinator lock.
            return self.screen().call(name, args, task_id=task['id'], run_id=run['id'], role=task.get('role', ''))
        if name in BROWSER_TOOLS:
            # Slow browser work runs outside the coordinator lock.
            return self.browser.call(name, args)
        return self.shell(name, args, task, run, cwd)

    def shell(self, name, args, task, run, cwd):
        if not self.shell_gate.acquire(timeout=1):
            raise ValueError('All local tool slots are busy; use other work before retrying')
        try:
            timeout = max(1,min(600 if self.full_access() else 120,int(args.get('timeout',60))))
            command = str(args.get('command',''))
            if not command:
                raise ValueError('Command is empty')
            creationflags = subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
            process = subprocess.Popen(shell_command(command),
                cwd=cwd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,creationflags=creationflags)
            try:
                output,_ = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                if os.name=='nt':
                    subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,creationflags=creationflags)
                else:
                    process.kill()
                process.communicate()
                raise ValueError('Command timed out; its process tree was stopped') from None
            self.store.event('tool_finished',run['id'],task_id=task['id'],tool=name,exit_code=process.returncode)
            return dict(exit_code=process.returncode,output=output.decode('utf-8',errors='replace')[-30000:])
        finally:
            self.shell_gate.release()

    def operator_call(self, app, name, args):
        import re, urllib.request, urllib.error
        from .harness_account import selected_account
        if name in SCREEN_OPERATOR_NAMES: return self.screen().call(name[3:], args, role='operator')
        if name in DEV_NAMES: return dev_call(name, args)
        if name=='nt_local_mcp':
            return self.local_refresh(str(args.get('server') or '') or None, bool(args.get('refresh')), schemas=bool(args.get('server')))
        if name=='nt_local_mcp_call':
            server=LOCAL_BY_KEY.get(str(args.get('server','')))
            if server is None: raise ValueError('Unknown local MCP server; use nt_local_mcp')
            if not local_enabled(self.store.settings(), server): raise ValueError('Switch on '+server['label']+' in MCP servers first')
            return self.local_client(server['key']).call(str(args.get('name','')), args.get('arguments') or {})
        if name=='nt_api':
            path=str(args.get('path',''))
            if not path.startswith('/api/'): raise ValueError('path must start with /api/')
            method=str(args.get('method') or ('POST' if args.get('body') is not None else 'GET')).upper()
            data=json.dumps(args.get('body') or {}).encode() if method=='POST' else None
            req=urllib.request.Request(f'http://127.0.0.1:{app.port}{path}',data=data,method=method,
                headers={'Authorization':'Bearer '+app.host_api_key,'Content-Type':'application/json'})
            try:
                with urllib.request.urlopen(req,timeout=float(args.get('timeout') or 60)) as r:
                    text,status=r.read(2000000).decode('utf-8','replace'),r.status
            except urllib.error.HTTPError as e:
                text,status=e.read(200000).decode('utf-8','replace'),e.code
            try: payload=json.loads(text)
            except ValueError: payload=text
            return {'status':status,'body':payload}
        account=str(args.get('account_id') or selected_account(self.store))
        if not account: raise ValueError('No harness account selected; sign in first')
        client=app.adapter.client(account)
        if name=='nt_notion_call':
            method=str(args.get('method',''))
            if not re.fullmatch(r'[A-Za-z]{3,64}',method): raise ValueError('Invalid Notion method name')
            return client.call(method,args.get('body') or {})
        if name=='nt_inference_body':
            info=client.info
            model=args.get('model') or app.adapter.template['transcript'][0]['value'].get('model')
            step=app.adapter.new_id(info['space_id'],'00aa')
            return app.adapter.body(client,str(args['thread_id']),step,str(args['prompt']),model,create=bool(args.get('create')))
        if name=='nt_notion_stream':
            body=args.get('body')
            if not isinstance(body,dict): raise ValueError('body must be an object')
            limit=min(int(args.get('max_lines') or 400),3000); lines=[]
            for line in client.transport.stream('runInferenceTranscript',body,client.headers(),timeout=float(args.get('timeout') or 180)):
                if isinstance(line,bytes): line=line.decode('utf-8','replace')
                if isinstance(line,str):
                    try: line=json.loads(line)
                    except ValueError: line=line[:4000]
                lines.append(line)
                if len(lines)>=limit: lines.append('[truncated]'); break
            return {'lines':lines}
        raise ValueError('Unknown operator tool')

    def rpc(self, message, operator=None):
        ident = message.get('id')
        method,params = message.get('method'),message.get('params') or {}
        if ident is None:
            return None
        try:
            if method=='initialize':
                version=params.get('protocolVersion')
                if version not in ('2024-11-05','2025-03-26','2025-06-18'):version='2024-11-05'
                result = dict(protocolVersion=version,capabilities={'tools':{'listChanged':False}},serverInfo={'name':'NotionTool','version':'0.2.0'})
            elif method=='ping':
                result = {}
            elif method=='tools/list':
                result = {'tools':[
                    {'name':'mcp_servers','description':'List NotionTool tool groups; no worker registration is needed',
                     'annotations':{'readOnlyHint':True,'destructiveHint':False},
                     'inputSchema':{'type':'object','properties':{}}},
                    {'name':'mcp_list_tools','description':'Get schemas for a NotionTool tool by name',
                     'annotations':{'readOnlyHint':True,'destructiveHint':False},
                     'inputSchema':{'type':'object','properties':{'tool_name':{'type':'string'}}}},
                    {'name':'mcp_call','description':'Execute a scoped tool using the assignment token from your current prompt',
                     'annotations':{'readOnlyHint':False,'destructiveHint':True},
                     'inputSchema':{'type':'object','properties':{'tool_name':{'type':'string'},'assignment_token':{'type':'string'},
                                   'arguments':{'type':'object'}},'required':['tool_name','assignment_token']}}]}
                if operator is not None:
                    result['tools'] += OPERATOR_TOOLS
            elif method=='tools/call':
                name,args = params.get('name'),params.get('arguments') or {}
                tools = self.tools()
                if operator is not None and name in OPERATOR_NAMES:
                    value = self.operator_call(operator, name, args)
                elif name=='mcp_servers':
                    value = [{'id':'NotionTool','tools':list(TOOLS),'full_pc_access':self.full_access()}]
                    if self.browser_access():
                        value.append({'id':'Browser','tools':list(BROWSER_TOOLS)})
                    if self.screen_access():
                        value.append({'id':'Screen','tools':list(SCREEN_TOOLS)})
                    settings = self.store.settings()
                    for server in LOCAL_SERVERS:
                        if local_enabled(settings, server):
                            value.append({'id':server['label'],'tools':list(tool_specs(server))})
                elif name=='mcp_list_tools':
                    value = [dict(name=n,description=d,inputSchema={'type':'object','properties':p}) for n,(d,p) in tools.items()
                             if not args.get('tool_name') or n==args['tool_name']]
                elif name=='mcp_call':
                    value = self.call(args.get('tool_name'),args.get('arguments') or {},args.get('assignment_token',''))
                else:
                    raise ValueError('Unknown MCP tool')
                image = value.pop('_image', None) if isinstance(value, dict) else None
                result = {'content':[{'type':'text','text':json.dumps(value,ensure_ascii=False)}]}
                if image:
                    result['content'].append({'type':'image','data':image['data'],'mimeType':image['mimeType']})
            else:
                return {'jsonrpc':'2.0','id':ident,'error':{'code':-32601,'message':'Method not found'}}
            return {'jsonrpc':'2.0','id':ident,'result':result}
        except (ValueError,OSError,KeyError,TypeError,RuntimeError) as exc:
            return {'jsonrpc':'2.0','id':ident,'result':{'isError':True,'content':[{'type':'text','text':str(exc)[:400]}]}}
