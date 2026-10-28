"""OpenAI-compatible API proxy for NotionTool: every chat request runs as one task in a Welcome-chat Coworker.

Start:  python -m notiontool.api_proxy [--port 8793] [--host-url http://127.0.0.1:8790] [--proxy-key KEY]
Use:    base_url http://127.0.0.1:8793/v1, model "notiontool-welcome", api_key = the --proxy-key (any text
        when no proxy key is set).

Routes: GET /v1/models, GET /v1/models/<id>, POST /v1/chat/completions (stream true/false), GET /health.
The proxy only talks to the NotionTool host API with the host key:
- POST /api/operator/delegate: one task per request; the host picks the Main and a free Coworker,
- GET /api/run?id=&compact=1: polls until the task is finished,
- GET /api/report?id=<task>: the answer text,
- POST /api/run/cancel: on timeout or when a streaming client disconnects.
It never creates Notion chats, agents, automations or triggers; the Coworkers are NotionTool's own
Welcome chats. Standard library only.
"""
import argparse
import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_PORT = 8793
DEFAULT_HOST_URL = 'http://127.0.0.1:8790'
MODEL_ID = 'notiontool-welcome'
# model name -> NotionTool quality ('fast' = one Coworker answer without extra design/review stages)
MODELS = {MODEL_ID: 'fast', MODEL_ID + '-standard': 'standard', MODEL_ID + '-high': 'high'}
TERMINAL = ('done', 'failed', 'blocked', 'cancelled')
MAX_PROMPT_CHARS = 60000
MAX_BODY_BYTES = 2 * 1024 * 1024
LOOPBACK = ('127.0.0.1', 'localhost', '::1')
RESULT_RE = re.compile(r'^\s*RESULT:\s*(DONE|NOT DONE\b.*)$', re.I)
ROLES = {'user': 'User', 'assistant': 'Assistant', 'tool': 'Tool', 'function': 'Tool'}


class ProxyError(Exception):
    """An error with the HTTP status and OpenAI error type the proxy answers with."""

    def __init__(self, status, message, kind='api_error'):
        super().__init__(message)
        self.status = status
        self.message = message
        self.kind = kind


def read_host_key(data_dir):
    """The host API key: env NT_HOST_KEY, else data/sessions (the same store the host writes), else ''."""
    env = os.environ.get('NT_HOST_KEY', '').strip()
    if env:
        return env
    try:
        from notiontool.vendor.sessions import SessionStore
        store = SessionStore(data_dir=str(Path(data_dir) / 'sessions'))
        return str((store.load_identity('host_api') or {}).get('key') or '')
    except Exception:  # noqa: BLE001 - no key is reported by main()
        return ''


class Host:
    """Small client for the NotionTool host API (Bearer host key)."""

    def __init__(self, url, key, timeout=30):
        self.url = str(url or DEFAULT_HOST_URL).rstrip('/')
        self.key = key
        self.timeout = timeout

    def call(self, path, body=None):
        data = None if body is None else json.dumps(body).encode('utf-8')
        request = urllib.request.Request(self.url + path, data=data, method='GET' if body is None else 'POST',
                                         headers={'Authorization': 'Bearer ' + self.key,
                                                  'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode('utf-8', 'replace')
            try:
                message = (json.loads(raw) or {}).get('error') or raw
            except (ValueError, AttributeError):
                message = raw
            message = str(message or 'HTTP %d' % exc.code)[:500]
            if exc.code in (401, 403):
                raise ProxyError(502, 'The NotionTool host refused the host API key (%s)' % message)
            if 400 <= exc.code < 500:
                raise ProxyError(400, 'NotionTool: ' + message, 'invalid_request_error')
            raise ProxyError(502, 'NotionTool host error %d: %s' % (exc.code, message))
        except (urllib.error.URLError, OSError) as exc:
            raise ProxyError(502, 'NotionTool host not reachable at %s (%s)' % (self.url, exc))
        try:
            return json.loads(raw) if raw.strip() else {}
        except ValueError:
            raise ProxyError(502, 'The NotionTool host sent no JSON')


def content_text(content):
    """Plain text of an OpenAI message content (string or list of parts); images become a short note."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get('type') in ('text', 'input_text'):
                parts.append(str(part.get('text') or ''))
            elif isinstance(part, dict) and part.get('type') in ('image_url', 'input_image', 'image'):
                parts.append('[image omitted: the proxy sends text only]')
        return '\n'.join(p for p in parts if p)
    return '' if content is None else str(content)


def build_prompt(messages):
    """(Coworker prompt, last user text) for an OpenAI messages list; ProxyError 400 when unusable."""
    if not isinstance(messages, list) or not messages:
        raise ProxyError(400, 'messages must be a non-empty list', 'invalid_request_error')
    system, turns, last_user = [], [], ''
    for message in messages:
        if not isinstance(message, dict):
            raise ProxyError(400, 'every message must be an object', 'invalid_request_error')
        role = str(message.get('role') or 'user').strip().lower()
        text = content_text(message.get('content')).strip()
        if not text:
            continue
        if role in ('system', 'developer'):
            system.append(text)
            continue
        turns.append('%s: %s' % (ROLES.get(role, role.title() or 'User'), text))
        if role == 'user':
            last_user = text
    if not last_user:
        raise ProxyError(400, 'messages need at least one user message with text', 'invalid_request_error')
    lines = ['You answer one chat request that came in through the NotionTool API proxy.',
             'Reply with the assistant answer to the last User message only: no preface and no notes '
             'about this task. Answer in the language of the last User message.']
    if system:
        lines += ['', 'Instructions from the caller:', '\n\n'.join(system)]
    lines += ['', 'Conversation:', '\n\n'.join(turns)]
    prompt = '\n'.join(lines)
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ProxyError(400, 'The conversation is too long (%d characters, limit %d)'
                         % (len(prompt), MAX_PROMPT_CHARS), 'invalid_request_error')
    return prompt, last_user


def strip_result_line(text):
    """The answer without NotionTool's closing 'RESULT: DONE' / 'RESULT: NOT DONE: ...' line."""
    lines = str(text or '').replace('\r\n', '\n').rstrip().split('\n')
    while lines and (not lines[-1].strip() or RESULT_RE.match(lines[-1])):
        lines.pop()
    return '\n'.join(lines).strip()


def answer_text(report):
    """Answer text from GET /api/report: the task result (string or JSON object), else the partial text."""
    report = report if isinstance(report, dict) else {}
    result = report.get('result')
    if isinstance(result, str) and result.strip().startswith('{'):
        try:
            result = json.loads(result)
        except ValueError:
            pass
    text = ''
    if isinstance(result, dict):
        for key in ('answer', 'text', 'content', 'response', 'output', 'result', 'summary'):
            if isinstance(result.get(key), str) and result[key].strip():
                text = result[key]
                break
        else:
            text = json.dumps(result, ensure_ascii=False)
    elif result is not None:
        text = str(result)
    if not text.strip():
        text = str(report.get('partial') or '')
    return strip_result_line(text)


def choose_task(tasks, ids, title):
    """The task that carries the answer: one of the delegated ids (title match first), else a title match."""
    tasks = [t for t in tasks or [] if isinstance(t, dict)]
    own = [t for t in tasks if t.get('id') in ids] or [t for t in tasks if t.get('title') == title]
    titled = [t for t in own if t.get('title') == title]
    return (titled or own or [None])[0]


def tokens(text):
    return max(1, (len(str(text or '')) + 3) // 4)


class Proxy:
    def __init__(self, host, chat_id='', timeout=600.0, poll=2.0, keepalive=15.0):
        self.host = host
        self.chat_id = chat_id
        self.timeout = float(timeout)
        self.poll = float(poll)
        self.keepalive = float(keepalive)

    def start(self, body, request_id):
        """Delegate the request as one task: (run_id, task ids, title)."""
        model = str(body.get('model') or MODEL_ID)
        if model not in MODELS:
            raise ProxyError(404, 'Unknown model %r; use one of %s' % (model, ', '.join(MODELS)),
                             'invalid_request_error')
        prompt, last_user = build_prompt(body.get('messages'))
        title = ('API: ' + ' '.join(last_user.split()))[:70]
        task = dict(id='t1', title=title, prompt=prompt,
                    acceptance='A direct and complete answer to the last User message.',
                    summary='Answers a chat request from the API proxy')
        payload = dict(goal=title, tasks=[task], request_id=request_id, quality=MODELS[model],
                       summary='Chat request from the NotionTool API proxy')
        if self.chat_id:
            payload['chat_id'] = self.chat_id
        result = self.host.call('/api/operator/delegate', payload) or {}
        run_id = str(result.get('run_id') or '')
        if not run_id:
            raise ProxyError(502, 'NotionTool did not return a run id')
        ids = [t.get('id') for t in result.get('tasks') or [] if isinstance(t, dict) and t.get('id')]
        return run_id, ids, title

    def cancel(self, run_id):
        try:
            self.host.call('/api/run/cancel', {'id': run_id})
        except ProxyError:
            pass  # best effort: the run is cancelled or already finished

    def answer(self, run_id, ids, title, alive=None):
        """Wait for the task and return (answer text, task id). alive() runs between polls (stream keepalive)."""
        deadline = time.monotonic() + self.timeout
        path = '/api/run?id=%s&compact=1' % urllib.parse.quote(run_id)
        while True:
            data = self.host.call(path) or {}
            run = data.get('run') or {}
            task = choose_task(data.get('tasks'), ids, title)
            if (task and task.get('state') in TERMINAL) or run.get('state') in TERMINAL:
                break
            if time.monotonic() >= deadline:
                self.cancel(run_id)
                raise ProxyError(504, 'No answer from the Coworker within %d s; the run was cancelled'
                                 % self.timeout, 'timeout')
            if alive is not None:
                alive()
            time.sleep(self.poll)
        if not task or task.get('state') != 'done':
            state = (task or {}).get('state') or run.get('state') or 'unknown'
            detail = str(run.get('error') or (task or {}).get('summary') or '').strip()
            raise ProxyError(502, 'The Coworker task ended as %s%s' % (state, (': ' + detail[:300]) if detail else ''))
        text = answer_text(self.host.call('/api/report?id=%s' % urllib.parse.quote(str(task['id']))))
        if not text:
            raise ProxyError(502, 'The Coworker finished without an answer text')
        return text, task['id']


def request_id_for(idempotency_key):
    """'api-' + a stable hash of the client's Idempotency-Key (same key = same run), else a fresh id."""
    key = str(idempotency_key or '').strip()
    if key:
        return 'api-' + hashlib.sha256(key.encode('utf-8')).hexdigest()[:32]
    return 'api-' + uuid.uuid4().hex


def make_handler(proxy, proxy_key='', allow_origins=()):
    allowed = set(allow_origins or ())

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        server_version = 'NotionToolProxy/1'

        def log_message(self, *args):
            pass

        def send_json(self, status, data):
            raw = json.dumps(data, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            try:
                self.wfile.write(raw)
            except OSError:
                pass

        def send_error_json(self, exc):
            self.send_json(exc.status, {'error': {'message': exc.message, 'type': exc.kind, 'code': None}})

        def guard(self):
            # A web page must not use the Coworkers through the user's browser (CSRF): no foreign Origin.
            origin = self.headers.get('Origin')
            if origin and origin not in allowed:
                raise ProxyError(403, 'Requests from web pages are not allowed (Origin %s)' % origin[:100],
                                 'permission_error')
            if proxy_key:
                sent = self.headers.get('Authorization', '')
                if not hmac.compare_digest(sent.encode('utf-8'), ('Bearer ' + proxy_key).encode('utf-8')):
                    raise ProxyError(401, 'Wrong or missing API key for the NotionTool proxy',
                                     'authentication_error')

        def do_GET(self):
            try:
                path = urllib.parse.urlsplit(self.path).path.rstrip('/')
                if path == '/health':
                    return self.send_json(200, {'ok': True, 'host': proxy.host.url})
                self.guard()
                if path == '/v1/models':
                    return self.send_json(200, {'object': 'list', 'data': [model_entry(m) for m in MODELS]})
                if path.startswith('/v1/models/'):
                    name = urllib.parse.unquote(path[len('/v1/models/'):])
                    if name in MODELS:
                        return self.send_json(200, model_entry(name))
                    raise ProxyError(404, 'Unknown model %r' % name, 'invalid_request_error')
                raise ProxyError(404, 'Not found', 'invalid_request_error')
            except ProxyError as exc:
                self.send_error_json(exc)

        def do_POST(self):
            try:
                path = urllib.parse.urlsplit(self.path).path.rstrip('/')
                self.guard()
                if path != '/v1/chat/completions':
                    raise ProxyError(404, 'Not found', 'invalid_request_error')
                length = int(self.headers.get('Content-Length') or 0)
                if length <= 0 or length > MAX_BODY_BYTES:
                    raise ProxyError(400, 'Send a JSON body (at most %d bytes)' % MAX_BODY_BYTES,
                                     'invalid_request_error')
                try:
                    body = json.loads(self.rfile.read(length).decode('utf-8'))
                except (ValueError, UnicodeDecodeError):
                    raise ProxyError(400, 'The body is not valid JSON', 'invalid_request_error')
                if not isinstance(body, dict):
                    raise ProxyError(400, 'The body must be a JSON object', 'invalid_request_error')
                request_id = request_id_for(self.headers.get('Idempotency-Key'))
                model = str(body.get('model') or MODEL_ID)
                run_id, ids, title = proxy.start(body, request_id)
                if body.get('stream'):
                    return self.stream(run_id, ids, title, model)
                text, task_id = proxy.answer(run_id, ids, title)
                prompt_tokens = tokens(json.dumps(body.get('messages'), ensure_ascii=False))
                done_tokens = tokens(text)
                return self.send_json(200, {
                    'id': 'chatcmpl-' + uuid.uuid4().hex[:24], 'object': 'chat.completion',
                    'created': int(time.time()), 'model': model,
                    'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': text},
                                 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': prompt_tokens, 'completion_tokens': done_tokens,
                              'total_tokens': prompt_tokens + done_tokens},
                    'notiontool': {'run_id': run_id, 'task_id': task_id, 'request_id': request_id}})
            except ProxyError as exc:
                self.send_error_json(exc)

        def sse(self, data):
            payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
            self.wfile.write(('data: %s\n\n' % payload).encode('utf-8'))
            self.wfile.flush()

        def stream(self, run_id, ids, title, model):
            self.close_connection = True
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'close')
            self.end_headers()
            ident = 'chatcmpl-' + uuid.uuid4().hex[:24]
            created = int(time.time())

            def chunk(delta, finish=None):
                return {'id': ident, 'object': 'chat.completion.chunk', 'created': created, 'model': model,
                        'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}

            last = [time.monotonic()]

            def alive():
                # SSE comment lines keep clients with short read timeouts connected while the Coworker works.
                if time.monotonic() - last[0] >= proxy.keepalive:
                    self.wfile.write(b': waiting for the Coworker\n\n')
                    self.wfile.flush()
                    last[0] = time.monotonic()

            try:
                self.sse(chunk({'role': 'assistant', 'content': ''}))
                try:
                    text, _task = proxy.answer(run_id, ids, title, alive=alive)
                except ProxyError as exc:
                    self.sse({'error': {'message': exc.message, 'type': exc.kind, 'code': None}})
                    self.sse('[DONE]')
                    return
                self.sse(chunk({'content': text}))
                self.sse(chunk({}, 'stop'))
                self.sse('[DONE]')
            except OSError:
                proxy.cancel(run_id)  # the client is gone: free the Coworker

    return Handler


def model_entry(name):
    return {'id': name, 'object': 'model', 'created': 0, 'owned_by': 'notiontool'}


def make_server(proxy, bind='127.0.0.1', port=DEFAULT_PORT, proxy_key='', allow_origins=()):
    if bind not in LOOPBACK and not proxy_key:
        raise ValueError('Binding to %s needs --proxy-key (only 127.0.0.1 may run without a key)' % bind)
    server = ThreadingHTTPServer((bind, port), make_handler(proxy, proxy_key, allow_origins))
    server.daemon_threads = True
    return server


def main(argv=None):
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description='OpenAI-compatible proxy: chat requests run in NotionTool Welcome-chat Coworkers.')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    parser.add_argument('--bind', default='127.0.0.1')
    parser.add_argument('--host-url', default=os.environ.get('NT_HOST_URL', DEFAULT_HOST_URL))
    parser.add_argument('--data', default=str(root / 'data'), help='NotionTool data folder (for the host key)')
    parser.add_argument('--chat-id', default='', help='Main chat that delegates (default: the host picks it)')
    parser.add_argument('--proxy-key', default=os.environ.get('NT_PROXY_KEY', ''))
    parser.add_argument('--timeout', type=float, default=600.0, help='seconds to wait for one answer')
    parser.add_argument('--poll', type=float, default=2.0)
    parser.add_argument('--allow-origin', action='append', default=[])
    args = parser.parse_args(argv)
    key = read_host_key(args.data)
    if not key:
        print('No host API key found (set NT_HOST_KEY or check %s\\sessions)' % args.data, file=sys.stderr)
        return 2
    proxy = Proxy(Host(args.host_url, key), chat_id=args.chat_id, timeout=args.timeout, poll=args.poll)
    try:
        server = make_server(proxy, args.bind, args.port, args.proxy_key, args.allow_origin)
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print('NotionTool API proxy on http://%s:%d/v1 -> %s (model %s)' % (args.bind, args.port, args.host_url, MODEL_ID),
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
