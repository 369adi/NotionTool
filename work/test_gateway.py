import json, sys, tempfile, threading, time, unittest, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, r'C:\Users\liket\Downloads\NotionTool-harness\NotionTool')
from notiontool import gateway as gw

class Core:
    def __init__(self):
        self.calls = []; self.mode = 'ok'; core = self
        class H(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *a): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                core.calls.append(body['method'])
                if core.mode == 'reset' and body['method'] == 'tools/call':
                    import socket; self.connection.shutdown(socket.SHUT_RDWR); self.close_connection = True; return
                if self.headers.get('Authorization') != 'Bearer ok':
                    data = b'{"error":"Unauthorized"}'; self.send_response(401)
                else:
                    data = json.dumps({'jsonrpc': '2.0', 'id': body.get('id'), 'result': {'m': body['method']}}).encode(); self.send_response(200)
                self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
    def stop(self):
        self.server.shutdown(); self.server.server_close()

def post(port, method, ident=1, auth='Bearer ok'):
    req = urllib.request.Request(f'http://127.0.0.1:{port}/nt-harness/mcp', data=json.dumps({'jsonrpc': '2.0', 'id': ident, 'method': method}).encode(),
                                 headers={'Content-Type': 'application/json', 'Authorization': auth}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=20) as r: return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read())

class GatewayTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.core = Core(); gw.publish_core(self.dir, self.core.server.server_port)
        self.g = gw.Gateway(self.dir, 0, hold=3, poll=0.1); self.g.make_server()
        self.port = self.g.server.server_port
        threading.Thread(target=self.g.server.serve_forever, daemon=True).start()
    def tearDown(self):
        self.g.server.shutdown(); self.g.server.server_close()
        try: self.core.stop()
        except Exception: pass

    def test_forward_and_health(self):
        self.assertEqual(post(self.port, 'tools/list')[1]['result']['m'], 'tools/list')
        with urllib.request.urlopen(f'http://127.0.0.1:{self.port}/__gateway') as r: h = json.loads(r.read())
        self.assertEqual(h['target'], self.core.server.server_port); self.assertIn('tools/list', h['cached'])
        self.assertNotIn('control', h)

    def test_held_until_new_core_is_up(self):
        post(self.port, 'tools/list'); self.core.stop()
        gw.publish_core(self.dir, 1)  # old core gone, port dead
        def restart():
            time.sleep(1); self.core = Core(); gw.publish_core(self.dir, self.core.server.server_port)
        threading.Thread(target=restart).start()
        t = time.time(); status, body = post(self.port, 'tools/call', 7)
        self.assertEqual(body['result']['m'], 'tools/call'); self.assertGreater(time.time() - t, 0.8)
        self.assertEqual(self.core.calls, ['tools/call'])  # delivered exactly once to the new core

    def test_tool_call_never_resent_after_reaching_core(self):
        self.core.mode = 'reset'
        status, body = post(self.port, 'tools/call', 9)
        self.assertTrue(body['result']['isError']); self.assertIn('NOT retried', body['result']['content'][0]['text'])
        self.assertEqual(self.core.calls.count('tools/call'), 1)

    def test_backup_answers_when_core_stays_down(self):
        post(self.port, 'tools/list', 1); self.core.stop(); gw.publish_core(self.dir, 1)
        status, body = post(self.port, 'tools/list', 42)
        self.assertEqual((status, body['id'], body['result']['m']), (200, 42, 'tools/list'))
        self.assertTrue(post(self.port, 'tools/call', 43)[1]['result']['isError'])
        self.assertEqual(post(self.port, 'ping', 44)[1]['result'], {})
        self.assertEqual(post(self.port, 'tools/list', 45, auth='Bearer wrong')[0], 503)  # cache only for accepted callers

    def test_unauthorized_not_cached(self):
        self.assertEqual(post(self.port, 'tools/list', auth='Bearer wrong')[0], 401)
        self.assertEqual(self.g.accepted, set())

    def test_stop_needs_control(self):
        req = urllib.request.Request(f'http://127.0.0.1:{self.port}/__gateway/stop', data=b'{}', method='POST', headers={'X-Gateway-Control': 'nope'})
        with self.assertRaises(urllib.error.HTTPError): urllib.request.urlopen(req, timeout=5)

if __name__ == '__main__':
    unittest.main(verbosity=1)
