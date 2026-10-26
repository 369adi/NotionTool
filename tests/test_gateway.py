"""Gateway v3: a busy/blocked core must never turn into 'Failed to connect' in Notion."""
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from notiontool import gateway as gw


class Core:
    def __init__(self, hang=False):
        self.release, self.calls = threading.Event(), 0
        core = self

        class H(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)))
                if body.get('method') == 'tools/call':
                    core.calls += 1
                if hang:
                    core.release.wait(20)
                res = {'tools': [{'name': 'nt_api'}]} if body.get('method') == 'tools/list' else {'ok': 1}
                data = json.dumps({'jsonrpc': '2.0', 'id': body.get('id'), 'result': res}).encode()
                try:
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


def call(g, method, ident=1):
    body = {'jsonrpc': '2.0', 'id': ident, 'method': method}
    if method == 'tools/call':
        body['params'] = {'name': 'nt_api'}
    status, _, data = g.handle('POST', '/mcp', {'authorization': 'Bearer x', 'content-type': 'application/json'},
                               json.dumps(body).encode())
    return status, json.loads(data)


class GatewayV3(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def gateway(self, core):
        gw.publish_core(self.tmp.name, core.server.server_port)
        return gw.Gateway(self.tmp.name, hold=1, poll=0.1, safe_timeout=1, call_timeout=2)

    def test_busy_core_safe_methods_answered_from_cache(self):
        core = Core()
        g = self.gateway(core)
        call(g, 'initialize')
        call(g, 'tools/list')
        core.close()
        busy = Core(hang=True)
        self.addCleanup(busy.close)
        gw.publish_core(self.tmp.name, busy.server.server_port)
        status, body = call(g, 'tools/list', 7)
        self.assertEqual((status, body['id'], body['result']['tools'][0]['name']), (200, 7, 'nt_api'))
        status, body = call(g, 'ping', 8)
        self.assertEqual((status, body['result']), (200, {}))

    def test_busy_core_tool_call_not_retried(self):
        core = Core(hang=True)
        self.addCleanup(core.close)
        g = self.gateway(core)
        g.accepted.add('x')
        status, body = call(g, 'tools/call')
        self.assertTrue(body['result']['isError'])
        self.assertIn('NOT retried', body['result']['content'][0]['text'])
        self.assertEqual(core.calls, 1)

    def test_cache_and_logins_survive_gateway_restart(self):
        core = Core()
        g = self.gateway(core)
        call(g, 'initialize')
        core.close()
        g2 = gw.Gateway(self.tmp.name, hold=0.3, poll=0.1, safe_timeout=1)
        self.assertTrue(g2.accepted)
        status, body = call(g2, 'initialize', 5)
        self.assertEqual((status, body['id']), (200, 5))

    def test_down_core_does_not_advertise_cached_tools(self):
        core = Core()
        g = self.gateway(core)
        call(g, 'tools/list')
        core.close()
        status, body = call(g, 'tools/list')
        self.assertEqual(body['result']['tools'], [])

    def test_every_request_logged_without_secrets(self):
        core = Core()
        self.addCleanup(core.close)
        g = self.gateway(core)
        call(g, 'initialize')
        text = (Path(self.tmp.name) / 'runtime' / 'gateway.log').read_text('utf-8')
        line = json.loads(text.splitlines()[-1])
        self.assertEqual((line['method'], line['outcome']), ('initialize', 'forwarded'))
        self.assertNotIn('Bearer', text)


if __name__ == '__main__':
    unittest.main()
