"""Generic local stdio MCP client (used for REA, Roblox Studio MCP, Blender MCP).

Started only when enabled in Settings. The owned process speaks JSON-RPC over
stdio; an unconfirmed call is never repeated.
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


def image_result(result, max_side=1280, quality=72):
    """Move MCP image content items into '_image' so the bridge sends a real image
    instead of base64 text (Roblox screen_capture, Blender renders, ...)."""
    items = result.get('content') if isinstance(result, dict) else None
    if not isinstance(items, list) or '_image' in result:
        return result
    images = [it for it in items if isinstance(it, dict) and it.get('type') == 'image' and it.get('data')]
    if not images:
        return result
    data, mime = images[0]['data'], images[0].get('mimeType') or 'image/png'
    note = 'image sent as MCP image content'
    try:
        import base64
        import io
        from PIL import Image
        picture = Image.open(io.BytesIO(base64.b64decode(data)))
        picture.load()
        if picture.mode != 'RGB':
            picture = picture.convert('RGB')
        picture.thumbnail((max_side, max_side))
        buffer = io.BytesIO()
        picture.save(buffer, 'JPEG', quality=quality)
        data, mime = base64.b64encode(buffer.getvalue()).decode('ascii'), 'image/jpeg'
        note += ' (%dx%d JPEG, %d KB)' % (picture.size[0], picture.size[1], len(buffer.getvalue()) // 1024)
    except Exception:  # noqa: BLE001 - keep the original image if Pillow cannot read it
        pass
    if len(images) > 1:
        note += '; %d more image(s) dropped' % (len(images) - 1)
    path = save_capture(data, mime)
    inline = os.environ.get('NOTIONTOOL_INLINE_IMAGES', '0') == '1' or not path
    if not inline:
        note = note.replace('image sent as MCP image content', 'image saved (not inlined)') + ': ' + path
    out = dict(result)
    out['content'] = [it for it in items if not any(it is im for im in images)] + [{'type': 'text', 'text': note}]
    if inline:
        out['_image'] = {'data': data, 'mimeType': mime}
    return out


def save_capture(data, mime):
    """Write a capture below work/captures and return its relative path ('' on failure).
    Notion chats get tool results as JSON text, so inline base64 is unreadable there
    and floods the context; set NOTIONTOOL_INLINE_IMAGES=1 to inline it anyway."""
    try:
        import base64
        from pathlib import Path
        folder = Path(__file__).resolve().parent.parent / 'work' / 'captures'
        folder.mkdir(parents=True, exist_ok=True)
        ext = '.jpg' if 'jpeg' in mime else '.' + (mime.split('/')[-1] or 'png')
        name = time.strftime('%Y%m%d-%H%M%S') + '-%03d' % (int(time.time() * 1000) % 1000) + ext
        (folder / name).write_bytes(base64.b64decode(data))
        return 'work/captures/' + name
    except Exception:  # noqa: BLE001 - fall back to inline content
        return ''


class Rea:
    def __init__(self, launch=subprocess.Popen, timeout=180, command=None, label='REA', prefix='rea'):
        self.launch, self.timeout = launch, timeout
        self.command = list(command) if command else COMMAND
        self.label, self.prefix = label, prefix
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
                raise RuntimeError(self.label + ' response is unconfirmed; the call was not repeated') from None
            if message is None:
                self.close()
                raise RuntimeError(self.label + ' disconnected; the call was not repeated')
            if message.get('id') == ident:
                if 'error' in message:
                    detail = message['error'].get('message', '') if isinstance(message['error'], dict) else ''
                    raise ValueError(self.label + ' rejected this request' + (': ' + str(detail)[:300] if detail else ''))
                return message.get('result', {})
            if deadline <= time.monotonic():
                self.close()
                raise RuntimeError(self.label + ' response is unconfirmed; the call was not repeated')

    def _ensure(self):
        if self.process is not None and self.process.poll() is None:
            return
        self.messages = queue.Queue()
        self.process = self.launch(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
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
        threading.Thread(target=read, daemon=True, name=self.prefix + '-mcp').start()
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
                raise ValueError('Unknown ' + self.label + ' tool; use ' + self.prefix + '_tools first')
            return image_result(self._request('tools/call', {'name': name, 'arguments': arguments}))

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
