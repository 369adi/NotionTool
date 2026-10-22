"""Isolation and render-budget guards for the NotionTool desktop app (rules: docs/DESKTOP_APP.md).

1. Desktop JS runs inside the Notion app window. It never calls the Notion API, never loads, navigates to
   or clicks open a coworker chat on its own and never opens windows or frames. The only page change it
   starts by itself is opening a Welcome chat the harness just created after a user action.
2. Coworker progress is read only by notiontool/progress_feed.py, through the harness Notion transport
   (every request takes a network budget ticket) at background priority: only busy coworkers, staggered,
   10 busy coworkers stay under 40 reads per minute.
3. The Live and progress views render at most once per second, only when their input changed, and stop
   their timers while the window is hidden.
Parts that do not exist yet are skipped with a clear message.
"""
import collections
import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, 'notiontool')
DESK = os.path.join(PKG, 'web', 'desktop')
FEED = os.path.join(PKG, 'progress_feed.py')


def read(path):
    with open(path, 'rb') as handle:
        return handle.read().decode('utf-8')


def code_of(path):
    """JS source without comments; line numbers stay the same, string contents like 'https://' stay."""
    text = re.sub(r'/\*.*?\*/', lambda m: re.sub(r'[^\n]', ' ', m.group(0)), read(path), flags=re.S)
    return re.sub(r'(^|[\s;{}(),])//[^\n]*', r'\1', text, flags=re.M)


def line_of(code, pos):
    return code.count('\n', 0, pos) + 1


def desk_files():
    return sorted(glob.glob(os.path.join(DESK, '*.js')))


def node_path():
    for path in (r'C:\Program Files\nodejs\node.exe',):
        if os.path.exists(path):
            return path
    return shutil.which('node')


def hits(patterns):
    found = []
    for path in desk_files():
        code = code_of(path)
        for pattern, label in patterns:
            for match in re.finditer(pattern, code):
                found.append('%s:%d: %s' % (os.path.basename(path), line_of(code, match.start()), label))
    return found


NETWORK = [
    (r'\bfetch\s*\(', 'fetch()'),
    (r'\bXMLHttpRequest\b', 'XMLHttpRequest'),
    (r'\bsendBeacon\s*\(', 'navigator.sendBeacon()'),
    (r'\bnew\s+WebSocket\b', 'WebSocket'),
    (r'\bnew\s+EventSource\b', 'EventSource'),
    (r'\bimportScripts\s*\(', 'importScripts()'),
]
NOTION_API = [
    (r'/api/v3\b', 'Notion API path /api/v3'),
    (r'\b(?:getThreadTranscript|runInferenceTranscript|getInferenceTranscript|loadCachedPageChunk|loadPageChunk|'
     r'syncRecordValues|saveTransactions|submitTransaction|queryCollection|getRecordValues)\b', 'Notion API method'),
]
LOADERS = [
    (r'\bwindow\.open\s*\(', 'window.open()'),
    (r'''createElement\(\s*['"](?:iframe|frame|webview|object|embed|portal)['"]''', 'frame element'),
    (r'\bhistory\.pushState\s*\(', 'history.pushState()'),
    (r'\blocation\.reload\s*\(', 'location.reload()'),
    (r'\.src\s*=\s*[^;\n]*(?:thread|/chat|notion\.so|notion\.com)', 'loading a Notion page into an element'),
]
NAVIGATE = re.compile(r'\blocation\.(?:href\s*=(?!=)|assign\s*\(|replace\s*\()|\blocation\s*=(?!=)')
THREADISH = re.compile(r'data-nt-desk-thread|chat-row|sidebarRow\(|threadFromHref|\bthread\b')
FUNC = re.compile(r'function\s*(\w*)\s*\(([^)]*)\)')


class DesktopJsIsolationTest(unittest.TestCase):
    """Coworker chats are never loaded or opened in the app window by our code."""

    def setUp(self):
        if not desk_files():
            self.skipTest('notiontool/web/desktop has no .js files yet')

    def test_no_network_calls_from_the_app_window(self):
        found = hits(NETWORK)
        self.assertEqual(found, [], 'Desktop JS must not open network connections; harness calls go through '
                                    'ctx.call and coworker reading through the harness transport:\n' + '\n'.join(found))

    def test_no_notion_api_endpoints(self):
        found = hits(NOTION_API)
        self.assertEqual(found, [], 'Desktop JS must never call the Notion API:\n' + '\n'.join(found))

    def test_never_loads_pages_or_opens_windows(self):
        found = hits(LOADERS)
        self.assertEqual(found, [], 'Desktop JS must not open windows, frames or reload/load pages:\n' + '\n'.join(found))

    def test_page_changes_only_open_a_new_welcome_chat(self):
        found = []
        for path in desk_files():
            code = code_of(path)
            for match in NAVIGATE.finditer(code):
                if 'new_welcome_chat' not in code[max(0, match.start() - 400):match.start()]:
                    found.append('%s:%d' % (os.path.basename(path), line_of(code, match.start())))
        self.assertEqual(found, [], 'Only the new Welcome chat (a user action) may change the page URL:\n'
                         + '\n'.join(found))

    def test_coworker_chats_open_only_on_a_user_click(self):
        found = []
        for path in desk_files():
            code = code_of(path)
            flat = code.replace('"', "'")
            for match in re.finditer(r'\.click\s*\(\s*\)', code):
                if not THREADISH.search(code[max(0, match.start() - 800):match.start()]):
                    continue  # not a chat row (own tabs, Notion navigation, downloads)
                funcs = list(FUNC.finditer(code, 0, match.start()))
                where = '%s:%d' % (os.path.basename(path), line_of(code, match.start()))
                if not funcs:
                    found.append(where)
                    continue
                func = funcs[-1]
                body = code[func.start():match.start()]
                before = flat[max(0, func.start() - 160):func.start()]
                name = func.group(1)
                handler = re.search(r"addEventListener\(\s*'click'\s*,\s*$", before) or (name and re.search(
                    r"addEventListener\(\s*'click'[^;]*\b%s\s*\(" % re.escape(name), flat))
                if not ('isTrusted' in body or handler):
                    found.append(where)
        self.assertEqual(found, [], 'A coworker chat row may only be clicked open from a real user click:\n'
                         + '\n'.join(found))

    def test_harness_never_navigates_or_reloads_the_app_window(self):
        # Only these modules navigate, and only their own tabs: the separate harness browser tool, the Edge
        # robots.txt transport tab and the Notion shell's own tab (once at start). Never the app window.
        # window_login.py: only the signed-out login page, once, right after the sign-in cookies were set.
        # error_page_heal.py: only Notion's own crash page ('Something went wrong ... Refresh'), max 2x after start.
        own_tabs = {'browser.py', 'edge_transport.py', 'notion_shell.py', 'window_login.py', 'error_page_heal.py'}
        found = []
        for path in sorted(glob.glob(os.path.join(PKG, '*.py'))):
            if os.path.basename(path) in own_tabs:
                continue
            text = read(path)
            for match in re.finditer(r'Page\.(?:navigate|reload)\b', text):
                found.append('%s:%d' % (os.path.basename(path), line_of(text, match.start())))
        self.assertEqual(found, [], 'The harness must never navigate or reload the app window over CDP:\n'
                         + '\n'.join(found))


class FakeStore:
    def __init__(self, tasks, workers):
        self.tasks = tasks
        self.workers = {w['id']: w for w in workers}

    def all(self, kind):
        return [dict(t) for t in self.tasks] if kind == 'tasks' else [dict(w) for w in self.workers.values()]

    def get(self, kind, ident):
        if kind == 'workers':
            return dict(self.workers[ident]) if ident in self.workers else None
        return next((dict(t) for t in self.tasks if t['id'] == ident), None)


class FakeBudget:
    def __init__(self, paused=False):
        self.paused = paused

    def status(self):
        return {'paused': self.paused, 'cooldown_seconds': 0}


class FakeAdapter:
    """Stands in for the harness Notion adapter; client().transcript() is the transport read."""

    def __init__(self, clock, paused=False):
        self.clock = clock
        self.reads = []
        self.network_budget = FakeBudget(paused)

    def client(self, account_id):
        adapter = self

        class Client:
            def transcript(self, thread, limit=None):
                from notiontool.network_budget import current_priority
                adapter.reads.append((adapter.clock(), thread, current_priority(), account_id))
                return {'patches': []}

        return Client()


def team(busy, extra=True):
    workers, tasks = [], []
    for n in range(busy):
        workers.append(dict(id='w%d' % n, purpose='worker', account_id='acc', thread_id='th-busy-%d' % n,
                            label='Coworker %d' % n))
        tasks.append(dict(id='t%d' % n, state='running', worker_id='w%d' % n, sent_at=999.0, thread_id='th-busy-%d' % n))
    if extra:
        workers += [dict(id='wi', purpose='worker', account_id='acc', thread_id='th-idle', label='Idle'),
                    dict(id='wq', purpose='worker', account_id='acc', thread_id='th-queued', label='Queued'),
                    dict(id='wm', purpose='main', account_id='acc', thread_id='th-main', label='Main')]
        tasks += [dict(id='td', state='done', worker_id='wi', sent_at=900.0, thread_id='th-idle'),
                  dict(id='tq', state='queued', worker_id='wq', thread_id='th-queued'),
                  dict(id='tm', state='running', worker_id='wm', sent_at=999.0, thread_id='th-main')]
    return FakeStore(tasks, workers)


class ProgressFeedTest(unittest.TestCase):
    """Coworker reading goes only through the harness transport, inside the network budget."""

    def setUp(self):
        if not os.path.exists(FEED):
            self.skipTest('notiontool/progress_feed.py does not exist yet')
        from notiontool import progress_feed
        self.mod = progress_feed
        self.now = [1000.0]

    def run_feed(self, store, seconds, paused=False, each=None):
        adapter = FakeAdapter(lambda: self.now[0], paused)
        feed = self.mod.ProgressFeed(store, adapter, clock=lambda: self.now[0])
        for sec in range(seconds):
            self.now[0] = 1000.0 + sec
            if each:
                each(sec, store)
            feed.tick()
        return adapter.reads

    @staticmethod
    def per_minute_max(reads):
        times = [r[0] for r in reads]
        return max((sum(1 for u in times if t <= u < t + 60) for t in times), default=0)

    def test_source_reads_only_through_the_harness_transport(self):
        src = read(FEED)
        for bad in ('devtools', 'Runtime.evaluate', 'Page.navigate', '9333', 'desktop_shell', '__ntHarness'):
            self.assertNotIn(bad, src, 'progress_feed.py must not touch the app window (%s)' % bad)
        self.assertIsNone(re.search(r'^\s*(?:import|from)\s+(?:urllib|requests|http|socket|websocket|webbrowser)\b',
                                    src, re.M), 'progress_feed.py must not open its own connections')
        self.assertIn('.client(', src)
        self.assertIn('.transcript(', src)
        self.assertIn("request_priority('background')", src)
        self.assertIn('network_budget', src)

    def test_harness_transport_takes_a_budget_ticket(self):
        edge, notion = os.path.join(PKG, 'edge_transport.py'), os.path.join(PKG, 'notion.py')
        if not (os.path.exists(edge) and os.path.exists(notion)):
            self.skipTest('edge_transport.py or notion.py does not exist yet')
        self.assertIn('self.budget.acquire(', read(edge), 'every Edge transport request must take a budget ticket')
        self.assertRegex(read(notion), r'EdgeTransport\([^\n]*network_budget',
                         'the Edge transport must get the shared network budget')

    def test_ten_busy_coworkers_stay_under_40_per_minute(self):
        reads = self.run_feed(team(10), 600)
        peak = self.per_minute_max(reads)
        self.assertLess(peak, 40, 'reads in the busiest minute: %d' % peak)
        self.assertGreaterEqual(peak, 20, 'the feed should still keep polling (busiest minute: %d)' % peak)
        for n in range(10):
            self.assertTrue(any(r[1] == 'th-busy-%d' % n and r[0] >= 1480 for r in reads),
                            'coworker %d was not read in the last two minutes' % n)

    def test_only_busy_coworkers_are_read(self):
        threads = {r[1] for r in self.run_feed(team(3), 120)}
        self.assertEqual(threads, {'th-busy-0', 'th-busy-1', 'th-busy-2'})

    def test_reads_are_staggered(self):
        reads = self.run_feed(team(10), 30)
        first = {}
        for at, thread, _, _ in reads:
            first.setdefault(thread, at)
        self.assertEqual(len(first), 10)
        self.assertEqual(len(set(first.values())), 10, 'two coworkers were first read in the same second')
        self.assertGreaterEqual(max(first.values()) - min(first.values()), 9, 'first reads are not spread out')
        per_second = collections.Counter(r[0] for r in reads)
        self.assertEqual(max(per_second.values()), 1, 'more than one read started in one second')

    def test_ended_task_is_dropped(self):
        def end_first(sec, store):
            if sec == 120:
                store.tasks[0]['state'] = 'done'
        reads = self.run_feed(team(3), 300, each=end_first)
        self.assertTrue(any(r[1] == 'th-busy-0' and r[0] < 1120 for r in reads))
        self.assertFalse(any(r[1] == 'th-busy-0' and r[0] >= 1120 for r in reads), 'an ended task was still read')

    def test_paused_budget_blocks_reads(self):
        self.assertEqual(self.run_feed(team(5), 90, paused=True), [])

    def test_reads_run_at_background_priority(self):
        from notiontool.network_budget import current_priority, request_priority
        with request_priority('background'):
            want = current_priority()
        reads = self.run_feed(team(4), 60)
        self.assertTrue(reads)
        self.assertEqual({r[2] for r in reads}, {want})


def const(code, name):
    match = re.search(r'\b%s\s*=\s*(\d+)' % re.escape(name), code)
    return int(match.group(1)) if match else None


def interval_delays(code):
    out = []
    for match in re.finditer(r'setInterval\(\s*[\w.]+\s*,\s*([\w.]+)\s*\)', code):
        arg = match.group(1)
        out.append((arg, int(arg) if arg.isdigit() else const(code, arg)))
    return out


LIVE_SCRIPT = r'''
'use strict';
var fs = require('fs'), vm = require('vm');
var NOW = 1700000000000;
Date.now = function () { return NOW; };
var created = 0, timeouts = {}, intervals = {}, nextId = 1, delays = [], warnings = [];
function El(tag) {
  var self = this;
  this.tagName = tag; this.childNodes = []; this.parentNode = null; this.attrs = {}; this.style = {};
  this.className = ''; this.textContent = ''; this.hidden = false; this.offsetWidth = 0; this.listeners = {};
  this.classList = { add: function (c) { self.className += ' ' + c; },
    remove: function (c) { self.className = self.className.split(' ').filter(function (x) { return x !== c; }).join(' '); } };
}
El.prototype.appendChild = function (c) {
  if (c.parentNode && c.parentNode.removeChild) c.parentNode.removeChild(c);
  c.parentNode = this; this.childNodes.push(c); return c;
};
El.prototype.removeChild = function (c) {
  var i = this.childNodes.indexOf(c); if (i >= 0) this.childNodes.splice(i, 1); c.parentNode = null; return c;
};
Object.defineProperty(El.prototype, 'firstChild', { get: function () { return this.childNodes[0] || null; } });
El.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
El.prototype.getAttribute = function (k) { return k in this.attrs ? this.attrs[k] : null; };
El.prototype.removeAttribute = function (k) { delete this.attrs[k]; };
El.prototype.addEventListener = function (t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); };
El.prototype.focus = function () {};
global.setTimeout = function (fn, ms) { var id = nextId++; timeouts[id] = fn; delays.push(ms || 0); return id; };
global.clearTimeout = function (id) { delete timeouts[id]; };
global.setInterval = function (fn, ms) { var id = nextId++; intervals[id] = { fn: fn, ms: ms }; return id; };
global.clearInterval = function (id) { delete intervals[id]; };
function runTimeouts() {
  Object.keys(timeouts).forEach(function (id) { var fn = timeouts[id]; delete timeouts[id]; if (fn) fn(); });
}
var winListeners = {}, docListeners = {};
function on(map) { return function (t, f) { (map[t] = map[t] || []).push(f); }; }
function off(map) { return function (t, f) { map[t] = (map[t] || []).filter(function (g) { return g !== f; }); }; }
function fire(map, t) { (map[t] || []).slice().forEach(function (f) { f({}); }); }
global.window = global;
global.addEventListener = on(winListeners);
global.removeEventListener = off(winListeners);
global.location = { hash: '', pathname: '/', search: '' };
global.history = { replaceState: function () {} };
console.warn = function () { warnings.push(Array.prototype.join.call(arguments, ' ')); };
global.document = { hidden: false, body: new El('body'),
  createElement: function (t) { created++; return new El(t); },
  createTextNode: function (t) { return { nodeType: 3, textContent: String(t), parentNode: null }; },
  querySelectorAll: function () { return []; },
  addEventListener: on(docListeners), removeEventListener: off(docListeners) };
vm.runInThisContext(fs.readFileSync(process.argv[2], 'utf8'));
var state = { runs: [{ id: 'run_zz91', state: 'executing', goal: 'Write the sales report.', created: 1 }],
  tasks: [{ id: 'task_qq77', run_id: 'run_zz91', state: 'running', title: 'Draft', summary: 'Draft the report',
    started: 1, created: 1, worker_id: 'w1' }],
  workers: [{ id: 'w1', purpose: 'worker', label: 'Cowork 1', state: 'busy', task_id: 'task_qq77' }] };
var push = null;
var view = window.__ntDesk.live({ getState: function () { return state; },
  onState: function (cb) { push = cb; return function () {}; },
  call: function () { return Promise.resolve({}); }, toast: function () {} });
var out = {};
view.open();
out.openCreated = created;
var c0 = created; delays = [];
for (var i = 0; i < 20; i++) push(state);
out.timersFor20Pushes = Object.keys(timeouts).length;
out.delays = delays.slice();
runTimeouts();
out.sameStateCreated = created - c0;
state = JSON.parse(JSON.stringify(state));
state.tasks[0].state = 'done';
var c1 = created;
push(state);
NOW += 1000;
runTimeouts();
out.changedCreated = created - c1;
out.intervalsVisible = Object.keys(intervals).length;
document.hidden = true;
fire(docListeners, 'visibilitychange');
out.intervalsHidden = Object.keys(intervals).length;
out.timeoutsHidden = Object.keys(timeouts).length;
push(state);
out.timeoutsAfterHiddenPush = Object.keys(timeouts).length;
document.hidden = false;
fire(docListeners, 'visibilitychange');
out.intervalsShownAgain = Object.keys(intervals).length;
view.stop();
out.intervalsStopped = Object.keys(intervals).length;
out.timeoutsStopped = Object.keys(timeouts).length;
out.warnings = warnings;
process.stdout.write(JSON.stringify(out) + '\n');
'''


class RenderBudgetTest(unittest.TestCase):
    """Live and progress views: at most one render per second, diffed, no timers while hidden."""

    def live(self):
        path = os.path.join(DESK, 'live.js')
        if not os.path.exists(path):
            self.skipTest('notiontool/web/desktop/live.js does not exist yet')
        return path

    def test_live_view_static_budget(self):
        code = code_of(self.live())
        self.assertGreaterEqual(const(code, 'RENDER_MS') or 0, 1000)
        self.assertIn("addEventListener('visibilitychange'", code)
        self.assertIn('document.hidden', code)
        self.assertIn('if (sigs[name] === key) return;', code, 'sections must repaint only when their input changed')
        for arg, value in interval_delays(code):
            self.assertIsNotNone(value, 'unknown interval delay %s' % arg)
            self.assertGreaterEqual(value, 1000, 'interval %s is faster than 1 s' % arg)

    def test_live_view_runtime_budget(self):
        path = self.live()
        node = node_path()
        if not node:
            self.skipTest('node is not installed')
        with tempfile.TemporaryDirectory() as tmp:
            script = os.path.join(tmp, 'live_budget.js')
            with open(script, 'w', encoding='utf-8', newline='\n') as handle:
                handle.write(LIVE_SCRIPT)
            proc = subprocess.run([node, script, path], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(out['warnings'], [])
        self.assertGreater(out['openCreated'], 0)
        self.assertEqual(out['timersFor20Pushes'], 1, '20 pushes must give one render')
        self.assertEqual(len(out['delays']), 1)
        self.assertGreaterEqual(out['delays'][0], 1000, 'next render must wait until 1 s after the last one')
        self.assertEqual(out['sameStateCreated'], 0, 'unchanged state must not repaint')
        self.assertGreater(out['changedCreated'], 0, 'changed state must repaint')
        self.assertGreaterEqual(out['intervalsVisible'], 1)
        self.assertEqual(out['intervalsHidden'], 0, 'timers must stop while the window is hidden')
        self.assertEqual(out['timeoutsHidden'], 0)
        self.assertEqual(out['timeoutsAfterHiddenPush'], 0, 'no render may be scheduled while hidden')
        self.assertGreaterEqual(out['intervalsShownAgain'], 1, 'timers must restart when shown again')
        self.assertEqual((out['intervalsStopped'], out['timeoutsStopped']), (0, 0))

    def test_progress_view_static_budget(self):
        path = os.path.join(DESK, 'progress.js')
        if not os.path.exists(path):
            self.skipTest('notiontool/web/desktop/progress.js does not exist yet')
        code = code_of(path)
        self.assertGreaterEqual(const(code, 'TICK_MS') or 0, 1000)
        delays = interval_delays(code)
        self.assertTrue(delays, 'progress.js has no interval timer')
        for arg, value in delays:
            self.assertIsNotNone(value, 'unknown interval delay %s' % arg)
            self.assertGreaterEqual(value, 1000, 'interval %s is faster than 1 s' % arg)
        self.assertIn("addEventListener('visibilitychange'", code)
        self.assertRegex(code, r'document\.hidden\)\s*\{\s*stopTimer\(\)', 'timer must stop while hidden')
        self.assertNotIn('innerHTML', code)
        self.assertIn('cardsById', code, 'cards must be reused by id (diffed), not rebuilt')


if __name__ == '__main__':
    unittest.main()
