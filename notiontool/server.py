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
from .engine import Engine, ACTIVE
from .notion import NotionAdapter
from .store import Store, uid
from .setup_status import account_setup


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
        from .mains import Mains, migrate
        migrate(self.store)
        self.mains = Mains(self)
        self.bridge = Bridge(self.store,self.engine)
        self.token = secrets.token_urlsafe(32)
        secret = self.adapter.sessions.load('bridge') or {}
        self.bridge_token = secret.get('token') or secrets.token_urlsafe(40)
        self.adapter.sessions.save('bridge',{'token':self.bridge_token})
        host = self.adapter.sessions.load('host_api') or {}
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
                        if method=='GET' and path=='/api/chats':
                            return self.response(200,{'chats':app.chat_list()})
                        if method=='GET' and path=='/api/run':
                            run = app.store.get('runs',query.get('id',[''])[0])
                            if not run:
                                return self.response(404,{'error':'Run not found'})
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
                        if method=='GET' and path=='/api/report':
                            task = app.store.get('tasks',query.get('id',[''])[0])
                            return self.response(200,{'task':app.safe_task(task),'result':task['result'],'partial':task.get('raw_response') or task.get('partial','')})
                        if method=='POST' and path=='/api/run/confirm':
                            return self.response(*app.confirm_run_request(self.read_body()))
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
        try:self.ensure_mcp_listener()  # MCP is reachable right after a restart, before the first maintenance run
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
        threading.Thread(target=self.sweep_main_archives,daemon=True,name='main-archive-sweep').start()
        self.updater = Updater(self)
        self.updater.start()
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
                active_run=next((r['id'] for r in reversed(runs) if r['state'] not in ('done', 'failed', 'blocked', 'cancelled')), ''),
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
                    if run.get('state') in ('done', 'failed', 'blocked', 'cancelled'):
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

    def close(self):
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
            if self.store.settings().get('bridge_auto') or self.store.settings().get('remote_mcp_enabled',True):
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
        selected = selected_account(self.store)
        canonical = main_chat(self.store, selected) if selected else None
        accounts = [{k:v for k,v in a.items() if k!='identity'} for a in self.store.all('accounts')]
        workers = [{k:v for k,v in w.items() if k not in ('lease','handoff')} for w in self.store.all('workers') if w.get('account_id') == selected]
        for account in accounts:
            account['setup']=account_setup(account,workers)
        ready_accounts={a['id'] for a in accounts if a.get('state')=='ready'}
        runs = [{k:v for k,v in r.items() if k!='context'} for r in self.store.all('runs')]
        return dict(name='NotionTool',mcp_servers=self.mcp_servers.public(),accounts=accounts,workers=workers,folders=self.store.all('folders'),
            chats=self.chat_list(),operations=[self.mains.public(o) for o in self.store.all('operations')][-30:],host_api=dict(port=self.port,bind=getattr(self,'bind','127.0.0.1')),limits=self.limits(),runs=runs,tasks=[self.safe_task(t) for t in self.store.all('tasks')],
            awaiting_main=[r['id'] for r in runs if r.get('state') == 'awaiting_main'],
            harness_account_id=selected,settings=self.store.settings(),jobs=self.store.all('jobs')[-30:],events=self.store.events(),
            bridge=dict(local_url=f'http://127.0.0.1:{self.port}/mcp',public_url=self.store.settings().get('public_mcp_url','')),
            stats=dict(running=sum(w['state']=='busy' for w in workers),ready=sum(w.get('enabled') and w['state']=='ready' and w['purpose']=='worker' and w.get('account_id') in ready_accounts for w in workers),
                       recovering=sum(w['state']=='recovering' for w in workers)))

    MAIN_ROTATE_LOCK = threading.Lock()
    MAIN_ROTATE_JOBS = {}  # account_id -> job id of the last /api/main/rotate job

    def sweep_main_archives(self):
        """Phase 5b: every 60 s let main_handoff_ops delete archived Main chats past DELETE_DELAY. Never raises."""
        last = ''
        while True:
            time.sleep(60)
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
            if path=='/api/send':
                return self.engine.submit(body['chat_id'],str(body.get('text','')),body.get('request_id',''))
            if path=='/api/run/cancel':
                self.engine.cancel(body['id'])
                return {'ok':True}
            if path=='/api/run/pause':
                self.engine.pause(body['id'],bool(body.get('paused',True)))
                return {'ok':True}
            if path=='/api/coordination/resume':
                return self.engine.resume_remote()
            if path=='/api/run/retry':
                self.engine.retry_blocked(body['id'])
                return {'ok':True}
            if path=='/api/task/reconcile':
                task = self.store.get('tasks',body['id'])
                if not task or not task.get('remote_unknown'):
                    raise ValueError('This task does not need manual reconciliation')
                task.update(state='uncertain',remote_unknown=False,reconcile_at=0,started=time.time())
                self.store.put('tasks',task)
                return {'ok':True}
            if path=='/api/worker':
                worker = self.store.get('workers',body['id'])
                if not worker:
                    raise ValueError('Worker not found')
                if worker['purpose']=='main':
                    raise ValueError('Main threads are reserved for their chat')
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
                for key,lo,hi in [('max_workers',1,40),('per_account',1,40),('max_attempts',1,5),
                                 ('task_timeout',60,3600),('max_fix_rounds',0,4),('context_token_limit',8000,2000000),
                                 ('context_turn_limit',5,2000),('context_handoff_chars',8000,40000),('max_runs_per_chat',1,6)]:
                    if key in body:
                        settings[key] = max(lo,min(hi,int(body[key])))
                if 'host_api_port' in body:
                    settings['host_api_port'] = max(0,min(65535,int(body['host_api_port'])))
                if 'host_api_allow_network' in body:
                    settings['host_api_allow_network'] = str(body['host_api_allow_network']).lower() in ('true','1','on','yes')
                if 'host_api_bind' in body:
                    settings['host_api_bind'] = str(body['host_api_bind']).strip()[:64] or '127.0.0.1'
                if 'coworker_fresh_context' in body:
                    if body['coworker_fresh_context'] not in ('threshold','every_task'):
                        raise ValueError('Choose threshold or every_task')
                    settings['coworker_fresh_context'] = body['coworker_fresh_context']
                if 'context_replace_mode' in body:
                    if body['context_replace_mode'] not in ('soft','full'):
                        raise ValueError('Choose soft or full')
                    settings['context_replace_mode'] = body['context_replace_mode']
                for key in ('public_mcp_url','theme'):
                    if key in body:
                        settings[key] = str(body[key])[:1000]
                from .local_mcp import SERVERS as LOCAL_SERVERS, SETTINGS as LOCAL_SETTINGS
                for key in ('harness_enabled', 'mcp_full_access', 'mcp_browser_access') + LOCAL_SETTINGS:
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
        if path=='/api/main/create':
            from .harness_account import selected_account
            op, new = self.mains.request_create(selected_account(self.store), body.get('request_id'),
                str(body.get('folder_id',''))[:100], str(body.get('project_path','')).strip()[:500])
            if new or op['state'] not in ('done','failed'):
                self.job_once('Create '+op['title'], lambda: self.mains.run_create(op['id']), 'op:'+op['id'])
            return dict(self.mains.public(op), duplicate=not new)
        if path=='/api/chat/compress':
            chat = self.store.get('chats', body['chat_id'])
            if not chat:
                raise ValueError('Chat not found')
            with self.store.lock:
                worker = self.store.get('workers', chat['main_worker'])
                if not worker:
                    raise ValueError('This Main has no Notion chat')
                usage = worker.get('context_usage') or {}
                tokens, limit = int(usage.get('estimated_tokens', 0)), int(usage.get('token_limit', 0) or self.store.settings().get('context_token_limit', 60000))
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
        if path=='/api/account/browser-login':
            def login():
                from .harness_account import selected_account
                result = self.adapter.browser_login()
                resumed = self.resume_after_sign_in(result.get('account_id', ''))
                if resumed is not None:
                    result['coordination'] = resumed
                if result.get('account_id') and result['account_id'] == selected_account(self.store):
                    result.update(self.prepare_account(result['account_id']))
                return result
            return self.job('Sign in to Notion in the browser window',login,'login')
        if path=='/api/account/remove':
            from .harness_account import selected_account
            ident=account_id or selected_account(self.store)
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
                        if w.get('label') in titles:
                            self.store.delete('workers', w['id']); dropped.append(w['thread_id'])
                        continue
                    if w.get('label') not in titles:
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
                for role in ('worker1', 'worker2', 'worker3', 'worker4', 'worker5'):
                    ledger.pop(role, None)
                account['team_threads'] = ledger
                self.store.put('accounts', account)
                self.store.event('coworkers_reset', message='Coworker chats deleted: %d, stale entries dropped: %d, skipped busy: %d, failed: %d'
                                 % (len(deleted), len(dropped), len(skipped), len(failed)))
                team = self.adapter.ensure_team(ident)
                return dict(deleted=deleted, dropped=dropped, skipped=skipped, failed=failed, team=team)
            return self.job('Reset Coworker chats', reset, ident)
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
            return self.job('Prepare five Welcome workers and one main',lambda:self.adapter.ensure_team(account_id),account_id)
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
            return self.job('Start Tailscale MCP and connect accounts',self.start_tunnel,'bridge')
        if path=='/api/project/pick':
            if not self.window:
                raise ValueError('Folder picker is available in the desktop app')
            import webview
            result = self.window.create_file_dialog(webview.FOLDER_DIALOG)
            return {'path':result[0] if result else ''}
        raise ValueError('Unknown action')

    def ensure_mcp_listener(self):
        if not self.mcp_http:
            try:self.mcp_http=ExclusiveHTTPServer(('127.0.0.1',int(self.store.settings().get('public_mcp_port',8873))),self.handler)
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
        self.gateway_port=self.ensure_gateway()
        self.tailscale=self.tailscale or TailscaleBridge(self.store)
        result=self.tailscale.start(self.gateway_port or self.mcp_http.server_port)
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
                if time.time()-account.get('bridge_checked_at',0)<300:
                    connected+=1;continue
                try:alive=self.adapter.bridge_alive(account['id'],result['url'])
                except Exception:alive=True  # a failed check never tears down a working bridge
                fresh=self.store.get('accounts',account['id']);fresh['bridge_checked_at']=time.time();self.store.put('accounts',fresh)
                fresh=self.store.get('accounts',account['id'])
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
