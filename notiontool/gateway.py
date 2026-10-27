"""Process 2: stable MCP gateway (and backup MCP) in front of the NotionTool core.

Tailscale Funnel targets this fixed local port instead of the core. The core can restart behind it
(updates, crashes) without Notion ever losing the MCP: requests that cannot reach the core are held
and delivered once the new core is up. A request that already reached the core is never resent (I5).
While the core stays down, the gateway answers initialize/tools/list/ping itself from its cache so the
Notion connection stays valid, and tool calls get a clear 'restarting' result instead of a dead endpoint.

v3 (25.10.2026, MCP 'Failed to connect' while nothing crashed): the core can also be UP but busy or
blocked (a long action holding the engine lock). v2 then waited up to 900 s and Notion's MCP client gave
up first. Now safe methods answer from the gateway after SAFE_SECONDS, tool calls get a 'busy, not
retried' result after CALL_SECONDS, the cache and accepted logins survive a gateway restart
(runtime/gateway_cache.json) and every request is logged to runtime/gateway.log.

v4 (user 26.10.2026: 'when NotionTool fails, the NotionTool MCP must not break with it'): a DOWN core
no longer empties tools/list. NotionTool's own cached tools (nt_*) stay advertised so Notion keeps the
MCP and its tools; proxied tools (roblox_*, blender_*, mcp_* ...) stay hidden while down so their own MCP
servers take over. Every call still gets a clear 'NOT executed' result until the app is back.

v5 (user 27.10.2026: 'the MCP must be a COMPLETELY separate application, it must work when NotionTool
does not'): NotionTool's own PC tools (nt_shell, nt_read_file, nt_write_file, nt_edit_file, nt_list_dir,
nt_search_code, nt_file_outline, nt_find_symbol, nt_memory_read, nt_memory_update) now RUN INSIDE the
gateway whenever the app does not answer a ping within PROBE_SECONDS. Only the owner's Host API bearer
(decrypted from data/sessions with DPAPI, same Windows user) may use them. Calls that already reached the
app are never resent, except read-only tools (no side effects). Long local calls hand off to a gateway
job (job_id 'gw-...') fetched with nt_job_result, like the app's own jobs.
"""
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = 5
LOCAL_TOOLS = {'nt_shell', 'nt_read_file', 'nt_write_file', 'nt_edit_file', 'nt_list_dir', 'nt_search_code',
               'nt_file_outline', 'nt_find_symbol', 'nt_memory_read', 'nt_memory_update'}
READ_ONLY_LOCAL = {'nt_read_file', 'nt_list_dir', 'nt_search_code', 'nt_file_outline', 'nt_find_symbol', 'nt_memory_read'}
DATA_GUARDED = {'nt_list_dir': 'Listing', 'nt_search_code': 'Search', 'nt_file_outline': 'Outline',
                'nt_find_symbol': 'Symbol search', 'nt_write_file': 'Write', 'nt_edit_file': 'Edit'}
JOB_TOOL = 'nt_job_result'
LOCAL_JOB_PREFIX = 'gw-'
PROBE_SECONDS = 3      # the app must answer a ping this fast, else the PC tools run in the gateway
LOCAL_SYNC_SECONDS = 45  # a local call longer than this becomes a gateway job (same as the app's jobs)
JOB_WAIT_MAX = 50
JOB_KEEP_SECONDS = 3600
DEFAULT_PORT = 8792
HOLD_SECONDS = 25  # Notion's MCP client gives up before 120 s -> 'Failed to connect'; answer fast instead
SAFE_SECONDS = 8  # initialize/ping/tools/list: answer from the gateway when the core is slower than this
CALL_SECONDS = 90  # tools/call: longest legit call is ~55 s (job tools hand off after 45-50 s)
CONNECT_SECONDS = 5
LOG_MAX_BYTES = 5 * 1024 * 1024
SAFE_METHODS = {'initialize', 'notifications/initialized', 'ping', 'tools/list', 'resources/list', 'prompts/list'}
CACHED_METHODS = ('initialize', 'tools/list')
OWN_TOOL_PREFIX = 'nt_'  # v4: NotionTool's own tools stay listed from the cache while the core is down
FORWARD_HEADERS = ('authorization', 'content-type', 'accept', 'mcp-session-id', 'mcp-protocol-version', 'last-event-id')
RETURN_HEADERS = ('content-type', 'mcp-session-id', 'cache-control')


def runtime_dir(data_dir):
    path = Path(data_dir) / 'runtime'
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path):
    try:
        return json.loads(Path(path).read_text('utf-8'))
    except (OSError, ValueError):
        return {}


def write_json(path, data):
    path = Path(path)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data), 'utf-8')
    os.replace(tmp, path)


def publish_core(data_dir, mcp_port):
    """Called by the core: tells the gateway where the live MCP listener is."""
    write_json(runtime_dir(data_dir) / 'core.json', {'mcp_port': int(mcp_port), 'pid': os.getpid(), 'at': time.time()})


class CoreDown(Exception):
    def __init__(self, sent):
        super().__init__('core unavailable')
        self.sent = sent  # True: the request may already have reached the core


class CoreBusy(Exception):
    """The core took the request but did not answer in time (busy or blocked). It may still run it."""


class LocalTools:
    """v5: NotionTool's PC tools, run inside the gateway process itself (independent of the app)."""

    def __init__(self, data_dir):
        self.data_dir = Path(data_dir).resolve()
        self.jobs = {}
        self.lock = threading.Lock()
        self._key, self._key_mtime, self._dev = '', None, None

    def dev(self):
        if self._dev is None:
            from . import devtools
            root = self.data_dir.parent
            if (root / 'notiontool').is_dir():  # the gateway EXE lives in data/runtime: point at the app folder
                devtools.ROOT = root
                devtools.MEMORY = root / 'project-memory'
            self._dev = devtools
        return self._dev

    def host_key(self):
        path = self.data_dir / 'sessions' / 'session_host_api.bin'
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return ''
        if mtime != self._key_mtime:
            try:
                from .vendor.sessions import SessionStore
                value = SessionStore(data_dir=str(path.parent)).load_identity('host_api') or {}
                self._key = str(value.get('key') or '')
            except Exception:  # noqa: BLE001 - no key: local tools stay off, calls go to the app
                self._key = ''
            self._key_mtime = mtime
        return self._key

    def owner(self, authorization):
        key = self.host_key()
        return bool(key) and hmac.compare_digest(str(authorization or ''), 'Bearer ' + key)

    @staticmethod
    def handles(name, args):
        if isinstance(args, dict) and args.get('assignment_token'):
            return False  # coworker task writes go to their own worktree: only the app knows it
        if name in LOCAL_TOOLS:
            return True
        return name == JOB_TOOL and str((args or {}).get('job_id') or '').startswith(LOCAL_JOB_PREFIX)

    def in_data(self, path):
        try:
            Path(path).resolve().relative_to(self.data_dir)
            return True
        except (ValueError, OSError):
            return False

    def run(self, name, args):
        dev = self.dev()
        args = dict(args or {})
        if name in DATA_GUARDED and self.in_data(dev._path(args.get('path') or '.')):
            raise ValueError('%s inside the NotionTool data folder is refused: %s' % (DATA_GUARDED[name], args.get('path')))
        result = dev.call(name, args)
        if not isinstance(result, dict):
            return result
        if name == 'nt_list_dir':
            base = dev._path(args.get('path') or '.')
            result = dict(result, entries=[e for e in result.get('entries', []) if not self.in_data(base / str(e).rstrip('/'))])
        elif name == 'nt_search_code':
            kept = []
            for hit in result.get('results', []):
                match = _HIT_PATH.match(str(hit))
                if not match or not self.in_data(dev._path(match.group(1))):
                    kept.append(hit)
            result = dict(result, results=kept)
        return result

    @staticmethod
    def ok(value):
        return {'content': [{'type': 'text', 'text': json.dumps(value, ensure_ascii=False)}]}

    @staticmethod
    def err(text):
        return {'content': [{'type': 'text', 'text': text}], 'isError': True}

    def call(self, name, args):
        """Run one PC tool here; longer than LOCAL_SYNC_SECONDS -> gateway job."""
        if name == JOB_TOOL:
            return self.job_result(args or {})
        job = {'id': LOCAL_JOB_PREFIX + uuid.uuid4().hex[:16], 'tool': name, 'done': threading.Event(),
               'result': None, 'started': time.time()}

        def work():
            try:
                job['result'] = self.ok(self.run(name, args))
            except Exception as exc:  # noqa: BLE001 - a tool error is a tool result, never a dead request
                job['result'] = self.err('%s: %s' % (type(exc).__name__, exc))
            finally:
                job['done'].set()

        threading.Thread(target=work, daemon=True, name='gateway-' + name).start()
        if job['done'].wait(LOCAL_SYNC_SECONDS):
            return job['result']
        with self.lock:
            now = time.time()
            for ident in [i for i, j in self.jobs.items() if now - j['started'] > JOB_KEEP_SECONDS]:
                self.jobs.pop(ident, None)
            self.jobs[job['id']] = job
        return self.ok({'still_running': True, 'job_id': job['id'], 'tool': name, 'elapsed_s': LOCAL_SYNC_SECONDS,
                        'hint': 'Still running on the PC (in the MCP gateway). Do not start it again. Call '
                                'nt_job_result with this job_id to get the result.'})

    def job_result(self, args):
        ident = str(args.get('job_id') or '')
        with self.lock:
            job = self.jobs.get(ident)
        if not job:
            return self.err('Unknown or expired job_id: ' + ident)
        try:
            wait = float(args.get('wait_s') or JOB_WAIT_MAX)
        except (TypeError, ValueError):
            wait = JOB_WAIT_MAX
        if not job['done'].wait(max(1.0, min(wait, JOB_WAIT_MAX))):
            return self.ok({'still_running': True, 'job_id': ident, 'tool': job['tool'],
                            'elapsed_s': round(time.time() - job['started'], 1),
                            'hint': 'Still running on the PC. Call nt_job_result again.'})
        with self.lock:
            self.jobs.pop(ident, None)
        return job['result']


class Gateway:
    def __init__(self, data_dir, port=DEFAULT_PORT, hold=HOLD_SECONDS, poll=0.5,
                 safe_timeout=SAFE_SECONDS, call_timeout=CALL_SECONDS):
        self.data_dir, self.port, self.hold, self.poll = str(data_dir), int(port), hold, poll
        self.safe_timeout, self.call_timeout = safe_timeout, call_timeout
        self.control = secrets.token_urlsafe(24)
        self.cache = {}          # rpc method -> (headers, body)
        self.accepted = set()    # sha256 of Authorization values the core accepted
        self.lock = threading.Lock()
        self.log_lock = threading.Lock()
        self.stopping = threading.Event()
        self.server = None
        self.local = LocalTools(data_dir)
        self.load_cache()

    # --- persistence and log -------------------------------------------------------------------
    def cache_path(self):
        return runtime_dir(self.data_dir) / 'gateway_cache.json'

    def log_path(self):
        return runtime_dir(self.data_dir) / 'gateway.log'

    def load_cache(self):
        data = read_json(self.cache_path())
        for name, entry in (data.get('cache') or {}).items():
            if name not in CACHED_METHODS or not isinstance(entry, dict):
                continue
            try:
                self.cache[name] = ([tuple(h) for h in entry.get('headers') or []], entry['body'].encode('utf-8'))
            except (KeyError, AttributeError, TypeError, ValueError):
                pass
        self.accepted.update(a for a in data.get('accepted') or [] if isinstance(a, str))

    def save_cache(self):
        """Caller holds self.lock."""
        try:
            write_json(self.cache_path(), {
                'cache': {name: {'headers': [list(h) for h in kept], 'body': body.decode('utf-8', 'replace')}
                          for name, (kept, body) in self.cache.items()},
                'accepted': list(self.accepted)[:50], 'saved': time.time()})
        except OSError:
            pass

    def log(self, started, calls, status, outcome):
        call = calls[0] if calls else {}
        params = call.get('params') if isinstance(call.get('params'), dict) else {}
        line = json.dumps({'at': round(started, 3), 'time': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(started)),
                           'method': call.get('method') or '', 'tool': params.get('name') or '',
                           'ms': int((time.time() - started) * 1000), 'status': status, 'outcome': outcome,
                           'core_port': self.target()})
        with self.log_lock:
            try:
                path = self.log_path()
                if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
                    os.replace(path, path.with_name(path.name + '.1'))
                with open(path, 'a', encoding='utf-8') as handle:
                    handle.write(line + '\n')
            except OSError:
                pass

    # --- forwarding ----------------------------------------------------------------------------
    def target(self):
        return int(read_json(runtime_dir(self.data_dir) / 'core.json').get('mcp_port') or 0)

    def forward(self, method, path, headers, body, timeout=None):
        port = self.target()
        if not port:
            raise CoreDown(False)
        timeout = self.call_timeout if timeout is None else timeout
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=min(CONNECT_SECONDS, timeout))
        try:
            try:
                conn.connect()
            except OSError:
                raise CoreDown(False) from None
            conn.sock.settimeout(timeout)
            try:
                conn.request(method, path, body=body, headers=headers)
                response = conn.getresponse()
                data = response.read()
            except (socket.timeout, TimeoutError):
                raise CoreBusy() from None
            except (OSError, http.client.HTTPException):
                raise CoreDown(True) from None
            kept = [(k, v) for k, v in response.getheaders() if k.lower() in RETURN_HEADERS]
            return response.status, kept, data
        finally:
            conn.close()

    @staticmethod
    def rpc_calls(body):
        try:
            parsed = json.loads(body or b'null')
        except ValueError:
            return None, []
        calls = parsed if isinstance(parsed, list) else [parsed]
        return parsed, [c for c in calls if isinstance(c, dict)]

    @staticmethod
    def reply(ident, result=None, error=None):
        data = {'jsonrpc': '2.0', 'id': ident}
        data.update({'error': error} if error else {'result': result})
        return 200, [('Content-Type', 'application/json')], json.dumps(data).encode()

    def handle(self, method, path, headers, body):
        started = time.time()
        parsed, calls = self.rpc_calls(body)
        names = {c.get('method') for c in calls}
        rpc_safe = bool(calls) and names <= SAFE_METHODS
        safe = method in ('GET', 'DELETE') or rpc_safe
        auth = hashlib.sha256((headers.get('authorization') or '').encode()).hexdigest()
        sent = {k: v for k, v in headers.items() if k in FORWARD_HEADERS}
        timeout = self.safe_timeout if rpc_safe else self.call_timeout
        deadline = started + (min(self.hold, self.safe_timeout) if rpc_safe else self.hold)
        local = self.local_target(method, parsed, headers)
        if local is not None:
            with self.lock:
                self.accepted.add(auth)  # the owner's own Host API key: always accepted, app up or not
            if local[0] == JOB_TOOL or not self.core_answers(path, sent):
                result = self.reply(parsed.get('id'), self.local.call(*local))
                self.log(started, calls, result[0], 'local')
                return result
        while True:
            try:
                status, kept, data = self.forward(method, path, sent, body, timeout)
            except CoreBusy:
                if local is not None and local[0] in READ_ONLY_LOCAL:
                    result, outcome = self.reply(parsed.get('id'), self.local.call(*local)), 'busy_local'
                elif rpc_safe:
                    result, outcome = self.backup(parsed, calls, auth, busy=True), 'busy_answered'
                else:
                    result, outcome = self.busy(calls), 'busy'
                break
            except CoreDown as down:
                if local is not None and (not down.sent or local[0] in READ_ONLY_LOCAL):
                    result, outcome = self.reply(parsed.get('id'), self.local.call(*local)), 'down_local'
                    break
                if down.sent and not safe:
                    result, outcome = self.interrupted(calls), 'interrupted'
                    break
                if time.time() >= deadline or self.stopping.is_set():
                    result, outcome = self.backup(parsed, calls, auth), 'core_down_answered'
                    break
                time.sleep(self.poll)
                continue
            if status == 200 and headers.get('authorization'):
                with self.lock:
                    changed = auth not in self.accepted
                    self.accepted.add(auth)
                    if isinstance(parsed, dict) and parsed.get('method') in CACHED_METHODS:
                        changed = changed or self.cache.get(parsed['method']) != (kept, data)
                        self.cache[parsed['method']] = (kept, data)
                    if changed:
                        self.save_cache()
            result, outcome = (status, kept, data), 'forwarded'
            break
        self.log(started, calls, result[0], outcome)
        return result

    def local_target(self, method, parsed, headers):
        """(name, args) when this request is one owner tools/call the gateway can run itself, else None."""
        if method != 'POST' or not isinstance(parsed, dict) or parsed.get('method') != 'tools/call' or 'id' not in parsed:
            return None
        params = parsed.get('params') if isinstance(parsed.get('params'), dict) else {}
        name = str(params.get('name') or '')
        args = params.get('arguments') if isinstance(params.get('arguments'), dict) else {}
        if not self.local.handles(name, args) or not self.local.owner(headers.get('authorization')):
            return None
        return name, args

    def core_answers(self, path, sent):
        """A ping runs nothing, so asking first never causes a double execution."""
        probe = json.dumps({'jsonrpc': '2.0', 'id': 'gateway-probe', 'method': 'ping'}).encode()
        headers = dict(sent)
        headers['content-type'] = 'application/json'
        try:
            status, _, _ = self.forward('POST', path, headers, probe, PROBE_SECONDS)
        except (CoreDown, CoreBusy):
            return False
        return status == 200

    def interrupted(self, calls):
        call = calls[0] if calls else {}
        text = ('NotionTool restarted while this tool call was running. It was NOT retried automatically; '
                'check whether it took effect before calling it again.')
        if call.get('method') == 'tools/call':
            return self.reply(call.get('id'), {'content': [{'type': 'text', 'text': text}], 'isError': True})
        return self.reply(call.get('id'), error={'code': -32000, 'message': text})

    def busy(self, calls):
        """The core is up but did not answer a non-safe call in time: never resent, may still be running."""
        call = calls[0] if calls else {}
        if 'id' not in call:
            return 202, [('Content-Type', 'application/json')], b'{}'
        text = ('NotionTool is busy: the app did not answer within %d s. This call was NOT retried and may '
                'still be running in the app; check its effect before calling it again.' % self.call_timeout)
        if call.get('method') == 'tools/call':
            return self.reply(call.get('id'), {'content': [{'type': 'text', 'text': text}], 'isError': True})
        return self.reply(call.get('id'), error={'code': -32000, 'message': text})

    def backup(self, parsed, calls, auth, busy=False):
        """Core down after the hold window (or busy for a safe method): answer what is safe from the gateway."""
        if auth not in self.accepted:
            return 503, [('Content-Type', 'application/json')], b'{"error":"NotionTool is restarting"}'
        if not isinstance(parsed, dict):
            return 503, [('Content-Type', 'application/json')], b'{"error":"NotionTool is restarting"}'
        name, ident = parsed.get('method'), parsed.get('id')
        if 'id' not in parsed:
            return 202, [('Content-Type', 'application/json')], b'{}'
        # v4: a DOWN core keeps NotionTool's own cached tools (nt_*) listed. An empty list made them vanish
        # from Notion whenever the app hung, so the MCP broke together with the app. Proxied tools (roblox_*,
        # blender_*, mcp_* ...) stay hidden while down so Notion uses their own MCP servers (e.g. the Roblox
        # MCP) instead. Calls stay safe: a down core gets the 'NOT executed' result below, never a resend.
        if name == 'tools/list' and not busy:
            try:
                tools = json.loads(self.cache[name][1])['result']['tools']
                return self.reply(ident, {'tools': [t for t in tools if str(t.get('name') or '').startswith(OWN_TOOL_PREFIX)]})
            except (KeyError, IndexError, TypeError, ValueError, AttributeError):
                return self.reply(ident, {'tools': []})
        # initialize is NOT refused any more: the cached answer below keeps the connection alive
        # while the app restarts (a refused initialize is what Notion shows as 'Failed to connect').
        if name in self.cache:
            kept, data = self.cache[name]
            try:
                cached = json.loads(data)
                cached['id'] = ident
                return 200, kept, json.dumps(cached).encode()
            except ValueError:
                pass
        if name == 'ping':
            return self.reply(ident, {})
        if name == 'tools/list':
            return self.reply(ident, {'tools': []})
        text = ('NotionTool is not reachable right now (restarting or hanging); this tool call was NOT executed. '
                'The MCP itself stays connected - try again in about a minute.')
        if name == 'tools/call':
            return self.reply(ident, {'content': [{'type': 'text', 'text': text}], 'isError': True})
        return self.reply(ident, error={'code': -32000, 'message': text})

    def health(self):
        return {'ok': True, 'service': 'notiontool-gateway', 'version': VERSION, 'pid': os.getpid(),
                'target': self.target(), 'cached': sorted(self.cache), 'local_tools': sorted(LOCAL_TOOLS),
                'local_jobs': len(self.local.jobs)}

    def make_server(self):
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass  # every MCP request is logged by Gateway.log (runtime/gateway.log)

            def send(self, status, headers, data):
                self.send_response(status)
                for key, value in headers:
                    self.send_header(key, value)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def run(self, method):
                length = int(self.headers.get('Content-Length') or 0)
                body = self.rfile.read(length) if length > 0 else b''
                path = self.path.split('?', 1)[0]
                if path == '/__gateway' and method == 'GET':
                    return self.send(200, [('Content-Type', 'application/json')], json.dumps(gateway.health()).encode())
                if path == '/__gateway/stop' and method == 'POST':
                    if self.client_address[0] != '127.0.0.1' or self.headers.get('X-Gateway-Control') != gateway.control:
                        return self.send(403, [('Content-Type', 'application/json')], b'{"error":"Forbidden"}')
                    self.send(200, [('Content-Type', 'application/json')], b'{"stopping":true}')
                    gateway.stopping.set()
                    threading.Thread(target=gateway.server.shutdown, daemon=True).start()
                    return
                headers = {k.lower(): v for k, v in self.headers.items()}
                self.send(*gateway.handle(method, self.path, headers, body))

            def do_GET(self):
                self.run('GET')

            def do_POST(self):
                self.run('POST')

            def do_DELETE(self):
                self.run('DELETE')

        class Server(ThreadingHTTPServer):
            allow_reuse_address = False
            daemon_threads = True
            request_queue_size = 64

            def server_bind(self):
                if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                    self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                super().server_bind()

        self.server = Server(('127.0.0.1', self.port), Handler)
        return self.server

    def serve(self):
        self.make_server()
        write_json(runtime_dir(self.data_dir) / 'gateway.json',
                   {'pid': os.getpid(), 'port': self.port, 'version': VERSION, 'control': self.control, 'started': time.time()})
        self.log(time.time(), [{'method': '__gateway_start'}], 0, 'started v%d' % VERSION)
        try:
            self.server.serve_forever()
        finally:
            self.server.server_close()


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    data_dir = argv[argv.index('--data-dir') + 1] if '--data-dir' in argv else str(Path(__file__).resolve().parent.parent / 'data')
    port = int(argv[argv.index('--gateway-port') + 1]) if '--gateway-port' in argv else DEFAULT_PORT
    try:
        Gateway(data_dir, port).serve()
    except OSError:
        return 1  # another gateway already owns the port: exactly one process 2
    return 0
