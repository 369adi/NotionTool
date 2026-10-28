"""Static checks for the desktop sidebar overlay (notiontool/web/desktop/overlay.js and overlay.css)."""
import json
import os
import re
import tempfile
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'notiontool' / 'web' / 'desktop'
JS_PATH = ASSETS / 'overlay.js'
CSS_PATH = ASSETS / 'overlay.css'
NODE = 'C:/Program Files/nodejs/node.exe'


def css_rule(css, selector):
    """Body of the first rule whose selector list is exactly `selector` (comments stripped)."""
    css = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
    for sel, body in re.findall(r'([^{}]+)\{([^{}]*)\}', css):
        if ' '.join(sel.split()) == selector:
            return body
    return None


class DesktopSidebarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js_bytes = JS_PATH.read_bytes()
        cls.css_bytes = CSS_PATH.read_bytes()
        cls.js = cls.js_bytes.decode('utf-8')
        cls.css = cls.css_bytes.decode('utf-8')

    def test_files_are_lf_without_bom(self):
        for data in (self.js_bytes, self.css_bytes, Path(__file__).read_bytes()):
            self.assertNotIn(b'\r', data)
            self.assertFalse(data.startswith(b'\xef\xbb\xbf'))

    def test_registers_sidebar_part(self):
        self.assertIn('window.__ntDesk', self.js)
        self.assertIn('window.__ntDesk = window.__ntDesk || {};', self.js)
        self.assertIn('.sidebar =', self.js)
        self.assertIn('stop: function', self.js)
        self.assertTrue(self.js.lstrip().startswith('/*'))
        self.assertIn('(function () {', self.js)
        self.assertTrue(self.js.rstrip().endswith('})();'))

    def test_filter_uses_state_and_observer(self):
        for text in ('MutationObserver', 'hidden_threads', 'mains', 'app_chat_filter', "'hide_coworkers'",
                     'nt-desk-hidden', 'data-nt-desk', 'ctx.onState', 'ctx.getState()', '250'):
            self.assertIn(text, self.js)
        self.assertEqual(self.js.count('new MutationObserver('), 1)

    def test_filter_decision_unchanged(self):
        self.assertIn('if (hidden.indexOf(key) >= 0) return true;', self.js)
        self.assertIn("return mains.indexOf(key) < 0 && state.app_chat_filter !== 'hide_coworkers';", self.js)
        self.assertIn('row.classList.toggle(CLS_HIDDEN, decide(state, key));', self.js)

    def test_new_chat_routed_to_welcome_chat(self):
        self.assertIn('new_welcome_chat', self.js)
        self.assertIn("ctx.call('new_welcome_chat', {})", self.js)
        self.assertIn('New chat', self.js)
        self.assertIn('Neuer Chat', self.js)
        self.assertIn("'new chat'", self.js)
        self.assertIn("'neuer chat'", self.js)
        self.assertIn('preventDefault', self.js)
        self.assertIn('stopPropagation', self.js)
        self.assertIn("document.addEventListener('click', onClick, true);", self.js)
        # Round 28 (I71): no page load; the new chat opens through its own sidebar row.
        self.assertNotIn('location.href = result.url', self.js)
        self.assertIn('openChat(threadKey(result.thread_id), result.url)', self.js)
        self.assertIn("'Could not create a Welcome chat: '", self.js)

    def test_never_removes_notion_nodes_or_uses_network(self):
        for text in ('.remove()', 'fetch(', 'XMLHttpRequest', 'WebSocket',
                     '127.0.0.1', 'Bearer', 'localhost', 'innerHTML', 'outerHTML', 'insertAdjacentHTML',
                     'nodeValue =', 'textContent = \'Mains\''):
            self.assertNotIn(text, self.js)
        # The single removal is dropOwn, guarded so it only touches our own inserted nodes.
        self.assertEqual(self.js.count('removeChild'), 1)
        # One interval only: the 5 s sidebar watchdog (empty Mains list after a PC restart), cleared in stop().
        self.assertEqual(self.js.count('setInterval('), 1)
        self.assertIn('watchdog = setInterval(', self.js)
        self.assertIn('clearInterval(watchdog)', self.js)
        self.assertIn('if (isOwn(node) && node.parentNode) node.parentNode.removeChild(node);', self.js)
        self.assertIn("node.setAttribute('data-nt-desk-own', '1');", self.js)

    def test_branded_header(self):
        self.assertIn("'data-nt-desk': 'brand'", self.js)
        self.assertIn("text: 'NotionTool'", self.js)
        self.assertIn('CLS_BRAND_MARK', self.js)
        self.assertIn('root.insertBefore(brand, root.firstChild);', self.js)
        mark = css_rule(self.css, '.nt-desk-brand-mark')
        self.assertIsNotNone(mark)
        self.assertIn('linear-gradient', mark)
        self.assertIn('#7c5cff', mark)
        self.assertIn('#22d3ee', mark)

    def test_mains_heading_overlay(self):
        for text in ("'notion ai'", "'notion-ki'", "text: 'Mains'", "'heading-orig'",
                     'block.classList.add(CLS_HIDDEN);'):
            self.assertIn(text, self.js)
        self.assertIsNotNone(css_rule(self.css, '.nt-desk-heading'))

    def test_main_rows_styled(self):
        for text in ('CLS_MAIN_ROW', 'CLS_ACTIVE', "text: 'Operator'", 'main.operator', 'aria-selected',
                     'aria-current', 'attributeFilter'):
            self.assertIn(text, self.js)
        row = css_rule(self.css, '.nt-desk-main-row')
        self.assertIn('border-radius: var(--nt-radius, 12px)', row)
        self.assertIn('box-shadow', css_rule(self.css, '.nt-desk-main-row:hover'))
        bar = css_rule(self.css, '.nt-desk-main-row.nt-desk-active::before')
        self.assertIn('left: 0', bar)
        self.assertIn('width: 3px', bar)
        self.assertIn('border-radius: 999px', css_rule(self.css, '.nt-desk-pill'))

    def test_clutter_hidden_except_main_rows(self):
        for text in ("'new agent'", "'neuer agent'", 'DATE_GROUP_RE', 'today', 'yesterday', 'heute', 'gestern',
                     "'clutter'", '[data-nt-desk="chat-row"]'):
            self.assertIn(text, self.js)
        clutter = self.js[self.js.index('function applyClutter'):self.js.index('// ---- sidebar filter')]
        self.assertGreaterEqual(clutter.count('chat-row'), 3)

    def test_css_uses_theme_variables_with_fallbacks(self):
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        uses = re.findall(r'var\(\s*(--[\w-]+)\s*(,)?', css)
        self.assertTrue(uses)
        for name, comma in uses:
            self.assertTrue(name.startswith('--nt-'), name)
            self.assertEqual(comma, ',', name + ' has no fallback')
        for name in ('--nt-surface', '--nt-raised', '--nt-radius', '--nt-accent-gradient'):
            self.assertIn(name, css)

    def test_js_classes_use_prefix(self):
        for name in re.findall(r"CLS_[A-Z_]+ = '([^']+)'", self.js):
            self.assertTrue(name.startswith('nt-desk-'), name)

    def test_css_classes_use_prefix(self):
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        selectors = ' '.join(re.findall(r'([^{}]+)\{', css))
        classes = re.findall(r'\.([A-Za-z_][\w-]*)', selectors)
        self.assertTrue(classes)
        for name in classes:
            self.assertTrue(name.startswith('nt-desk-'), name)
        self.assertIn('.nt-desk-hidden { display: none !important; }', self.css)
        self.assertIn('.nt-desk-newchat', self.css)

    def test_ui_text_is_english(self):
        for text in ('Notion-KI', 'Neuer Agent', 'Heute'):
            # German labels only appear as lower-case match keys, never as shown text.
            self.assertNotIn("text: '" + text, self.js)
        for text in re.findall(r"text: '([^']*)'", self.js):
            self.assertRegex(text, r'^[A-Za-z ]+$')

    def section(self, start, end):
        return self.js[self.js.index(start):self.js.index(end)]

    def test_new_agent_tile_detected_on_any_element(self):
        finder = self.section('function labelTargets(root, labels)', 'function tileBlock')
        self.assertIn('NodeFilter.SHOW_TEXT', finder)
        self.assertIn('depth < LABEL_DEPTH', finder)  # split labels match on an ancestor
        self.assertIn('norm(cur.textContent)', finder)
        self.assertIn('label.length > MAX_LABEL', finder)
        self.assertIn("[data-nt-desk-own]", finder)
        self.assertNotIn('CONTROL_SELECTOR', finder)  # any element type, not only buttons/links
        tile = self.section('function tileBlock(node, root)', '// ---- branding')
        self.assertIn("!tile.parentElement.querySelector('a[href]')", tile)
        self.assertIn('tile.parentElement !== root', tile)
        clutter = self.section('function applyClutter', 'function isShell')
        self.assertIn('labelTargets(root, NEW_AGENT_LABELS)', clutter)
        self.assertIn('var box = tileBlock(hit, root);', clutter)
        self.assertIn('box.classList.add(CLS_HIDDEN);', clutter)
        self.assertIn('NEW_AGENT_LABELS.indexOf(visibleLabel(control))', clutter)  # control path kept
        self.assertIn("var NEW_AGENT_LABELS = ['new agent', 'neuer agent'];", self.js)
        hidden = css_rule(self.css, '.nt-desk-hidden')
        self.assertIn('display: none !important', hidden)

    def test_empty_wrappers_collapse(self):
        shell = self.section('function isShell(node)', 'function collapseEmpty')
        self.assertIn('kid.nodeType === 3 && kid.nodeValue.trim()', shell)
        self.assertIn('!kid.classList.contains(CLS_HIDDEN)', shell)
        collapse = self.section('function uncollapse(root)', '// ---- sidebar filter')
        self.assertIn("'[data-nt-desk=\"collapsed\"]'", collapse)
        self.assertIn('old[i].classList.remove(CLS_HIDDEN);', collapse)  # recomputed every run
        self.assertIn("cur.setAttribute('data-nt-desk', 'collapsed');", collapse)
        self.assertIn('cur !== root', collapse)
        self.assertIn('!isOwn(cur)', collapse)
        run = self.section('function run()', 'function schedule')
        self.assertLess(run.index('uncollapse(root);'), run.index('applyClutter(root);'))
        self.assertLess(run.index('applyClutter(root);'), run.index('applyRows(state, root);'))
        self.assertLess(run.index('applyRows(state, root);'), run.index('collapseEmpty(root);'))
        group = css_rule(self.css, '.nt-desk-chat-group')
        self.assertIn('padding-top: 0 !important', group)
        self.assertIn('padding-bottom: 0 !important', group)
        self.assertIn('CLS_GROUP', self.js[self.js.index('var OUR_CLASSES'):self.js.index('var THREAD_ID_RE')])

    def run_node(self, body):
        if not os.path.exists(NODE):
            self.skipTest('node is not installed at ' + NODE)
        consts = '\n'.join(line for line in self.js.split('\n')
                           if re.match(r'\s*var (THREAD_RE|THREAD_ID_RE|SPACE_PARAM_RE|FIBER_DEPTH) =', line))
        source = '\n'.join([consts,
                            self.section('function norm(text)', 'function message(err)'),
                            self.section('function list(value)', 'function visibleLabel'),
                            self.section('function decide(state, key)', 'function isActive('),
                            body])
        with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as handle:
            handle.write(source)
        try:
            done = subprocess.run([NODE, handle.name], capture_output=True, text=True, timeout=40)
        finally:
            os.unlink(handle.name)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    # Live shapes (CDP read, 2026-10-19): rows carry no href; the only sidebar link is the agent
    # tile '/ai?spaceId=72fa1b13-...'; the row thread id sits in React props threadStore.id; the
    # state titles are harness labels ('Operator Main') while Notion shows 'Main'.
    LIVE = r"""
    var D = '3fea1b13-e54d-805c-bc47-00a9af92c91f', U = '3fea1b13e54d805cbc4700a9af92c91f';
    var C = '3fda1b13-e54d-801c-b8c3-00a9ada51ec3', CU = '3fda1b13e54d801cb8c300a9ada51ec3';
    var OTHER = '3fda1b13-e54d-8061-acf1-00a9d4252286';
    var SPACE = '72fa1b13-e54d-81ec-9fd4-0003d8c35ba0';
    var MAINS = [{ title: 'Operator Main', thread_id: D, operator: true },
                 { title: 'Main 4', thread_id: '3fda1b13-e54d-80f7-88fc-00a9b1c2e514', operator: false }];
    var live = { connected: true, app_chat_filter: 'mains_only', hidden_threads: [C], mains: MAINS };
    var flat = { connected: true, app_chat_filter: 'mains_only', hidden_threads: [CU],
                 mains: [{ title: 'x', thread_id: U, operator: true }] };
    var show = { connected: true, app_chat_filter: 'hide_coworkers', hidden_threads: [C], mains: MAINS };
    function fiberRow(id) {
      var f = { memoizedProps: { icon: { props: { threadStore: { id: id, _pointer: { id: id, spaceId: SPACE } } } } },
                return: null };
      for (var i = 0; i < 5; i++) f = { memoizedProps: { children: null }, return: f };
      return { '__reactFiber$abc': f, '__reactProps$abc': {} };
    }
    """

    def test_thread_id_formats_with_and_without_dashes(self):
        out = self.run_node(self.LIVE + r"""
        console.log(JSON.stringify({
          dashed: threadFromHref('/chat?t=' + D), flat: threadFromHref('/chat?t=' + U),
          upper: threadFromHref('/chat?t=' + D.toUpperCase()), path: threadFromHref('/chat/' + D + '?x=1'),
          spaceOnly: threadFromHref('/ai?spaceId=' + SPACE),
          spaceAndThread: threadFromHref('/ai?spaceId=' + SPACE + '&t=' + D),
          keyD: threadKey(D), keyU: threadKey(U),
          stateDashedKeyFlat: decide(live, threadKey(U)), stateFlatKeyDashed: decide(flat, threadKey(D)),
          hiddenFlatKeyDashed: decide(flat, threadKey(C)),
          rowDashed: rowThread(fiberRow(D)), rowFlat: rowThread(fiberRow(U)), rowNone: rowThread({}),
          rowSpaceOnly: rowThread({ '__reactFiber$x': { memoizedProps: { spaceId: SPACE }, return: null } })
        }));""")
        for name in ('dashed', 'flat', 'upper', 'path', 'spaceAndThread', 'keyD', 'keyU', 'rowDashed', 'rowFlat'):
            self.assertEqual(out[name], '3fea1b13e54d805cbc4700a9af92c91f', name)
        self.assertEqual(out['spaceOnly'], '')  # the workspace id is never a thread
        self.assertEqual(out['rowNone'], '')
        self.assertEqual(out['rowSpaceOnly'], '')
        self.assertFalse(out['stateDashedKeyFlat'])
        self.assertFalse(out['stateFlatKeyDashed'])
        self.assertTrue(out['hiddenFlatKeyDashed'])

    def test_inverted_filter_regression(self):
        out = self.run_node(self.LIVE + r"""
        var op = mainOf(live, threadKey(D));
        console.log(JSON.stringify({
          mainHidden: decide(live, threadKey(D)), main4Hidden: decide(live, threadKey(MAINS[1].thread_id)),
          operator: !!(op && op.operator), coworkerHidden: decide(live, threadKey(C)),
          otherHidden: decide(live, threadKey(OTHER)), otherShownOnRequest: decide(show, threadKey(OTHER)),
          mainShownOnRequest: decide(show, threadKey(D)), retiredStillHidden: decide(show, threadKey(C)),
          rowMainHidden: decide(live, rowThread(fiberRow(D))), rowCoworkerHidden: decide(live, rowThread(fiberRow(C)))
        }));""")
        self.assertFalse(out['mainHidden'])  # the operator Main 'Main' must stay visible
        self.assertFalse(out['main4Hidden'])
        self.assertTrue(out['operator'])
        self.assertTrue(out['coworkerHidden'])
        self.assertTrue(out['otherHidden'])  # not a Main, mains_only
        self.assertFalse(out['otherShownOnRequest'])
        self.assertFalse(out['mainShownOnRequest'])
        self.assertTrue(out['retiredStillHidden'])
        self.assertFalse(out['rowMainHidden'])
        self.assertTrue(out['rowCoworkerHidden'])
        # Rows are matched by thread id, never by title (state titles are harness labels).
        for text in ('mainByTitle', 'decideTitle', 'norm(mains[i].title)', 'TITLE_PREFIX_RE'):
            self.assertNotIn(text, self.js)
        rows = self.section('function applyRows(state, root)', 'function resetMarks')
        self.assertIn('var key = rowThread(row);', rows)
        self.assertIn('row.classList.toggle(CLS_HIDDEN, decide(state, key));', rows)
        self.assertIn('row.classList.toggle(CLS_MAIN_ROW, !!main);', rows)
        self.assertIn('row.classList.toggle(CLS_ACTIVE, !!main && rowActive(row, key));', rows)
        self.assertIn('ROW_ITEM_SELECTOR', rows)  # every row in the sidebar, not only date groups
        clutter = self.section('function applyClutter', 'function isShell')
        self.assertIn("'[role=\"list\"] a[href]'", clutter)  # agent tile hidden as clutter, not as a chat

    def test_offline_state_hides_and_relabels_nothing(self):
        out = self.run_node(self.LIVE + r"""
        console.log(JSON.stringify({
          decideNull: decide(null, threadKey(D)), decideNullCoworker: decide(null, threadKey(C)),
          mainNull: mainOf(null, threadKey(D)), onNull: online(null), onUndef: online(undefined),
          onFalse: online({ connected: false, mains: MAINS }), onLive: online(live), onNoFlag: online({ mains: [] })
        }));""")
        self.assertFalse(out['decideNull'])
        self.assertFalse(out['decideNullCoworker'])
        self.assertIsNone(out['mainNull'])
        self.assertFalse(out['onNull'])
        self.assertFalse(out['onUndef'])
        self.assertFalse(out['onFalse'])
        self.assertTrue(out['onLive'])
        self.assertTrue(out['onNoFlag'])
        run = self.section('function run()', 'function schedule')
        gate = run.index('if (!online(state)) {')
        offline = run[gate:run.index('applied = true;')]
        # Offline undoes earlier marks (the New chat dialog also closes its menu/dialog there).
        self.assertIn('if (applied) {', offline)
        undo = offline[offline.index('if (applied) {'):]
        self.assertIn('resetMarks();', undo[:undo.index('}')])
        self.assertIn('return;', offline)
        for text in ('applyFilter', 'applyHeading', 'applyBrand', 'applyRows', 'CLS_HIDDEN', 'Mains'):
            self.assertNotIn(text, offline)
        self.assertLess(gate, run.index('applyFilter(state);'))
        self.assertLess(gate, run.index('applyHeading(root);'))
        reset = self.section('function resetMarks()', '// ---- New chat control')
        self.assertIn('OUR_CLASSES.length', reset)
        self.assertIn("removeAttribute('data-nt-desk')", reset)
        self.assertIn('dropOwn(own[j])', reset)
        self.assertIn('resetMarks();', self.section('stop: function', '})();'))

    def test_node_check(self):
        if not os.path.exists(NODE):
            self.skipTest('node is not installed at ' + NODE)
        done = subprocess.run([NODE, '--check', str(JS_PATH)], capture_output=True, text=True, timeout=40)
        self.assertEqual(done.returncode, 0, done.stderr)


if __name__ == '__main__':
    unittest.main()
