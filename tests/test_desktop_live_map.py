"""Live page map (reference v6 Roles / v7 Direct): mode badge, Main on top, 4 coworkers per row (more wrap),
Roles stage rows (Analyse, Build, Review) with an idle Quality planner, 'waits for Cowork 2', role labels,
a 340 px side panel with Answer/Stop wired to the host actions (accept, confirm, retry, cancel),
the rail badge = running coworkers, and Esc on the Live page goes back to Chat.

Reuses the fake DOM of test_desktop_live.py (NODE_DOM up to the state fixture).
"""
import importlib.util
import json
import os
import re
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location('_nt_live_base', os.path.join(HERE, 'test_desktop_live.py'))
base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(base)
LIVE_CSS = os.path.join(HERE, '..', 'notiontool', 'web', 'desktop', 'live.css')

SCENARIO = r"""
const now = Date.now();
const opened = [], views = {};
win.__ntShell = { register(id, view) { views[id] = view; return function () {}; }, open(tab) { opened.push(tab); } };
const stateObj = {
  runs: [{ id: 'r1', state: 'executing', goal: 'Ship it. Then more words', created: now / 1000 - 300, allocation: { mode: 'roles' } }],
  tasks: [{ id: 't1', run_id: 'r1', state: 'running', role: 'researcher', title: 'Read the code', worker_id: 'w1', created: 1 },
          { id: 't2', run_id: 'r1', state: 'running', role: 'architect', title: 'Shape the plan', worker_id: 'w2', created: 2 },
          { id: 't3', run_id: 'r1', state: 'queued', role: 'executor', title: 'Build it', worker_id: 'w3', deps: ['t2'], created: 3 },
          { id: 't4', run_id: 'r1', state: 'uncertain', role: 'quality_planner', title: 'Plan checks', worker_id: 'w4', created: 4 },
          { id: 't5', run_id: 'r1', state: 'queued', role: 'executor', title: 'Build more', worker_id: 'w5', deps: ['t2'], created: 5 }],
  workers: [{ id: 'w1', label: 'Cowork 1', purpose: 'worker', state: 'busy', task_id: 't1' },
            { id: 'w2', label: 'Cowork 2', purpose: 'worker', state: 'busy', task_id: 't2' },
            { id: 'w3', label: 'Cowork 3', purpose: 'worker', state: 'ready' },
            { id: 'w4', label: 'Cowork 4', purpose: 'worker', state: 'ready' },
            { id: 'w5', label: 'Cowork 5', purpose: 'worker', state: 'ready' },
            { id: 'm', label: 'Main', purpose: 'main', state: 'busy' }],
  attention: [{ message: 'a' }, { message: 'b' }, { message: 'c' }, { message: 'd' }, { message: 'e' }, { message: 'f' }],
  events: [{ ts: now / 1000 - 5, kind: 'task_started', message: 'Cowork 1 reads the code', worker_id: 'w1' }]
};
const calls = [];
const util = win.__ntDesk.liveUtil;
const part = win.__ntDesk.live({ getState: () => stateObj, onState: () => () => {},
  call: (action, args) => { calls.push([action, args]);
    return action === 'live_answer' && args.text === 'boom' ? Promise.reject(new Error('Chat is locked')) : Promise.resolve({}); } });
const tick = () => new Promise(r => setImmediate(r));
const clone = o => JSON.parse(JSON.stringify(o));
(async () => {
  const out = { registered: !!views.live };
  if (!views.live) { process.stdout.write(JSON.stringify(out)); return; }
  out.badge = views.live.badge();
  const host = new Node('main');
  views.live.mount(host);
  const root = find(host, 'nt-desk-lv')[0];
  out.page = !!root && root.classList.contains('nt-desk-lv-page');
  out.side = find(host, 'nt-desk-lv-side').length;
  const map = find(host, 'nt-desk-lv-slot-map')[0], focus = find(host, 'nt-desk-lv-slot-focus')[0];
  const mt = text(map);
  out.mapTexts = ['Mode: Roles', 'Main', 'Ship it.', 'Cowork 1', 'Cowork 2', 'Cowork 3', 'Cowork 4', 'Cowork 5', 'Researcher', 'Architect',
    'Worker', 'Quality planner', 'waits for Cowork 2', 'Needs your answer', 'Analyse', 'Build', 'Review'].filter(x => mt.indexOf(x) < 0);
  out.cards = find(map, 'nt-desk-lv-cw').length;
  out.stageLabels = find(map, 'nt-desk-lv-stage-label').map(n => text(n).trim());
  out.stageCards = find(map, 'nt-desk-lv-stage').map(st => find(st, 'nt-desk-lv-cw').length);
  out.forbidden = ['Finished today'].filter(x => text(host).indexOf(x) >= 0);
  out.focusName = text(find(focus, 'nt-desk-lv-focus-name')[0]).trim();
  click(buttons(focus, 'Resolve')[0]);
  const box = find(host, 'nt-desk-lv-confirm')[0];
  out.answerAsks = !box.hidden && calls.length === 0;
  click(buttons(box, 'Check & accept')[0]);
  await tick(); await tick();
  out.answerCalls = calls.slice();
  click(find(map, 'nt-desk-lv-cw')[0]);
  out.selected = text(find(focus, 'nt-desk-lv-focus-name')[0]).trim();
  out.focusEvents = text(focus).indexOf('Cowork 1 reads the code') >= 0;
  click(buttons(focus, 'Stop')[0]);
  out.stopAsks = !box.hidden && calls.length === 1;
  click(buttons(box, 'Stop')[0]);
  await tick(); await tick();
  out.stopCalls = calls.slice(1);
  let rbox = find(host, 'nt-desk-lv-reply')[0], rsend = find(host, 'nt-desk-lv-send')[0];
  const rstate = () => text(find(host, 'nt-desk-lv-reply-state')[0]).trim();
  out.replyUi = !!rbox && !!rsend && text(rsend).indexOf('Answer') >= 0 && find(host, 'nt-desk-lv-question').length === 1;
  rbox.value = ''; click(rsend);
  out.replyEmpty = [rstate(), calls.filter(c => c[0] === 'live_answer').length];
  rbox.value = 'boom'; click(rsend);
  out.replySendingErr = rstate();
  await tick(); await tick();
  out.replyErr = rstate();
  rbox = find(host, 'nt-desk-lv-reply')[0];
  out.draftKept = rbox.value;
  rbox.value = 'Use the v2 API'; rbox.listeners.input();
  rsend = find(host, 'nt-desk-lv-send')[0]; click(rsend);
  out.replySending = rstate();
  await tick(); await tick();
  out.replySent = rstate();
  out.draftCleared = find(host, 'nt-desk-lv-reply')[0].value;
  out.replyCalls = calls.filter(c => c[0] === 'live_answer').map(c => [c[1].worker_id, c[1].text]);
  out.replyRequestId = /^live-/.test(String(calls.filter(c => c[0] === 'live_answer')[1][1].request_id));
  rbox = find(host, 'nt-desk-lv-reply')[0];
  listeners.keydown({ key: 'Escape', target: { tagName: 'INPUT' }, preventDefault() {} });
  out.escTyping = opened.slice();
  let prevented = false;
  listeners.keydown({ key: 'Escape', preventDefault() { prevented = true; } });
  out.escOpened = opened.slice();
  out.escPrevented = prevented;
  listeners.keydown({ key: 'Escape', target: rbox, preventDefault() {} });
  out.escReply = opened.slice(1);
  const direct = clone(stateObj);
  direct.runs[0].allocation.mode = 'direct';
  const cards = util.liveCards(direct, now);
  out.direct = { mode: util.liveMode(direct), roles: cards.map(c => c.role), stages: cards.map(c => c.stage),
    texts: cards.map(c => c.text), count: cards.length, fifth: cards[4] && [cards[4].label, cards[4].text] };
  const noQp = clone(stateObj);
  noQp.tasks = noQp.tasks.filter(t => t.id !== 't4');
  const review = util.liveStages(util.liveCards(noQp, now)).filter(st => st.id === 'review')[0];
  out.idleQp = review.cards.map(c => [c.label, c.kind, c.text, !!c.slot]);
  const answerOf = (st, i) => { const c = util.liveCards(st, now)[i]; return c && c.answer ? [c.answer.action, c.answer.args] : null; };
  const stopOf = (st, i) => { const c = util.liveCards(st, now)[i]; return c && c.stop ? [c.stop.action, c.stop.args] : null; };
  const waitsOk = clone(stateObj); waitsOk.runs[0].state = 'awaiting_main'; waitsOk.tasks[3].state = 'running';
  const blocked = clone(stateObj); blocked.runs[0].state = 'blocked'; blocked.tasks[3].state = 'failed';
  out.answerConfirm = answerOf(waitsOk, 3);
  out.answerRetry = answerOf(blocked, 3);
  out.stopDef = stopOf(stateObj, 0);
  out.modes = [util.liveMode({}), util.liveMode({ runs: [{ id: 'x', state: 'executing' }], tasks: [{ id: 'y', run_id: 'x', role: 'researcher' }] }),
    util.liveMode({ runs: [{ id: 'x', state: 'executing' }], tasks: [{ id: 'y', run_id: 'x', role: 'executor' }] })];
  out.running = util.runningCount(stateObj);
  views.live.unmount();
  out.pageRemoved = !root.classList.contains('nt-desk-lv-page');
  part.stop();
  process.stdout.write(JSON.stringify(out));
})().catch(e => process.stdout.write(JSON.stringify({ error: String(e && e.stack || e) })));
"""


def run_map():
    script = base.NODE_DOM.split('const now = Date.now();')[0] + SCENARIO
    result = subprocess.run([base.NODE, '-e', script, base.JS], capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


class LiveMapTests(unittest.TestCase):
    def test_static_layout_rules(self):
        js, css = base.read(base.JS), base.css_text()
        self.assertIn('TEAM_MAX = 4', js)
        self.assertNotIn('slice(0, TEAM_MAX)', js)  # more than 4 coworkers wrap, none are dropped
        for label in ("'Mode: Roles'", "'Mode: Direct'", "'waits for '", "'Researcher'", "'Architect'", "'Worker'",
                      "'Quality planner'", "'Answer'", "'Live events'", "'Analyse'", "'Build'", "'Review'"):
            self.assertIn(label, js)
        for word in ('Finished today', 'radial'):
            self.assertNotIn(word, js)
        self.assertIn('grid-template-columns: minmax(0, 1fr) 340px', css)
        self.assertIn('width: 340px', css)
        self.assertIn('grid-template-columns: repeat(4, minmax(0, 1fr))', css)
        self.assertIn('.nt-desk-lv-cw-idle, .nt-desk-lv-cw-off { border-style: dashed; }', css)
        self.assertNotIn('<table', js)
        self.assertIsNone(re.search(r"el\('table'", js))

    def test_live_page_styles_use_theme_tokens(self):
        with open(LIVE_CSS, encoding='utf-8') as fh:
            section = fh.read().split('/* Live page (reference v6 Roles / v7 Direct)')[1]
        self.assertEqual(re.findall(r'#[0-9a-fA-F]{3,8}\b|rgba?\(', section), [])
        self.assertNotIn('13px', section)

    @unittest.skipUnless(os.path.exists(base.NODE), 'node not installed')
    def test_node_map_answer_stop_badge_esc(self):
        out = run_map()
        self.assertNotIn('error', out, out.get('error'))
        self.assertTrue(out['registered'])
        self.assertEqual(out['badge'], 2)  # 2 coworkers run; the 6 attention items do not count
        self.assertEqual(out['running'], 2)
        self.assertTrue(out['page'])
        self.assertEqual(out['side'], 1)
        self.assertEqual(out['mapTexts'], [])
        self.assertEqual(out['cards'], 5)  # Cowork 5 is shown, not dropped
        self.assertEqual(out['stageLabels'], ['Analyse', 'Build', 'Review'])
        self.assertEqual(out['stageCards'], [2, 2, 1])
        self.assertEqual(out['forbidden'], [])
        self.assertEqual(out['focusName'], 'Cowork 4')  # the coworker that needs an answer is shown first
        self.assertTrue(out['answerAsks'])
        self.assertEqual(len(out['answerCalls']), 1)
        action, args = out['answerCalls'][0]
        self.assertEqual(action, 'task_reconcile')
        self.assertEqual((args['id'], args['resolution']), ('t4', 'accept'))
        self.assertEqual(out['selected'], 'Cowork 1')
        self.assertTrue(out['focusEvents'])
        self.assertTrue(out['stopAsks'])
        self.assertEqual(out['stopCalls'], [['run_cancel', {'id': 'r1'}]])
        self.assertEqual(out['escTyping'], [])  # Esc while typing never navigates
        self.assertEqual(out['escOpened'], ['chat'])
        self.assertTrue(out['escPrevented'])
        self.assertEqual(out['direct']['mode'], 'direct')
        self.assertEqual(out['direct']['roles'], ['', '', '', '', ''])
        self.assertEqual(out['direct']['stages'], ['', '', '', '', ''])
        self.assertEqual(out['direct']['count'], 5)
        self.assertEqual(out['direct']['fifth'], ['Cowork 5', 'waits for Cowork 2'])
        self.assertEqual(out['idleQp'], [['Quality planner', 'idle', 'Idle', True]])
        self.assertEqual(out['modes'], ['direct', 'roles', 'direct'])
        self.assertTrue(out['pageRemoved'])
        # Answer box: typed reply -> host action live_answer (POST /api/send), with sending / sent / error states
        self.assertTrue(out['replyUi'])
        self.assertEqual(out['replyEmpty'], ['Could not send: Type a reply first', 0])  # empty text is never sent
        self.assertEqual(out['replySendingErr'], 'Sending\u2026')
        self.assertEqual(out['replyErr'], 'Could not send: Chat is locked')
        self.assertEqual(out['draftKept'], 'boom')  # the draft survives an error
        self.assertEqual(out['replySending'], 'Sending\u2026')
        self.assertEqual(out['replySent'], 'Sent to the chat')
        self.assertEqual(out['draftCleared'], '')
        self.assertEqual(out['replyCalls'], [['w1', 'boom'], ['w1', 'Use the v2 API']])
        self.assertTrue(out['replyRequestId'])
        self.assertEqual(out['escReply'], ['chat'])  # Esc inside the reply box goes back to Chat

    @unittest.skipUnless(os.path.exists(base.NODE), 'node not installed')
    def test_node_answer_and_stop_paths(self):
        out = run_map()
        self.assertNotIn('error', out, out.get('error'))
        self.assertEqual(out['answerConfirm'], ['run_confirm', {'id': 'r1'}])  # the request waits for the user's OK
        self.assertEqual(out['answerRetry'], ['run_retry', {'id': 'r1'}])  # the request is blocked
        self.assertEqual(out['stopDef'], ['run_cancel', {'id': 'r1'}])  # Stop = POST /api/run/cancel


if __name__ == '__main__':
    unittest.main()
