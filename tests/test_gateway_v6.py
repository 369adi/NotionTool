"""Gateway v6: the owner's PC tools always run in the gateway, even when the app is up but stuck.

v5 asked the app with a ping first. A busy app still answers pings, so PC tool calls waited behind one
long call (40-90 s) and Notion gave up after ~60 s. These tests use an app that answers pings instantly
but hangs on every tools/call: the PC tools must answer at once and never reach the app.
"""
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from notiontool import gateway as gw

OWNER = 'Bearer owner-key'


class StuckCore:
    """Answers ping/initialize/tools/list at once, hangs on tools/call until closed."""

    def __init__(self):
        self.release, self.calls = threading.Event(), []
        core = self

        class H(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)))
                if body.get('method') == 'tools/call':
                    core.calls.append(body['params']['name'])
                    core.release.wait(20)
                data = json.dumps({'jsonrpc': '2.0', 'id': body.get('id'), 'result': {'ok': 1}}).encode()
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


class GatewayV6(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.core = StuckCore()
        self.addCleanup(self.core.close)
        gw.publish_core(self.tmp.name, self.core.server.server_port)
        self.g = gw.Gateway(self.tmp.name, hold=1, poll=0.1, safe_timeout=1, call_timeout=2)
        self.g.local.host_key = lambda: 'owner-key'
        self.ran = []

        def run(name, args):
            self.ran.append(name)
            return {'ran': name, 'args': args}

        self.g.local.run = run

    def tool(self, name, args=None, auth=OWNER):
        body = {'jsonrpc': '2.0', 'id': 7, 'method': 'tools/call', 'params': {'name': name, 'arguments': args or {}}}
        started = time.time()
        status, _, data = self.g.handle('POST', '/mcp', {'authorization': auth, 'content-type': 'application/json'},
                                        json.dumps(body).encode())
        return status, json.loads(data), time.time() - started

    def test_pc_tools_run_in_the_gateway_while_the_app_is_stuck(self):
        for name in sorted(gw.LOCAL_TOOLS):
            status, data, took = self.tool(name, {'path': '.'})
            self.assertEqual(status, 200)
            self.assertLess(took, 1.0, name)
            self.assertEqual(json.loads(data['result']['content'][0]['text'])['ran'], name)
        self.assertEqual(self.core.calls, [])  # never reached the app
        self.assertEqual(sorted(self.ran), sorted(gw.LOCAL_TOOLS))

    def test_pc_tools_never_ask_the_app_first(self):
        self.g.core_answers = lambda *a: self.fail('v6 must not wait for an app ping')
        status, data, _ = self.tool('nt_shell', {'command': 'echo hi'})
        self.assertEqual(status, 200)
        self.assertNotIn('isError', data['result'])

    def test_local_calls_are_logged_as_local(self):
        self.tool('nt_read_file', {'path': 'README.md'})
        lines = [json.loads(line) for line in self.g.log_path().read_text('utf-8').splitlines()]
        self.assertEqual((lines[-1]['tool'], lines[-1]['outcome']), ('nt_read_file', 'local'))

    def test_other_callers_and_app_tools_are_still_forwarded(self):
        status, data, _ = self.tool('nt_shell', {'command': 'echo hi'}, auth='Bearer someone-else')
        self.assertIn('NOT retried', data['result']['content'][0]['text'])
        status, data, _ = self.tool('roblox_call', {'x': 1})
        self.assertIn('NOT retried', data['result']['content'][0]['text'])
        status, data, _ = self.tool('nt_write_file', {'path': 'a.txt', 'assignment_token': 'task'})
        self.assertIn('NOT retried', data['result']['content'][0]['text'])
        self.assertEqual(self.core.calls, ['nt_shell', 'roblox_call', 'nt_write_file'])
        self.assertEqual(self.ran, [])

    def test_job_results_route_by_id(self):
        status, data, took = self.tool('nt_job_result', {'job_id': 'gw-unknown'})
        self.assertLess(took, 1.0)
        self.assertIn('Unknown or expired job_id', data['result']['content'][0]['text'])
        self.tool('nt_job_result', {'job_id': 'job-123'})
        self.assertEqual(self.core.calls, ['nt_job_result'])  # the app's own jobs stay in the app

    def test_busy_answer_comes_before_notions_timeout(self):
        self.assertLess(gw.CALL_SECONDS, 60)
        self.assertGreaterEqual(gw.CALL_SECONDS, gw.LOCAL_SYNC_SECONDS)

    def test_new_version_replaces_the_old_gateway(self):
        self.assertGreaterEqual(gw.VERSION, 6)


if __name__ == '__main__':
    unittest.main()
