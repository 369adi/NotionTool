from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import hmac
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import urllib.parse

from .updater import Updater
from .bridge import Bridge
from .engine import Engine, ACTIVE, TERMINAL
from .notion import NotionAdapter
from .store import Store, uid
from .token_budget import public as token_budget_public
from .setup_status import account_setup
from .bridge import READ_ONLY_ROLES
from . import role_policy, role_split


# P4b-1: role definitions. Built-in roles come from role_policy (tool rights), role_split (tester)
# and bridge.READ_ONLY_ROLES (the roles the bridge keeps read-only). Custom roles come from the
# role_definitions setting; a setting may never make a READ_ONLY_ROLES role writable (rule I7).
ROLE_TOOLS = (role_policy.READ_ONLY, role_policy.READ_WRITE)
ROLE_NAME_RE = re.compile(r'^[a-z][a-z0-9_]{1,31}$')
ROLE_FIELDS = ('name', 'tools', 'description')
MAX_CUSTOM_ROLES = 20
STALL_REPORTED_CAP = 500  # newest stall-reported task ids kept in the store (meta key stall_reported)
# Events that prove a running turn is still working (tool calls), even before any text streams.
TOOL_STEP_KINDS = ('tool_started', 'tool_finished', 'verify_started', 'verify_done')


def default_roles():
    """The built-in roles with their current tool rights (the behaviour without custom roles)."""
    out, seen = [], set()
    for name in tuple(role_policy.ROLES) + (role_split.TESTER,) + tuple(READ_ONLY_ROLES):
        if name in seen:
            continue
        seen.add(name)
        read_only = name in READ_ONLY_ROLES or (name in role_policy.ROLES and role_policy.tool_rights(name) == role_policy.READ_ONLY)
        out.append(dict(name=name, tools=role_policy.READ_ONLY if read_only else role_policy.READ_WRITE, description='',
                        builtin=True, protected=name in READ_ONLY_ROLES, custom=False, active=True))
    return out


def validate_role_definitions(value):
    """Return clean custom role definitions, or raise ValueError with a clear English message."""
    if value in (None, ''):
        return []
    if not isinstance(value, list):
        raise ValueError('role_definitions must be a list of objects with name, tools and an optional description.')
    if len(value) > MAX_CUSTOM_ROLES:
        raise ValueError('role_definitions allows at most %d custom roles.' % MAX_CUSTOM_ROLES)
    clean, seen = [], set()
    for number, item in enumerate(value, 1):
        if not isinstance(item, dict):
            raise ValueError('Role definition %d must be an object with name and tools.' % number)
        unknown = sorted(str(key) for key in item if key not in ROLE_FIELDS)
        if unknown:
            raise ValueError('Role definition %d has unknown fields: %s. Allowed: name, tools, description.' % (number, ', '.join(unknown)))
        name = item.get('name')
        if not isinstance(name, str) or not ROLE_NAME_RE.match(name):
            raise ValueError('Role name %r is invalid: use 2-32 lowercase letters, digits or _, starting with a letter.' % (name,))
        if name in seen:
            raise ValueError('Role %r is defined twice.' % name)
        tools = item.get('tools', role_policy.READ_ONLY)
        if tools not in ROLE_TOOLS:
            raise ValueError("Role %r: tools must be 'read_only' or 'read_write'." % name)
        if name in READ_ONLY_ROLES and tools != role_policy.READ_ONLY:
            raise ValueError('Role %r is read-only (READ_ONLY_ROLES) and cannot be made writable (rule I7).' % name)
        description = item.get('description', '')
        if not isinstance(description, str) or len(description) > 200:
            raise ValueError('Role %r: description must be text of at most 200 characters.' % name)
        seen.add(name)
        clean.append(dict(name=name, tools=tools, description=description.strip()))
    return clean


def role_state(settings):
    """Roles for /api/state. Never raises: an invalid stored value falls back to the built-in roles."""
    dynamic = bool(settings.get('dynamic_roles', False))
    roles = {role['name']: role for role in default_roles()}
    error = ''
    try:
        custom = validate_role_definitions(settings.get('role_definitions'))
    except ValueError as exc:
        custom, error = [], str(exc)
    for item in custom:
        base = roles.get(item['name'])
        roles[item['name']] = dict(name=item['name'], tools=item['tools'],
                                   description=item['description'] or (base or {}).get('description', ''),
                                   builtin=bool(base), protected=bool(base and base['protected']), custom=True, active=dynamic)
    return dict(dynamic=dynamic, read_only_roles=list(READ_ONLY_ROLES), definitions=list(roles.values()), error=error)


class ExclusiveHTTPServer(ThreadingHTTPServer):
    """Fails instead of sharing a port another process already owns (Windows SO_REUSEADDR would share it)."""
    allow_reuse_address = False
    def server_bind(self):
        import socket
        if hasattr(socket,'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
        super().server_bind()


class Application:
    BRIDGE_JOB_TITLE = 'Maintain Tailscale MCP and account connections'
    BRIDGE_JOB_ID = 'j-bridge-maintenance'

    def __init__(self, directory, adapter_factory=NotionAdapter):
        self.store = Store(directory)
        self.data_dir = str(directory)
        self.gateway_port = 0
        stale = []
        for job in self.store.all('jobs'):
            if job.get('title')==self.BRIDGE_JOB_TITLE and job['id']!=self.BRIDGE_JOB_ID:
                stale.append(('jobs',job['id']))
            elif job['state']=='running':
                periodic = job['id']==self.BRIDGE_JOB_ID
                job.update(state='interrupted',error='App restarted; the next maintenance run follows automatically.' if periodic else 'App restarted. Resume sign-in or team preparation using the saved account state.')
                self.store.put('jobs',job)
        if stale:
            self.store.batch([],stale)
        self.store.delete_events('operation_finished',message=self.BRIDGE_JOB_TITLE,state='done')
        self.adapter = adapter_factory(self.store)
        from .account_pages import AccountPages
        self.account_pages = AccountPages(self.adapter)
        from .mcp_servers import McpServers
        self.mcp_servers = McpServers(self.adapter)
        self.engine = Engine(self.store,self.adapter)
        from .team_grow import TeamGrower
        self.team_grower = TeamGrower(self.store, self.adapter, lambda title, fn, ident: self.job(title, fn, ident))
        self.engine.team_grower = self.team_grower  # automatic growth: startup + every 5 min from the engine loop
        from .team_rotation import LoopChecks, TeamRotator  # 23h Welcome rotation shares the same 5-min slot
        self.team_rotator = TeamRotator(self.store, self.adapter, lambda title, fn, ident: self.job(title, fn, ident))
        self.adapter.on_coworker_joined = self.team_rotator.signal_name_needed  # instant rename on join
        self.engine.team_grower = LoopChecks(self.team_grower, self.team_rotator)
        from .mains import Mains, migrate
        migrate(self.store)
        self.mains = Mains(self)
        self.bridge = Bridge(self.store,self.engine)
        self.token = secrets.token_urlsafe(32)
        secret = self.adapter.sessions.load_identity('bridge') or {}
        self.bridge_token = secret.get('token') or secrets.token_urlsafe(40)
        self.adapter.sessions.save('bridge',{'token':self.bridge_token})
        host = self.adapter.sessions.load_identity('host_api') or {}
        self.host_api_key = host.get('key') or secrets.token_urlsafe(40)
        if not host.get('key'):
            self.adapter.sessions.save('host_api', {'key': self.host_api_key})
        self.admin = ThreadPoolExecutor(max_workers=4,thread_name_prefix='account-operation')
        self.admin_lock = threading.Lock()
        self.account_operations = set()
        self.tunnel = None
        self.http = None
        self.window = None
        self.mcp_http = None
        self.handler = None
        self.tailscale = None
        self.notion_shell = None
        self.desktop = None

    def launch(self, port=None):
        app = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self,*args):
                pass

            def response(self, code, data, content_type='application/json'):
                content = json.dumps(data,ensure_ascii=False).encode() if content_type=='application/json' else data
                self.send_response(code)
                self.send_header('Content-Type',content_type)
                self.send_header('Content-Length',str(len(content)))
                self.send_header('Cache-Control','no-store')
                self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
                self.end_headers()
                try:
                    self.wfile.write(content)
                except (BrokenPipeError,ConnectionResetError):
                    pass

            def handle_request(self, method):
                parsed = urllib.parse.urlsplit(self.path)
                path = parsed.path
                if path.startswith('/nt-harness/'):path=path[len('/nt-harness'):]  # shared Funnel listener mount
                query = urllib.parse.parse_qs(parsed.query)
                if getattr(self.server,'mcp_only',False) and path!='/mcp':
                    return self.response(404,{'error':'Not found'})
                if path=='/mcp':
                    auth = self.headers.get('Authorization','')
                    operator = hmac.compare_digest(auth,'Bearer '+app.host_api_key)
                    if not operator and not hmac.compare_digest(auth,'Bearer '+app.bridge_token):
                        return self.response(401,{'error':'Unauthorized'})
                    if method=='GET':
                        return self.response(405,{'error':'Use MCP POST'})
                    if method=='DELETE':
                        return self.response(200,{})
                    try:
                        body = self.read_body()
                        result = app.bridge.rpc(body, operator=app if operator else None)
                        return self.response(200,result) if result is not None else self.response(202,{})
                    except (ValueError,TypeError,AttributeError):
                        return self.response(400,{'error':'Invalid MCP request'})
                    except Exception as exc:  # noqa: BLE001 - never drop the connection (the gateway would report a restart)
                        app.bridge.log_failure('http', {}, exc)
                        return self.response(500,{'error':'Internal MCP error ('+type(exc).__name__+')'})
                if path.startswith('/api/'):
                    bearer = hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+app.host_api_key)
                    ui = hmac.compare_digest(self.headers.get('X-NotionTool-Token',''),app.token)
                    if not (bearer or ui):
                        return self.response(401,{'error':'Unauthorized: send Authorization: Bearer <api-key>'})
                    origin = self.headers.get('Origin')
                    if not bearer and origin and origin not in app.origins():
                        return self.response(403,{'error':'Invalid origin'})
                    try:
                        if method=='GET' and path=='/api/state':
                            return self.response(200,app.snapshot())
                        if method=='GET' and path=='/api/health':
                            return self.response(200,app.health())
                        if method=='GET' and path=='/api/chat/ages':
                            return self.response(200,app.chat_ages())  # I50: {warn_h, limit_h, chats: [...]}
                        if method=='GET' and path=='/api/chats':
                            return self.response(200,{'chats':app.chat_list()})
                        if method=='GET' and path=='/api/run':
                            run = app.store.get('runs',query.get('id',[''])[0])
                            if not run:
                                return self.response(404,{'error':'Run not found'})
                            if query.get('compact',[''])[0] in ('1','true','yes') and compact_enabled(app.store):
                                return self.response(200,compact_run_response(app.engine,run))
                            return self.response(200,{'run':{k:v for k,v in run.items() if k!='context'},
                                'tasks':[app.safe_task(t) for t in app.engine.tasks(run['id'])]})
                        if method=='GET' and path=='/api/request':
                            ident = query.get('id',[''])[0]
                            run = next((r for r in app.store.all('runs') if ident and r.get('request_id')==ident),None)
                            return self.response(200,{'found':bool(run),'run_id':run['id'] if run else '','state':run['state'] if run else ''})
                        if method=='GET' and path=='/api/operations':
                            return self.response(200,{'operations':[app.mains.public(o) for o in app.store.all('operations')][-100:]})
                        if method=='GET' and path=='/api/operation':
                            op = app.store.get('operations',query.get('id',[''])[0])
                            if not op:
                                return self.response(404,{'error':'Operation not found'})
                            return self.response(200,app.mains.public(op))
                        if method=='GET' and path=='/api/stream':
                            return app.stream(self,query)
                        if method=='GET' and path=='/api/chat':
                            ident = query.get('id',[''])[0]
                            page = app.store.message_page(ident, query.get('limit',['100'])[0])
                            return self.response(200,dict(page,
                                tasks=[app.safe_task(t) for t in app.store.all('tasks') if app.store.get('runs',t['run_id'],{}).get('chat_id')==ident][-200:]))
                        if method=='GET' and path=='/api/metrics':
                            from .telemetry import task_metrics
                            return self.response(200,{'tasks':task_metrics(app.store,query.get('run_id',[''])[0] or None)})
                        if method=='GET' and path=='/api/coordination':
                            return self.response(200,app.engine.coordination())
                        if method=='GET' and path=='/api/backup':
                            from .backup import status as backup_status
                            return self.response(200,backup_status(app.data_dir or app.store.directory))
                        if method=='POST' and path=='/api/backup/now':
                            from .backup import backup_now
                            return self.response(200,backup_now(app.data_dir or app.store.directory,app.store,reason='manual'))
                        if method=='GET' and path=='/api/patch':
                            return self.response(200,app.engine.patch_status())
                        if method=='GET' and path=='/api/usage':
                            return self.response(200,usage_response(app.store))
                        if method=='GET' and path=='/api/report':
                            task = app.store.get('tasks',query.get('id',[''])[0])
                            return self.response(200,{'task':app.safe_task(task),'result':task['result'],'partial':task.get('raw_response') or task.get('partial','')})
                        if method in ('GET','POST') and (path=='/api/ui/projects' or path.startswith('/api/ui/projects/')):
                            from .projects_routes import http_route as projects_http_route  # Projects page: list, detail, create, update, open, archive
                            return self.response(*projects_http_route(app,method,path,query,self.read_body() if method=='POST' else None))
                        if method=='GET' and (path=='/api/notes' or path.startswith('/api/notes/')):
                            from .notes_routes import http_route as notes_http_route
                            return self.response(*notes_http_route(app,path,query))
                        if method=='GET' and path=='/api/operator/plan_report':
                            from .parallel_plan import plan_report  # rank 10: median and p90 of real/est per role (measured only)
                            return self.response(200,plan_report(app.store,query.get('gain',[''])[0] in ('1','true','yes')))
                        if method=='GET' and path=='/api/history/search':
                            from . import templates  # Live History search over all runs and tasks, limit 50
                            return self.response(200,templates.http_search(app,query))
                        if method=='POST' and path=='/api/ui/error':
                            from . import problems  # Live view problem inbox: overlay errors, deduped and rate limited
                            return self.response(200,problems.handle(app,self.read_body()))
                        if method=='POST' and path=='/api/run/confirm':
                            return self.response(*app.confirm_run_request(self.read_body()))
                        if method=='POST' and path=='/api/run/retry':
                            return self.response(*app.retry_run_request(self.read_body()))
                        if method=='POST':
                            return self.response(200,app.action(path,self.read_body()))
                        return self.response(404,{'error':'Unknown API route'})
                    except (ValueError,KeyError,TypeError,OSError) as exc:
                        return self.response(400,{'error':str(exc)[:400]})
                    except Exception as exc:
                        app.store.event('api_error',message=type(exc).__name__)
                        return self.response(500,{'error':'Operation failed; see the activity log'})
                if method!='GET':
                    return self.response(405,{'error':'Method not allowed'})
                root = Path(__file__).parent/'web'
                file = (root/('index.html' if path=='/' else urllib.parse.unquote(path).lstrip('/'))).resolve()
                if not file.is_relative_to(root.resolve()) or not file.is_file():
                    return self.response(404,b'Not found','text/plain')
                return self.response(200,file.read_bytes(),mimetypes.guess_type(file.name)[0] or 'application/octet-stream')

            def read_body(self):
                length = int(self.headers.get('Content-Length',0))
                if length<=0 or length>2*1024*1024:
                    raise ValueError('Invalid request size')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body,dict):
                    raise ValueError('Expected a JSON object')
                return body

            def do_GET(self):
                self.handle_request('GET')
            def do_POST(self):
                self.handle_request('POST')
            def do_DELETE(self):
                self.handle_request('DELETE')
        settings = self.store.settings()
        bind = '127.0.0.1'
        if settings.get('host_api_allow_network'):
            # Network access is opt-in; every API call still needs the Bearer key or UI token.
            bind = str(settings.get('host_api_bind') or '127.0.0.1')
        if port is None:
            port = int(settings.get('host_api_port') or 0)
        try:
            self.http = ThreadingHTTPServer((bind,port),Handler)
        except OSError:
            self.store.event('host_api_port_busy', message=f'Port {port} is in use; a free local port was chosen')
            self.http = ThreadingHTTPServer((bind,0),Handler)
        self.bind = bind
        self.handler = Handler
        self.http.daemon_threads = True
        self.port = self.http.server_port
        self.adapter.sessions.save('runtime',{'port':self.port,'token':self.token})
        threading.Thread(target=self.http.serve_forever,daemon=True,name='local-http').start()
        if __import__('os').environ.get('NOTIONTOOL_TEST_INSTANCE')=='1':
            # B40 test app (test_app.py): local API, engine and desk actions on its own port and empty data
            # folder. No Notion window, gateway, tunnel, updater, backups, coworker upkeep or wake, so it can
            # never touch the real app or the accounts; it only proves the current code boots and serves.
            self.test_instance=True
            self.engine.start()
            from .turn_watch import TurnWatch
            self.turn_watch=TurnWatch(self.store,self.adapter,self.engine)
            if __import__('os').environ.get('NOTIONTOOL_FAKE_NOTION')=='1':
                # Simulated Notion (fake_notion.py): seeded fake accounts and chats plus the real loops that
                # need Notion data (turn watch, main wake, coworker upkeep). Without the env var nothing changes.
                from .test_app import boot_fake_notion
                boot_fake_notion(self)
            from . import ai_disable_detector, restart_guard, problems, templates
            restart_guard.install_desk_actions(); problems.install_desk_actions(); templates.install_desk_actions()
            self.store.event('test_instance_started', message=f'Test app serving on port {self.port}')
            return f'http://127.0.0.1:{self.port}/#'+self.token
        if self.store.settings().get('remote_mcp_enabled', False):
            try:self.ensure_mcp_listener()
            except Exception as exc:self.store.event('mcp_listener_failed',message=str(exc)[:200])
        self.engine.start()
        threading.Thread(target=self.maintain_bridge,daemon=True,name='bridge-maintenance').start()
        # Resume durable operations; reconciliation never resends an accepted creation.
        for op in self.store.all('operations'):
            if op['state'] not in ('done','failed'):
                self.store.event('operation_resume', message='Resuming '+op['kind']+' after restart', operation_id=op['id'])
                self.job('Resume '+op['kind'].replace('_',' '), lambda ident=op['id']: self.mains.resume(ident), 'op:'+op['id'])
        threading.Thread(target=self.maintain_context,daemon=True,name='chat-context').start()
        from .turn_watch import TurnWatch
        self.turn_watch=TurnWatch(self.store,self.adapter,self.engine)
        threading.Thread(target=self.turn_watch.loop,daemon=True,name='turn-watch').start()
        self.prune_handoffs()  # once at start; sweep_main_archives repeats it every 60 s
        threading.Thread(target=self.sweep_main_archives,daemon=True,name='main-archive-sweep').start()
        threading.Thread(target=self.sweep_main_switches,daemon=True,name='main-switch-sweep').start()
        from .coworker_upkeep import CoworkerUpkeep  # retire deleted/missing coworkers, refill the pool
        self.coworker_upkeep=CoworkerUpkeep(self.store,self.adapter,self.engine)
        threading.Thread(target=self.coworker_upkeep.loop,daemon=True,name='coworker-upkeep').start()
        from .main_wake import MainWake  # tells the operator Main once when a delegated run is completely finished
        self.main_wake=MainWake(self.store,self.adapter,self.engine)
        threading.Thread(target=self.main_wake.loop,daemon=True,name='main-wake').start()
        from .backup import daily_loop  # one backup per day in data/backups (backup.py)
        threading.Thread(target=daily_loop,args=(self.data_dir or self.store.directory,self.store,self.engine.stop_event),daemon=True,name='daily-backup').start()
        from . import ai_disable_detector  # always on from startup, not only after an /api/state call
        ai_disable_detector.start(self.store)
        self.updater = Updater(self)
        self.updater.start()
        if self.store.settings().get('ui_mode','classic')=='notion':
            # Phase 8b: open the Notion AI chat page with the harness overlay; 'classic' changes nothing.
            threading.Thread(target=self.start_notion_shell,daemon=True,name='notion-shell').start()
        try:
            # NotionTool desktop app (docs/DESKTOP_APP.md): opens only when app_autostart is on.
            from . import restart_guard
            restart_guard.install_desk_actions()  # overlay actions app_restart, app_restart_status, app_health
            from . import problems
            problems.install_desk_actions()  # overlay action ui_error (Live view Problems)
            from . import templates
            templates.install_desk_actions()  # overlay actions request_templates, request_draft, history_search
            from .desktop_shell import DesktopController
            self.desktop = DesktopController(self)
            self.desktop.autostart()
        except Exception as exc:
            self.store.event('desktop_app_failed', message=str(exc)[:200])
        return f'http://127.0.0.1:{self.port}/#'+self.token

    def origins(self):
        return {f'http://127.0.0.1:{self.port}', f'http://localhost:{self.port}'}

    def limits(self):
        settings = self.store.settings()
        now = time.time()
        cooling = [a for a in self.store.all('accounts') if a.get('cooldown_until', 0) > now]
        coordination = self.engine.coordination()
        pause = coordination.get('pause') or {}
        paused = ('Remote calls paused: Notion account blocked (' + str(pause.get('reason') or 'sign-in required')
                  + '). Sign in again, then use Retry on the blocked run to resume.') if coordination.get('paused') else ''
        return dict(confirmed='No published concurrency or message limit exists for internal Notion AI chats.',
            observed=(f'Rate limited: cooldown until {time.strftime("%H:%M:%S", time.localtime(max(a["cooldown_until"] for a in cooling)))} (temporary, not a chat limit)'
                      if cooling else 'No active rate limit observed.') if not paused else paused,
            configured=f"Local limits: max {settings.get('max_workers')} parallel chats, {settings.get('per_account')} per account (local setting, not a Notion guarantee).")

    def health(self):
        from .harness_account import selected_account
        ident = selected_account(self.store)
        account = self.store.get('accounts', ident, {}) if ident else {}
        return dict(ok=True, authenticated=True, host='NotionTool', port=self.port, bind=getattr(self, 'bind', '127.0.0.1'),
            network_access=getattr(self, 'bind', '127.0.0.1') not in ('127.0.0.1', 'localhost'),
            schema_version=self.store.schema_version(), account_id=ident, account_state=account.get('state', 'missing'),
            notion_transport=self.store.settings().get('notion_transport'),
            note='Notion connectivity is confirmed only by a completed request; this checks the local host and stored session state.')

    def chat_list(self):
        from .harness_account import selected_account
        selected = selected_account(self.store)
        chats = []
        for chat in self.store.all('chats'):
            worker = self.store.get('workers', chat.get('main_worker'), {})
            if selected and worker.get('account_id') != selected:
                continue
            runs = [r for r in self.store.all('runs') if r['chat_id'] == chat['id']]
            marker = self.store.message_marker(chat['id'])
            chats.append(dict(chat, state=chat.get('state', 'active'), worker_state=worker.get('state', 'missing'),
                generation=int(worker.get('generation', 0)) + 1, context_usage=worker.get('context_usage', {}),
                active_run=next((r['id'] for r in reversed(runs) if r['state'] not in ('done', 'failed', 'blocked', 'cancelled', 'stale')), ''),
                awaiting_main=[r['id'] for r in runs if r.get('state') == 'awaiting_main'],
                message_marker=marker))
        return sorted(chats, key=lambda c: c.get('main_number') or 0)

    def stream(self, handler, query):
        """Server-sent events: durable events (id = event sequence) plus task partial text.
        Reconnect with Last-Event-ID, ?since= or ?after=; state is re-read, nothing is resent upstream."""
        run_filter = query.get('run_id', [''])[0]
        try:
            last = max(0, int(handler.headers.get('Last-Event-ID') or query.get('since', query.get('after', ['0']))[0] or 0))
        except ValueError:
            last = 0
        duration = max(5, min(900, int(query.get('timeout', ['300'])[0] or 300)))
        handler.send_response(200)
        handler.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        handler.send_header('Cache-Control', 'no-store')
        handler.send_header('Connection', 'close')
        handler.end_headers()
        handler.close_connection = True
        sent = {}
        deadline, ping = time.time() + duration, time.time()
        def write(text):
            handler.wfile.write(text.encode('utf-8'))
            handler.wfile.flush()
        try:
            write('retry: 2000\n\n')
            while time.time() < deadline and not self.engine.stop_event.is_set():
                for event in self.store.events(after=last, limit=2000):
                    last = event['seq']
                    if run_filter and event.get('run_id') not in ('', run_filter):
                        continue
                    write(f"id: {event['seq']}\nevent: {event['kind']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n")
                if run_filter:
                    for task in self.engine.tasks(run_filter):
                        text = task.get('partial') or ''
                        key = (task['id'], task.get('attempts', 0))
                        if text and sent.get(key) != len(text):
                            write('event: partial\ndata: ' + json.dumps(dict(task_id=task['id'], attempt=task.get('attempts', 0),
                                state=task['state'], text=text, complete=False), ensure_ascii=False) + '\n\n')
                            sent[key] = len(text)
                    run = self.store.get('runs', run_filter, {})
                    if run.get('state') in ('done', 'failed', 'blocked', 'cancelled', 'stale'):
                        write('event: run_finished\ndata: ' + json.dumps(dict(run_id=run_filter, state=run['state'],
                            summary=run.get('summary', ''), error=run.get('error', ''))) + '\n\n')
                        break
                if time.time() - ping > 15:
                    write(': keepalive\n\n')
                    ping = time.time()
                time.sleep(0.5)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        return None

    def shell_handlers(self):
        """Page actions for the Notion shell (ui_mode 'notion').

        Hard rule: the shell never creates Notion chats; it only opens existing Welcome/worker
        chats. notion_shell.default_handlers also maps new_welcome_chat to POST /api/main/create,
        so that handler is removed here and the page gets 'Action not available'.
        """
        from . import notion_shell
        handlers = notion_shell.default_handlers(self)
        handlers.pop('new_welcome_chat', None)
        return handlers

    def start_notion_shell(self):
        """Start notion_shell.NotionShell in the selected account's Edge profile."""
        from . import notion_shell
        from .harness_account import selected_account
        try:
            account_id = selected_account(self.store)
            if not account_id:
                raise ValueError('Choose a harness account before using the Notion UI mode')
            browser = self.adapter.edge_transport(account_id).browser
            shell = notion_shell.NotionShell(browser, account_id, self.shell_handlers(),
                                             secrets=notion_shell.shell_secrets(self))
            shell.start()
        except Exception as exc:
            self.store.event('notion_shell_failed', message=str(exc)[:200])
            return None
        self.notion_shell = shell
        return shell

    def close(self):
        from .backup import before_restart
        before_restart(self)  # backup before every stop, restart and update; never raises
        if getattr(self, 'desktop', None) is not None:
            try:self.desktop.stop(close_window=False)
            except Exception:pass
        if getattr(self, 'notion_shell', None) is not None:
            try:self.notion_shell.close()
            except Exception:pass
        self.bridge.close_local()
        if self.bridge._browser is not None:
            self.bridge._browser.close()
        self.account_pages.close()
        self.engine.close()
        for transport in getattr(self.adapter, 'edge_transports', {}).values():
            transport.close()
        self.admin.shutdown(wait=False,cancel_futures=True)
        if self.tunnel:
            self.tunnel.terminate()
        if self.http:
            self.http.shutdown()
            self.http.server_close()
        if self.mcp_http:
            self.mcp_http.shutdown();self.mcp_http.server_close()
        if self.tailscale and not self.gateway_port:  # with the gateway the route stays; process 2 bridges the restart
            try:self.tailscale.stop()
            except Exception:pass

    def maintain_bridge(self):
        while not self.engine.stop_event.is_set():
            settings=self.store.settings()
            if settings.get('remote_mcp_enabled',False) or (settings.get('automatic_setup', False) and settings.get('bridge_auto', False)):
                try:self.job(self.BRIDGE_JOB_TITLE,self.start_tunnel,'bridge',key=self.BRIDGE_JOB_ID)
                except ValueError:pass
            if self.engine.stop_event.wait(60):break

    @staticmethod
    def safe_task(task):
        return {k:v for k,v in task.items() if k not in ('lease','prompt','submitted_prompt','step_id','result','partial','raw_response')}

    def maintain_context(self):
        from .harness_account import selected_account
        from .chat_context import ChatContext
        from .network_budget import set_thread_priority
        set_thread_priority('background')  # scan failures must never throttle task parallelism
        while not self.engine.stop_event.wait(30):
            if not self.store.settings().get('background_remote', False):
                continue
            account_id = selected_account(self.store)
            if not account_id or self.store.get('accounts', account_id, {}).get('state') != 'ready':
                continue
            try:
                failed = None
                for worker in self.store.all('workers'):
                    if self.engine.stop_event.is_set():
                        return
                    if worker.get('account_id') != account_id or worker.get('state') != 'ready':
                        continue
                    if time.time() - worker.get('context_checked', 0) < 120:
                        continue
                    try:
                        transcript = self.adapter.client(account_id).transcript(worker['thread_id'], limit=30)
                        archive = self.store.get('thread_context', account_id + ':' + worker['thread_id'], {})
                        if not archive.get('native_archived_at'):
                            ChatContext(self.store).archive_native(self.adapter.client(account_id), worker)
                        usage = ChatContext(self.store).usage(worker, transcript)
                    except Exception as exc:
                        # One unreadable chat (e.g. a very long one) must not stop the scan of the others.
                        failed = failed or exc
                        with self.engine.lock:
                            fresh = self.store.get('workers', worker['id'])
                            if fresh and fresh['thread_id'] == worker['thread_id']:
                                fresh.update(context_checked=time.time())
                                self.store.put('workers', fresh)
                        continue
                    with self.engine.lock:
                        fresh = self.store.get('workers', worker['id'])
                        if fresh and fresh['thread_id'] == worker['thread_id']:
                            fresh.update(context_usage=usage, context_checked=time.time())
                            self.store.put('workers', fresh)
                if failed:
                    raise failed
                if getattr(self, 'context_scan_blocked', False):
                    self.context_scan_blocked = False
                    self.store.event('context_scan_resumed', message='Native context scan works again')
            except Exception as exc:
                # Record the waiting state once per change instead of on every pass.
                if not getattr(self, 'context_scan_blocked', False):
                    self.context_scan_blocked = True
                    from .telemetry import classify
                    self.store.event('context_scan_waiting', error=type(exc).__name__, error_class=classify(exc),
                                     message='Native context scan deferred; check the selected account and Edge tab')

    def snapshot(self):
        from .harness_account import selected_account, main_chat
        from . import ai_disable_detector
        ai_disable_detector.start(self.store)  # always-on detector for 'Notion AI is disabled'
        ai_disable_detector.scan(self.store)
        selected = selected_account(self.store)
        canonical = main_chat(self.store, selected) if selected else None
        accounts = [{k:v for k,v in a.items() if k!='identity'} for a in self.store.all('accounts')]
        workers = [{k:v for k,v in w.items() if k not in ('lease','handoff')} for w in self.store.all('workers') if w.get('account_id') == selected]
        for account in accounts:
            account['setup']=account_setup(account,workers)
        ready_accounts={a['id'] for a in accounts if a.get('state')=='ready'}
        runs = [{k:v for k,v in r.items() if k!='context'} for r in self.store.all('runs')]
        tasks = [self.safe_task(t) for t in self.store.all('tasks')]
        stalled = Application.stall_flags(self, tasks)  # O8: memory only, no store read beyond tasks
        return dict(name='NotionTool',mcp_servers=self.mcp_servers.public(),accounts=accounts,workers=workers,folders=self.store.all('folders'),
            chats=self.chat_list(),operations=[self.mains.public(o) for o in self.store.all('operations')][-30:],host_api=dict(port=self.port,bind=getattr(self,'bind','127.0.0.1')),limits=self.limits(),runs=runs,tasks=tasks,stalled=stalled,
            main_context_usage=Application.main_context_usage(self, workers),
            token_budget=token_budget_public(self.store),
            ai_disable_reports=ai_disable_detector.reports(self.store, 5),
            team=Application.team_state(self, selected),  # automatic team growth (team_grow.py)
            roles=role_state(self.store.settings()),  # P4b-1: built-in + custom role definitions
            stale_context_tasks=Application.stale_context(self),  # report only (stale_tasks.py)
            awaiting_main=[r['id'] for r in runs if r.get('state') == 'awaiting_main'],
            harness_account_id=selected,settings=self.store.settings(),jobs=self.store.all('jobs')[-30:],events=self.store.events(),
            bridge=dict(local_url=f'http://127.0.0.1:{self.port}/mcp',public_url=self.store.settings().get('public_mcp_url','')),
            stats=dict(running=sum(w['state']=='busy' for w in workers),ready=sum(w.get('enabled') and w['state']=='ready' and w['purpose']=='worker' and w.get('account_id') in ready_accounts for w in workers),
                       recovering=sum(w['state']=='recovering' for w in workers)))

    def stale_context(self):
        """Stuck native-compression tasks for /api/state (read-only, at most 20). Never raises."""
        try:
            from .stale_tasks import stale_context_tasks
            return stale_context_tasks(self.store)[:20]
        except Exception:
            return []

    MAIN_ROTATE_LOCK = threading.Lock()
    MAIN_ROTATE_JOBS = {}  # account_id -> job id of the last /api/main/rotate job

    def prune_handoffs(self):
        """Delete finished or expired handoff files, only inside store.directory/'handoffs'. Never raises."""
        try:
            from . import handoff_files
            folder = Path(self.store.directory)/'handoffs'
            deleted = handoff_files.gc(str(folder), time.time())
            if deleted:
                self.store.event('handoff_files_pruned', message='%d handoff file(s) removed' % len(deleted))
            return deleted
        except Exception as exc:
            try:
                self.store.event('handoff_prune_failed', message=(type(exc).__name__ + ': ' + str(exc))[:250])
            except Exception:
                pass
            return []

    def report_check_failure(self, kind, exc):
        """Write one event per distinct failure of a periodic check. Never raises."""
        text = (type(exc).__name__ + ': ' + str(exc))[:250]
        seen = self.__dict__.setdefault('check_failures', {})
        if seen.get(kind) == text:
            return
        seen[kind] = text
        try:
            self.store.event(kind, message=text)
        except Exception:
            pass

    def last_tool_step(self, task):
        """Newest tool or verify event of one task (epoch seconds), or the stored last_tool_at if newer.
        Lets the stall watch see coworkers that call tools but have not streamed text yet. Never raises."""
        best = task.get('last_tool_at')
        try:
            store = self.store
            with store.lock:
                row = store.db.execute(
                    "SELECT max(ts) FROM events WHERE json_extract(doc, '$.task_id')=? AND kind IN (?,?,?,?)",
                    (task.get('id'), *TOOL_STEP_KINDS)).fetchone()
            if row and row[0] is not None and (not isinstance(best, (int, float)) or row[0] > best):
                best = row[0]
        except Exception:
            pass  # stores without an events table (tests, stand-ins) keep the stored value
        return best

    def check_stalls(self, now=None):
        """O8 report only (rule I5): flag running tasks without progress and log ONE task_stalled event per task.
        Never stops, cancels, pauses or resends anything and never writes a task. Never raises."""
        try:
            from . import stall_watch
            now = time.time() if now is None else now
            workers = {w.get('id'): w for w in self.store.all('workers')}
            tasks, turns, open_ids = {}, [], set()
            for task in self.store.all('tasks'):
                if task.get('state') not in TERMINAL and not task.get('remote_done_at'):
                    open_ids.add(task.get('id'))  # open turn, also while recovering ('uncertain') after a restart
                if task.get('state') != 'running' or not task.get('sent_at') or task.get('remote_done_at'):
                    continue
                worker = workers.get(task.get('worker_id')) or {}
                tasks[task['id']] = task
                turns.append(dict(task_id=task['id'], thread_id=task.get('thread_id') or worker.get('thread_id'),
                                  worker_id=task.get('worker_id'), worker_label=worker.get('label'),
                                  sent_at=task.get('sent_at'),
                                  last_output_at=task.get('last_output_at') or task.get('first_output_at'),
                                  last_event_at=task.get('last_event_at'), last_tool_at=Application.last_tool_step(self, task)))
            reports = stall_watch.report_stalled(turns, [], now, self.store.settings())
            reported = Application.load_stall_reported(self)  # persisted: a restart does not report the same turn again
            before = list(reported)
            for ident in [i for i in reported if i not in open_ids]:
                del reported[ident]  # the turn is finished or gone: prune
            current = {}
            for report in reports:
                ident = report['task_id']
                current[ident] = dict(last_progress_at=report['last_progress_at'], idle_s=report['idle_s'],
                                      worker_label=report.get('worker_label'))
                if ident not in reported:
                    reported[ident] = True
                    self.store.event('task_stalled', tasks[ident].get('run_id', ''), task_id=ident,
                                     worker_id=report.get('worker_id'), idle_s=report['idle_s'],
                                     message=stall_watch.report_text(report))
            while len(reported) > STALL_REPORTED_CAP:
                del reported[next(iter(reported))]  # keep the newest ids only
            if list(reported) != before:
                Application.save_stall_reported(self, reported)
            self.stalled_tasks = current
            return current
        except Exception as exc:
            Application.report_check_failure(self, 'stall_check_failed', exc)
            return {}

    def load_stall_reported(self):
        """Stall-reported task ids (insertion order = oldest first), loaded once from the store meta table.
        A store without a database (test fakes) keeps them in memory only."""
        reported = self.__dict__.get('stall_reported')
        if reported is None:
            ids = []
            db = getattr(self.store, 'db', None)
            if db is not None:
                with self.store.lock:
                    row = db.execute("SELECT value FROM meta WHERE key='stall_reported'").fetchone()
                try:
                    ids = [str(i) for i in json.loads(row[0]) if i] if row else []
                except (TypeError, ValueError):
                    ids = []  # unreadable value: start empty
            reported = self.__dict__['stall_reported'] = dict.fromkeys(ids[-STALL_REPORTED_CAP:], True)
        return reported

    def save_stall_reported(self, reported):
        """Save the newest STALL_REPORTED_CAP ids, as store.set_schema_version saves meta values."""
        db = getattr(self.store, 'db', None)
        if db is None:
            return False
        value = json.dumps(list(reported)[-STALL_REPORTED_CAP:])
        with self.store.lock, db:
            db.execute("INSERT INTO meta VALUES('stall_reported',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (value,))
        return True

    def stall_flags(self, tasks, now=None):
        """O8: add stalled/stall_age_s to running tasks flagged by check_stalls (memory only). Never raises."""
        try:
            now = time.time() if now is None else now
            stalled = getattr(self, 'stalled_tasks', None) or {}
            flagged = []
            for task in tasks:
                entry = stalled.get(task.get('id'))
                if entry and task.get('state') == 'running':
                    task.update(stalled=True, stall_age_s=round(max(0.0, now - entry['last_progress_at']), 1))
                    flagged.append(dict(task_id=task['id'], stall_age_s=task['stall_age_s'],
                                        worker_label=entry.get('worker_label')))
            return flagged
        except Exception as exc:
            Application.report_check_failure(self, 'stall_check_failed', exc)
            return []

    def main_context_usage(self, workers):
        """P5: each Main chat's context usage against context_token_limit / context_turn_limit.
        Reuses worker['context_usage'] written by ChatContext.usage() in rotate() (chat_context.py:45, :214, :222);
        nothing is recomputed and no Notion call is made. Never raises."""
        try:
            settings = self.store.settings()
            token_limit = int(settings.get('context_token_limit', 1200000))
            turn_limit = int(settings.get('context_turn_limit', 600))
            rows = []
            for worker in workers:
                if worker.get('purpose') != 'main':
                    continue
                usage = worker.get('context_usage') if isinstance(worker.get('context_usage'), dict) else {}
                tokens, turns = int(usage.get('estimated_tokens') or 0), int(usage.get('turns') or 0)
                rows.append(dict(worker_id=worker.get('id'), label=worker.get('label'), thread_id=worker.get('thread_id'),
                                 account_id=worker.get('account_id'), tokens=tokens, turns=turns,
                                 token_limit=token_limit, turn_limit=turn_limit,
                                 token_percent=round(100.0*tokens/token_limit, 1) if token_limit > 0 else None,
                                 turn_percent=round(100.0*turns/turn_limit, 1) if turn_limit > 0 else None,
                                 over_limit=tokens >= token_limit or turns >= turn_limit, measured=bool(usage)))
            return rows
        except Exception as exc:
            Application.report_check_failure(self, 'main_context_usage_failed', exc)
            return []

    def sweep_main_switches(self):
        # Drives ask-first Main switches (main_switch.py); does nothing while no switch is active.
        from . import main_switch
        from .main_handoff_ops import switch_ops
        last_error = ''
        while not self.engine.stop_event.wait(20):  # stops with the app, like main-archive-sweep
            try:
                if any(r.get('state') in main_switch.ACTIVE for r in self.store.all(main_switch.KIND)):
                    main_switch.sweep(self.store, lambda record: switch_ops(self.store, self.adapter, record))
                last_error = ''
            except Exception as exc:
                if str(exc) != last_error:
                    last_error = str(exc)
                    try:
                        self.store.event('main_switch_sweep_failed', message=last_error[:300])
                    except Exception:
                        return  # the store is closed: the app is shutting down

    def sweep_main_archives(self):
        """Phase 5b: every 60 s let main_handoff_ops delete archived Main chats past DELETE_DELAY. Never raises."""
        last = ''
        while not self.engine.stop_event.wait(60):
            self.prune_handoffs()  # local handoff cleanup; runs even without background_remote
            Application.check_stalls(self)  # O8 report only: event + flag; never stops, cancels or resends
            if not self.store.settings().get('background_remote', False):
                continue
            try:
                from . import main_handoff_ops
                main_handoff_ops.sweep_archives(self.store, self.adapter)
                last = ''
            except Exception as exc:
                text = (type(exc).__name__ + ': ' + str(exc))[:250]
                if text != last:  # log each distinct error once, not every minute
                    last = text
                    try:
                        self.store.event('main_archive_sweep_failed', message=text)
                    except Exception:
                        pass

    def grow_teams(self, old_size):
        """After a settings save: when the Coworker team size grew, reconcile each account with a managed team.
        ensure_team only adds the missing roles (spare pool chats first; the pending ledger keeps I5).
        A smaller size changes nothing in Notion. Manual route: POST /api/account/team."""
        from .team_size import team_accounts, team_size
        size = team_size(self.store.settings())
        if size <= old_size:
            return []
        started = []
        for ident in team_accounts(self.store.all('accounts')):
            try:
                job = self.job('Grow Welcome Coworkers to %d' % size, lambda ident=ident: self.adapter.ensure_team(ident), ident)
                started.append(job['job_id'])
            except ValueError as exc:
                self.store.event('team_resize_deferred', message='Coworker team resize waits: ' + str(exc)[:160])
        return started

    def team_state(self, account_id=''):
        """state.team {account, target, usable, growing, last_error} from team_grow; None without a ready account. Never raises."""
        grower = getattr(self, 'team_grower', None)
        if grower is None:
            return None
        try:
            return grower.public(account_id)
        except Exception:
            return None

    def job(self, title, fn, account_id='', key=''):
        """Run fn on the admin pool. A key keeps one record for a periodic job; its event is logged only when the outcome changes."""
        with self.admin_lock:
            if account_id and account_id in self.account_operations:
                raise ValueError('An operation is already running for this account')
            if account_id:
                self.account_operations.add(account_id)
        previous = self.store.get('jobs',key) if key else None
        job = self.store.put('jobs',dict(id=key or uid('j'),title=title,state='running',created=time.time(),error='',result={},account_id=account_id))
        def work():
            try:
                result = fn()
                job.update(state='done',result=result)
            except Exception as exc:
                job.update(state='failed',error=str(exc)[:400])
            finally:
                if account_id:
                    with self.admin_lock:
                        self.account_operations.discard(account_id)
            self.store.put('jobs',job)
            if not previous or (previous.get('state'),previous.get('error'))!=(job['state'],job['error']):
                self.store.event('operation_finished',message=title,state=job['state'],error=job['error'])
        self.admin.submit(work)
        return {'job_id':job['id']}

    def job_once(self, title, fn, key):
        """Like job(), but a second request for the same operation key is a no-op."""
        try:
            return self.job(title, fn, key)
        except ValueError as exc:
            if 'already running' in str(exc):
                return {}
            raise

    def prepare_account(self, account_id):
        """After sign-in or an account switch: load the Main and set up the Tailscale MCP bridge automatically."""
        result = {'account_id': account_id}
        if not self.store.settings().get('automatic_setup', False):
            return result
        try:
            result['main'] = self.adapter.ensure_main(account_id)
        except Exception as exc:
            result['main_error'] = str(exc)[:250]
            self.store.event('main_load_failed', message=str(exc)[:250])
        try:
            bridge = self.start_tunnel()
            result['bridge'] = bridge
            account = self.store.get('accounts', account_id, {})
            self.store.event('mcp_auto_setup', message=('NotionTool MCP connected via Tailscale' if account.get('bridge_connected')
                             else 'Tailscale ready; MCP connection pending: ' + str(account.get('bridge_error', ''))[:200]))
        except Exception as exc:
            result['bridge_error'] = str(exc)[:250]
            account = self.store.get('accounts', account_id)
            if account:
                account['bridge_error'] = str(exc)[:250]
                self.store.put('accounts', account)
            self.store.event('mcp_auto_setup_failed', message=str(exc)[:250])
        return result

    def first_login_setup(self, account_id):
        """First sign-in of an account: NotionTool MCP (read and write tools run automatically) if it is not
        connected yet, then one Main and five Welcome Coworkers, all as Welcome chats.

        Runs once per account (first_setup_done); ensure_team is idempotent, so nothing is created twice."""
        account = self.store.get('accounts', account_id, {}) or {}
        result = {'account_id': account_id}
        if account.get('state') != 'ready':
            return {**result, 'skipped': 'account state ' + str(account.get('state'))}
        from .setup_status import account_setup
        from .account_settings import open_main
        # A local flag is not proof: read Notion's connection and read/write permissions on every login.
        from .notion_connections import ensure_notiontool
        try:
            result['mcp'] = ensure_notiontool(self, account_id)
            account = self.store.get('accounts', account_id, {}) or {}
        except Exception as exc:  # noqa: BLE001
            result['mcp_error'] = str(exc)[:250]
            account = dict(self.store.get('accounts', account_id, {}) or {})
            account.update(bridge_connected=False, bridge_error=result['mcp_error'])
            self.store.put('accounts', account)
            self.store.event('mcp_auto_setup_failed', message='Sign-in: ' + str(exc)[:230])
        if ('mcp_error' not in result and account.get('first_setup_done') and
                account_setup(account, self.store.all('workers'), size=5)['complete']):
            open_main(self, account_id)
            return {**result, 'skipped': 'already set up'}
        try:
            result['team'] = self.adapter.ensure_team(account_id, create_main=True, count=5)
        except Exception as exc:  # noqa: BLE001
            result['team_error'] = str(exc)[:250]
            self.store.event('first_login_setup_failed', message='Main/Coworkers: ' + str(exc)[:220])
            open_main(self, account_id)
            return result
        if 'mcp_error' not in result and result['team'].get('verified'):
            fresh = self.store.get('accounts', account_id, {}) or {}
            fresh['first_setup_done'] = True
            self.store.put('accounts', fresh)
        self.store.event('first_login_setup', message='Main and five Welcome Coworkers ' +
                         ('ready' if result['team'].get('verified') else 'created; waiting for confirmation'))
        open_main(self, account_id)
        return result

    def window_signed_in(self, cookies):
        """The NotionTool window signed in (window_login): use the same account in the harness, then the first-login setup."""
        def adopt():
            result = self.adapter.adopt_session_cookies(cookies, 'window_login', select=True)
            ident = result.get('account_id', '')
            resumed = self.resume_after_sign_in(ident)
            if resumed is not None:
                result['coordination'] = resumed
            result['first_setup'] = self.first_login_setup(ident)
            return result
        return self.job('Use the NotionTool window sign-in', adopt, 'login')

    def resume_after_sign_in(self, account_id):
        """Lift an auth pause once the account that caused it signed in successfully."""
        coordination = self.engine.coordination()
        pause = coordination.get('pause') or {}
        blocked_account = str(pause.get('account_id') or '')
        if not coordination.get('paused') or (blocked_account and blocked_account != account_id):
            return None
        return self.engine.resume_remote()

    def finish_account_result(self, result):
        ident = result.get('account_id')
        account = self.store.get('accounts', ident, {})
        if account.get('state') != 'ready':
            return result
        if not self.store.settings().get('automatic_setup', False):
            status = account_setup(account, self.store.all('workers'))
            return {**result, 'setup': status, 'complete': status['complete']}
        if not account.get('bridge_connected'):
            url = self.store.settings().get('public_mcp_url')
            if not url:
                self.start_tunnel()
                url = self.store.settings().get('public_mcp_url')
            self.adapter.connect_bridge(ident, url, self.bridge_token)
        status = account_setup(self.store.get('accounts', ident), self.store.all('workers'))
        if self.mcp_servers.public():
            self.mcp_servers.connect(ident)
        return {**result, 'setup': status, 'complete': status['complete']}

    def add_worker(self, account_id=''):
        if not account_id:
            from .harness_account import selected_account
            account_id = selected_account(self.store)
        account = self.store.get('accounts', account_id)
        if not account:
            raise ValueError('Account not found')
        status = account_setup(account, self.store.all('workers'))
        if status['stage'] == 'ai_disabled':
            raise ValueError(account.get('error') or 'Notion AI is disabled for this workspace; refresh after access is restored.')
        if status['complete']:
            before = {w['id'] for w in self.store.all('workers') if w['account_id'] == account_id}
            result = self.adapter.provision(account_id, 1)
            client = self.adapter.client(account_id)
            model = client.model()
            for worker in self.store.all('workers'):
                if worker['account_id'] == account_id and worker['id'] not in before:
                    self.adapter.ensure_thread_model(client, worker['thread_id'], model)
            return {**result, 'account_id': account_id}
        if account['state'] != 'ready':
            raise ValueError('Sign in to Notion again (Settings > Sign in) before preparing chats.')
        # Setup uses the account's existing paid plan; nothing is activated.
        return self.finish_account_result(self.adapter.ensure_team(account_id))

    RESEND_REFUSAL = 'Retry would resend work that already reached Notion'

    CHAT_TOO_OLD = 'chat_too_old'

    def chat_age(self, chat, now=None):
        """I50: {age_s, created_at} of one harness chat; both None while the creation time is unknown.

        The Notion thread of the chat's Main worker counts first (created_at, thread_created_at), then the
        chat record (created_at, created). Times are UTC epoch seconds (team_rotation.epoch)."""
        from .team_rotation import epoch
        now = time.time() if now is None else now
        chat = chat or {}
        worker = (self.store.get('workers', chat['main_worker']) or {}) if chat.get('main_worker') else {}
        created = 0.0
        for value in (worker.get('created_at'), worker.get('thread_created_at'), Application._switched_at(self, worker),
                      chat.get('created_at'), chat.get('created')):
            created = epoch(value)
            if created:
                break
        if not created:
            return dict(age_s=None, created_at=None)
        return dict(age_s=int(max(0.0, now - created)), created_at=created)

    def _switched_at(self, worker):
        """Time a Main switch moved a chat to this worker's thread (0 if none). Bug 2026-10-25: after a switch
        the chat record keeps its old 'created', so a fresh Main was shown as 27 h old."""
        thread = str((worker or {}).get('thread_id') or '')
        if not thread:
            return 0
        for record in self.store.all('main_switches'):
            if record.get('new_chat_id') == thread and record.get('state') == 'switched':
                return record.get('prompt_at') or record.get('updated') or 0
        return 0

    CHAT_AGE_WARN_HOURS = 20
    SWITCH_BUSY_STATES = ('running', 'sending', 'sent', 'uncertain', 'reviewing', 'cancelling', 'claimed',
                          'tool_pending', 'executing', 'fixing', 'checkpoint', 'preparing')

    def chat_ages(self, now=None):
        """GET /api/chat/ages (I50): {warn_h: 20, limit_h: 23, chats: [...]} for the desktop sidebar.

        One row per harness chat (its Main worker's Notion thread) plus one per Coworker chat that is no
        chat's Main. Row: id, worker_id, thread_id, title, purpose, created_at, age_h (= age_s / 3600) and
        blocked (age at or over coworker_age_guard.HARD_MAX_AGE_HOURS, the limit of the /api/send block).
        Unknown age: age_h None, never blocked."""
        from .coworker_age_guard import HARD_MAX_AGE_HOURS, HARD_MAX_AGE_S
        from .team_rotation import epoch
        now = time.time() if now is None else now

        def row(ident, worker_id, thread_id, title, purpose, age):
            age_s = age.get('age_s')
            return dict(id=str(ident), worker_id=str(worker_id or ''), thread_id=str(thread_id or ''),
                        title=str(title or '')[:120], purpose=str(purpose or ''), created_at=age.get('created_at'),
                        age_h=(age_s / 3600.0) if age_s is not None else None,
                        blocked=bool(age_s is not None and age_s >= HARD_MAX_AGE_S))
        rows, seen = [], set()
        for chat in self.store.all('chats'):
            if not chat.get('id'):
                continue
            wid = str(chat.get('main_worker') or '')
            worker = (self.store.get('workers', wid) or {}) if wid else {}
            rows.append(row(chat['id'], wid, worker.get('thread_id') or chat.get('thread_id'),
                            chat.get('title') or worker.get('label'), worker.get('purpose') or 'main',
                            Application.chat_age(self, chat, now)))
            if wid:
                seen.add(wid)
        for worker in self.store.all('workers'):
            if worker.get('id') in seen or worker.get('purpose') != 'worker' or not worker.get('thread_id'):
                continue
            created = 0.0
            for value in (worker.get('created_at'), worker.get('thread_created_at')):
                created = epoch(value)
                if created:
                    break
            age = dict(age_s=int(max(0.0, now - created)), created_at=created) if created else dict(age_s=None, created_at=None)
            rows.append(row(worker['id'], worker['id'], worker['thread_id'], worker.get('label') or 'Coworker', 'worker', age))
        return dict(warn_h=Application.CHAT_AGE_WARN_HOURS, limit_h=HARD_MAX_AGE_HOURS, chats=rows)

    def chat_context_switch(self, body, now=None):
        """POST /api/chat/context_switch (I50): move one chat into a fresh Notion chat.

        Takes worker_id, or chat_id (-> the chat's Main worker). Uses the Coworker rotation path
        ChatContext.fresh_welcome: a new Welcome chat is created and verified, the worker and a short handoff
        move there, the old thread goes to retired_threads (sidebar archive). The age restarts (created_at =
        now). Returns worker_id, chat_id, the new thread_id and old_thread_id. Raises ValueError (nothing
        moved, the old chat stays in use) when the chat is busy or Notion did not list the new chat."""
        from .chat_context import ChatContext
        body = body or {}
        worker_id = str(body.get('worker_id') or '').strip()[:100]
        chat_id = str(body.get('chat_id') or '').strip()[:100]
        if not worker_id and chat_id:
            chat = self.store.get('chats', chat_id)
            if not chat:
                raise ValueError('Chat not found')
            worker_id = str(chat.get('main_worker') or '')
            if not worker_id:
                raise ValueError('This chat has no Notion chat to switch')
        if not worker_id:
            raise ValueError('Choose a chat (worker_id or chat_id) to switch')
        worker = self.store.get('workers', worker_id)
        if not worker or not worker.get('thread_id'):
            raise ValueError('Worker not found or without a Notion chat')
        if not worker.get('account_id'):
            raise ValueError('This chat has no Notion account')
        if any(t.get('worker_id') == worker_id and t.get('state') in Application.SWITCH_BUSY_STATES
               for t in self.store.all('tasks')):
            raise ValueError('This chat is working on a task; switch after it finishes')
        from . import foreign_chat_guard
        if foreign_chat_guard.is_own(worker):
            # RBX fix: an own chat switches like the 22 h auto switch (fresh chat keeps the user's name,
            # the old one becomes '<title> (expired)').
            result = foreign_chat_guard.switch_own_chat(self.store, self.adapter, worker, now, reason='user')
            return dict(result, chat_id=chat_id)
        if worker.get('purpose') != 'worker':
            # One way only: a Main always switches through the file handoff (main_switch.py).
            from . import main_switch
            from .operator_main import delegate
            chat_id = chat_id or next((str(c.get('id') or '') for c in self.store.all('chats')
                                       if c.get('main_worker') == worker_id), '')
            request_id = str(body.get('request_id') or ('main-switch-' + time.strftime('%Y%m%d-%H%M%S')))
            return main_switch.start(self.store, chat_id,
                lambda b: delegate(self.engine, b['chat_id'], b['goal'], b['tasks'], b['request_id']), request_id,
                brief_text=body.get('brief_text'), trigger='user')
        old, label = worker['thread_id'], worker.get('label')
        handoff = ('Context switch from the NotionTool desktop: this chat replaces the older chat %s '
                   '(chats are limited to %d h). Continue the work of that chat; NotionTool keeps its state.'
                   % (old, Application.chat_switch_limit()))
        client = self.adapter.client(worker['account_id'])
        task = dict(id='', run_id=str(body.get('run_id') or '')[:100])
        main = worker.get('purpose') != 'worker'
        # Manual switch: the old chat is never deleted (it stays in the sidebar archive), only renamed;
        # a Main's old chat becomes '<label> (expired)', a Coworker's keeps the retire_old title.
        switch_adapter = ManualSwitchAdapter(self.adapter, archived_title(label) if main else None)
        moved = ChatContext(self.store).fresh_welcome(switch_adapter, client, worker, task, dict(handoff=handoff),
                                                      worker.get('context_usage') or {})
        new = (moved or {}).get('thread_id') or ''
        if not new or new == old:
            raise ValueError('Context switch failed: Notion did not list the new chat; the old chat is still used')
        now = time.time() if now is None else now
        with self.store.lock:
            fresh = self.store.get('workers', worker_id) or dict(moved)
            fresh.update(created_at=now, thread_created_at=now)
            if main and label:
                fresh['label'] = label  # a Main keeps its name
            self.store.put('workers', fresh)
        renamed = None
        if main and label:
            # The new Notion chat is titled 'Welcome to Notion'; give it the Main's name. A failed rename
            # never undoes the switch (the worker already moved); it is only reported.
            try:
                self.adapter.rename_thread(client, new, label)
                renamed = True
            except Exception as exc:  # noqa: BLE001
                renamed = False
                self.store.event('chat_context_switch_rename_failed', '', worker_id=worker_id,
                                 message=(new + ': ' + type(exc).__name__ + ': ' + str(exc))[:250])
        self.store.event('chat_context_switch', '', worker_id=worker_id, message=old + ' -> ' + new)
        return dict(ok=True, worker_id=worker_id, chat_id=chat_id, thread_id=new, old_thread_id=old,
                    renamed=renamed)

    @staticmethod
    def chat_switch_limit():
        from .coworker_age_guard import HARD_MAX_AGE_HOURS
        return HARD_MAX_AGE_HOURS

    def require_young_chat(self, chat_id, now=None):
        """I50 hard send block: a chat 23 h or older (coworker_age_guard.HARD_MAX_AGE_HOURS) is never written to.
        Raises ValueError (HTTP 400) before anything reaches the engine or Notion. Unknown age is not blocked.
        RBX fix: the chat's Notion thread is also checked by its Notion created_time (foreign_chat_guard), so
        a chat record that restarted its age can never hide an old thread."""
        from .coworker_age_guard import HARD_MAX_AGE_S
        from . import foreign_chat_guard
        chat = self.store.get('chats', chat_id)
        age = Application.chat_age(self, chat, now)['age_s']
        if age is not None and age >= HARD_MAX_AGE_S:
            self.store.event('chat_send_blocked_too_old', '', message='%s: %.1f h old (chat record)' % (chat_id, age / 3600.0))
            raise ValueError(foreign_chat_guard.block_text())
        worker = self.store.get('workers', str((chat or {}).get('main_worker') or '')) or {}
        if worker.get('thread_id'):
            foreign_chat_guard.require_young(self.store, self.adapter, worker['thread_id'], now,
                                             str(worker.get('account_id') or ''))

    def chat_send_check(self, body, now=None):
        """POST /api/chat/send_check {thread_id, send}: the one age check of the composer send paths (desktop
        and Edge shell) for ANY Notion chat, also chats NotionTool did not create (RBX). Age = Notion thread
        created_time. send=true and >= 23 h: refused (allowed=false, message) and the own chat's context switch
        starts in the background; the composer never passes the text to Notion."""
        from . import foreign_chat_guard
        body = body if isinstance(body, dict) else {}
        thread = foreign_chat_guard.thread_key(body.get('thread_id'))
        if len(thread) != 32:
            raise ValueError('Choose a chat (thread_id) to check')
        account = str(body.get('account_id') or '')[:100]
        on_block = None
        if body.get('send'):
            def on_block(info):
                work = lambda: foreign_chat_guard.blocked_send(self.store, self.adapter, thread, account)
                job = getattr(self, 'job_once', None)
                if callable(job) and not getattr(self, 'app_chat_inline', False):
                    job('Context switch (chat age) ' + thread[:8], work, 'age-switch:' + thread)
                else:
                    work()
        result = foreign_chat_guard.send_check(self.store, self.adapter, thread, now, account, on_block)
        if body.get('send') and not result.get('allowed', True):
            raise ValueError(foreign_chat_guard.refusal_text(result))  # 400: a 23 h chat or unknown age is refused
        return result

    def retry_run_request(self, body):
        """POST /api/run/retry {id, allow_resend}: (status, payload). Only the engine's resend refusal becomes 409."""
        try:
            return 200, self.action('/api/run/retry', body)
        except ValueError as exc:
            text = str(exc)
            if text.startswith(self.RESEND_REFUSAL):
                return 409, {'error': text}
            raise  # every other error keeps the generic handling (400/500)

    def confirm_run_request(self, body):
        """POST /api/run/confirm {id}: the Main confirms a run in awaiting_main. Returns (status, payload)."""
        confirm = getattr(self.engine, 'confirm_run', None)
        if not callable(confirm):
            return 501, {'error': 'Run confirmation is not available: the engine has no confirm_run yet'}
        run_id = str((body or {}).get('id') or '').strip()
        run = self.store.get('runs', run_id) if run_id else None
        if not run:
            return 404, {'error': 'Run not found'}
        if run.get('state') != 'awaiting_main':
            return 409, {'error': 'Run is not awaiting Main confirmation (state: %s)' % run.get('state', '')}
        result = confirm(run_id)
        saved = self.store.get('runs', run_id) or {}
        return 200, {'ok': True, 'id': run_id, 'state': saved.get('state', ''),
                     'result': result if isinstance(result, dict) else None}

    def action(self,path,body):
        with self.engine.lock:
            if path=='/api/folder':
                name = str(body.get('name','')).strip()[:100]
                if not name:
                    raise ValueError('Enter a folder name')
                ident = body.get('id') or uid('f')
                parent = body.get('parent_id','')
                if parent and not self.store.get('folders',parent):
                    raise ValueError('Parent folder not found')
                chain = {ident}
                cursor = parent
                while cursor:
                    if cursor in chain:
                        raise ValueError('Folder hierarchy would contain a cycle')
                    chain.add(cursor)
                    cursor = self.store.get('folders',cursor,{}).get('parent_id','')
                return self.store.put('folders',dict(id=ident,name=name,parent_id=parent))
            if path=='/api/chat':
                # Promotes an existing ready Notion chat; Create Main (/api/main/create) makes a new one.
                from .harness_account import selected_account
                account_id = selected_account(self.store)
                worker = self.store.get('workers',body.get('worker_id'))
                if not worker or worker['state']!='ready':
                    raise ValueError('Choose a ready Notion chat')
                if worker.get('account_id') != account_id:
                    raise ValueError('Choose a chat from the harness account')
                if worker['purpose']=='main':
                    raise ValueError('This Notion thread already belongs to a main chat')
                if worker['purpose'] in ('external','operator'):
                    raise ValueError('This Notion chat was released to its user')
                project = str(body.get('project_path','')).strip()
                if project and not Path(project).is_dir():
                    raise ValueError('Project folder does not exist')
                folder_id = body.get('folder_id','')
                if folder_id and not self.store.get('folders',folder_id):
                    raise ValueError('Folder not found')
                worker.update(purpose='main',enabled=True)
                ident = uid('c')
                number = self.store.next_counter('main', lambda n: [('workers', dict(worker, main_number=n)),
                    ('chats', dict(id=ident, title=f'Main {n}', main_worker=worker['id'], folder_id=folder_id,
                        project_path=project, created=time.time(), main_number=n, state='active'))])
                return self.store.get('chats', ident)
            if path=='/api/chat/release':
                # The Notion thread belongs to the user (e.g. promoted by mistake): detach it for good.
                from .chat_release import release_chat
                return release_chat(self.store, str(body.get('id', '')))
            if path=='/api/operator/register':
                # The operator's own Notion AI chat becomes the Main that delegates; the harness never writes into it.
                from .operator_main import register_operator
                return register_operator(self.store, str(body.get('worker_id') or ''), str(body.get('project_path') or ''))
            if path=='/api/operator/plan':
                # Ask before delegating: does calling Coworkers save time for these parts (measured costs)?
                from .parallel_plan import delegation_decision, new_plan_id, record_plan, without_factor
                from .coworker_age_guard import young
                free = [w for w in self.store.all('workers') if w.get('purpose') == 'worker' and w.get('enabled')]
                # without_factor: est_factor is measured (GET /api/operator/plan_report), never applied from a request.
                decision = delegation_decision(body.get('parts'), len(young(self.store, free, time.time())), without_factor(body.get('stats')))
                # Rank 10: keep est_s per part (objects table, kind plan_part) to compare it with the real seconds later.
                plan_id = new_plan_id()
                try:
                    record_plan(self.store, plan_id, body.get('parts'), time.time())
                    decision = dict(decision, plan_id=plan_id)  # pass plan_id to /api/operator/delegate to link the tasks
                except Exception as exc:  # noqa: BLE001 - measuring must never break the answer
                    self.store.event('plan_record_failed', message=str(exc)[:200])
                return decision
            if path=='/api/operator/delegate':
                from .operator_main import delegate
                decision = None
                plan_id = str(body.get('plan_id') or '')[:60]
                if body.get('parts'):
                    # Small work is faster solo (bench: 30 s alone vs ~60 s fixed cost per coworker).
                    from .parallel_plan import delegation_decision, new_plan_id, record_plan, without_factor
                    from .coworker_age_guard import young
                    free = [w for w in self.store.all('workers') if w.get('purpose') == 'worker' and w.get('enabled')]
                    decision = delegation_decision(body.get('parts'), len(young(self.store, free, time.time())), without_factor(body.get('stats')))
                    if not decision['delegate'] and not body.get('force'):
                        raise ValueError('Do it yourself: ' + decision['reason'] + '. Nothing was delegated; send force=true to delegate anyway.')
                    if not plan_id:
                        # No earlier /api/operator/plan call: record the parts now so est_s is still measured.
                        plan_id = new_plan_id()
                        try:
                            record_plan(self.store, plan_id, body.get('parts'), time.time())
                        except Exception as exc:  # noqa: BLE001
                            self.store.event('plan_record_failed', message=str(exc)[:200])
                result = delegate(self.engine, str(body.get('chat_id') or ''), body.get('goal', ''), body.get('tasks'), str(body.get('request_id') or ''), summary=body.get('summary', ''), quality=str(body.get('quality') or ''), plan_id=plan_id)
                if decision is not None and isinstance(result, dict):
                    result['delegation'] = decision
                return result
            if path=='/api/chat/update':
                chat = self.store.get('chats',body['id'])
                if not chat:
                    raise ValueError('Chat not found')
                if body.get('main_worker'):
                    # Re-point a chat whose Main thread vanished to a ready Main worker.
                    target = self.store.get('workers', str(body['main_worker']))
                    if not target or target.get('state') != 'ready' or target.get('purpose') != 'main':
                        raise ValueError('Choose a ready Notion main chat')
                    if any(c['id'] != chat['id'] and c.get('main_worker') == target['id'] for c in self.store.all('chats')):
                        raise ValueError('This Notion thread already belongs to a main chat')
                    chat['main_worker'] = target['id']
                for key in ('title','folder_id','project_path','harness'):
                    if key in body:
                        chat[key] = str(body[key])[:500]
                if chat['project_path'] and not Path(chat['project_path']).is_dir():
                    raise ValueError('Project folder does not exist')
                if chat['folder_id'] and not self.store.get('folders',chat['folder_id']):
                    raise ValueError('Folder not found')
                return self.store.put('chats',chat)
            if path=='/api/chat/context_switch':
                # I50 Context switch button: real switch of one chat (worker_id or chat_id) into a fresh chat.
                return Application.chat_context_switch(self, body)
            if path=='/api/chat/send_check':
                # RBX fix: one age check (Notion thread created_time) for the composer sends of ANY chat.
                return Application.chat_send_check(self, body)
            if path=='/api/send':
                # I50 hard send block: a chat 23 h or older is refused (400) before the engine or Notion sees the text.
                Application.require_young_chat(self, body['chat_id'])
                return self.engine.submit(body['chat_id'],str(body.get('text','')),body.get('request_id',''))
            if path=='/api/step_guard/seed':
                # Fill the 48 h step counter from history (hour start -> steps); the larger value per hour wins.
                from . import step_guard
                total = step_guard.seed_h48(self.store, str(body['account']), dict(body.get('h48') or {}))
                return {'ok':True,'steps_48h':total}
            if path=='/api/run/cancel':
                self.engine.cancel(body['id'])
                return {'ok':True}
            if path=='/api/run/pause':
                self.engine.pause(body['id'],bool(body.get('paused',True)))
                return {'ok':True}
            if path=='/api/coordination/resume':
                return self.engine.resume_remote()
            if path=='/api/run/retry':
                self.engine.retry_blocked(body['id'],allow_resend=bool(body.get('allow_resend')))
                return {'ok':True}
            if path=='/api/task/remerge':
                # A merge rejected as red (or main_red) goes back to 'waiting' when its worktree still exists.
                if body.get('id'):
                    return dict(ok=True, **self.engine.remerge(task_id=str(body['id'])))
                if body.get('since') is not None:
                    return dict(ok=True, **self.engine.remerge(since=float(body['since'])))
                raise ValueError('Give a task id or since (unix time)')
            if path=='/api/task/reconcile':
                if body.get('resolution'):
                    # The Main decides an unconfirmed task after checking the work itself; nothing is resent (I5).
                    result = self.engine.resolve_uncertain(str(body['id']), str(body['resolution']), body.get('note', ''), stop=body.get('stop') is True)
                    return dict(ok=True, **result)
                task = self.store.get('tasks',body['id'])
                # Unconfirmed tasks (uncertain, or overdue and reserved) are checked again; nothing is resent.
                if not task or not (task.get('remote_unknown') or task.get('state') == 'uncertain'):
                    raise ValueError('This task does not need manual reconciliation')
                task.update(state='uncertain',remote_unknown=False,reconcile_at=0,reconcile_checks=0,started=time.time())
                self.store.put('tasks',task)
                return {'ok':True}
            if path=='/api/worker':
                worker = self.store.get('workers',body['id'])
                if not worker:
                    raise ValueError('Worker not found')
                if worker['purpose']=='main':
                    raise ValueError('Main threads are reserved for their chat')
                if worker['purpose'] in ('external','operator'):
                    raise ValueError('This Notion chat was released to its user')
                worker['enabled'] = bool(body.get('enabled'))
                self.store.put('workers',worker)
                return {'ok':True}
            if path=='/api/workers/enable':
                count = 0
                for worker in self.store.all('workers'):
                    if worker['purpose']=='worker' and worker['account_id']==body['account_id'] and worker['state']=='ready':
                        worker['enabled'] = bool(body.get('enabled',True))
                        self.store.put('workers',worker)
                        count += 1
                return {'count':count}
            if path=='/api/settings':
                settings = self.store.settings()
                from .team_size import team_size
                old_team = team_size(settings)
                for key,lo,hi in [('max_workers',1,40),('per_account',1,40),('max_attempts',1,5),
                                 ('task_timeout',60,3600),('max_fix_rounds',0,4),('context_token_limit',8000,10000000),
                                 ('context_turn_limit',5,2000),('context_handoff_chars',8000,40000),('max_runs_per_chat',1,6),('coworker_max_age_hours',1,72),
                                 # Measured Coworker switch rule (chat_context.switch_decision, desktop_settings.SWITCH_SETTINGS)
                                 ('switch_min_tokens',0,10000000),('switch_base_tokens',0,1000000),('switch_cost_s',0,3600),
                                 ('switch_slope_ms_per_100k',0,600000),('switch_error_permille_per_100k',0,1000),
                                 ('switch_retry_s',0,3600),('switch_remaining_tasks',1,50)]:
                    if key in body:
                        settings[key] = max(lo,min(hi,int(body[key])))
                if 'host_api_port' in body:
                    settings['host_api_port'] = max(0,min(65535,int(body['host_api_port'])))
                if 'host_api_allow_network' in body:
                    settings['host_api_allow_network'] = str(body['host_api_allow_network']).lower() in ('true','1','on','yes')
                if 'patch_mode' in body:  # Work in parallel copies (patch_mode.py); default on
                    settings['patch_mode'] = str(body['patch_mode']).lower() in ('true','1','on','yes')
                if 'host_api_bind' in body:
                    settings['host_api_bind'] = str(body['host_api_bind']).strip()[:64] or '127.0.0.1'
                if 'coworker_fresh_context' in body:
                    if body['coworker_fresh_context'] not in ('threshold','every_task'):
                        raise ValueError('Choose threshold or every_task')
                    settings['coworker_fresh_context'] = body['coworker_fresh_context']
                if 'coworker_switch_policy' in body:
                    if body['coworker_switch_policy'] not in ('threshold','measured'):
                        raise ValueError("coworker_switch_policy must be 'threshold' or 'measured'")
                    settings['coworker_switch_policy'] = body['coworker_switch_policy']
                if 'context_replace_mode' in body:
                    if body['context_replace_mode'] not in ('soft','full'):
                        raise ValueError('Choose soft or full')
                    settings['context_replace_mode'] = body['context_replace_mode']
                if 'ui_mode' in body:
                    if body['ui_mode'] not in ('classic','notion'):
                        raise ValueError("ui_mode must be 'classic' or 'notion'")
                    settings['ui_mode'] = body['ui_mode']
                if 'coworker_rotate_mode' in body:
                    if body['coworker_rotate_mode'] not in ('new_welcome','same_thread'):
                        raise ValueError("coworker_rotate_mode must be 'new_welcome' or 'same_thread'")
                    settings['coworker_rotate_mode'] = body['coworker_rotate_mode']
                if 'main_context_policy' in body:
                    if body['main_context_policy'] not in ('hint','auto','ask'):
                        raise ValueError("main_context_policy must be 'hint', 'auto' or 'ask'")
                    settings['main_context_policy'] = body['main_context_policy']
                if 'coworker_delete_old_chat' in body:
                    # False keeps a retired coworker chat (renamed) instead of deleting it in Notion.
                    settings['coworker_delete_old_chat'] = str(body['coworker_delete_old_chat']).lower() in ('true','1','on','yes')
                for key, low, high in (('main_context_ask_tokens', 50000, 10000000), ('main_context_ask_turns', 20, 5000)):
                    if key in body:
                        # When the operator Main reaches this size, it asks the user about a switch (main_switch.py).
                        settings[key] = max(low, min(high, int(body[key])))
                for key, low, high in (('token_budget_6h', 1000000, 500000000), ('burst_soft_15m', 100000, 100000000),
                                       ('burst_hard_15m', 100000, 200000000), ('burst_hard_60m', 100000, 500000000),
                                       ('burst_turns_5m', 5, 1000), ('burst_spacing_s', 1, 600)):
                    if key in body:
                        # Token budget (6 h) and burst guard (token_budget.py): slows coworkers on short peaks.
                        settings[key] = max(low, min(high, int(body[key])))
                if 'dynamic_roles' in body:
                    settings['dynamic_roles'] = str(body['dynamic_roles']).lower() in ('true','1','on','yes')
                if 'role_definitions' in body:
                    settings['role_definitions'] = validate_role_definitions(body['role_definitions'])  # rule I7: raises before anything is saved
                for key in ('public_mcp_url','theme'):
                    if key in body:
                        settings[key] = str(body[key])[:1000]
                from .local_mcp import SERVERS as LOCAL_SERVERS, SETTINGS as LOCAL_SETTINGS
                for key in ('harness_enabled', 'mcp_full_access', 'mcp_browser_access',
                            'automatic_setup', 'background_remote', 'bridge_auto',
                            'remote_mcp_enabled', 'advertise_project_tools',
                            'turn_watch', 'main_auto_continue', 'auto_continue_on_done', 'idle_mode', 'api_run_compact') + LOCAL_SETTINGS:
                    if key in body:
                        settings[key] = str(body[key]).lower() in ('true', '1', 'on', 'yes')
                if 'browser_path' in body:
                    settings['browser_path'] = str(body['browser_path']).strip()
                if 'notion_transport' in body:
                    if body['notion_transport'] not in ('edge', 'native'):
                        raise ValueError('Choose Edge or native API transport')
                    settings['notion_transport'] = body['notion_transport']
                self.store.put('settings',settings)
                off = [s['key'] for s in LOCAL_SERVERS if not settings.get(s['setting'])]
                threading.Thread(target=self.bridge.close_local, args=(off,), daemon=True, name='local-mcp-close').start()
                Application.grow_teams(self, old_team)  # a larger Coworker team size reconciles the managed teams (jobs)
                return {'ok':True}
        # Slow network calls run outside the coordinator lock.
        if path=='/api/mcp/refresh':
            return {'ok':True,'servers':[{k:v for k,v in e.items() if k!='tools'} for e in self.bridge.local_refresh()]}
        account_id = body.get('account_id','')
        if path=='/api/account/select':
            from .harness_account import select_account
            with self.engine.lock:
                result = select_account(self.store, account_id)
            # The chosen account's Main is loaded (created once if missing); nothing is deleted.
            self.job_once('Load Main chat and MCP', lambda: self.prepare_account(account_id), 'main:'+account_id)
            return result
        if path=='/api/app/restart':
            # Safe restart (restart_guard.py): 'idle' waits for running tasks, 'now' restarts right away.
            from . import restart_guard
            return restart_guard.handle(self,body)
        if path in ('/api/app/start','/api/app/stop','/api/app/status','/api/app/settings'):
            # NotionTool desktop app lifecycle (desktop_shell.DesktopController).
            from . import desktop_shell
            from . import restart_guard
            restart_guard.install_desk_actions()
            from . import problems
            problems.install_desk_actions()  # overlay action ui_error (Live view Problems)
            from . import templates
            templates.install_desk_actions()  # overlay actions request_templates, request_draft, history_search
            if getattr(self,'desktop',None) is None:
                self.desktop = desktop_shell.DesktopController(self)
            if path=='/api/app/start':
                return self.desktop.start()
            if path=='/api/app/stop':
                return self.desktop.stop(close_window=body.get('close_window', True) is not False)
            if path=='/api/app/settings':
                values = body.get('values') if isinstance(body.get('values'), dict) else {}
                return desktop_shell.save_app_settings(values) if values else desktop_shell.load_app_settings()
            return self.desktop.status()
        if path.startswith('/api/app/chat/'):
            # Own New chat dialog: create (Welcome chat + user's name), rename, promote.
            from . import own_chats
            return own_chats.handle_app_route(self, path, body)
        if path=='/api/main/refresh':
            # Settings 'Refresh Main': new Welcome Main first, the old Main is retired only after it exists.
            from .harness_account import selected_account
            from . import main_refresh
            return main_refresh.start(self, selected_account(self.store), body.get('request_id'))
        if path=='/api/main/create':
            from .harness_account import selected_account
            op, new = self.mains.request_create(selected_account(self.store), body.get('request_id'),
                str(body.get('folder_id',''))[:100], str(body.get('project_path','')).strip()[:500])
            if new or op['state'] not in ('done','failed'):
                self.job_once('Create '+op['title'], lambda: self.mains.run_create(op['id']), 'op:'+op['id'])
            return dict(self.mains.public(op), duplicate=not new)
        if path=='/api/chat/compress':
            if body.get('worker_id'):
                # A free coworker gets a fresh context before its next task (a new Welcome chat, or the same
                # chat when coworker_rotate_mode is same_thread). Nothing is sent to Notion by this request.
                with self.store.lock:
                    worker = self.store.get('workers', str(body['worker_id']))
                    if not worker or worker.get('purpose') != 'worker':
                        raise ValueError('Coworker not found')
                    if not worker.get('enabled') or worker.get('state') != 'ready':
                        raise ValueError('Only a free coworker can get a fresh context (state: %s)' % worker.get('state', ''))
                    usage = worker.get('context_usage') or {}
                    reason = (str(body.get('reason') or '').strip()[:300] or
                              'Fresh context requested at about %d tokens' % int(usage.get('estimated_tokens', 0) or 0))
                    worker.update(compress_requested=True, compress_reason=reason)
                    self.store.put('workers', worker)
                same = self.store.settings().get('coworker_rotate_mode', 'new_welcome') == 'same_thread'
                self.store.event('compress_requested', '', worker_id=worker['id'], message=reason)
                return dict(ok=True, worker_id=worker['id'], reason=reason, same_chat=same)
            chat = self.store.get('chats', body['chat_id'])
            if not chat:
                raise ValueError('Chat not found')
            with self.store.lock:
                worker = self.store.get('workers', chat['main_worker'])
                if not worker:
                    raise ValueError('This Main has no Notion chat')
                usage = worker.get('context_usage') or {}
                tokens, limit = int(usage.get('estimated_tokens', 0)), int(usage.get('token_limit', 0) or self.store.settings().get('context_token_limit', 1200000))
                reason = str(body.get('reason') or '').strip()[:300] or ('Context at about %d of %d tokens' % (tokens, limit))
                native = (worker.get('purpose') == 'main' and self.store.settings().get('main_context_policy', 'hint') != 'auto')
                if native:
                    # Fix Q: the operator Main is compacted by Notion itself. Never inject a checkpoint turn there;
                    # clear the request and drop a rotation whose task is no longer running.
                    pending = self.store.get('rotations', worker['id'])
                    task = self.store.get('tasks', (pending or {}).get('task_id', '')) or {}
                    stale = bool(pending) and task.get('state') not in ('running', 'cancelling', 'uncertain', 'checkpoint', 'preparing')
                    worker.update(compress_requested=False, compress_reason='', context_hint=None)
                    self.store.put('workers', worker)
                    if stale:
                        self.store.batch([], [('rotations', worker['id'])])
                else:
                    worker.update(compress_requested=True, compress_reason=reason)
                    self.store.put('workers', worker)
            if native:
                self.store.event('main_compress_native', '', worker_id=worker['id'], stale_rotation=stale,
                                 message='Operator Main is compacted by Notion; no checkpoint turn was sent. ' + reason)
                return dict(ok=True, chat_id=chat['id'], reason=reason, same_chat=True, native=True, stale_rotation_removed=stale)
            self.store.event('compress_requested', '', worker_id=worker['id'], message=reason)
            return dict(ok=True, chat_id=chat['id'], reason=reason, same_chat=True)
        if path=='/api/operation/resume':
            op = self.store.get('operations', body['id'])
            if not op:
                raise ValueError('Operation not found')
            self.job_once('Resume '+op['kind'].replace('_',' '), lambda: self.mains.resume(op['id']), 'op:'+op['id'])
            return self.mains.public(op)
        if path=='/api/host/key':
            if body.get('rotate'):
                self.host_api_key = secrets.token_urlsafe(40)
                self.adapter.sessions.save('host_api', {'key': self.host_api_key})
                self.store.event('host_api_key_rotated', message='Host API key rotated')
            return {'api_key': self.host_api_key, 'port': self.port}
        if path=='/api/mcp/add':
            server=self.mcp_servers.add(body)
            return self.job('Connect user MCP server to accounts',lambda:self.mcp_servers.connect(),'mcp_custom')
        if path=='/api/account/add-worker':
            return self.job('Add Coworker chat', lambda:self.add_worker(account_id), account_id or 'automatic')
        if path=='/api/account/open':
            from .harness_account import selected_account
            if account_id != selected_account(self.store):
                raise ValueError('Select this account for the harness first')
            return self.job('Open signed-in Notion in Edge', lambda:self.adapter.edge_transport(account_id).open_app(), account_id)
        if path=='/api/account/adopt-window':
            # Use the account the NotionTool window is signed in with, then the first-login setup.
            from .window_login import window_cookies
            return self.window_signed_in(window_cookies(getattr(getattr(self, 'desktop', None), 'port', 0) or 9333))
        if path=='/api/account/adopt-app-session':
            def adopt():
                from .harness_account import selected_account
                from .app_session import adopt_app_session
                target = account_id or selected_account(self.store)
                result = adopt_app_session(self.adapter, target)
                resumed = self.resume_after_sign_in(target)
                if resumed is not None:
                    result['coordination'] = resumed
                if target == selected_account(self.store):
                    result.update(self.prepare_account(target))
                result['first_setup'] = self.first_login_setup(target)
                return result
            return self.job('Use the NotionTool sign-in in Edge', adopt, account_id or 'login')
        if path=='/api/account/browser-login':
            def login():
                from .harness_account import selected_account
                result = self.adapter.browser_login()
                resumed = self.resume_after_sign_in(result.get('account_id', ''))
                if resumed is not None:
                    result['coordination'] = resumed
                if result.get('account_id') and result['account_id'] == selected_account(self.store):
                    result.update(self.prepare_account(result['account_id']))
                if result.get('account_id'):
                    result['first_setup'] = self.first_login_setup(result['account_id'])
                return result
            return self.job('Sign in to Notion in the browser window',login,'login')
        if path=='/api/account/remove':
            # B45: removing needs an explicit account_id; never falls back to the logged-in account.
            if not account_id:
                raise ValueError('Choose the account to remove (account_id)')
            ident=account_id
            chats={c['id'] for c in self.store.all('chats') if self.store.get('workers',c.get('main_worker'),{}).get('account_id')==ident}
            if any(r.get('chat_id') in chats and r.get('state') in ('queued','paused','approach','planning','executing') for r in self.store.all('runs')):
                raise ValueError('Work is still running or paused on this account. Stop it before removing the account.')
            result=self.adapter.remove_account(ident)
            self.store.event('account_removed',message='Account removed: '+result['email'])
            return result
        if path=='/api/account/reset-coworkers':
            # Fix Z: delete every unnamed Coworker chat (Welcome/Cowork titles), drop missing entries, create a fresh pool.
            # Named chats and the Main are never touched; a worker with an active task is skipped.
            from .harness_account import selected_account
            ident = account_id or selected_account(self.store)
            def reset():
                titles = ('Welcome to Notion', 'Cowork joined', 'Cowork (retired)')
                active = ('running', 'preparing', 'uncertain', 'checkpoint', 'cancelling')
                busy = {t.get('worker_id') for t in self.store.all('tasks') if t.get('state') in active or t.get('remote_unknown')}
                client = self.adapter.client(ident)
                deleted, dropped, skipped, failed = [], [], [], []
                for w in list(self.store.all('workers')):
                    if w.get('account_id') != ident or w.get('purpose', 'worker') != 'worker':
                        continue
                    if w.get('state') == 'missing':
                        if w.get('label') in titles or re.match(r'^Coworker \d+$', str(w.get('label') or '')):
                            self.store.delete('workers', w['id']); dropped.append(w['thread_id'])
                        continue
                    # 'Coworker N' = renamed pool chats (I41 naming); user-named chats stay untouched.
                    if w.get('label') not in titles and not re.match(r'^Coworker \d+$', str(w.get('label') or '')):
                        continue
                    if w['id'] in busy:
                        skipped.append(w['thread_id']); continue
                    try:
                        self.adapter.delete_thread(client, w['thread_id'])
                        self.store.delete('workers', w['id']); deleted.append(w['thread_id'])
                    except Exception as exc:
                        failed.append(w['thread_id'] + ': ' + str(exc)[:120])
                account = self.store.get('accounts', ident)
                ledger = account.get('team_threads') or {}
                from .team_size import ledger_roles
                for role in ledger_roles(ledger):
                    ledger.pop(role, None)
                account['team_threads'] = ledger
                self.store.put('accounts', account)
                self.store.event('coworkers_reset', message='Coworker chats deleted: %d, stale entries dropped: %d, skipped busy: %d, failed: %d'
                                 % (len(deleted), len(dropped), len(skipped), len(failed)))
                team = self.adapter.ensure_team(ident)
                return dict(deleted=deleted, dropped=dropped, skipped=skipped, failed=failed, team=team)
            return self.job('Reset Coworker chats', reset, ident)
        if path=='/api/main/switch/request':
            # Switch context button (owner 2026-10-25 23:06): the Main gets a request into its own chat and
            # does the switch itself (handoff + brief, /api/main/switch/start with brief_text, advance).
            # MainWake delivers it when the chat is idle; one immediate try here (switch_request.py).
            from . import switch_request
            result = switch_request.request(self.store, body)
            wake = getattr(self, 'main_wake', None)
            if wake is not None and not result.get('duplicate'):
                threading.Thread(target=wake.switch_request_tick, daemon=True, name='switch-request').start()
            return result
        if path=='/api/main/switch/start':
            # Ask-first Main switch (main_switch.py): a coworker writes the handoff brief for a fresh Welcome Main.
            from . import main_switch
            from .operator_main import delegate, switch_chat_id
            chat_id = switch_chat_id(self.store, body)
            request_id = str(body.get('request_id') or ('main-switch-' + time.strftime('%Y%m%d-%H%M%S')))
            return main_switch.start(self.store, chat_id,
                lambda b: delegate(self.engine, b['chat_id'], b['goal'], b['tasks'], b['request_id']), request_id,
                brief_text=body.get('brief_text'), trigger=str(body.get('trigger') or 'user'))
        if path=='/api/main/switch/rollback':
            # Undo a switched operator switch whose new Main broke: the old chat is the Main again.
            from .main_handoff_ops import rollback_switch
            return rollback_switch(self.store, self.adapter, str(body.get('id') or ''))
        if path=='/api/main/switch/advance':
            from . import main_switch
            from .main_handoff_ops import switch_ops
            record = self.store.get('main_switches', str(body.get('id') or ''))
            if not record:
                raise ValueError('Switch not found')
            return main_switch.advance(self.store, record, switch_ops(self.store, self.adapter, record))
        if path=='/api/chat/delete-retired':
            # Deletes retired coworker chats (worker retired_threads) after the safety checks in retired_chats.py.
            # Main, operator, named and user-renamed chats are never deleted; dry_run only reports.
            dry = str(body.get('dry_run', False)).lower() in ('true','1','on','yes')
            def delete_retired():
                from . import retired_chats  # imported here so server.py loads without the module
                return retired_chats.delete_retired(self.store, self.adapter, dry_run=dry)
            return self.job('Delete retired coworker chats' + (' (dry run)' if dry else ''), delete_retired)
        if path=='/api/main/rotate':
            # Phase 5b: move the Main into a fresh Welcome chat through the file handoff.
            # Runs as a job like reset-coworkers; a second request for the same account while
            # a rotation runs returns the running job and starts nothing new.
            from .harness_account import selected_account
            ident = account_id or selected_account(self.store)
            if not ident:
                raise ValueError('account_id is required')
            def rotate():
                from . import main_handoff_ops  # imported here so server.py loads without the module
                def emit(kind, message='', **fields):
                    fields.setdefault('account_id', ident)
                    self.store.event(kind, message=message, **fields)
                return main_handoff_ops.run_rotation(self.store, self.adapter, ident, emit)
            with Application.MAIN_ROTATE_LOCK:
                jobs = Application.MAIN_ROTATE_JOBS
                existing = self.store.get('jobs', jobs[ident], None) if ident in jobs else None
                if existing and existing.get('state') == 'running':
                    return {'job_id': existing['id'], 'already_running': True}
                started = self.job('Rotate Main into a fresh chat', rotate, ident)
                jobs[ident] = started['job_id']
                return started
        if path=='/api/account/team':
            return self.job('Prepare the Welcome Coworkers and one Main',lambda:self.adapter.ensure_team(account_id),account_id)
        if path=='/api/account/setup':
            return self.job('Complete account setup',lambda:self.adapter.onboard(account_id),account_id)
        if path=='/api/account/refresh':
            return self.job('Refresh account and chats',lambda:self.adapter.refresh(account_id),account_id)
        if path=='/api/account/provision':
            return self.job('Create worker chats',lambda:self.adapter.provision(account_id,body.get('count',1)),account_id)
        if path=='/api/bridge/connect':
            url = self.store.settings().get('public_mcp_url','')
            return self.job('Connect NotionTool tools',lambda:self.adapter.connect_bridge(account_id,url,self.bridge_token),account_id)
        if path=='/api/bridge/tunnel':
            settings=self.store.settings();settings['remote_mcp_enabled']=True;self.store.put('settings',settings)
            return self.job('Start remote MCP access',self.start_tunnel,'bridge')
        if path=='/api/project/pick':
            if not self.window:
                raise ValueError('Folder picker is available in the desktop app')
            import webview
            result = self.window.create_file_dialog(webview.FOLDER_DIALOG)
            return {'path':result[0] if result else ''}
        raise ValueError('Unknown action')

    def ensure_mcp_listener(self):
        if not self.mcp_http:
            try:self.mcp_http=ExclusiveHTTPServer(('127.0.0.1',int(self.store.settings().get('public_mcp_port',53768))),self.handler)
            except OSError:self.mcp_http=ExclusiveHTTPServer(('127.0.0.1',0),self.handler);_s=self.store.settings();_s['public_mcp_port']=self.mcp_http.server_port;self.store.put('settings',_s)  # port taken (e.g. another NotionTool): any free local port; Funnel targets it
            self.mcp_http.mcp_only=True
            self.mcp_http.daemon_threads=True
            threading.Thread(target=self.mcp_http.serve_forever,daemon=True,name='public-mcp').start()
        from .gateway import publish_core
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
        """Process 2: the stable MCP gateway Tailscale points to. It survives every restart of this app,
        holds MCP requests while the app is down and answers as backup MCP if the restart takes longer."""
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
        env={k:v for k,v in os.environ.items() if not k.startswith('_PYI') and k!='_MEIPASS2'}
        env['PYINSTALLER_RESET_ENVIRONMENT']='1'  # the gateway EXE unpacks its own copy, independent of this process
        options=dict(stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,close_fds=True,cwd=str(gw.runtime_dir(self.data_dir)),env=env)
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
        from .tailscale_bridge import TailscaleBridge
        self.ensure_mcp_listener()
        automatic = self.store.settings().get('automatic_setup', False)
        self.gateway_port=self.ensure_gateway()
        self.tailscale=self.tailscale or TailscaleBridge(self.store)
        result=self.tailscale.start(self.gateway_port or self.mcp_http.server_port)
        if not automatic:
            return dict(result, connected_accounts=0, errors=[])
        connected=0;errors=[]
        for account in self.store.all('accounts'):
            from .harness_account import selected_account
            if account['id'] != selected_account(self.store):
                continue
            if account['state']=='ready' and time.time()-account.get('pool_checked_at',0)>120 and any(
                    w['account_id']==account['id'] and w.get('join_state')=='pending' and w.get('state')!='missing'
                    for w in self.store.all('workers')):
                fresh=self.store.get('accounts',account['id']);fresh['pool_checked_at']=time.time();self.store.put('accounts',fresh)
                try:self.adapter.refresh(account['id'])  # renames Coworkers whose join is now confirmed
                except Exception:pass
                account=self.store.get('accounts',account['id'])
            if account['state']!='ready' or account.get('bridge_blocked') or account.get('bridge_next_at',0)>time.time():continue
            if account.get('bridge_connected') and account.get('bridge_url')==result['url']:
                if time.time()-account.get('bridge_checked_at',0)<60:  # I51: check every 60 s (was 300 s) for fast reconnect
                    connected+=1;continue
                try:alive=self.adapter.bridge_alive(account['id'],result['url'])
                except Exception:alive=True  # a failed check never tears down a working bridge
                fresh=self.store.get('accounts',account['id']);fresh['bridge_checked_at']=time.time();self.store.put('accounts',fresh)
                fresh=self.store.get('accounts',account['id'])
                if alive:
                    if fresh.get('bridge_misses'):
                        fresh.update(bridge_misses=0,bridge_missing_since=0);self.store.put('accounts',fresh)
                    connected+=1;continue
                # I51: reconnect on first confirmed miss (was: two misses). Only wait while Coworkers are actively busy (max 10 min).
                misses=fresh.get('bridge_misses',0)+1
                fresh.update(bridge_misses=misses,bridge_missing_since=fresh.get('bridge_missing_since') or time.time())
                self.store.put('accounts',fresh)
                busy=any(w['account_id']==account['id'] and w.get('state') in ('busy','recovering') for w in self.store.all('workers'))
                if busy and time.time()-fresh['bridge_missing_since']<600:
                    self.store.event('mcp_reconnect_deferred',message='NotionTool MCP missing; reconnect waits until the Coworkers are idle',slot=account.get('slot'))
                    connected+=1;continue
                self.store.event('mcp_missing',message='NotionTool MCP is no longer active in the account; reconnecting',slot=account.get('slot'))
            try:
                self.adapter.connect_bridge(account['id'],result['url'],self.bridge_token)
                connected+=1
                if account.get('bridge_url'):
                    self.store.event('mcp_reconnected',message='NotionTool MCP reconnected automatically',slot=account.get('slot'))
                fresh=self.store.get('accounts',account['id']);fresh.update(bridge_checked_at=time.time(),bridge_misses=0,bridge_missing_since=0);self.store.put('accounts',fresh)
            except Exception as exc:
                fresh=self.store.get('accounts',account['id'])
                fresh.update(bridge_connected=False,bridge_error=str(exc)[:250],
                             bridge_next_at=time.time()+300,
                             bridge_blocked=('HTTP 403' in str(exc) or 'HTTP 400' in str(exc)))
                self.store.put('accounts',fresh)
                errors.append({'slot':account['slot'],'error':str(exc)[:250]})
        return dict(result,connected_accounts=connected,errors=errors)


# ---- Usage by task/role/model and the compact run view (token savings) ----
def usage_response(store):
    """GET /api/usage: the existing report unchanged plus by_task, by_role and by_model from the token ledger."""
    from .backup import usage_report
    from . import token_budget
    report = usage_report(store)
    try:
        ledger = token_budget.usage_by_task(store)
    except Exception:
        ledger = {}
    report['by_task'] = ledger.get('by_task', {})
    report['by_role'] = ledger.get('by_role', {})
    report['by_model'] = ledger.get('by_model', {})
    try:
        from . import step_guard
        st = step_guard.public(store)
        lim = st.get('limits') or {}
        report['steps'] = dict(level=st.get('level'), reason=st.get('reason'), account=st.get('account'),
                               steps_15m=st.get('steps_15m', 0), steps_60m=st.get('steps_60m', 0),
                               steps_48h=st.get('steps_48h', 0), hard_15m=lim.get('step_hard_15m'),
                               hard_60m=lim.get('step_hard_60m'), soft_48h=lim.get('step_soft_48h'),
                               hard_48h=lim.get('step_hard_48h'))
    except Exception:
        report['steps'] = {}
    return report


COMPACT_TASK_KEYS = ('id', 'title', 'role', 'state', 'stage', 'review_state', 'patch_state', 'model', 'worker_id', 'summary')


def compact_run_response(engine, run):
    """GET /api/run?compact=1: per task only the listed keys; no prompts, results, proofs or events."""
    from .operator_main import plain_task_summary
    from .operator_roles import stage_of
    tasks = []
    for t in engine.tasks(run['id']):
        model = t.get('model')
        if isinstance(model, dict):
            model = model.get('name') or model.get('id')
        try:
            stage = stage_of(t)
        except Exception:
            stage = ''
        tasks.append(dict(id=t.get('id', ''), title=t.get('title', ''), role=t.get('role', ''), state=t.get('state', ''),
                          stage=stage, review_state=t.get('review_state', ''), patch_state=t.get('patch_state', ''),
                          model=str(model or ''), worker_id=t.get('worker_id', ''),
                          summary=' '.join(str(plain_task_summary(t) or '').split())[:200]))
    return {'run': {k: run.get(k) for k in ('id', 'state', 'title', 'stage') if k in run}, 'tasks': tasks}


def archived_title(label):
    """Notion title of a Main's old chat after a manual context switch (I50), e.g. 'Main 3 (expired)'."""
    return (str(label or 'Main').strip()[:80] or 'Main') + ' (expired)'


class ManualSwitchAdapter:
    """Adapter view for POST /api/chat/context_switch (I50: rename instead of delete).

    ChatContext.retire_old deletes the old chat when the adapter has delete_thread; a manual switch must
    keep it (it is listed in the sidebar archive via retired_threads), so delete_thread is hidden here and
    retire_old falls back to rename_thread. retired_title (Mains) replaces the Coworker retire title.
    Every other attribute is the real adapter's."""
    delete_thread = None

    def __init__(self, adapter, retired_title=None):
        self._adapter = adapter
        self._retired_title = retired_title

    def __getattr__(self, name):
        if name.startswith('__') or name in ('_adapter', '_retired_title'):
            raise AttributeError(name)
        return getattr(self._adapter, name)

    def rename_thread(self, client, tid, title):
        return self._adapter.rename_thread(client, tid, self._retired_title or title)


def compact_enabled(store):
    """Setting api_run_compact (default on). 'false', '0', 'off', 'no' and '' are off, like token_ledger_enabled."""
    try:
        value = (store.settings() or {}).get('api_run_compact', True)
    except Exception:
        return True
    if isinstance(value, str):
        return value.strip().lower() not in ('0', 'false', 'off', 'no', '')
    return bool(value) if value is not None else True
