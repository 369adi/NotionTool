"""Notes tab: read-only notes routes with a fake Notion client (notes_routes.py) and static checks of notes.js/css."""
import os
import re
import shutil
import subprocess
import tempfile
import types
import unittest
from pathlib import Path

from notiontool import notes_routes as nr
from notiontool.store import Store

ROOT = Path(__file__).resolve().parents[1]
DESK = ROOT / 'notiontool' / 'web' / 'desktop'
NODE = r'C:\Program Files\nodejs\node.exe'
BASE = ''.join(('https://', 'www.', 'notion', '.so/'))
P1 = 'aaaaaaaa1111222233334444bbbbbbbb'
P2 = 'cccccccc-1111-2222-3333-dddddddddddd'
P3 = 'eeeeeeee1111222233334444ffffffff'
D1 = nr.page_id(P1)
D2 = nr.page_id(P2)
D3 = nr.page_id(P3)


class FakeClient:
    """Records every call; only the two read paths are allowed."""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail
        self.info = {'space_id': 'space-1'}

    def call(self, path, body, timeout=45):
        self.calls.append((path, body))
        if path not in ('syncRecordValues', 'loadPageChunk'):
            raise AssertionError('write or unexpected call: ' + path)
        if self.fail:
            raise ValueError('Notion %s: HTTP 500' % path)
        if path == 'syncRecordValues':
            blocks = {}
            for req in body['requests']:
                ident = req['pointer']['id']
                title = {D1: '[Draft] Q4 pipeline plan', D2: 'Call notes ACME', D3: 'Old page'}[ident]
                blocks[ident] = {'value': {'value': {'id': ident, 'type': 'page', 'alive': ident != D3,
                                 'properties': {'title': [[title]]}, 'last_edited_time': 1700000000000}}}
            return {'recordMap': {'block': blocks}}
        ident = body['pageId']
        b = lambda i, t, text, **kw: (i, {'value': {'value': dict(id=i, type=t, alive=True,
                                       properties=dict({'title': [[text]]}, **kw.pop('props', {})), **kw)}})
        blocks = dict([
            (ident, {'value': {'value': {'id': ident, 'type': 'page', 'alive': True, 'last_edited_time': 1700000500000,
                                         'properties': {'title': [['Q4 pipeline plan']]},
                                         'content': ['b1', 'b2', 'b3', 'b4', 'b5', 'b6', 'b7', 'b8']}}}),
            b('b1', 'text', 'Status: Done'),
            b('b2', 'header', 'Goal'),
            b('b3', 'text', 'Close 3 deals.'),
            b('b4', 'bulleted_list', 'Call ACME', content=['b4a']),
            b('b4a', 'bulleted_list', 'Ask about budget'),
            b('b5', 'to_do', 'Send offer', props={'checked': [['Yes']]}),
            b('b6', 'code', 'print(1)', props={'language': [['Python']]}),
            b('b7', 'divider', ''),
            b('b8', 'image', ''),
        ])
        return {'recordMap': {'block': blocks}}


class Clock:
    def __init__(self):
        self.now = 1700001000.0

    def __call__(self):
        return self.now


class NotesRoutesTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = Store(self.dir)
        now = 1700000900.0
        self.store.batch([
            ('workers', dict(id='w0', label='Main Sales', purpose='main')),
            ('runs', dict(id='r1', goal='Plan Q4 pipeline', main_worker='w0', created=now - 50)),
            ('runs', dict(id='r2', goal='Notes for ' + BASE + 'Call-notes-' + P2.replace('-', ''),
                          main_worker='w0', created=now - 400)),
            ('tasks', dict(id='k1', run_id='r1', worker_id='w1', state='done', finished=now,
                           result='Done. Page: ' + BASE + 'Q4-pipeline-plan-' + P1 + '?pvs=4 and again ' + BASE + P1)),
            ('tasks', dict(id='k2', run_id='r1', worker_id='w1', state='done', finished=now - 100,
                           result='Old: ' + BASE + 'x-' + P3)),
            ('tasks', dict(id='k3', run_id='r1', worker_id='w1', state='done', result='No link here ' + P1)),
        ])
        self.store.db.commit()
        self.client = FakeClient()
        self.clock = Clock()
        self.routes = nr.NotesRoutes(self.store, lambda: self.client, clock=self.clock)

    def tearDown(self):
        self.store.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_page_id_parsing(self):
        self.assertEqual(nr.page_id(P1), 'aaaaaaaa-1111-2222-3333-4444bbbbbbbb')
        self.assertEqual(nr.page_id(BASE + 'Some-title-' + P1 + '?pvs=4'), D1)
        self.assertEqual(nr.page_ids_in('x ' + BASE + 'a-' + P1 + ' y ' + BASE + P1), [D1])
        self.assertEqual(nr.page_ids_in('bare ' + P1), [])
        self.assertEqual(nr.page_id('nope'), '')
        self.assertTrue(nr.page_url(D1).endswith(P1))

    def test_list_titles_status_links_and_filters(self):
        res = self.routes.list({})
        ids = [n['id'] for n in res['notes']]
        self.assertEqual(set(ids), {D1, D2})            # D3 is deleted in Notion; bare ids are ignored
        plan = [n for n in res['notes'] if n['id'] == D1][0]
        self.assertEqual(plan['title'], 'Q4 pipeline plan')
        self.assertEqual(plan['status'], 'draft')
        self.assertEqual(plan['kind'], 'plan')
        self.assertEqual(plan['run_id'], 'r1')
        self.assertEqual(plan['main_label'], 'Main Sales')
        self.assertEqual(plan['run_title'], 'Plan Q4 pipeline')
        self.assertAlmostEqual(plan['updated'], 1700000000.0)
        note = [n for n in res['notes'] if n['id'] == D2][0]
        self.assertEqual((note['kind'], note['status'], note['run_id']), ('note', 'active', 'r2'))
        self.assertEqual([n['id'] for n in self.routes.list({'status': 'draft'})['notes']], [D1])
        self.assertEqual([n['id'] for n in self.routes.list({'kind': 'note'})['notes']], [D2])
        self.assertEqual([n['id'] for n in self.routes.list({'query': 'acme'})['notes']], [D2])

    def test_list_is_cached(self):
        self.routes.list({})
        first = len(self.client.calls)
        self.assertEqual(first, 1)                      # one batched syncRecordValues for all pages
        self.routes.list({'query': 'q4'})
        self.assertEqual(len(self.client.calls), first)
        self.clock.now += nr.TITLE_TTL + 1
        self.routes.list({})
        self.assertEqual(len(self.client.calls), first + 1)

    def test_budget_cap_serves_stale(self):
        routes = nr.NotesRoutes(self.store, lambda: self.client, clock=self.clock, calls_per_min=1)
        routes.page({'id': P1})
        self.assertEqual(len(self.client.calls), 1)
        self.clock.now += 5
        again = routes.page({'id': P1, 'refresh': True})
        self.assertTrue(again['stale'])                 # budget used up: cached copy, no new call
        self.assertEqual(len(self.client.calls), 1)
        res = routes.list({})
        self.assertTrue(res['stale'])
        self.assertEqual(len(self.client.calls), 1)
        self.clock.now += 61
        self.assertFalse(routes.list({})['stale'])

    def test_page_render(self):
        page = self.routes.page({'id': BASE + 'Q4-' + P1})
        self.assertEqual(page['status'], 'done')
        self.assertEqual(page['title'], 'Q4 pipeline plan')
        types_ = [(b['type'], b['text'], b['depth']) for b in page['blocks']]
        self.assertEqual(types_[1], ('h1', 'Goal', 0))
        self.assertIn(('li', 'Call ACME', 0), types_)
        self.assertIn(('li', 'Ask about budget', 1), types_)
        todo = [b for b in page['blocks'] if b['type'] == 'todo'][0]
        self.assertTrue(todo['checked'])
        code = [b for b in page['blocks'] if b['type'] == 'code'][0]
        self.assertEqual(code['language'], 'Python')
        self.assertIn('divider', [b['type'] for b in page['blocks']])
        self.assertNotIn('', [b['type'] for b in page['blocks']])   # empty image block skipped
        self.assertTrue(page['url'].startswith(BASE))
        n = len(self.client.calls)
        self.routes.page({'id': P1})
        self.assertEqual(len(self.client.calls), n)    # cached
        self.routes.page({'id': P1, 'refresh': True})
        self.assertEqual(len(self.client.calls), n + 1)
        listed = [x for x in self.routes.list({})['notes'] if x['id'] == D1][0]
        self.assertEqual(listed['status'], 'done')     # page status line wins after a read

    def test_errors_are_plain(self):
        routes = nr.NotesRoutes(self.store, lambda: FakeClient(fail=True), clock=self.clock)
        with self.assertRaises(ValueError):
            routes.page({'id': P1})
        self.assertTrue(routes.list({})['stale'])
        none = nr.NotesRoutes(self.store, lambda: None, clock=self.clock)
        with self.assertRaises(nr.NotesUnavailable):
            none.page({'id': P1})
        with self.assertRaises(ValueError):
            self.routes.page({'id': 'nothing'})

    def test_draft_never_sends_or_calls_notion(self):
        new = self.routes.draft({'mode': 'new', 'title': 'Q1 plan', 'idea': 'More leads'})
        self.assertFalse(new['sent'])
        self.assertIn('Status: Draft', new['text'])
        self.assertIn('Title: Q1 plan', new['text'])
        upd = self.routes.draft({'mode': 'update', 'id': P1, 'title': 'Q4 pipeline plan'})
        self.assertIn('Q4 pipeline plan', upd['text'])
        self.assertIn(P1, upd['text'])
        self.assertEqual(self.client.calls, [])

    def test_read_only_store(self):
        before = self.store.db.total_changes
        self.routes.list({})
        self.routes.page({'id': P1})
        self.assertEqual(self.store.db.total_changes, before)

    def test_http_route_and_desk_handlers(self):
        server = types.SimpleNamespace(store=self.store, adapter=None)
        server._nt_notes_routes = self.routes
        status, payload = nr.http_route(server, '/api/notes', {'kind': ['plan']})
        self.assertEqual(status, 200)
        self.assertEqual([n['id'] for n in payload['notes']], [D1])
        self.assertEqual(nr.http_route(server, '/api/notes/page', {'id': [P1]})[0], 200)
        self.assertEqual(nr.http_route(server, '/api/notes/page', {})[0], 400)
        self.assertEqual(nr.http_route(server, '/api/notes/draft', {'mode': ['new']})[0], 200)
        self.assertEqual(nr.http_route(server, '/api/notes/nope', {})[0], 404)
        handlers = nr.desk_handlers(server)
        self.assertEqual(set(handlers), {'notes_list', 'notes_page', 'notes_draft'})
        self.assertEqual(len(handlers['notes_list']({})['notes']), 2)
        bare = types.SimpleNamespace(store=self.store, adapter=None)
        self.assertEqual(nr.http_route(bare, '/api/notes/page', {'id': [P1]})[0], 409)   # no account


def read(path):
    return Path(path).read_bytes().decode('utf-8')


class NotesJsTest(unittest.TestCase):
    def setUp(self):
        self.js = read(DESK / 'notes.js')
        self.css = read(DESK / 'notes.css')

    def test_part_contract_and_shell_registration(self):
        js = self.js
        self.assertIn('window.__ntDesk = window.__ntDesk || {}', js)
        self.assertIn('window.__ntDesk.notes = function (ctx)', js)
        self.assertIn('return { stop: stop, open: open, close: close }', js)
        self.assertIn("sh.register('notes', view)", js)
        self.assertIn('window.__ntShellQueue', js)
        self.assertIn('setInterval', js)
        self.assertTrue(js.rstrip().endswith('})();'))

    def test_uses_read_only_actions(self):
        for action in ('notes_list', 'notes_page', 'notes_draft'):
            self.assertIn("ctx.call('%s'" % action, self.js)
        self.assertIsNone(re.search(r"ctx\.call\('(?!notes_)", self.js))

    def test_labels_plain_english(self):
        for label in ('New plan', 'Update plan', 'Open in Notion', 'Search notes', "'Draft'", "'Active'", "'Done'",
                      'Put in chat box', 'Copy', 'Nothing is sent. Check the text, then send it yourself in Chat.'):
            self.assertIn(label, self.js)

    def test_never_sends(self):
        js = self.js
        self.assertNotIn("'Enter'", js)
        self.assertNotIn('KeyboardEvent', js)
        self.assertNotIn('dispatchEvent', js)
        self.assertNotIn('submit', js.lower())
        self.assertIsNone(re.search(r'send.{0,40}\.click\(', js, re.I))
        self.assertEqual(js.count('.click()'), 1)      # only the hidden in-app page link for 'Open in Notion'
        self.assertNotIn('location.reload', js)
        self.assertNotIn('location.href =', js)

    def test_safe_dom(self):
        js = self.js
        self.assertNotIn('innerHTML', js)
        self.assertNotIn('insertAdjacentHTML', js)
        self.assertNotIn('removeChild(document', js)
        self.assertIn("'data-nt-desk': 'notes-view'", js)
        for bad in ('127.0.0.1', 'Bearer ', 'X-NotionTool-Token', 'token_v2'):
            self.assertNotIn(bad, js)

    def test_css_tokens_and_scope(self):
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        for token in ('var(--nt-bg', 'var(--nt-surface', 'var(--nt-raised', 'var(--nt-radius', '#7c5cff', '#22d3ee'):
            self.assertIn(token, css)
        selectors = [s.strip() for block in re.findall(r'([^{}]+)\{', css) for s in block.split(',')]
        for sel in selectors:
            if sel and not sel.startswith('@') and not sel.startswith('from') and not sel.startswith('to'):
                self.assertTrue(sel.startswith('.nt-notes'), sel)

    def test_line_endings_lf(self):
        for path in (DESK / 'notes.js', DESK / 'notes.css', ROOT / 'notiontool' / 'notes_routes.py'):
            self.assertNotIn(b'\r\n', path.read_bytes(), str(path))

    @unittest.skipUnless(os.path.exists(NODE), 'node not installed')
    def test_node_check(self):
        out = subprocess.run([NODE, '--check', str(DESK / 'notes.js')], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)


if __name__ == '__main__':
    unittest.main()
