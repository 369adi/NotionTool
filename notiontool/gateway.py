"""Process 2: stable MCP gateway (and backup MCP) in front of the NotionTool core.

Tailscale Funnel targets this fixed local port instead of the core. The core can restart behind it
(updates, crashes) without Notion ever losing the MCP: requests that cannot reach the core are held
and delivered once the new core is up. A request that already reached the core is never resent (I5).
While the core stays down, the gateway answers initialize/tools/list/ping itself from its cache so the
Notion connection stays valid, and tool calls get a clear 'restarting' result instead of a dead endpoint.
"""
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = 2
DEFAULT_PORT = 8792
HOLD_SECONDS = 25  # Notion's MCP client gives up before 120 s -> 'Failed to connect'; answer fast instead
SAFE_METHODS = {'initialize', 'notifications/initialized', 'tools/list', 'ping', 'resources/list', 'prompts/list'}
CACHED_METHODS = ('initialize', 'tools/list')
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


class Gateway:
    def __init__(self, data_dir, port=DEFAULT_PORT, hold=HOLD_SECONDS, poll=0.5):
        self.data_dir, self.port, self.hold, self.poll = str(data_dir), int(port), hold, poll
        self.control = secrets.token_urlsafe(24)
        self.cache = {}          # rpc method -> (headers, body)
        self.accepted = set()    # sha256 of Authorization values the core accepted
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.server = None

    def target(self):
        return int(read_json(runtime_dir(self.data_dir) / 'core.json').get('mcp_port') or 0)

    def forward(self, method, path, headers, body):
        port = self.target()
        if not port:
            raise CoreDown(False)
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=900)
        try:
            try:
                conn.connect()
            except OSError:
                raise CoreDown(False) from None
            try:
                conn.request(method, path, body=body, headers=headers)
                response = conn.getresponse()
                data = response.read()
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
        parsed, calls = self.rpc_calls(body)
        names = {c.get('method') for c in calls}
        safe = method in ('GET', 'DELETE') or (bool(calls) and names <= SAFE_METHODS)
        auth = hashlib.sha256((headers.get('authorization') or '').encode()).hexdigest()
        sent = {k: v for k, v in headers.items() if k in FORWARD_HEADERS}
        deadline = time.time() + self.hold
        while True:
            try:
                status, kept, data = self.forward(method, path, sent, body)
            except CoreDown as down:
                if down.sent and not safe:
                    return self.interrupted(calls)
                if time.time() >= deadline or self.stopping.is_set():
                    break
                time.sleep(self.poll)
                continue
            if status == 200 and headers.get('authorization'):
                with self.lock:
                    self.accepted.add(auth)
                    if isinstance(parsed, dict) and parsed.get('method') in CACHED_METHODS:
                        self.cache[parsed['method']] = (kept, data)
            return status, kept, data
        return self.backup(parsed, calls, auth)

    def interrupted(self, calls):
        call = calls[0] if calls else {}
        text = ('NotionTool restarted while this tool call was running. It was NOT retried automatically; '
                'check whether it took effect before calling it again.')
        if call.get('method') == 'tools/call':
            return self.reply(call.get('id'), {'content': [{'type': 'text', 'text': text}], 'isError': True})
        return self.reply(call.get('id'), error={'code': -32000, 'message': text})

    def backup(self, parsed, calls, auth):
        """Core still down after the hold window: answer what is safe from the gateway itself."""
        if auth not in self.accepted:
            return 503, [('Content-Type', 'application/json')], b'{"error":"NotionTool is restarting"}'
        if not isinstance(parsed, dict):
            return 503, [('Content-Type', 'application/json')], b'{"error":"NotionTool is restarting"}'
        name, ident = parsed.get('method'), parsed.get('id')
        if 'id' not in parsed:
            return 202, [('Content-Type', 'application/json')], b'{}'
        # A cached schema is not proof that the disconnected server can execute it.
        if name == 'tools/list':
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
        text = 'NotionTool is restarting; this tool call was not executed. Try again in about a minute.'
        if name == 'tools/call':
            return self.reply(ident, {'content': [{'type': 'text', 'text': text}], 'isError': True})
        return self.reply(ident, error={'code': -32000, 'message': text})

    def health(self):
        return {'ok': True, 'service': 'notiontool-gateway', 'version': VERSION, 'pid': os.getpid(),
                'target': self.target(), 'cached': sorted(self.cache)}

    def make_server(self):
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

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
