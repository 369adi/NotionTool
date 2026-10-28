"""API proxy: OpenAI chat requests become one delegated Coworker task on the NotionTool host API."""
import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from notiontool import api_proxy as ap

HOST_KEY = 'host-key-for-tests'
ALLOWED_HOST_PATHS = {'/api/operator/delegate', '/api/run', '/api/report', '/api/run/cancel'}


class FakeHost:
    """A tiny NotionTool host: delegate -> run polls -> report, plus cancel."""

    def __init__(self, polls_until_done=2, final_state='done', result='Hallo Welt\n\nRESULT: DONE',
                 delegate_error=None, never_done=False):
        self.polls_until_done = polls_until_done
        self.final_state = final_state
        self.result = result
        self.delegate_error = delegate_error
        self.never_done = never_done
        self.delegates, self.cancelled, self.paths = [], [], []
        self.polls = 0
        self.title = ''
        host = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, data):
                raw = json.dumps(data).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def body(self):
                length = int(self.headers.get('Content-Length') or 0)
                return json.loads(self.rfile.read(length) or b'{}')

            def authorized(self):
                return self.headers.get('Authorization') == 'Bearer ' + HOST_KEY

            def do_GET(self):
                url = urlsplit(self.path)
                host.paths.append(url.path)
                if not self.authorized():
                    return self.reply(401, {'error': 'unauthorized'})
                query = parse_qs(url.query)
                if url.path == '/api/run':
                    host.polls += 1
                    finished = not host.never_done and host.polls >= host.polls_until_done
                    state = host.final_state if finished else 'running'
                    run = {'id': query['id'][0], 'state': 'blocked' if state == 'blocked' else 'running'}
                    if state == 'blocked':
                        run['error'] = 'Coworker quota reached'
                    return self.reply(200, {'run': run, 'tasks': [{'id': 'task-9', 'title': host.title, 'state': state}]})
                if url.path == '/api/report':
                    assert query['id'] == ['task-9']
                    return self.reply(200, {'task': {'id': 'task-9'}, 'result': host.result, 'partial': ''})
                return self.reply(404, {'error': 'not found'})

            def do_POST(self):
                url = urlsplit(self.path)
                host.paths.append(url.path)
                if not self.authorized():
                    return self.reply(401, {'error': 'unauthorized'})
                body = self.body()
                if url.path == '/api/operator/delegate':
                    host.delegates.append(body)
                    if host.delegate_error:
                        return self.reply(400, {'error': host.delegate_error})
                    host.title = body['tasks'][0]['title']
                    return self.reply(200, {'ok': True, 'run_id': 'r1', 'state': 'queued',
                                            'tasks': [{'id': 'task-9', 'title': host.title, 'state': 'queued'}]})
                if url.path == '/api/run/cancel':
                    host.cancelled.append(body.get('id'))
                    return self.reply(200, {'ok': True})
                return self.reply(404, {'error': 'not found'})

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = 'http://127.0.0.1:%d' % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@contextmanager
def running(host_args=None, proxy_key='', timeout=5.0):
    host = FakeHost(**(host_args or {}))
    proxy = ap.Proxy(ap.Host(host.url, HOST_KEY), timeout=timeout, poll=0.01, keepalive=0.0)
    server = ap.make_server(proxy, '127.0.0.1', 0, proxy_key)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield host, 'http://127.0.0.1:%d' % server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        host.close()
        assert set(host.paths) <= ALLOWED_HOST_PATHS


def call(base, path, body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(base + path, data=data, method='GET' if body is None else 'POST',
                                     headers=dict({'Content-Type': 'application/json'}, **(headers or {})))
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


CHAT = {'model': 'notiontool-welcome', 'messages': [
    {'role': 'system', 'content': 'Sei kurz.'},
    {'role': 'user', 'content': 'Hi'},
    {'role': 'assistant', 'content': 'Hallo!'},
    {'role': 'user', 'content': [{'type': 'text', 'text': 'Wie geht es?'}]}]}


def test_models_list_and_health():
    with running() as (host, base):
        status, raw = call(base, '/v1/models')
        assert status == 200
        assert 'notiontool-welcome' in [m['id'] for m in json.loads(raw)['data']]
        status, raw = call(base, '/health')
        assert status == 200 and json.loads(raw)['ok'] is True
        assert host.delegates == []


def test_completion_delegates_one_task_and_returns_the_clean_answer():
    with running() as (host, base):
        status, raw = call(base, '/v1/chat/completions', CHAT)
        assert status == 200, raw
        data = json.loads(raw)
        assert data['object'] == 'chat.completion'
        assert data['choices'][0]['message'] == {'role': 'assistant', 'content': 'Hallo Welt'}
        assert data['choices'][0]['finish_reason'] == 'stop'
        assert data['usage']['total_tokens'] > 0
        sent = host.delegates[0]
        assert len(sent['tasks']) == 1 and sent['quality'] == 'fast'
        assert sent['request_id'].startswith('api-') and 'chat_id' not in sent and 'parts' not in sent
        prompt = sent['tasks'][0]['prompt']
        assert 'Sei kurz.' in prompt and 'User: Hi' in prompt and 'Assistant: Hallo!' in prompt
        assert prompt.rstrip().endswith('User: Wie geht es?')
        assert sent['tasks'][0]['title'] == 'API: Wie geht es?'


def test_same_idempotency_key_gives_the_same_request_id():
    with running() as (host, base):
        for _ in range(2):
            assert call(base, '/v1/chat/completions', CHAT, {'Idempotency-Key': 'abc'})[0] == 200
        call(base, '/v1/chat/completions', CHAT)
        ids = [d['request_id'] for d in host.delegates]
        assert ids[0] == ids[1] and ids[2] != ids[0]


def test_stream_sends_keepalive_answer_and_done():
    with running({'polls_until_done': 3}) as (host, base):
        status, raw = call(base, '/v1/chat/completions', dict(CHAT, stream=True))
        assert status == 200
        assert ': waiting for the Coworker' in raw
        events = [json.loads(line[6:]) for line in raw.splitlines()
                  if line.startswith('data: ') and line != 'data: [DONE]']
        text = ''.join(e['choices'][0]['delta'].get('content') or '' for e in events)
        assert text == 'Hallo Welt'
        assert events[-1]['choices'][0]['finish_reason'] == 'stop'
        assert raw.rstrip().endswith('data: [DONE]')


def test_proxy_key_is_checked():
    with running(proxy_key='geheim') as (host, base):
        assert call(base, '/v1/models')[0] == 401
        assert call(base, '/v1/chat/completions', CHAT, {'Authorization': 'Bearer falsch'})[0] == 401
        assert call(base, '/v1/models', headers={'Authorization': 'Bearer geheim'})[0] == 200
        assert host.delegates == []


def test_web_pages_cannot_use_the_proxy():
    with running() as (host, base):
        status, raw = call(base, '/v1/chat/completions', CHAT, {'Origin': 'https://evil.example'})
        assert status == 403 and host.delegates == []


def test_delegate_error_reaches_the_client():
    with running({'delegate_error': 'No enabled Coworker in this account'}) as (host, base):
        status, raw = call(base, '/v1/chat/completions', CHAT)
        assert status == 400
        assert 'No enabled Coworker in this account' in json.loads(raw)['error']['message']


def test_blocked_task_is_an_api_error_with_the_reason():
    with running({'final_state': 'blocked'}) as (host, base):
        status, raw = call(base, '/v1/chat/completions', CHAT)
        assert status == 502
        message = json.loads(raw)['error']['message']
        assert 'blocked' in message and 'quota' in message


def test_timeout_cancels_the_run():
    with running({'never_done': True}, timeout=0.2) as (host, base):
        status, raw = call(base, '/v1/chat/completions', CHAT)
        assert status == 504
        assert host.cancelled == ['r1']


def test_bad_requests_do_not_delegate():
    with running() as (host, base):
        assert call(base, '/v1/chat/completions', dict(CHAT, model='gpt-4o'))[0] == 404
        assert call(base, '/v1/chat/completions', {'messages': []})[0] == 400
        assert call(base, '/v1/chat/completions', {'messages': [{'role': 'system', 'content': 'x'}]})[0] == 400
        assert host.delegates == []


def test_answer_text_shapes():
    assert ap.answer_text({'result': {'summary': 'Kurz', 'result_file': 'x.md'}}) == 'Kurz'
    assert ap.answer_text({'result': '{"text": "Aus JSON\\nRESULT: DONE"}'}) == 'Aus JSON'
    assert ap.answer_text({'result': '', 'partial': 'Teil\nRESULT: DONE\n'}) == 'Teil'
    assert ap.strip_result_line('Antwort\r\nRESULT: NOT DONE: kein Zugriff') == 'Antwort'
    assert ap.strip_result_line('RESULT: DONE ist ein Wort\nEnde') == 'RESULT: DONE ist ein Wort\nEnde'


def test_open_bind_needs_a_proxy_key():
    proxy = ap.Proxy(ap.Host('http://127.0.0.1:1', HOST_KEY))
    with pytest.raises(ValueError):
        ap.make_server(proxy, '0.0.0.0', 0, '')
