import json, os, re, subprocess, tempfile, types, unittest
from pathlib import Path

from notiontool import desktop_shell, projects as P, projects_routes as R
from notiontool.store import Store

ROOT = Path(__file__).resolve().parent.parent
DESKTOP = ROOT / 'notiontool' / 'web' / 'desktop'
ACTIONS = ('projects_list', 'projects_detail', 'projects_create', 'projects_update', 'projects_open',
           'projects_archive', 'projects_unarchive')


def git(repo, *args):
    subprocess.run(['git', '-c', 'user.name=Tester', '-c', 'user.email=t@example.com', '-c', 'commit.gpgsign=false',
                    '-C', str(repo), *args], check=True, capture_output=True)


class ProjectRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(os.path.join(self.temp.name, 'db'))
        self.app = types.SimpleNamespace(store=self.store)
        for number, ident in enumerate(('c1', 'c2', 'c3'), 1):
            self.store.put('chats', dict(id=ident, title='Main %d' % number, main_number=number, state='active'))
        self.store.put('chats', dict(id='c9', title='Gone', main_number=9, state='released'))
        self.store.put('workers', dict(id='w1', label='Coworker 1', state='ready'))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def get(self, path='/api/ui/projects', **query):
        return R.http_route(self.app, 'GET', path, {k: [str(v)] for k, v in query.items()})

    def post(self, path, body):
        return R.http_route(self.app, 'POST', path, None, body)

    def make(self, name='Alpha', **fields):
        status, payload = self.post('/api/ui/projects', dict(name=name, **fields))
        self.assertEqual(status, 200)
        return payload['project']

    def test_matches_only_the_projects_prefix(self):
        self.assertTrue(R.matches('/api/ui/projects'))
        self.assertTrue(R.matches('/api/ui/projects/p-1'))
        self.assertFalse(R.matches('/api/ui/projectsx'))
        self.assertFalse(R.matches('/api/notes'))

    def test_list_empty_offers_free_chats_without_released_ones(self):
        status, payload = self.get()
        self.assertEqual(status, 200)
        self.assertEqual((payload['projects'], payload['current_project_id']), ([], ''))
        self.assertEqual([c['id'] for c in payload['chats']], ['c1', 'c2', 'c3'])
        self.assertTrue(all(c['project_id'] == '' for c in payload['chats']))

    def test_create_returns_view_with_chat_titles(self):
        p = self.make('  Alpha ', goal='Ship', repo_path='C:/r', main_chat_id='c1', chat_ids=['c2'])
        self.assertEqual((p['name'], p['goal'], p['repo_path'], p['status'], p['archived']), ('Alpha', 'Ship', 'C:/r', 'active', False))
        self.assertEqual(p['chat_ids'], ['c1', 'c2'])
        self.assertEqual(p['main_chat_title'], 'Main 1')
        self.assertEqual([(c['id'], c['main'], c['missing']) for c in p['chats']], [('c1', True, False), ('c2', False, False)])
        owners = {c['id']: c['project_id'] for c in self.get()[1]['chats']}
        self.assertEqual((owners['c1'], owners['c2'], owners['c3']), (p['id'], p['id'], ''))

    def test_create_errors(self):
        with self.assertRaises(P.ProjectError):
            self.post('/api/ui/projects', {'name': '   '})
        with self.assertRaises(P.ProjectError):
            self.post('/api/ui/projects', {'name': 'X', 'chat_ids': 'c1'})
        self.make('Alpha', main_chat_id='c1')
        with self.assertRaises(P.ProjectError):
            self.post('/api/ui/projects', {'name': 'Beta', 'chat_ids': ['c1']})
        self.assertEqual(len(self.get()[1]['projects']), 1)

    def test_list_orders_by_update_and_hides_archived_by_default(self):
        a = self.make('Alpha')
        b = self.make('Beta')
        self.post('/api/ui/projects/update', {'id': a['id'], 'goal': 'newer'})
        self.assertEqual([p['name'] for p in self.get()[1]['projects']], ['Alpha', 'Beta'])
        self.post('/api/ui/projects/archive', {'id': b['id']})
        self.assertEqual([p['name'] for p in self.get()[1]['projects']], ['Alpha'])
        shown = self.get(archived=1)[1]['projects']
        self.assertEqual({p['name']: p['archived'] for p in shown}, {'Alpha': False, 'Beta': True})

    def test_update_changes_given_fields_and_links_chats(self):
        p = self.make('Alpha', main_chat_id='c1')
        status, payload = self.post('/api/ui/projects/update', {'id': p['id'], 'name': 'Gamma', 'chat_ids': ['c3'], 'main_chat_id': 'c3'})
        self.assertEqual(status, 200)
        u = payload['project']
        self.assertEqual((u['name'], u['main_chat_id'], u['goal']), ('Gamma', 'c3', ''))
        self.assertEqual(u['chat_ids'], ['c3'])
        with self.assertRaises(P.ProjectError):
            self.post('/api/ui/projects/update', {'id': p['id'], 'name': ' '})
        with self.assertRaises(P.ProjectError):
            self.post('/api/ui/projects/update', {'id': 'p-missing', 'goal': 'x'})
        self.assertEqual(self.get()[1]['projects'][0]['name'], 'Gamma')

    def test_open_sets_current_and_archive_clears_it(self):
        p = self.make('Alpha')
        status, payload = self.post('/api/ui/projects/open', {'id': p['id']})
        self.assertEqual((status, payload['current_project_id']), (200, p['id']))
        self.assertEqual(self.get()[1]['current_project_id'], p['id'])
        status, payload = self.post('/api/ui/projects/archive', {'id': p['id']})
        self.assertEqual((payload['project']['archived'], payload['current_project_id']), (True, ''))
        with self.assertRaises(P.ProjectError):
            self.post('/api/ui/projects/open', {'id': p['id']})
        with self.assertRaises(P.ProjectError):
            self.post('/api/ui/projects/open', {})

    def test_unarchive_restores_and_is_idempotent(self):
        p = self.make('Alpha', main_chat_id='c1')
        self.post('/api/ui/projects/archive', {'id': p['id']})
        status, payload = self.post('/api/ui/projects/unarchive', {'id': p['id']})
        self.assertEqual((status, payload['project']['status'], payload['project']['archived']), (200, 'active', False))
        again = self.post('/api/ui/projects/unarchive', {'id': p['id']})[1]['project']
        self.assertEqual(again['status'], 'active')

    def test_unarchive_refuses_a_chat_taken_meanwhile(self):
        a = self.make('Alpha', main_chat_id='c1')
        self.post('/api/ui/projects/archive', {'id': a['id']})
        self.make('Beta', main_chat_id='c1')
        with self.assertRaises(P.ProjectError):
            self.post('/api/ui/projects/unarchive', {'id': a['id']})
        self.assertEqual(P.get_project(self.store, a['id'])['status'], 'archived')

    def test_detail_has_commits_and_checks_the_id(self):
        repo = Path(self.temp.name) / 'repo'
        repo.mkdir()
        git(repo, 'init')
        (repo / 'a.txt').write_text('one')
        git(repo, 'add', '.')
        git(repo, 'commit', '-m', 'First commit')
        (repo / 'a.txt').write_text('two')
        git(repo, 'commit', '-am', 'Second commit')
        p = self.make('Alpha', repo_path=str(repo))
        status, payload = self.get('/api/ui/projects/' + p['id'], n=1)
        self.assertEqual(status, 200)
        commits = payload['project']['commits']
        self.assertEqual([c['subject'] for c in commits], ['Second commit'])
        self.assertEqual(set(commits[0]), {'sha', 'subject', 'author', 'date'})
        self.assertEqual(self.get('/api/ui/projects/' + p['id'])[1]['project']['commits'][1]['subject'], 'First commit')
        with self.assertRaises(P.ProjectError):
            self.get('/api/ui/projects/p-missing')
        with self.assertRaises(P.ProjectError):
            self.get('/api/ui/projects/' + p['id'], n='many')

    def test_detail_without_repo_has_no_commits(self):
        p = self.make('Alpha')
        self.assertEqual(self.get('/api/ui/projects/' + p['id'])[1]['project']['commits'], [])

    def test_methods_and_unknown_routes(self):
        self.assertEqual(R.http_route(self.app, 'DELETE', '/api/ui/projects', None, None)[0], 405)
        self.assertEqual(self.post('/api/ui/projects/nothing', {})[0], 404)

    def link_threads(self):
        self.store.put('workers', dict(id='w2', label='Coworker 2', state='busy', thread_id='aaaa-bbbb-1111'))
        self.store.put('chats', dict(id='c1', title='Main 1', main_number=1, state='active', main_worker='w2'))
        self.store.put('chats', dict(id='c2', title='Main 2', main_number=2, state='active', thread_id='cccc-2222'))

    def test_chats_carry_the_thread_id_for_the_sidebar(self):
        self.link_threads()
        p = self.make('Alpha', main_chat_id='c1', chat_ids=['c2', 'c3'])
        threads = {c['id']: c['thread_id'] for c in p['chats']}
        self.assertEqual(threads, {'c1': 'aaaa-bbbb-1111', 'c2': 'cccc-2222', 'c3': ''})
        self.assertEqual(p['main_thread_id'], 'aaaa-bbbb-1111')
        listed = self.get()[1]['projects'][0]
        self.assertEqual({c['id']: c['thread_id'] for c in listed['chats']}, threads)
        shown = self.get('/api/ui/projects/' + p['id'])[1]['project']
        self.assertEqual(shown['main_thread_id'], 'aaaa-bbbb-1111')

    def test_open_answers_with_chats_so_the_main_chat_can_be_shown(self):
        self.link_threads()
        p = self.make('Alpha', main_chat_id='c2')
        project = self.post('/api/ui/projects/open', {'id': p['id']})[1]['project']
        main = [c for c in project['chats'] if c['main'] and not c['missing']]
        self.assertEqual([(c['id'], c['thread_id']) for c in main], [('c2', 'cccc-2222')])

    def test_chat_without_a_record_is_missing_and_has_no_thread(self):
        p = self.make('Alpha', chat_ids=['c1'])
        self.store.put('chats', dict(id='c1', title='Main 1', main_number=1, state='active'))
        shown = self.get('/api/ui/projects/' + p['id'])[1]['project']
        self.assertEqual([(c['thread_id'], c['missing']) for c in shown['chats']], [('', False)])
        self.assertEqual(shown['main_thread_id'], '')

    def test_unlink_a_chat_and_clear_the_main_chat_in_one_update(self):
        p = self.make('Alpha', main_chat_id='c1', chat_ids=['c2'])
        u = self.post('/api/ui/projects/update', {'id': p['id'], 'chat_ids': ['c2'], 'main_chat_id': ''})[1]['project']
        self.assertEqual((u['chat_ids'], u['main_chat_id'], u['main_chat_title']), (['c2'], '', ''))
        freed = {c['id']: c['project_id'] for c in self.get()[1]['chats']}
        self.assertEqual((freed['c1'], freed['c2']), ('', p['id']))

    def test_archived_project_is_listed_only_with_the_archived_flag_and_comes_back(self):
        p = self.make('Alpha')
        self.post('/api/ui/projects/archive', {'id': p['id']})
        self.assertEqual(self.get()[1]['projects'], [])
        self.assertEqual([x['archived'] for x in self.get(archived=1)[1]['projects']], [True])
        self.post('/api/ui/projects/unarchive', {'id': p['id']})
        self.assertEqual([x['archived'] for x in self.get()[1]['projects']], [False])

    def test_update_without_id_and_unarchive_of_unknown_id_fail(self):
        with self.assertRaises(P.ProjectError):
            self.post('/api/ui/projects/update', {'name': 'X'})
        with self.assertRaises(P.ProjectError):
            self.post('/api/ui/projects/unarchive', {'id': 'p-missing'})
        with self.assertRaises(P.ProjectError):
            self.post('/api/ui/projects/archive', {})


class ProjectWiringTests(unittest.TestCase):
    def test_server_dispatches_and_stays_crlf(self):
        raw = (ROOT / 'notiontool' / 'server.py').read_bytes()
        self.assertEqual(raw.count(b'\n'), raw.count(b'\r\n'))
        self.assertEqual(raw.count(b'from .projects_routes import http_route as projects_http_route'), 1)
        self.assertIn(b"path=='/api/ui/projects'", raw)

    def test_part_and_actions_are_registered(self):
        for seq in (desktop_shell.PART_FILES, desktop_shell.MOUNT_ORDER):
            self.assertEqual(seq.count('projects'), 1)
            self.assertGreater(seq.index('projects'), seq.index('shell'))
        for name in ACTIONS:
            self.assertIn(name, desktop_shell.DESK_ACTIONS)
        self.assertEqual(desktop_shell.PROJECTS_PATH, '/api/ui/projects')
        self.assertTrue((DESKTOP / 'projects.js').is_file() and (DESKTOP / 'projects.css').is_file())

    def test_handlers_map_to_the_routes(self):
        calls = []

        def host(method, path, body=None):
            calls.append((method, path, body))
            return {'ok': True}
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store = Store(os.path.join(tmp, 'db'))
            try:
                server = types.SimpleNamespace(store=store, action=lambda path, body: {}, engine=None)
                handlers = desktop_shell.desktop_handlers(server, tmp, host_call=host)
            finally:
                store.close()
        for name in ACTIONS:
            self.assertIn(name, handlers)
        handlers['projects_list']({'archived': True})
        handlers['projects_detail']({'id': 'p 1', 'n': 5})
        handlers['projects_create']({'name': 'A', 'goal': 'g', 'chat_ids': ['c1']})
        handlers['projects_update']({'id': 'p1', 'name': 'B'})
        for name in ('open', 'archive', 'unarchive'):
            handlers['projects_' + name]({'id': 'p1'})
        self.assertEqual(calls[0], ('GET', '/api/ui/projects?archived=1', None))
        self.assertEqual(calls[1][:2], ('GET', '/api/ui/projects/p%201?n=5'))
        self.assertEqual(calls[2][:2], ('POST', '/api/ui/projects'))
        self.assertEqual(calls[2][2]['name'], 'A')
        self.assertEqual([c[1] for c in calls[3:]], ['/api/ui/projects/update', '/api/ui/projects/open',
                                                    '/api/ui/projects/archive', '/api/ui/projects/unarchive'])
        with self.assertRaises(ValueError):
            handlers['projects_open']({})

    def test_assets_use_tokens_only_and_no_own_network(self):
        css = (DESKTOP / 'projects.css').read_text(encoding='utf-8')
        js = (DESKTOP / 'projects.js').read_text(encoding='utf-8')
        for text in (css, js):
            self.assertIsNone(re.search(r'#[0-9a-fA-F]{3,8}\b', text))
        for banned in ('innerHTML', 'fetch(', 'XMLHttpRequest', 'WebSocket', 'EventSource'):
            self.assertNotIn(banned, js)
        for action in ACTIONS:
            self.assertIn("'" + action + "'", js)

    def test_ui_flows_use_dialogs_confirmations_and_the_chat_row_hint(self):
        js = (DESKTOP / 'projects.js').read_text(encoding='utf-8')
        for needle in ("role: 'dialog'", "'aria-modal'", 'function openCreate', 'function confirmArchive', 'function openChat',
                       'data-nt-desk-thread', "'Escape'", "'1.75'", 'function stateLabel'):
            self.assertIn(needle, js)
        self.assertNotIn("mode = 'create'", js)
        self.assertNotIn("'Details'", js)
        self.assertNotIn('.click()', js)
        css = (DESKTOP / 'projects.css').read_text(encoding='utf-8')
        for cls in ('.nt-prj-scrim', '.nt-prj-dialog', '.nt-prj-dialog-error'):
            self.assertIn(cls, css)

    def test_js_parses_when_node_is_available(self):
        try:
            done = subprocess.run(['node', '--check', str(DESKTOP / 'projects.js')], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            self.skipTest('node is not available')
        self.assertEqual(done.returncode, 0, done.stderr)


# Runs projects.js in node with a small fake DOM and presses Esc in the situations the Projects page handles.
ESC_HARNESS = r'''
const fs = require('fs'); const vm = require('vm');
const jsPath = process.argv[2];
let doc;
function parse(sel) {
  return sel.split(',').map(s => s.trim()).filter(Boolean).map(s => {
    const t = { tag: null, cls: [], attrs: [] }; let m;
    const tm = /^[a-zA-Z][\w-]*/.exec(s); if (tm) { t.tag = tm[0].toUpperCase(); s = s.slice(tm[0].length); }
    const re = /\.([\w-]+)|\[([\w-]+)(?:="([^"]*)")?\]/g;
    while ((m = re.exec(s))) { if (m[1]) t.cls.push(m[1]); else t.attrs.push([m[2], m[3]]); }
    return t;
  });
}
function matches(n, sel) {
  return parse(sel).some(t => (!t.tag || n.tagName === t.tag) && t.cls.every(c => n.classList.contains(c)) &&
    t.attrs.every(([k, v]) => { const a = n.getAttribute(k); return a !== null && (v === undefined || a === v); }));
}
class El {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase(); this.nodeType = 1; this.attrs = {}; this.children = []; this.parentNode = null;
    this.listeners = {}; this._text = ''; this.value = ''; const s = [];
    this.classList = { _s: s, contains: c => s.indexOf(c) >= 0 };
  }
  get className() { return this.classList._s.join(' '); }
  set className(v) { const s = this.classList._s; s.splice(0); String(v).split(/\s+/).filter(Boolean).forEach(c => s.push(c)); }
  setAttribute(k, v) { if (k === 'class') this.className = v; else this.attrs[k] = String(v); }
  getAttribute(k) { if (k === 'class') return this.className; return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; }
  appendChild(c) { if (c.parentNode) c.parentNode.removeChild(c); c.parentNode = this; this.children.push(c); return c; }
  removeChild(c) { const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); c.parentNode = null; return c; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  set textContent(v) { this.children.forEach(c => { c.parentNode = null; }); this.children = []; this._text = String(v); }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  removeEventListener(t, f) { const a = this.listeners[t] || []; const i = a.indexOf(f); if (i >= 0) a.splice(i, 1); }
  contains(n) { while (n) { if (n === this) return true; n = n.parentNode; } return false; }
  querySelectorAll(sel) { const out = []; (function walk(n) { n.children.forEach(k => { if (matches(k, sel)) out.push(k); walk(k); }); })(this); return out; }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  focus() { doc.activeElement = this; }
  select() {}
}
doc = new El('#document');
doc.body = doc.appendChild(new El('body'));
doc.activeElement = doc.body; doc.hidden = false;
doc.createElement = t => new El(t);
doc.createElementNS = (ns, t) => new El(t);
doc.createTextNode = t => { const n = new El('#text'); n.nodeType = 3; n._text = String(t); return n; };
doc.addEventListener = () => {}; doc.removeEventListener = () => {};
doc.contains = n => { while (n) { if (n === doc) return true; n = n.parentNode; } return false; };

const winL = {};
const win = { addEventListener(t, f) { (winL[t] = winL[t] || []).push(f); },
  removeEventListener(t, f) { const a = winL[t] || []; const i = a.indexOf(f); if (i >= 0) a.splice(i, 1); } };
const sandbox = { window: win, document: doc, console, setTimeout, clearTimeout, setInterval, clearInterval, Date, Math, JSON };
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(jsPath, 'utf8'), sandbox);

const sleep = ms => new Promise(r => setTimeout(r, ms));
const calls = [];
const P1 = { id: 'p1', name: 'Alpha', goal: 'Ship', repo_path: '', archived: false, chat_ids: [], coworker_ids: [], chats: [],
  coworkers: [], commits: [], updated: Date.now() / 1000 };
const ctx = { toast() {}, call(a) {
  calls.push(a);
  if (a === 'projects_list') return Promise.resolve({ projects: [P1], chats: [], current_project_id: '' });
  return Promise.resolve({ project: P1, current_project_id: '' });
} };
const host = new El('div'); doc.body.appendChild(host);
const title = () => host.querySelector('.nt-prj-title').textContent;
const dialog = () => !!host.querySelector('.nt-prj-dialog');
const btn = label => host.querySelectorAll('button').filter(b => b.textContent === label)[0];
const click = n => (n.listeners.click || []).slice().forEach(f => f({ target: n, currentTarget: n, stopPropagation() {}, preventDefault() {} }));
const esc = o => {
  const ev = Object.assign({ key: 'Escape', defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, stopPropagation() {} }, o);
  (winL.keydown || []).slice().forEach(f => f(ev));
};
const field = () => host.querySelector('[aria-label="Project name"]');

(async () => {
  const out = {};
  const handle = win.__ntDesk.projects(ctx);
  win.__ntShellQueue[0][1].mount(host);
  await sleep(30);
  out.list = [title(), host.querySelectorAll('.nt-prj-card').length];

  // Esc closes the dialog first, even while typing in its name field
  click(btn('New project'));
  out.dialogOpen = [dialog(), doc.activeElement.tagName];
  esc({ target: doc.activeElement });
  out.afterDialogEsc = [dialog(), title()];

  // detail
  click(host.querySelector('.nt-prj-card'));
  await sleep(30);
  out.detail = title();

  // typing in a detail field: Esc changes nothing
  const name = field();
  name.value = 'Edited'; name.focus();
  esc({ target: name });
  out.typingTarget = [title(), field() === name, name.value];
  esc({ target: undefined });
  out.typingActive = [title(), field() === name, name.value];

  // modifiers, handled and composing events do not leave the detail either
  const open = btn('Open'); open.focus();
  out.keep = [{ ctrlKey: true }, { altKey: true }, { shiftKey: true }, { metaKey: true }, { defaultPrevented: true }, { isComposing: true }]
    .map(o => { esc(Object.assign({ target: open }, o)); return title(); });

  // a dialog opened from the detail closes first and the detail stays
  click(btn('Archive'));
  out.confirmOpen = dialog();
  esc({});
  out.afterConfirmEsc = [dialog(), title()];

  // a plain Esc on a focused button goes back to the list
  esc({ target: doc.activeElement });
  await sleep(30);
  out.back = [title(), host.querySelectorAll('.nt-prj-card').length];
  out.calls = calls;
  handle.stop();
  console.log(JSON.stringify(out));
  process.exit(0);
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
'''


class ProjectsEscKeyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            script = Path(tmp) / 'harness.js'
            script.write_text(ESC_HARNESS, encoding='utf-8')
            try:
                done = subprocess.run(['node', str(script), str(DESKTOP / 'projects.js')], capture_output=True,
                                      text=True, encoding='utf-8', timeout=60)
            except (OSError, subprocess.SubprocessError):
                raise unittest.SkipTest('node is not available')
        if done.returncode:
            raise AssertionError(done.stderr)
        cls.out = json.loads(done.stdout.strip().splitlines()[-1])

    def test_esc_closes_the_dialog_first(self):
        self.assertEqual(self.out['list'], ['Projects', 1])
        self.assertEqual(self.out['dialogOpen'], [True, 'INPUT'])
        self.assertEqual(self.out['afterDialogEsc'], [False, 'Projects'])

    def test_esc_goes_from_detail_to_list(self):
        self.assertEqual(self.out['detail'], 'Alpha')
        self.assertEqual(self.out['back'], ['Projects', 1])
        self.assertIn('projects_detail', self.out['calls'])

    def test_esc_while_typing_in_a_detail_field_changes_nothing(self):
        self.assertEqual(self.out['typingTarget'], ['Alpha', True, 'Edited'])
        self.assertEqual(self.out['typingActive'], ['Alpha', True, 'Edited'])

    def test_esc_needs_a_plain_unhandled_key(self):
        self.assertEqual(self.out['keep'], ['Alpha'] * 6)

    def test_esc_closes_a_dialog_opened_from_the_detail_and_stays(self):
        self.assertTrue(self.out['confirmOpen'])
        self.assertEqual(self.out['afterConfirmEsc'], [False, 'Alpha'])


if __name__ == '__main__':
    unittest.main()
