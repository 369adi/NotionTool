"""Optional REA (Reverse Engineer Anything, npm rea-agents) stdio MCP.

Started only when enabled in Settings; runs locally through npx. The owned
process speaks JSON-RPC over stdio; an unconfirmed call is never repeated.
"""
import json
import os
import queue
import subprocess
import threading
import time

REA_VERSION = '6.5.0'
COMMAND = (['cmd.exe', '/d', '/c'] if os.name == 'nt' else []) + ['npx', '-y', 'rea-agents@' + REA_VERSION, 'mcp']


def node_available():
    from shutil import which
    return bool(which('npx'))


class Rea:
    def __init__(self, launch=subprocess.Popen, timeout=180):
        self.launch, self.timeout = launch, timeout
        self.lock = threading.RLock()
        self.process = None
        self.messages = queue.Queue()
        self.sequence = 0
        self.schemas = {}

    def _send(self, message):
        self.process.stdin.write(json.dumps(message, separators=(',', ':')) + '\n')
        self.process.stdin.flush()

    def _request(self, method, params=None):
        self.sequence += 1
        ident = self.sequence
        self._send({'jsonrpc': '2.0', 'id': ident, 'method': method, 'params': params or {}})
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                message = self.messages.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                self.close()
                raise RuntimeError('REA response is unconfirmed; the call was not repeated') from None
            if message is None:
                self.close()
                raise RuntimeError('REA disconnected; the call was not repeated')
            if message.get('id') == ident:
                if 'error' in message:
                    raise ValueError('REA rejected this request')
                return message.get('result', {})
            if deadline <= time.monotonic():
                self.close()
                raise RuntimeError('REA response is unconfirmed; the call was not repeated')

    def _ensure(self):
        if self.process is not None and self.process.poll() is None:
            return
        self.messages = queue.Queue()
        self.process = self.launch(COMMAND, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding='utf-8',
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        process, messages = self.process, self.messages

        def read():
            try:
                for line in process.stdout:
                    try:
                        value = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if isinstance(value, dict):
                        messages.put(value)
            finally:
                messages.put(None)
        threading.Thread(target=read, daemon=True, name='rea-mcp').start()
        try:
            self._request('initialize', {'protocolVersion': '2024-11-05', 'capabilities': {},
                'clientInfo': {'name': 'NotionTool', 'version': '0.3.0'}})
            self._send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
            tools, cursor = [], None
            for _ in range(20):
                result = self._request('tools/list', {'cursor': cursor} if cursor else {})
                tools.extend(result.get('tools', []))
                cursor = result.get('nextCursor')
                if not cursor:
                    break
            self.schemas = {tool['name']: tool for tool in tools}
        except Exception:
            self.close()
            raise

    def tools(self):
        with self.lock:
            self._ensure()
            return list(self.schemas.values())

    def call(self, name, arguments):
        with self.lock:
            self._ensure()
            if name not in self.schemas:
                raise ValueError('Unknown REA tool; use rea_tools first')
            return self._request('tools/call', {'name': name, 'arguments': arguments})

    def close(self):
        with self.lock:
            process, self.process = self.process, None
            self.schemas = {}
            if process is None:
                return
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()
