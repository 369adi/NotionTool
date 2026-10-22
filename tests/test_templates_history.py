"""Request templates and history search (templates.py, server route, live.js, extras.js). No real clients."""
import json
import os
import shutil
import subprocess
import types
from pathlib import Path

import pytest

from notiontool import templates

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / 'notiontool'
LIVE = PKG / 'web' / 'desktop' / 'live.js'
EXTRAS = PKG / 'web' / 'desktop' / 'extras.js'
TITLES = ['Fix a UI bug', 'Build a feature', 'Review and test', 'Research']


def _node():
    found = shutil.which('node')
    if found:
        return found
    win = r'C:\Program Files\nodejs\node.exe'
    return win if os.path.exists(win) else None


class FakeStore:
    def __init__(self, runs=(), tasks=()):
        self.data = {'runs': list(runs), 'tasks': list(tasks)}

    def all(self, kind):
        return [dict(x) for x in self.data.get(kind, [])]


def _store():
    runs = [
        dict(id='run-1', goal='Fix the login button', state='done', created=900, finished=1000, summary='Button works again'),
        dict(id='run-2', goal='Write the help page', state='failed', created=1900, finished=2000, summary='Stopped early'),
    ]
    tasks = [
        dict(id='task-1', run_id='run-1', title='Change the button style', state='done', created=2900, finished=3000,
             result='Changed the colour', files=['notiontool/web/desktop/live.js']),
        dict(id='task-2', run_id='run-2', title='Check the parser', state='failed', created=400, finished=500,
             result='Timeout in the Parser step', error='Timeout in the parser'),
    ]
    return FakeStore(runs, tasks)


# ---- templates ----

def test_four_templates_with_fields_and_checks():
    items = templates.templates()
    assert [t['title'] for t in items] == TITLES
    assert [f[0] for f in templates.FIELDS] == ['what', 'where', 'done_when']
    for t in items:
        assert t['id'] and t['hint'] and len(t['checks']) >= 3


def test_draft_format_and_fill_in():
    text = templates.draft('ui_bug', {'what': '  Button  is\tgrey ', 'where': 'Live view'})
    lines = text.split('\n')
    assert lines[0] == 'New request: Fix a UI bug'
    assert lines[1] == ''
    assert lines[2] == 'What: Button is grey'
    assert lines[3] == 'Where: Live view'
    assert lines[4] == 'Done when: (fill in)'
    assert 'Checks before you report done:' in lines
    assert lines[-1].startswith('4. ')
    assert templates.draft('Research', None).startswith('New request: Research')
    with pytest.raises(ValueError):
        templates.draft('nope', {})


# ---- history search ----

def test_search_matches_title_result_and_files():
    store = _store()
    hit = templates.search(store, 'live.js')['results']
    assert [h['id'] for h in hit] == ['task-1'] and hit[0]['field'] == 'Files'
    assert hit[0]['kind'] == 'task' and hit[0]['run_id'] == 'run-1'
    assert 'live.js' in ' '.join(hit[0]['files'])
    res = templates.search(store, 'PARSER timeout')['results']
    assert [h['id'] for h in res] == ['task-2']
    assert 'Timeout' in res[0]['outcome'] or res[0]['outcome']
    assert [h['id'] for h in templates.search(store, 'login')['results']] == ['run-1']
    assert templates.search(store, 'login nothinglikethis')['results'] == []


def test_search_empty_query_and_newest_first_with_date():
    store = _store()
    empty = templates.search(store, '   ')
    assert empty['ok'] is True and empty['results'] == [] and empty['total'] == 0
    res = templates.search(store, 'the')['results']
    assert len(res) == 4
    ats = [h['at'] for h in res]
    assert ats == sorted(ats, reverse=True)
    for h in res:
        assert 'outcome' in h and h['date'] and len(h['date']) == 16


def test_search_limit_is_50_and_clamped():
    store = FakeStore(tasks=[dict(id='t%d' % i, title='alpha %d' % i, state='done', finished=1000 + i) for i in range(80)])
    res = templates.search(store, 'alpha')
    assert len(res['results']) == 50 and res['total'] == 80 and res['limit'] == 50
    assert res['results'][0]['id'] == 't79'
    assert len(templates.search(store, 'alpha', 500)['results']) == 50
    assert len(templates.search(store, 'alpha', 5)['results']) == 5
    assert len(templates.search(store, 'alpha', 'bad')['results']) == 50


def test_http_search_uses_query_dict():
    server = types.SimpleNamespace(store=_store())
    res = templates.http_search(server, {'q': ['button'], 'limit': ['1']})
    assert res['ok'] is True and len(res['results']) == 1 and res['total'] >= 2
    assert templates.http_search(server, {})['results'] == []


def test_install_desk_actions_is_idempotent_and_keeps_markers():
    def handlers(server):
        return {'state': lambda args: {'ok': True}}
    handlers.nt_problem_actions = True
    fake = types.SimpleNamespace(DESK_ACTIONS=('state',), desktop_handlers=handlers)
    templates.install_desk_actions(fake)
    first = fake.desktop_handlers
    templates.install_desk_actions(fake)
    assert fake.desktop_handlers is first
    assert first.nt_problem_actions is True and first.nt_template_actions is True
    for name in templates.DESK_ACTION_NAMES:
        assert fake.DESK_ACTIONS.count(name) == 1
    got = first(types.SimpleNamespace(store=_store()))
    assert set(['state', 'request_templates', 'request_draft', 'history_search']) <= set(got)
    assert got['request_draft']({'id': 'review', 'values': {}})['text'].startswith('New request: Review and test')
    assert got['history_search']({'q': 'login'})['results'][0]['id'] == 'run-1'
    assert len(got['request_templates']({})['templates']) == 4


# ---- server.py (CRLF byte patch) ----

def test_server_route_and_install_crlf():
    data = (PKG / 'server.py').read_bytes()
    assert data.count(b"path=='/api/history/search'") == 1
    assert data.count(b'templates.install_desk_actions()') == 2
    for line in data.split(b'\n'):
        if b'/api/history/search' in line or b'templates.install_desk_actions()' in line or b'templates.http_search' in line:
            assert line.endswith(b'\r')


# ---- live.js / extras.js ----

def test_live_js_static():
    src = LIVE.read_text(encoding='utf-8')
    for needle in ('New request', 'history_search', 'openRequestTemplate', 'Search all requests and steps',
                   'Put in chat', 'Nothing found.', 'requestDraft'):
        assert needle in src
    assert 'innerHTML' not in src and 'fetch(' not in src
    block = src.split('// ---- New request', 1)[1].split('// ---- History search', 1)[0]
    for bad in ('dispatchEvent', '.click()', "'Enter'", 'submit(', 'KeyboardEvent'):
        assert bad not in block, bad
    assert 'send it yourself' in block


def test_extras_js_palette_entries():
    src = EXTRAS.read_text(encoding='utf-8')
    assert "'New request: ' + tpl[1]" in src and 'openRequestTemplate' in src
    for title in TITLES:
        assert "'%s'" % title in src
    pal = src.split('PALETTE_ACTIONS', 1)[1].split(']', 1)[0]
    for name in templates.DESK_ACTION_NAMES:
        assert name not in pal


@pytest.mark.skipif(_node() is None, reason='node not installed')
def test_node_check_both_files():
    for path in (LIVE, EXTRAS):
        out = subprocess.run([_node(), '--check', str(path)], capture_output=True, text=True, encoding='utf-8')
        assert out.returncode == 0, out.stderr


NODE_DRAFTS = r"""
const fs = require('fs'), vm = require('vm');
const file = process.argv[process.argv.length - 1];
const window = { __ntDesk: {}, addEventListener() {}, removeEventListener() {} };
const sandbox = { window, console, setTimeout, clearTimeout, setInterval, clearInterval,
  location: { hash: '', pathname: '/', search: '' }, navigator: {} };
sandbox.self = window;
vm.runInNewContext(fs.readFileSync(file, 'utf8'), sandbox);
const u = window.__ntDesk.liveUtil;
const values = { what: '  Button  is\tgrey ', where: 'Live view', done_when: '' };
console.log(JSON.stringify({
  templates: u.REQUEST_TEMPLATES.map(t => [t.id, t.title, t.hint, t.checks]),
  drafts: u.REQUEST_TEMPLATES.map(t => u.requestDraft(t.id, values)),
  byTitle: u.requestDraft('Research', null),
  unknown: u.requestDraft('nope', {})
}));
"""


@pytest.mark.skipif(_node() is None, reason='node not installed')
def test_live_js_drafts_match_python():
    out = subprocess.run([_node(), '-e', NODE_DRAFTS, str(LIVE)], capture_output=True, text=True, encoding='utf-8')
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got['templates'] == [[t['id'], t['title'], t['hint'], t['checks']] for t in templates.templates()]
    values = {'what': '  Button  is\tgrey ', 'where': 'Live view', 'done_when': ''}
    assert got['drafts'] == [templates.draft(t['id'], values) for t in templates.templates()]
    assert got['byTitle'] == templates.draft('research', None)
    assert got['unknown'] == ''


def test_lf_line_endings():
    for path in (PKG / 'templates.py', Path(__file__), LIVE, EXTRAS):
        assert b'\r\n' not in path.read_bytes(), path
