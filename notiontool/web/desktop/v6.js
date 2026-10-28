/* NotionTool desktop overlay part: v6 - Chat v6 layout 1:1 to the mockup
 * (Pictures/AI Bilder/Screenshot 2026-10-25 010922.png, docs/plan/redesign/reference HANDOVER Chat v6).
 * Contract: window.__ntDesk.v6 = function (ctx) { ... return { stop } }.
 *
 * - Sidebar panel (our own node, data-nt-desk-own) laid exactly over Notion's sidebar box
 *   (.notion-sidebar-container at its live width; the width is never forced). Notion's sidebar stays
 *   mounted below it: chat rows and New chat press Notion's own rows, so navigation stays Notion's.
 *   Project card (projects_list), active chat card (title, model chip, age ring, LIVE tool line),
 *   Chats (Notion's rows, ages from chat_ages), Coworkers with an active count and number badges (harness state workers),
 *   Project card (tasks running, last commit), gradient New chat.
 * - Header over Notion's chat top row (the row only gets .nt-v6-toprow: taller, children hidden).
 *   Back, chat title, project; status group: model (presses Notion's model button), effort (Notion's
 *   effort button), chat age ring x / 23h, context tokens, steps per hour, Switch context
 *   (chat_context_switch after a second click), Stop (Notion's Stop AI message button).
 * - Desktop JS never opens a network connection itself: data comes from ctx.getState/onState and ctx.call.
 * - One 600 ms tick; a panel is only rewritten when its rendered HTML changes (no lag while typing).
 */
(function () {
  'use strict';
  window.__ntDesk = window.__ntDesk || {};

  var CLS_ON = 'nt-v6-on';
  var CLS_SHOW = 'nt-v6-show';
  var CLS_TOPROW = 'nt-v6-toprow';
  var TICK_MS = 600;
  var AGES_MS = 60000;
  var PROJECTS_MS = 120000;
  var LIMIT_H = 23;
  var CONFIRM_MS = 4000;
  var ROW_H = 42;
  var FIXED_H = 430;
  var CW_H = 70;
  var COWORKER_RE = /^cowork(er)?\s*\d+/i;
  var ARCH_RE = /\((?:expired|archived?)\b|^archived?\b/i;
  var MODEL_RE = /^(opus|sonnet|haiku|auto|gpt|gemini|claude|o\d)\b/i;
  var BUSY = { busy: 1, working: 1, running: 1, sending: 1, sent: 1 };
  var THINK = { preparing: 1, thinking: 1, starting: 1, queued: 1 };
  var BAD = { error: 1, missing: 1, failed: 1, stuck: 1, offline: 1 };
  var TASK_DONE = { done: 1, failed: 1, cancelled: 1, canceled: 1, abandoned: 1, accepted: 1, checkpoint: 1, rejected: 1 };
  var RUN_DONE = { done: 1, failed: 1, cancelled: 1, canceled: 1, completed: 1, archived: 1, abandoned: 1 };
  var HIDDEN_SEL = '.nt-desk-hidden,.nt-desk-sb-hidden,.nt-shell-hide';

  // Lucide icons (24x24 viewBox, stroke drawn by v6.css).
  var P = {
    folder: '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>',
    down: '<path d="m6 9 6 6 6-6"/>',
    right: '<path d="m9 18 6-6-6-6"/>',
    msg: '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
    msgText: '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/><path d="M13 8H7"/><path d="M17 12H7"/>',
    plus: '<path d="M5 12h14"/><path d="M12 5v14"/>',
    back: '<path d="m12 19-7-7 7-7"/><path d="M19 12H5"/>',
    gauge: '<path d="m12 14 4-4"/><path d="M3.34 19a10 10 0 1 1 17.32 0"/>',
    lock: '<rect width="18" height="11" x="3" y="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    zap: '<path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z"/>',
    swap: '<path d="M8 3 4 7l4 4"/><path d="M4 7h16"/><path d="m16 21 4-4-4-4"/><path d="M20 17H4"/>',
    box: '<path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/><path d="m3.3 7 8.7 5 8.7-5"/><path d="M12 22V12"/>',
    msgDots: '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/><path d="M8 10h.01"/><path d="M12 10h.01"/><path d="M16 10h.01"/>',
    users: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    mic: '<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><path d="M12 19v3"/>',
    archive: '<rect width="20" height="5" x="2" y="3" rx="1"/><path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8"/><path d="M10 12h4"/>'
  };
  var TOOL_RE = /^(?:\S\s*)?[\w .-]{2,40}?\s*\/\s*[\w.-]{2,60}$/;
  function svg(name, cls) { return '<svg viewBox="0 0 24 24" aria-hidden="true"' + (cls ? ' class="' + cls + '"' : '') + '>' + (P[name] || '') + '</svg>'; }

  function str(v) { return v == null ? '' : String(v); }
  function list(v) { return Array.isArray(v) ? v : []; }
  function obj(v) { return v && typeof v === 'object' && !Array.isArray(v) ? v : {}; }
  function key(v) { return str(v).toLowerCase().replace(/[^0-9a-f]/g, ''); }
  function esc(v) {
    return str(v).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; });
  }
  function text(el) { return el ? str(el.textContent).replace(/\s+/g, ' ').trim() : ''; }
  function shown(el) { if (!el) return false; var r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; }
  function unwrap(reply) {
    var r = obj(reply);
    return r.result && typeof r.result === 'object' && !r.chats && !r.projects && !r.project ? obj(r.result) : r;
  }
  function clamp01(v) { v = Number(v); return isFinite(v) ? Math.max(0, Math.min(1, v)) : 0; }
  function fmtTok(n) {
    n = Number(n);
    if (!isFinite(n) || n < 0) return '';
    if (n >= 1e6) return String(Math.round(n / 1e5) / 10).replace(/\.0$/, '') + 'M';
    if (n >= 1e3) return Math.round(n / 1e3) + 'k';
    return String(Math.round(n));
  }
  function ageText(h) {
    h = Number(h);
    if (!isFinite(h) || h < 0) return '';
    if (h < 1) return Math.max(1, Math.floor(h * 60)) + 'm';
    if (h < 24) return Math.floor(h) + 'h';
    return Math.floor(h / 24) + 'd';
  }
  function agoText(ms) {
    if (!isFinite(ms)) return '';
    var s = Math.max(0, (Date.now() - ms) / 1000);
    if (s < 60) return 'just now';
    if (s < 3600) return Math.floor(s / 60) + 'm ago';
    if (s < 86400) return Math.floor(s / 3600) + 'h ago';
    return Math.floor(s / 86400) + 'd ago';
  }
  function ring(frac) {
    var c = 2 * Math.PI * 8, f = clamp01(frac);
    return '<svg class="nt-v6-ring" viewBox="0 0 20 20" aria-hidden="true"><circle class="bg" cx="10" cy="10" r="8"/>' +
      '<circle class="fg" cx="10" cy="10" r="8" stroke-dasharray="' + (c * f).toFixed(1) + ' ' + c.toFixed(1) + '"/></svg>';
  }
  function bar(frac) { return '<div class="nt-v6-bar"><span style="width:' + (clamp01(frac) * 100).toFixed(1) + '%"></span></div>'; }
  // Coworker badge: the number of "Coworker 12" (else the first letter); replaces the second status dot.
  function avatar(label) { var s = str(label).trim(), m = s.match(/(\d+)\s*$/); return '<span class="nt-v6-av">' + esc(m ? m[1] : (s.charAt(0) || '?').toUpperCase()) + '</span>'; }
  function cap(s) { s = str(s); return s ? s.charAt(0).toUpperCase() + s.slice(1) : ''; }

  // Panels are patched in place instead of replaced (user review 26.10.): a full innerHTML swap restarted
  // every spinner, dropped hover and made bars jump on each update. Same node type and tag = same node:
  // attributes and text are updated, missing nodes are added, extra nodes are removed.
  var TPL = null;
  function syncAttrs(a, b) {
    var i, n;
    for (i = a.attributes.length - 1; i >= 0; i--) { n = a.attributes[i].name; if (!b.hasAttribute(n)) a.removeAttribute(n); }
    for (i = 0; i < b.attributes.length; i++) { n = b.attributes[i]; if (a.getAttribute(n.name) !== n.value) a.setAttribute(n.name, n.value); }
  }
  function syncKids(to, from) {
    var a = to.firstChild, b = from.firstChild, nb, na;
    while (b) {
      nb = b.nextSibling;
      if (!a) to.appendChild(b);
      else if (a.nodeType === b.nodeType && a.nodeName === b.nodeName) {
        if (a.nodeType === 1) { syncAttrs(a, b); syncKids(a, b); } else if (a.nodeValue !== b.nodeValue) a.nodeValue = b.nodeValue;
        a = a.nextSibling;
      } else { na = a.nextSibling; to.replaceChild(b, a); a = na; }
      b = nb;
    }
    while (a) { na = a.nextSibling; to.removeChild(a); a = na; }
  }
  function morph(el, html) {
    if (!TPL) TPL = document.createElement('template');
    TPL.innerHTML = html;
    syncKids(el, TPL.content);
  }

  // ---- Notion DOM readers ------------------------------------------------------------------
  function sidebarBox() { return document.querySelector('.notion-sidebar-container'); }
  function rowTitle(el) {
    var t = el.querySelector('div.xuxw1ft');
    var s = t ? text(t) : str(el.innerText || el.textContent).split('\n')[0];
    return str(s).replace(/\s+/g, ' ').trim();
  }
  function notionRows() {
    var out = [], seen = {};
    var nodes = document.querySelectorAll('.notion-sidebar [role="treeitem"]');
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      if (el.closest('[data-nt-desk-own]') || el.closest(HIDDEN_SEL) || el.classList.contains('nt-desk-newchat')) continue;
      var title = rowTitle(el);
      if (!title || seen[title]) continue;
      seen[title] = 1;
      out.push({ el: el, title: title });
    }
    return out;
  }
  function findRow(title) {
    var rows = notionRows();
    for (var i = 0; i < rows.length; i++) if (rows[i].title === title) return rows[i].el;
    var low = str(title).toLowerCase();
    for (var j = 0; j < rows.length; j++) if (rows[j].title.toLowerCase() === low) return rows[j].el;
    return null;
  }
  function currentThread() {
    try {
      var u = new URL(location.href);
      var t = u.searchParams.get('t') || u.searchParams.get('thread') || u.searchParams.get('threadId');
      if (t) return key(t);
    } catch (e) { /* ignore */ }
    return '';
  }
  function currentTitle() {
    var t = text(document.querySelector('.nt-desk-chat-title'));
    if (t) return t;
    return str(document.title).replace(/^NotionTool\s*[-\u2013\u2014]\s*/i, '').trim();
  }
  function composerButtons() {
    var box = document.querySelector('.nt-desk-composer');
    return box ? box.querySelectorAll('[role="button"],button') : [];
  }
  function modelButton() {
    var btns = composerButtons();
    for (var i = 0; i < btns.length; i++) if (MODEL_RE.test(text(btns[i])) && shown(btns[i])) return btns[i];
    return null;
  }
  function effortButton() { return document.querySelector('[aria-label^="Change effort"]'); }
  function stopButton() {
    var b = document.querySelector('[aria-label="Stop AI message"]');
    return b && shown(b) && b.getAttribute('aria-disabled') !== 'true' ? b : null;
  }
  function lastTool() {
    var root = document.querySelector('.layout-content');
    if (!root) return '';
    var nodes = root.querySelectorAll('[role="button"]');
    for (var i = nodes.length - 1, n = 0; i >= 0 && n < 80; i--, n++) {
      var m = text(nodes[i]).match(/^(?:\S\s+)?([\w .-]{2,40}?)\s*\/\s*([\w.-]{2,60})$/);
      if (m) return m[2];
    }
    return '';
  }
  function press(el) {
    if (!el) return false;
    try { el.click(); return true; } catch (e) { return false; }
  }
  // Notion swaps a chat row's icon for an animated <canvas> while that chat is thinking or answering.
  function rowLive(el) { return !!(el && el.querySelector('canvas')); }
  // Marks Notion nodes with a data attribute (React never rewrites attributes it did not set).
  function mark(el, v) { if (el && el.getAttribute('data-nt-v6') !== v) el.setAttribute('data-nt-v6', v); }

  // Rail: mockup labels and icons, plus a badge on Live with the number of chats working right now.
  function decorateRail(liveCount) {
    var rail = document.querySelector('.nt-rail');
    if (!rail) return;
    var want = { chat: ['Chats', 'msgText'], projects: ['Projects', 'box'], live: ['Live', 'zap'] };
    Object.keys(want).forEach(function (id) {
      var tab = rail.querySelector('[data-nt-shell-tab="' + id + '"]');
      if (!tab) return;
      var lab = tab.querySelector('.nt-shell-tab-label'), ico = tab.querySelector('.nt-shell-tab-icon');
      if (lab && lab.textContent !== want[id][0]) lab.textContent = want[id][0];
      if (ico && ico.getAttribute('data-nt-v6') !== want[id][1]) { ico.innerHTML = svg(want[id][1]); ico.setAttribute('data-nt-v6', want[id][1]); }
    });
    var lt = rail.querySelector('[data-nt-shell-tab="live"]');
    if (!lt) return;
    var b = lt.querySelector('.nt-v6-badge');
    if (!b) {
      b = document.createElement('span');
      b.className = 'nt-v6-badge';
      b.setAttribute('data-nt-desk-own', 'v6');
      b.setAttribute('aria-hidden', 'true');
    }
    // shell.js writes its own count into btn.lastChild, so this badge must never be the last child.
    var sbd = lt.querySelector('.nt-shell-tab-badge');
    if (sbd && b.nextSibling !== sbd) lt.insertBefore(b, sbd);
    else if (!sbd && b.parentNode !== lt) lt.insertBefore(b, lt.firstChild);
    var t = liveCount > 0 ? String(liveCount) : '';
    if (b.textContent !== t) b.textContent = t;
    b.style.display = t ? '' : 'none';
    lt.title = t ? t + (t === '1' ? ' chat is' : ' chats are') + ' working right now (Ctrl+3)' : 'See what your team is doing now (Ctrl+3)';
  }

  // Chat column: tool rows become the mockup timeline, user messages the gradient bubble,
  // composer gets the mockup placeholder and Attach / Commands labels (Notion's own buttons).
  // Tool durations (mockup: "4.2s" right of each tool step). Notion shows none, so NotionTool measures them:
  // the newest tool step runs from when it appears until the next one appears or the answer ends.
  var DUR_KEY = 'nt-v6-durs', durs = null, durOpen = null;
  function loadDurs() {
    if (durs) return durs;
    try { durs = JSON.parse(localStorage.getItem(DUR_KEY) || '{}') || {}; } catch (e) { durs = {}; }
    return durs;
  }
  function fmtDur(ms) {
    var s = Math.max(0, ms) / 1000;
    return s < 10 ? s.toFixed(1) + 's' : s < 60 ? Math.round(s) + 's' : Math.floor(s / 60) + 'm ' + Math.round(s % 60) + 's';
  }
  function durKey(title, i, el) { return (title || '') + '|' + i + '|' + text(el).slice(0, 60); }
  function trackDurations(title, tools, running) {
    var d = loadDurs(), now = Date.now(), changed = false, n = tools.length;
    if (durOpen && (!running || durOpen.n !== n || durOpen.title !== title)) {
      var el = durOpen.title === title && tools[durOpen.i];
      var k = el ? durKey(title, durOpen.i, el) : durOpen.k;
      if (!d[k]) { d[k] = now - durOpen.t0; changed = true; }
      durOpen = null;
    }
    if (running && n && !durOpen) {
      var k2 = durKey(title, n - 1, tools[n - 1]);
      if (!d[k2]) durOpen = { k: k2, i: n - 1, t0: now, n: n, title: title };
    }
    for (var i = Math.max(0, n - 80); i < n; i++) {
      var s = '';
      if (durOpen && durOpen.i === i) s = fmtDur(now - durOpen.t0);
      else { var v = d[durKey(title, i, tools[i])]; if (v) s = fmtDur(v); }
      if (tools[i].getAttribute('data-nt-v6-dur') !== s) {
        if (s) tools[i].setAttribute('data-nt-v6-dur', s); else tools[i].removeAttribute('data-nt-v6-dur');
      }
    }
    if (changed) {
      var ks = Object.keys(d);
      for (var j = 0; j < ks.length - 800; j++) delete d[ks[j]];
      try { localStorage.setItem(DUR_KEY, JSON.stringify(d)); } catch (e) { /* ignore */ }
    }
  }

  function decorateChat(title) {
    var lc = document.querySelector('.layout-content');
    if (!lc) return null;
    var steps = lc.querySelectorAll('.nt-desk-steps');
    for (var s = 0; s < steps.length; s++) mark(steps[s].closest('[role="button"]'), 'steps');
    var btns = lc.querySelectorAll('[role="button"][aria-expanded]:not([data-nt-v6])');
    for (var i = 0; i < btns.length; i++) {
      var b = btns[i];
      if (b.closest('.nt-desk-composer')) continue;
      var t = text(b);
      if (!t) continue;
      mark(b, t.length < 90 && TOOL_RE.test(t) && b.querySelector('[role="img"]') ? 'tool' : 'x');
    }
    try { trackDurations(title, lc.querySelectorAll('[data-nt-v6="tool"]'), !!stopButton()); } catch (e) { /* ignore */ }
    var us = lc.querySelectorAll('[data-agent-chat-user-step-id]:not([data-nt-v6u])');
    for (var j = Math.max(0, us.length - 30); j < us.length; j++) {
      var ds = us[j].querySelectorAll('div'), bub = null;
      for (var k = 0; k < ds.length && k < 14; k++) {
        var cs = getComputedStyle(ds[k]);
        if (cs.backgroundColor !== 'rgba(0, 0, 0, 0)' && cs.backgroundColor !== 'transparent' && parseFloat(cs.borderTopLeftRadius) >= 8) { bub = ds[k]; break; }
      }
      if (bub) { mark(bub, 'ububble'); us[j].setAttribute('data-nt-v6u', '1'); }
    }
    var comp = document.querySelector('.nt-desk-composer');
    if (!comp) return null;
    var ed = comp.querySelector('[contenteditable="true"][placeholder]');
    var ph = 'Message ' + (title || 'Main') + '...';
    if (ed && ed.getAttribute('placeholder') !== ph) ed.setAttribute('placeholder', ph);
    var cmd = null, cb = comp.querySelectorAll('[role="button"],button');
    for (var m = 0; m < cb.length; m++) {
      var al = str(cb[m].getAttribute('aria-label'));
      if (al === 'Give context') mark(cb[m], 'attach');
      else if (al === 'Settings') { mark(cb[m], 'cmd'); cmd = cb[m]; }
      else if (/^Change effort/.test(al) || (!al && MODEL_RE.test(text(cb[m])))) mark(cb[m], 'quiet');
      else if (/^(Stop AI message|Submit|Send)/i.test(al)) mark(cb[m], 'send');
      else if (/dictat|voice|microphone|speech|transcri/i.test(al)) mark(cb[m], 'mic');
    }
    return cmd && shown(cmd) ? cmd.getBoundingClientRect() : null;
  }

  window.__ntDesk.v6 = function (ctx) {
    var stopped = false, timer = null, offState = null;
    var state = obj(ctx && ctx.getState ? ctx.getState() : null);
    var ages = { byKey: {}, byTitle: {}, limit: LIMIT_H }, agesAt = 0;
    var project = null, projectCommitMs = NaN, projectsAt = 0;
    var expanded = false, confirmUntil = 0, errLogged = false;
    // Folded sidebar groups (user 2026-10-27): idle coworkers and the archive stay one line until opened.
    var CWOPEN_KEY = 'nt-v6-cw-open', ARCHOPEN_KEY = 'nt-v6-arch-open', cwOpen = false, archOpen = false;
    try { cwOpen = localStorage.getItem(CWOPEN_KEY) === '1'; archOpen = localStorage.getItem(ARCHOPEN_KEY) === '1'; } catch (e) { /* ignore */ }
    // Finished answers (user 2026-10-27): a chat row that stops being live gets a Done badge until that chat
    // is opened; the open chat shows "Answer finished" in its card for 10 minutes.
    var DONE_KEY = 'nt-v6-done', doneMap = null, wasLive = {}, mainDoneAt = 0, mainWasRunning = false, mainRunTitle = '';
    function loadDone() {
      if (doneMap) return doneMap;
      try { doneMap = JSON.parse(localStorage.getItem(DONE_KEY) || '{}') || {}; } catch (e) { doneMap = {}; }
      return doneMap;
    }
    function trackDone(liveMap, title, running) {
      var d = loadDone(), now = Date.now(), changed = false;
      Object.keys(liveMap).forEach(function (t) {
        if (liveMap[t]) { if (d[t]) { delete d[t]; changed = true; } }
        else if (wasLive[t] && t !== title) { d[t] = now; changed = true; }
        wasLive[t] = liveMap[t];
      });
      if (title && d[title]) { delete d[title]; changed = true; }
      Object.keys(d).forEach(function (t) { if (!(now - d[t] < 43200000)) { delete d[t]; changed = true; } });
      if (title !== mainRunTitle) { mainRunTitle = title; mainDoneAt = 0; mainWasRunning = running; }
      else { if (running) mainDoneAt = 0; else if (mainWasRunning) mainDoneAt = now; mainWasRunning = running; }
      if (changed) { try { localStorage.setItem(DONE_KEY, JSON.stringify(d)); } catch (e) { /* ignore */ } }
      return d;
    }
    var sb = null, hd = null, defs = null, dg = null, lastSb = '', lastHd = '', lastBox = '', lastHdBox = '', lastDg = '';
    var root = document.documentElement;
    var archUntil = 0;

    function call(action, args) {
      if (!ctx || typeof ctx.call !== 'function') return Promise.reject(new Error('NotionTool is not connected'));
      return Promise.resolve(ctx.call(action, args || {})).then(unwrap);
    }
    function toast(msg, kind) { try { if (ctx && ctx.toast) ctx.toast(msg, kind); } catch (e) { /* ignore */ } }

    // ---- data ------------------------------------------------------------------------------
    function loadAges() {
      agesAt = Date.now();
      call('chat_ages', {}).then(function (r) {
        var out = { byKey: {}, byTitle: {}, limit: Number(r.limit_h) > 0 ? Number(r.limit_h) : LIMIT_H };
        var nowS = Date.now() / 1000;
        list(r.chats).forEach(function (c) {
          c = obj(c);
          var h = Number(c.age_h);
          if (!(c.age_h != null && isFinite(h) && h >= 0) && c.created_at != null) {
            var at = Number(c.created_at);
            if (at > 1e12) at = at / 1000;
            h = isFinite(at) && at > 0 ? (nowS - at) / 3600 : NaN;
          }
          if (!isFinite(h)) return;
          var entry = { hours: h, blocked: !!c.blocked };
          var k = key(c.thread_id);
          if (k) out.byKey[k] = entry;
          var t = str(c.title).trim().toLowerCase();
          if (t && !out.byTitle[t]) out.byTitle[t] = entry;
        });
        ages = out;
      }).catch(function () { /* keep the last ages */ });
    }
    function loadProject() {
      projectsAt = Date.now();
      call('projects_list', {}).then(function (r) {
        var all = list(r.projects), cur = str(r.current_project_id), pick = null;
        for (var i = 0; i < all.length; i++) if (str(all[i].id) === cur) pick = all[i];
        if (!pick) for (var j = 0; j < all.length; j++) if (!all[j].archived) { pick = all[j]; break; }
        project = pick ? obj(pick) : null;
        if (!project || !project.id) return null;
        return call('projects_detail', { id: project.id, n: 1 }).then(function (d) {
          var c = list(obj(d.project).commits)[0];
          projectCommitMs = c ? Date.parse(str(obj(c).date)) : NaN;
        });
      }).catch(function () { /* keep the last project */ });
    }

    function workers() { return list(state.workers); }
    function currentWorker(title) {
      var k = currentThread(), ws = workers(), i;
      if (k) for (i = 0; i < ws.length; i++) if (key(ws[i].thread_id) === k) return obj(ws[i]);
      for (i = 0; i < ws.length; i++) if (str(ws[i].label) === title) return obj(ws[i]);
      return null;
    }
    // Round 28 (user 08:51): own chats from 'New chat' are private chats (not in state.workers) or app Mains;
    // Switch context and Move to archive look them up in state.mains / state.private_chats too (thread first).
    function switchTarget(title) {
      var w = currentWorker(title);
      if (w) return w;
      var k = currentThread(), t = str(title).trim(), groups = [list(state.mains), list(state.private_chats)], g, i, m;
      for (g = 0; g < groups.length; g++) for (i = 0; i < groups[g].length; i++) {
        m = obj(groups[g][i]);
        if (k && key(m.thread_id) === k) return ownTarget(m);
      }
      for (g = 0; g < groups.length; g++) for (i = 0; i < groups[g].length; i++) {
        m = obj(groups[g][i]);
        if (t && str(m.title).trim() === t) return ownTarget(m);
      }
      return null;
    }
    function ownTarget(m) {
      return { id: str(m.worker_id), thread_id: str(m.thread_id), chat_id: str(m.chat_id), label: str(m.title),
               purpose: m.role === 'main' ? 'main' : 'private', pending: !!m.pending, own: true };
    }
    function ageFor(threadId, title) {
      var k = key(threadId);
      if (k && ages.byKey[k]) return ages.byKey[k].hours;
      var t = str(title).trim().toLowerCase();
      if (t && ages.byTitle[t]) return ages.byTitle[t].hours;
      return NaN;
    }
    // round 25 (user 07:12: Anims shows no age): own chats NotionTool did not create are not in the chat age
    // list; their age comes from the Notion thread (chat_thread_age, send=false), cached per chat for 60 s.
    var tAge = {}, tAgeAt = {};
    function threadAge(threadId) {
      var k = key(threadId), now = Date.now();
      if (!k || k.length !== 32) return NaN;
      if (!(tAgeAt[k] > now - 60000) && ctx && typeof ctx.call === 'function') {
        tAgeAt[k] = now;
        try {
          Promise.resolve(ctx.call('chat_thread_age', { thread_id: k, send: false })).then(function (r) {
            var rec = obj(r), raw = rec.age_h != null ? rec.age_h : obj(rec.result).age_h;
            tAge[k] = raw == null ? NaN : Number(raw);
          }, function () {});
        } catch (e) { /* ignore */ }
      }
      var v = tAge[k];
      return typeof v === 'number' ? v : NaN;
    }
    function isCoworker(w) {
      var p = str(w.purpose).toLowerCase();
      return p === 'coworker' || p === 'worker' || (p !== 'main' && COWORKER_RE.test(str(w.label)));
    }
    function needsAnswer(w) {
      var att = list(state.attention), id = str(w.id), task = str(w.task_id);
      for (var i = 0; i < att.length; i++) {
        var a = obj(att[i]);
        if (/question|ask|answer|input/i.test(str(a.kind)) && (str(a.worker_id) === id || (task && str(a.id) === task))) return true;
      }
      return /ask|question|waiting_user|needs_input/i.test(str(w.state));
    }
    function tasksRunning() {
      var run = 0, queued = 0, waiting = 0, liveRuns = {}, busy = {}, runs = list(state.runs), hasRuns = runs.length > 0;
      workers().forEach(function (w) { w = obj(w); var ws = str(w.state).toLowerCase(); if (w.id) busy[str(w.id)] = !!(BUSY[ws] || THINK[ws] || w.preparing); });
      runs.forEach(function (r) { r = obj(r); if (r.id && !RUN_DONE[str(r.state).toLowerCase()]) liveRuns[str(r.id)] = 1; });
      list(state.tasks).forEach(function (t) {
        t = obj(t);
        var s = str(t.state).toLowerCase();
        if (t.finished || TASK_DONE[s]) return;
        // Tasks of cancelled or finished runs are not running anymore (a cancelled run kept showing "3 tasks running").
        if (hasRuns && t.run_id && !liveRuns[str(t.run_id)]) return;
        if (s === 'queued' || s === 'retry' || s === 'waiting' || s === 'pending') queued++;
        // Running = a coworker is on it right now; tasks waiting for a review, an approval or an unknown remote state count as waiting.
        else if (/block|review|await|approv|pause|stuck|unknown|hold|need/.test(s) || t.remote_unknown || (t.worker_id && (str(t.worker_id) in busy) && !busy[str(t.worker_id)])) waiting++;
        else run++;
      });
      return { run: run, queued: queued, waiting: waiting };
    }
    // Notion sidebar row title -> thread id (harness workers, mains, private chats, chats).
    function titleThreads() {
      var map = {};
      function put(title, thread) { var t = str(title).trim(); if (t && thread && !map[t]) map[t] = str(thread); }
      workers().forEach(function (w) { w = obj(w); put(w.label, w.thread_id); });
      list(state.mains).forEach(function (m) { m = obj(m); put(m.title, m.thread_id); });
      list(state.private_chats).forEach(function (m) { m = obj(m); put(m.title, m.thread_id); });
      list(state.chats).forEach(function (c) {
        c = obj(c);
        if (str(c.state) === 'retired') return;
        var id = str(c.thread_id || c.id), cut = id.lastIndexOf(':');
        put(c.title, cut >= 0 ? id.slice(cut + 1) : id);
      });
      return map;
    }

    // ---- render: sidebar ---------------------------------------------------------------------
    function sidebarHtml(info) {
      var p = project, html = [];
      html.push('<div class="nt-v6-sb-in">');
      html.push('<div class="nt-v6-proj nt-v6-btn" data-act="proj" title="Open Projects"><div class="nt-v6-ico">' + svg('folder') + '</div>' +
        '<div class="nt-v6-col"><div class="nt-v6-t1">' + esc(p ? (p.name || 'Untitled') : 'No project') + '</div>' +
        '<div class="nt-v6-t2">' + esc(p ? (str(p.repo_path).replace(/\\/g, '/') || 'No repo path') : 'Create one in Projects') + '</div></div>' +
        '<span class="nt-v6-chev">' + svg('down') + '</span></div>');

      var live = info.running
        ? '<i class="nt-v6-spin"></i><b>' + (info.tool ? 'Working' : 'Thinking') + '</b>' + (info.tool ? esc(info.tool) : '...')
        : (info.mainDoneAt && Date.now() - info.mainDoneAt < 600000
          ? '<i class="nt-v6-dot ok"></i><b>Done</b>Answer finished ' + esc(agoText(info.mainDoneAt))
          : '<i class="nt-v6-dot n"></i>' + esc(info.state || 'Ready'));
      html.push('<div class="nt-v6-main' + (info.running ? ' run' : '') + '"><div class="nt-v6-main-top"><div class="nt-v6-ico">' + svg('msgText') + '</div>' +
        '<div class="nt-v6-col"><div class="nt-v6-t1">' + esc(info.title || 'Chat') + '</div>' +
        (info.model ? '<span class="nt-v6-chip"><i class="nt-v6-dot"></i>' + esc(info.model) + '</span>' : '') + '</div>' +
        (info.ageLabel ? '<div class="nt-v6-age" title="Chat age (sending locks at ' + ages.limit + ' h)">' + esc(info.ageLabel) + ring(info.ageFrac) + '</div>' : '') +
        '</div><div class="nt-v6-live">' + live + '</div></div>');

      var chats = info.chats, limit = expanded ? chats.length : Math.max(6, Math.min(chats.length, info.fit));
      // Own chats first and larger than the coworkers (user 2026-10-27: too many coworkers, too little Main).
      html.push('<div class="nt-v6-scroll"><div class="nt-v6-h mine">Your chats' + (info.doneCount ? '<i class="nt-v6-hn done">' + info.doneCount + ' done</i>' : '') + '</div><div class="nt-v6-list' + (expanded ? ' exp' : '') + '">');
      if (!chats.length) html.push('<div class="nt-v6-empty">No other chats</div>');
      pushRows(chats, limit, '');
      html.push('</div>');
      if (chats.length > 4 && (expanded || limit < chats.length)) {
        html.push('<div class="nt-v6-more nt-v6-btn" data-act="more">' + (expanded ? 'Show less' : 'Show all (' + info.total + ')') + svg('right') + '</div>');
      }

      // Coworkers: working or asking ones as cards, idle ones folded into one line (Done ones stay visible).
      var cws = info.coworkers, idle = cws.filter(function (w) { return w.idle; });
      var cwBusy = cws.length - idle.length;
      html.push('<div class="nt-v6-sep"></div><div class="nt-v6-h cwh">Coworkers' + (cws.length ? '<i class="nt-v6-hn' + (cwBusy ? ' on' : '') + '">' + (cwBusy ? cwBusy + ' active' : 'all idle') + '</i>' : '') + '</div>');
      if (!cws.length) html.push('<div class="nt-v6-empty">No coworkers right now</div>');
      cws.forEach(function (w) {
        if (w.idle) return;
        html.push('<div class="nt-v6-cw nt-v6-btn' + (w.live ? ' live' : '') + '" data-act="cw" data-arg="' + esc(w.label) + '" title="Open ' + esc(w.label) + '"><i class="nt-v6-dot ' + w.dot + '"></i>' + avatar(w.label) +
          '<div class="nt-v6-col"><div class="nt-v6-cw-top"><span class="nt-v6-t1">' + esc(w.label) + '</span><span class="nt-v6-when">' + esc(w.age) + '</span></div>' +
          (w.ask
            ? '<div class="nt-v6-cw-sub nt-v6-cw-ask">Needs your answer<span class="nt-v6-answer nt-v6-btn" data-act="cw" data-arg="' + esc(w.label) + '">Answer</span></div>'
            : '<div class="nt-v6-cw-sub">' + esc(w.sub) + '</div><div class="nt-v6-cw-tool">&gt; ' + esc(w.step) + '</div>' +
              (w.ctxLabel ? '<div class="nt-v6-prog">' + bar(w.ctxFrac) + '<em>' + esc(w.ctxLabel) + '</em></div>' : '')) +
          '</div></div>');
      });
      if (idle.length) {
        html.push('<div class="nt-v6-fold nt-v6-btn" data-act="cwidle" title="' + (cwOpen ? 'Hide' : 'Show') + ' idle coworkers">' + svg(cwOpen ? 'down' : 'right') + '<span>' + idle.length + ' idle</span></div>');
        idle.forEach(function (w) {
          if (!cwOpen && !w.done) return;
          html.push('<div class="nt-v6-cwl nt-v6-btn' + (w.done ? ' done' : '') + '" data-act="cw" data-arg="' + esc(w.label) + '" title="Open ' + esc(w.label) + (w.done ? ' - finished answering' : '') + '">' + avatar(w.label) +
            '<span class="nt-v6-t1">' + esc(w.label) + '</span>' +
            (w.done ? '<span class="nt-v6-donetag"><i></i>Done</span>' : '<span class="nt-v6-when">' + esc(w.age) + '</span>') + '</div>');
        });
      }

      // Archive: expired and archived chats get their own folded group below the coworkers.
      var arch = info.archived || [];
      if (arch.length) {
        html.push('<div class="nt-v6-sep"></div><div class="nt-v6-fold nt-v6-arch nt-v6-btn" data-act="arch" title="' + (archOpen ? 'Hide' : 'Show') + ' expired and archived chats">' + svg(archOpen ? 'down' : 'right') + '<span>Archive</span><i>' + arch.length + '</i></div>');
        if (archOpen) { html.push('<div class="nt-v6-list arch">'); pushRows(arch, arch.length, ' arch'); html.push('</div>'); }
      }

      var running = info.tasks.run, queued = info.tasks.queued;
      html.push('</div><div class="nt-v6-sep"></div><div class="nt-v6-pcard nt-v6-btn" data-act="proj" title="Open Projects"><div class="nt-v6-ico">' + svg('folder') + '</div>' +
        '<div class="nt-v6-col"><div class="nt-v6-t1">' + esc(p ? (p.name || 'Project') : 'Tasks') + '</div><div class="nt-v6-t2">' + (running ? running + (running === 1 ? ' task running' : ' tasks running') : 'No tasks running') +
        (queued ? ' \u00b7 ' + queued + ' queued' : '') + (info.tasks.waiting ? ' \u00b7 ' + info.tasks.waiting + ' waiting' : '') + '</div>' +
        (isFinite(projectCommitMs) ? '<div class="nt-v6-t2">Last commit ' + esc(agoText(projectCommitMs)) + '</div>' : '') + '</div></div>');
      html.push('<div class="nt-v6-spacer"></div><div class="nt-v6-new nt-v6-btn" data-act="new" title="Start a new chat">' + svg('plus') + 'New chat</div>');
      html.push('</div>');
      return html.join('');
      function pushRows(arr, n, extra) {
      for (var i = 0; i < arr.length && i < n; i++) {
        var c = arr[i];
        var href = !c.pending && /^[0-9a-f-]{32,36}$/i.test(c.thread_id || '') ? 'https://app.notion.com/chat?t=' + c.thread_id.replace(/-/g, '') : '';
        var tag = href ? 'a' : 'div';
        var tail = c.pending ? '<span class="nt-v6-livetag"><i class="nt-v6-spin"></i>Creating...</span>' :
          (c.live ? '<span class="nt-v6-livetag"><i class="nt-v6-spin"></i>thinking</span>' :
            (c.done ? '<span class="nt-v6-donetag" title="Finished ' + esc(agoText(c.done)) + '"><i></i>Done</span>' : '<span class="nt-v6-when">' + esc(c.age) + '</span>'));
        html.push('<' + tag + (href ? ' href="' + esc(href) + '" style="color:inherit;text-decoration:none"' : '') + ' class="nt-v6-row nt-v6-btn' + (c.live ? ' live' : '') + (c.done ? ' done' : '') + extra + '" data-act="' + (c.pending ? 'creating' : 'chat') + '" data-arg="' + esc(c.title) + '" title="' +
          esc(c.title) + (c.live ? ' - thinking or answering right now' : (c.done ? ' - finished answering, not opened yet' : '')) + '"><span class="nt-v6-rico">' + svg('msgDots') + '</span>' +
          '<span class="nt-v6-t1">' + esc(c.title) + '</span>' + tail + '</' + tag + '>');
      }
      }
    }

    // ---- render: header ----------------------------------------------------------------------
    function headerHtml(info) {
      var limit = ages.limit, h = [];
      // Round 28c (UI review): the Main is the home chat, so it gets no back arrow.
      if (!info.isMain) h.push('<div class="nt-v6-back nt-v6-btn" data-act="back" title="Back">' + svg('back') + '</div>');
      h.push('<div class="nt-v6-ttl"><b>' + esc(info.title || 'Chat') + '</b>' +
        '<span>' + svg('folder') + esc(project ? (project.name || 'Project') : 'No project') + '</span></div>');
      h.push('<div class="nt-v6-stat">');
      h.push('<div class="nt-v6-si nt-v6-btn" data-act="model" title="Model (pinned, never Auto)">' + svg('gauge') + esc(info.model || 'Model') + svg('lock', 'nt-v6-lock') + '</div>');
      h.push('<div class="nt-v6-si nt-v6-btn" data-act="effort" title="Effort">' + svg('zap') + esc(info.effort || '') + '</div>');
      h.push('<div class="nt-v6-si" title="Chat age (sending locks at ' + limit + ' h)">' + ring(info.ageFrac) + esc((info.ageLabel || '-') + ' / ' + limit + 'h') + '</div>');
      // Only coworkers have a context limit; the Main just shows its token count (user review 26.10. 03:21).
      var cf = info.isCoworker ? (Number(info.ctxFrac) || 0) : 0, cOver = cf >= 1, cWarn = !cOver && cf >= 0.8;
      h.push('<div class="nt-v6-si' + (cOver ? ' over' : cWarn ? ' warn' : '') + '" title="' + (cOver ? 'Context is over the limit - Switch context continues in a fresh chat with a handoff' : cWarn ? 'Context is almost full' : 'Context of this chat') + '"><div class="nt-v6-ctx"><small>Context</small><b>' + esc(info.ctxLabel || '-') + '</b>' + (info.isCoworker ? bar(info.ctxFrac) : '') + '</div></div>');
      h.push('<div class="nt-v6-si" title="Steps in the last hour">' + ring(info.stepsFrac) + '<div class="nt-v6-ctx"><small>Steps</small><b>' + esc(info.stepsLabel) + '</b></div></div>');
      var ask = confirmUntil > Date.now();
      // Round 28c: a private chat has no switch path yet - the button is dimmed and its tooltip says why.
      var swPriv = !!info.isPrivate;
      h.push('<div class="nt-v6-si nt-v6-btn' + (swPriv ? ' nt-v6-swoff' : (cOver && !ask ? ' hint' : '')) + '" data-act="switch" title="' + (swPriv ? 'Private chat: open its settings and choose &quot;Make this a Main&quot; to use Switch context' : cOver ? 'Context is over the limit: continue in a fresh chat with a handoff' : 'Continue in a fresh chat with a handoff') + '">' + svg('swap') +
        '<span class="nt-v6-sw' + (ask ? ' ask' : '') + '">' + (ask ? 'Click again to switch' : 'Switch context') + '</span></div>');
      // Move to archive (user 2026-10-27 21:05): renames the chat to "Archived · <name>" so it lands in the Archive group.
      if (!info.isCoworker && info.title && !ARCH_RE.test(str(info.title)) && str(info.title).trim().toLowerCase() !== 'main') {
        var aask = archUntil > Date.now();
        h.push('<div class="nt-v6-si nt-v6-btn nt-v6-archbtn' + (aask ? ' ask' : '') + '" data-act="archive" title="Rename this chat to Archived · name and move it into the Archive group">' + svg('archive') +
          '<span>' + (aask ? 'Click again to archive' : 'Move to archive') + '</span></div>');
      }
      h.push('<div class="nt-v6-si nt-v6-stop nt-v6-btn' + (info.running ? '' : ' off') + '" data-act="stop" title="' + (info.running ? 'Stop the answer' : 'Nothing is running') + '"><span><i></i>Stop</span></div>');
      h.push('</div>');
      return h.join('');
    }

    function collect() {
      var title = currentTitle();
      var w = currentWorker(title);
      var own = w ? null : switchTarget(title);
      var mb = modelButton(), eb = effortButton(), sbtn = stopButton();
      var hours = ageFor(w ? w.thread_id : currentThread(), title);
      if (!isFinite(hours) && w && Number(w.age_s) >= 0 && w.age_s != null) hours = Number(w.age_s) / 3600;
      if (!isFinite(hours)) hours = threadAge(w ? w.thread_id : currentThread());  // round 25: own chats
      var em = str(eb && eb.getAttribute('aria-label')).match(/currently\s+([\w-]+(?:\s[\w-]+)*)/i);
      var wctx = obj(w && w.context);
      var steps = obj(state.steps_hour), stepsLimit = Number(steps.limit) > 0 ? Number(steps.limit) : 450, stepsUsed = Number(steps.used) || 0;
      var rows = notionRows(), chats = [], total = 0, tmap = titleThreads(), rowLiveMap = {}, liveCount = 0;
      rows.forEach(function (r) {
        var lv = rowLive(r.el);
        rowLiveMap[r.title] = lv;
        if (lv) liveCount++;
        if (COWORKER_RE.test(r.title)) return;
        total++;
        if (r.title === title) return;
        chats.push({ title: r.title, age: ageText(ageFor(tmap[r.title] || '', r.title)), live: lv });
      });
      chats = mergePrivateChats(chats, list(state.private_chats));
      // The open chat is shown in the main card; it must never also appear in the Chats list.
      // Notion's own row for it was skipped above (r.title === title), but a private chat with the same
      // title/thread was merged back in by mergePrivateChats, causing a duplicate (user report 2026-10-27).
      var curKey = currentThread();
      var curTitle = title;
      chats = chats.filter(function (c) {
        if (curKey && c.thread_id && key(c.thread_id) === curKey) return false;
        if (curTitle && c.title === curTitle) return false;
        return true;
      });
      chats.forEach(function (chat) {
        if (!chat.age && chat.thread_id) chat.age = ageText(ageFor(chat.thread_id, chat.title));
      });
      total = chats.length;
      var cws = workers().filter(isCoworker).map(function (x) {
        x = obj(x);
        var s = str(x.state).toLowerCase(), ask = needsAnswer(x), label = str(x.label) || 'Coworker';
        var lv = !ask && (rowLiveMap[label] === true || (label === title && !!sbtn));
        var dot = ask ? 'a' : (BAD[s] ? 'r' : (lv ? 'g p' : (x.preparing || THINK[s] ? 'v' : (BUSY[s] ? 'g' : 'n'))));
        var c = obj(x.context), est = Number(c.estimated_tokens), lim = Number(c.limit);
        return {
          label: label, dot: dot, ask: ask, live: lv,
          age: ageText(isFinite(ageFor(x.thread_id, x.label)) ? ageFor(x.thread_id, x.label) : (x.age_s != null ? Number(x.age_s) / 3600 : NaN)),
          sub: str(x.task_title) || (lv ? 'Answering' : (BUSY[s] ? 'Working' : 'Idle')),
          step: lv ? (label === title && sbtn ? (lastTool() || 'thinking...') : 'thinking...') :
            (x.preparing ? 'preparing...' : (BUSY[s] ? 'working...' : (THINK[s] ? 'thinking...' : (s || 'ready')))),
          ctxLabel: isFinite(est) && lim > 0 ? fmtTok(est) + ' / ' + fmtTok(lim) : '',
          ctxFrac: lim > 0 ? est / lim : 0,
          idle: !ask && dot === 'n'
        };
      });
      cws.sort(function (a, b) { return (a.live ? 0 : (a.idle ? 2 : 1)) - (b.live ? 0 : (b.idle ? 2 : 1)); });
      var running = !!sbtn || rowLiveMap[title] === true;
      var dmap = trackDone(rowLiveMap, title, running);
      chats.forEach(function (c, i) { c.done = !c.live && !c.pending ? (dmap[c.title] || 0) : 0; c.ord = i; });
      var archived = chats.filter(function (c) { return !c.live && !c.pending && ARCH_RE.test(str(c.title)); });
      chats = chats.filter(function (c) { return archived.indexOf(c) < 0; });
      chats.sort(function (a, b) { return (a.live ? 0 : (a.done ? 1 : 2)) - (b.live ? 0 : (b.done ? 1 : 2)) || a.ord - b.ord; });
      total = chats.length;
      var doneCount = chats.filter(function (c) { return c.done; }).length;
      cws.forEach(function (x) { x.done = x.idle && dmap[x.label] ? dmap[x.label] : 0; });
      // Compact coworkers leave the room to the user's own chats.
      var cwH = 0, idleN = 0;
      cws.forEach(function (x) { if (x.idle) { idleN++; if (cwOpen || x.done) cwH += 30; } else cwH += x.ask ? 62 : CW_H + 16; });
      if (idleN) cwH += 30;
      if (archived.length) cwH += 44 + (archOpen ? archived.length * 37 : 0);
      var box = sidebarBox(), boxH = box ? box.getBoundingClientRect().height : innerHeight;
      var fit = Math.floor((boxH - FIXED_H - Math.max(44, cwH)) / ROW_H);
      return {
        title: title || (w ? str(w.label) : ''),
        model: mb ? text(mb) : (w ? str(w.model) : ''),
        effort: effortPick(currentThread()) || (em ? cap(em[1]) : ''),
        running: running,
        tool: sbtn ? lastTool() : '',
        liveCount: liveCount,
        state: w ? cap(str(w.state)) : 'Ready',
        ageLabel: ageText(hours),
        ageFrac: isFinite(hours) ? hours / ages.limit : 0,
        isMain: !!((w && (w.purpose === 'main' || w.purpose === 'operator')) || (own && own.purpose === 'main')),
        isPrivate: !!(own && own.purpose === 'private'),
        isCoworker: !!(w && isCoworker(w)),
        ctxLabel: (w && isCoworker(w)) ? (Number(wctx.limit) > 0 ? fmtTok(wctx.estimated_tokens) + ' / ' + fmtTok(wctx.limit) : '') :
          (wctx.estimated_tokens != null && isFinite(Number(wctx.estimated_tokens)) ? fmtTok(wctx.estimated_tokens) + ' tokens' : ''),
        ctxFrac: Number(wctx.limit) > 0 ? Number(wctx.estimated_tokens) / Number(wctx.limit) : 0,
        stepsLabel: stepsUsed + ' / ' + stepsLimit,
        stepsFrac: stepsUsed / stepsLimit,
        chats: chats, total: total, fit: fit, coworkers: cws, tasks: tasksRunning(),
        archived: archived, doneCount: doneCount, mainDoneAt: mainDoneAt
      };
    }

    function findTopRow() {
      var cached = document.querySelector('.' + CLS_TOPROW);
      if (cached && cached.isConnected && shown(cached)) return cached;
      var b = document.querySelector('[aria-label="Start new chat"]') || document.querySelector('.nt-desk-chat-title');
      var lc = b && b.closest('.layout-content');
      if (!lc) { if (cached) cached.classList.remove(CLS_TOPROW); return null; }
      var lw = lc.getBoundingClientRect().width, el = b;
      while (el && el.parentElement && el.parentElement !== lc) {
        var r = el.getBoundingClientRect();
        if (r.width >= lw * 0.9 && r.height > 0 && r.height < 120) break;
        el = el.parentElement;
      }
      if (!el || el === lc || el.getBoundingClientRect().height >= 120) return null;
      if (cached && cached !== el) cached.classList.remove(CLS_TOPROW);
      el.classList.add(CLS_TOPROW);
      return el;
    }

    function ensureNodes() {
      if (!defs || !defs.isConnected) {
        defs = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        defs.setAttribute('data-nt-desk-own', 'v6');
        defs.setAttribute('width', '0');
        defs.setAttribute('height', '0');
        defs.setAttribute('aria-hidden', 'true');
        defs.style.cssText = 'position:absolute;width:0;height:0;overflow:hidden';
        defs.innerHTML = '<defs><linearGradient id="ntv6g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#8b5cf6"/>' +
          '<stop offset="1" stop-color="#22d3ee"/></linearGradient></defs>';
        document.body.appendChild(defs);
      }
      if (!sb || !sb.isConnected) {
        sb = document.createElement('div');
        sb.className = 'nt-v6-sb';
        sb.setAttribute('data-nt-desk-own', 'v6');
        sb.setAttribute('aria-label', 'Chats and coworkers');
        sb.addEventListener('click', onClick, true);
        sb.addEventListener('scroll', fadeScroll, true);
        document.body.appendChild(sb);
        lastSb = ''; lastBox = '';
      }
      if (!hd || !hd.isConnected) {
        hd = document.createElement('div');
        hd.className = 'nt-v6-hd';
        hd.setAttribute('data-nt-desk-own', 'v6');
        hd.setAttribute('aria-label', 'Chat header');
        hd.addEventListener('click', onClick, true);
      hd.addEventListener('pointerdown', onPillDown, true);
        document.body.appendChild(hd);
        lastHd = ''; lastHdBox = '';
      }
      if (!dg || !dg.isConnected) {
        dg = document.createElement('div');
        dg.className = 'nt-v6-dg nt-v6-btn';
        dg.setAttribute('data-nt-desk-own', 'v6');
        dg.setAttribute('data-act', 'delegate');
        dg.setAttribute('role', 'button');
        dg.title = 'Give a task to a coworker (opens Live)';
        dg.innerHTML = svg('users') + 'Delegate';
        dg.addEventListener('click', onClick, true);
        document.body.appendChild(dg);
      }
    }

    // Own frame (user 26.10. 03:31): the sidebar is NotionTool's, with its own width and resize handle.
    // Notion's sidebar box only reserves that width and stays invisible behind it.
    var wk = null, lastWk = '';
    // user 2026-10-28 04:02: the chat is the focus - narrower panel by default (new key, so the old 320 is not reused)
    // 260 is the narrowest width Notion's own sidebar box accepts; below it Notion showed through next to the panel
    var SBW_KEY = 'nt-v6-sbw3', SBW_MIN = 260, SBW_MAX = 520, sbw = 260, rz = null, rzDrag = null, rzRaf = 0;
    try { var sbv = parseInt(localStorage.getItem(SBW_KEY), 10); if (sbv >= SBW_MIN && sbv <= SBW_MAX) sbw = sbv; } catch (e) { /* ignore */ }
    function railRight() {
      // user 2026-10-28 04:18: the rail is a small icon row on top of the panel now (like Notion), Notion is not pushed right
      return 0;
    }
    function applySbw() {
      if (rzDrag) return;  // user 2026-10-27 21:05: Notion re-laid out behind the panel while dragging; apply on release
      var v = sbw + 'px';
      if (root.style.getPropertyValue('--nt-v6-sbw') !== v) root.style.setProperty('--nt-v6-sbw', v);
    }
    // user 2026-10-28 04:06: the panel always covers Notion's whole sidebar box, also when Notion keeps it wider than sbw
    function sbRect() {
      applySbw();
      var left = railRight(), w = sbw;
      try {
        var sc = document.querySelector('.notion-sidebar-container'), scR = sc && sc.getBoundingClientRect();
        if (scR && scR.width) { var need = Math.round(scR.right) - left; if (need > w && need < w + 240) w = need; }
      } catch (e) { /* ignore */ }
      return { left: left, top: 0, width: w, height: window.innerHeight };
    }
    function placeRz(r) {
      if (!rz || !rz.isConnected) {
        rz = document.createElement('div');
        rz.className = 'nt-v6-rz';
        rz.setAttribute('data-nt-desk-own', 'v6');
        rz.setAttribute('role', 'separator');
        rz.setAttribute('aria-label', 'Resize sidebar');
        rz.addEventListener('pointerdown', onRzDown);
        document.body.appendChild(rz);
      }
      var x = Math.round(r.left + r.width - 11 - 4) + 'px';
      if (rz.style.left !== x) rz.style.left = x;
      rz.classList.add(CLS_SHOW);
    }
    function onRzDown(e) {
      if (e.button !== 0) return;
      e.preventDefault();
      rzDrag = { x0: e.clientX, w0: sbw };
      rz.classList.add('drag');
      root.classList.add('nt-v6-rz-drag');
      try { rz.setPointerCapture(e.pointerId); } catch (x) { /* ignore */ }
      rz.addEventListener('pointermove', onRzMove);
      rz.addEventListener('pointerup', onRzUp);
      rz.addEventListener('pointercancel', onRzUp);
    }
    function onRzMove(e) {
      if (!rzDrag) return;
      var w = Math.max(SBW_MIN, Math.min(SBW_MAX, Math.round(rzDrag.w0 + e.clientX - rzDrag.x0)));
      if (w === sbw) return;
      sbw = w;
      var r = sbRect();
      lastBox = place(sb, r, lastBox);
      placeRz(r);
      // Header, feed and composer follow in the same frame (user 2026-10-27: Notion showed through while dragging).
      try { tick(); } catch (x) { /* ignore */ }  // header and feed follow in the same frame
      try {  // rzHdFollow: Notion stays frozen while dragging, so the header follows the panel directly
        var hdN = document.querySelector('.nt-v6-hd'), sR = sbRect(), hdL = Math.round(sR.left + sR.width + 12);
        if (hdN) { hdN.style.left = hdL + 'px'; hdN.style.width = Math.max(0, window.innerWidth - hdL - 12) + 'px'; }
      } catch (x) { /* ignore */ }
    }
    function onRzUp() {
      if (!rzDrag) return;
      rzDrag = null;
      applySbw();
      rz.classList.remove('drag');
      root.classList.remove('nt-v6-rz-drag');
      rz.removeEventListener('pointermove', onRzMove);
      rz.removeEventListener('pointerup', onRzUp);
      rz.removeEventListener('pointercancel', onRzUp);
      try { localStorage.setItem(SBW_KEY, String(sbw)); } catch (x) { /* ignore */ }
      render();
    }

    function place(node, r, last) {
      var sig = Math.round(r.left) + ',' + Math.round(r.top) + ',' + Math.round(r.width) + ',' + Math.round(r.height);
      if (sig !== last) {
        node.style.left = Math.round(r.left) + 'px';
        node.style.top = Math.round(r.top) + 'px';
        node.style.width = Math.round(r.width) + 'px';
        node.style.height = Math.round(r.height) + 'px';
      }
      return sig;
    }

    // Fade the sidebar list edges while more chats or coworkers are scrolled out of view.
    function fadeScroll() {
      var sc = sb && sb.querySelector('.nt-v6-scroll');
      if (!sc) return;
      var top = sc.scrollTop > 2, below = sc.scrollHeight - sc.scrollTop - sc.clientHeight > 2;
      if (sc.classList.contains('ft') !== top) sc.classList.toggle('ft', top);
      if (sc.classList.contains('fb') !== below) sc.classList.toggle('fb', below);
    }

    // round 19 (user 2026-10-28 05:02: Notion showed through on chat switch, window move and start): in a chat
    // Notion's app paints nothing by default (v6.css html.nt-v6-mask); only our nodes, the composer, menus and
    // the chat area paint. The boot cover of the desktop bootstrap goes once Notion's own layout is there.
    function maskSync() {
      var chat = document.querySelector('.layout-chat');
      var on = !!(chat || currentThread());
      if (root.classList.contains('nt-v6-mask') !== on) root.classList.toggle('nt-v6-mask', on);
      if (typeof window.__ntBootCoverOff === 'function' && (chat || document.querySelector('.layout-content,.notion-frame'))) {
        var off = window.__ntBootCoverOff; window.__ntBootCoverOff = null; off();
      }
    }

    function render() {
      if (stopped || !document.body) return;
      if (!root.classList.contains(CLS_ON)) root.classList.add(CLS_ON);
      resumeTick();
      maskSync();
      ensureNodes();
      var now = Date.now();
      if (now - agesAt > AGES_MS) loadAges();
      if (now - projectsAt > PROJECTS_MS) loadProject();
      var info = collect();

      var r = sbRect();
      placeRz(r);
      if (r.height > 200) {
        lastBox = place(sb, r, lastBox);
        var html = sidebarHtml(info);
        if (html !== lastSb) { morph(sb, html); lastSb = html; }
        fadeScroll();
        sb.classList.add(CLS_SHOW);
      } else {
        sb.classList.remove(CLS_SHOW);
      }

      var row = findTopRow(), hr = row && row.getBoundingClientRect();
      if (hr && hr.width > 300) {
        lastHdBox = place(hd, hr, lastHdBox);
        var hh = headerHtml(info);
        if (hh !== lastHd) { morph(hd, hh); lastHd = hh; }
        hd.classList.add(CLS_SHOW);
      } else {
        hd.classList.remove(CLS_SHOW);
      }

      decorateRail(info.liveCount);
      var cr = decorateChat(info.title);
      if (cr && !root.classList.contains('nt-shell-view-open')) {
        var dsig = Math.round(cr.right + 4) + ',' + Math.round(cr.top) + ',' + Math.round(cr.height);
        if (dsig !== lastDg) {
          dg.style.left = Math.round(cr.right + 4) + 'px';
          dg.style.top = Math.round(cr.top) + 'px';
          dg.style.height = Math.round(cr.height) + 'px';
          lastDg = dsig;
        }
        dg.classList.add(CLS_SHOW);
      } else {
        dg.classList.remove(CLS_SHOW);
      }

      // "Model is working..." row above the composer while the open chat answers (mockup).
      if (cr && info.running && !root.classList.contains('nt-shell-view-open')) {
        if (!wk || !wk.isConnected) {
          wk = document.createElement('div');
          wk.className = 'nt-v6-work';
          wk.setAttribute('data-nt-desk-own', 'v6');
          wk.innerHTML = '<i class="nt-v6-wring"></i><span>Model is working...</span>';
          document.body.appendChild(wk);
          lastWk = '';
        }
        var wsig = Math.round(cr.left) + ',' + Math.round(cr.top);
        if (wsig !== lastWk) { wk.style.left = Math.round(cr.left + 2) + 'px'; wk.style.top = Math.round(cr.top - 36) + 'px'; lastWk = wsig; }
        wk.classList.add(CLS_SHOW);
      } else if (wk) wk.classList.remove(CLS_SHOW);
    }

    // Notion's "Scroll to bottom" button sits on top of the last message text; park it beside the composer.
    function placeScrollBtn() {
      var b = document.querySelector('[aria-label="Scroll to bottom"]'), c = document.querySelector('.nt-desk-composer');
      if (!b || !c) return;
      var cr = c.getBoundingClientRect();
      if (!cr.width) return;
      mark(b, 'scroll-btn');
      // An ancestor is transformed, so position:fixed is not viewport-based: shift from the natural spot instead.
      var prev = b.style.getPropertyValue('transform');
      b.style.setProperty('transform', 'none', 'important');
      var r = b.getBoundingClientRect();
      // centred just above the composer (user review 26.10. 03:21)
      var dx = Math.round(cr.left + cr.width / 2 - r.width / 2 - r.left), dy = Math.round(cr.top - r.height - 12 - r.top);
      var next = 'translate(' + dx + 'px, ' + dy + 'px)';
      b.style.setProperty('transform', r.width ? next : prev || 'none', 'important');
    }

    // Notion's artifacts side panel popped up on the right for a few seconds until the throttled
    // declutter scan caught it. Hide it in the same frame it is added.
    var goneObs = null, goneQ = [], goneRaf = 0, GONE_RE = /^\s*(Artifacts|Artefakte)\s*$/;
    function hideArtifacts(scope) {
      var w = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT), tn, W = window.innerWidth;
      while ((tn = w.nextNode())) {
        if (!GONE_RE.test(tn.nodeValue)) continue;
        var el = tn.parentElement, best = null;
        if (!el || el.closest('[data-nt-desk-own],.nt-desk-message,[data-message-id],.notion-page-content,[contenteditable="true"]')) continue;
        for (var k = 0; el && el !== document.body && k < 14; k++, el = el.parentElement) {
          var r = el.getBoundingClientRect();
          if (r.width > 0 && r.left > W * 0.55 && r.width < 520) best = el;
          else if (r.width > 0) break;
        }
        if (best) best.classList.add('nt-v6-gone');
      }
    }
    function goneCheck() {
      goneRaf = 0;
      var q = goneQ; goneQ = [];
      for (var i = 0; i < q.length && i < 400; i++) {
        var n = q[i];
        if (!n.isConnected) continue;
        var t = n.textContent || '';
        if (t.length > 4000 || t.indexOf('Artifacts') < 0 && t.indexOf('Artefakte') < 0) continue;
        try { hideArtifacts(n); } catch (e) { /* ignore */ }
      }
    }
    function startGone() {
      if (goneObs || !window.MutationObserver) return;
      goneObs = new MutationObserver(function (ms) {
        for (var i = 0; i < ms.length; i++) {
          var a = ms[i].addedNodes;
          for (var j = 0; j < a.length; j++) if (a[j].nodeType === 1) goneQ.push(a[j]);
        }
        if (goneQ.length && !goneRaf) goneRaf = requestAnimationFrame(goneCheck);
      });
      goneObs.observe(document.body, { childList: true, subtree: true });
      try { hideArtifacts(document.body); } catch (e) { /* ignore */ }
    }
    function stopGone() { if (goneObs) { goneObs.disconnect(); goneObs = null; } goneQ = []; }

    // ---- own chat feed (own frame step 2): NotionTool draws the messages itself -------------
    // Notion's message list keeps running hidden underneath (data-nt-v6-src); every visible turn
    // is mirrored into the feed, clicks on buttons are forwarded to the original element.
    var fd = null, fdIn = null, fdSc = null, fdOlder = null, fdWork = null, fdMap = new Map(), fdList = null, fdObs = null, fdQ = 0, fdStick = true;
    // smooth + no blank chat (user 2026-10-27 22:25): only rows that changed are re-read; when Notion
    // re-mounts its list (voice recording cancelled) the mirrored turns stay for up to FEED_GRACE_MS.
    var fdDirty = new Set(), fdAll = true, fdFullAt = 0, fdGone = 0, fdThread = '', fdScr = null, fdScrAt = 0, fdPlaced = '', fdRo = null;
    var FEED_GRACE_MS = 6000, FEED_FULL_MS = 3000;
    // user 2026-10-28 04:19: own chats stay loaded - the last turns of the last chats are kept, so a switch shows them
    // at once while Notion loads the thread. 04:24: only the last 5 messages per chat, never the whole history.
    // Coworker chats are not kept.
    var fdCache = new Map(), fdSnap = null, fdSnapAt = 0, fdTitle = '', FD_CACHE_MAX = 8, FD_KEEP_TURNS = 5, FD_SNAP_MS = 15000;
    function feedStash() {
      if (!fdIn || !fdThread || fdSnap || COWORKER_RE.test(fdTitle)) return;
      var keep = [];
      for (var c = fdIn.firstChild; c; c = c.nextSibling) if (c !== fdWork && c.nodeType === 1 && c.hasAttribute('data-nt-v6-turn')) keep.push(c);
      keep = keep.slice(-FD_KEEP_TURNS);
      if (!keep.length) return;
      for (var i = 0; i < keep.length; i++) keep[i].__ntOrig = null;  // do not hold Notion's old nodes in memory
      fdCache.delete(fdThread); fdCache.set(fdThread, keep);
      while (fdCache.size > FD_CACHE_MAX) fdCache.delete(fdCache.keys().next().value);
      feedLsPut(fdThread, keep);
    }
    // round 21 (user 2026-10-28 06:14, switch must be instant): the kept last turns also live in localStorage,
    // so own chats are there at once even right after a restart; a chat that was never opened shows an
    // empty feed while Notion loads it (FD_HOLD_MS) instead of a blank chat or Notion underneath.
    var FD_LS = 'nt-v6-fc:', FD_LS_IDX = 'nt-v6-fc-idx', FD_LS_MAX = 400000, fdHoldAt = 0, FD_HOLD_MS = 4000;
    // round 22 (user 06:26: switch still loads, blinks and jumps to the top):
    // - a click on a chat row shows that chat's kept turns in the same click, before Notion navigates (fdPend);
    // - the old chat's list is never mirrored into the new chat while Notion still shows it (fdOld);
    // - only the user's own scrolling unpins the bottom (fdUserAt / fdUserDrag), never a content swap.
    var fdUserAt = 0, fdUserDrag = false, fdPend = '', fdPendAt = 0, fdOld = '';
    function feedUserKey(e) {
      var t = e.target;
      if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
      if (/^(PageUp|PageDown|ArrowUp|ArrowDown|Home|End| )$/.test(e.key)) fdUserAt = Date.now();
    }
    function feedUserUp() { fdUserDrag = false; }
    function feedRowClick(ev) {
      if (stopped) return;
      var r = ev.target && ev.target.closest && ev.target.closest('[data-nt-desk="chat-row"]');
      if (!r) { feedLinkClick(ev); return; }
      var b = ev.target.closest('button,[aria-haspopup],input,textarea,[contenteditable="true"]');
      if (b && b !== r && r.contains(b)) return;  // row menu or rename field: no switch
      feedSwitch(r.getAttribute('data-nt-desk-thread') || '', r.textContent || '');
    }
    // round 24 (user 2026-10-28 06:47: switches still not instant): Notion's own chat list rows are links with
    // the thread in the URL (?t=...). A click there gets the same instant switch as our own rows.
    function feedLinkClick(ev) {
      if (ev.button || ev.ctrlKey || ev.metaKey || ev.shiftKey || ev.altKey) return;
      var a = ev.target.closest('a[href]');
      if (!a || a.closest('.nt-v6-feed')) return;
      var b = ev.target.closest('button,[aria-haspopup],input,textarea,[contenteditable="true"]');
      if (b && a.contains(b)) return;
      var t = '';
      try {
        var u = new URL(a.getAttribute('href'), location.href);
        t = u.searchParams.get('t') || u.searchParams.get('thread') || u.searchParams.get('threadId') || '';
      } catch (e) { t = ''; }
      if (!t || !/^[0-9a-f-]{32,36}$/i.test(t)) return;
      feedSwitch(key(t), a.textContent || '');
    }
    function feedSwitch(k, title) {
      var host = document.querySelector('.layout-chat'), from = currentThread();
      if (!k || k === fdThread || k === from || !host || !document.querySelector('.nt-v6-hd')) return false;
      try { if (localStorage.getItem('nt-v6-feed') === '0') return false; } catch (e) { /* ignore */ }
      fdOld = fdList ? feedSig(fdList) : '';
      feedStash(); fdSnap = null; fdGone = 0; fdTitle = title;
      fdMap = new Map(); if (fdIn) fdIn.textContent = ''; fdWork = null; fdStick = true; fdAll = true;
      fdThread = k; fdHoldAt = Date.now(); fdPend = from; fdPendAt = Date.now();
      askEffort(k);  // round 24: the effort of the new chat is fetched at the click, not after the switch
      if (!feedSnapShow(host, k)) feedHold(host);
      return true;
    }
    document.addEventListener('click', feedRowClick, true);
    document.addEventListener('keydown', feedUserKey, true);
    window.addEventListener('pointerup', feedUserUp, true);
    // round 25 (user 07:10, screenshot: the whole view moved left out of nowhere): boxes with overflow hidden
    // (Notion's app frame is 1980 px wide in a 1320 px window) can still be scrolled sideways by focus or
    // scrollIntoView. Such a box is put back at once; real sideways scrollers (code, tables, the feed) stay.
    function noSideScroll(ev) {
      var t = ev.target;
      if (t === document || t === window) t = document.scrollingElement;
      if (!t || t.nodeType !== 1 || !t.scrollLeft) return;
      if (t.closest('.nt-v6-feed, pre, table, code')) return;
      var ox = getComputedStyle(t).overflowX;
      if (ox === 'hidden' || ox === 'clip' || t === document.scrollingElement) t.scrollLeft = 0;
    }
    document.addEventListener('scroll', noSideScroll, true);
    function feedLsIdx(th, drop) {
      var idx = [];
      try { idx = JSON.parse(localStorage.getItem(FD_LS_IDX) || '[]'); } catch (e) { idx = []; }
      if (!Array.isArray(idx)) idx = [];
      idx = idx.filter(function (k) { return k !== th; });
      if (!drop) idx.push(th);
      while (idx.length > FD_CACHE_MAX) { try { localStorage.removeItem(FD_LS + idx.shift()); } catch (e) { /* ignore */ } }
      try { localStorage.setItem(FD_LS_IDX, JSON.stringify(idx)); } catch (e) { /* ignore */ }
    }
    function feedLsPut(th, keep) {
      // round 25 (user 07:10: a switch still shows nothing first): a big chat keeps its newest turns that
      // fit instead of no stored turns at all.
      var parts = [], html = '';
      for (var i = 0; i < keep.length; i++) parts.push(keep[i].outerHTML);
      while (parts.length > 1 && parts.join('').length > FD_LS_MAX) parts.shift();
      html = parts.join('');
      try {
        if (html.length > FD_LS_MAX) { localStorage.removeItem(FD_LS + th); feedLsIdx(th, true); return; }
        localStorage.setItem(FD_LS + th, html); feedLsIdx(th, false);
      } catch (e) { /* storage full: memory cache only */ }
    }
    function feedLsDel(th) { try { localStorage.removeItem(FD_LS + th); } catch (e) { /* ignore */ } feedLsIdx(th, true); }
    function feedLsGet(th) {
      var html = null;
      try { html = localStorage.getItem(FD_LS + th); } catch (e) { html = null; }
      if (!html) return null;
      var tpl = document.createElement('template');
      tpl.innerHTML = html;
      var bad = tpl.content.querySelectorAll('script,iframe,object,embed');
      for (var b = 0; b < bad.length; b++) bad[b].remove();
      var all = tpl.content.querySelectorAll('*');
      for (var a = 0; a < all.length; a++) {
        for (var k = all[a].attributes.length - 1; k >= 0; k--) if (/^on/i.test(all[a].attributes[k].name)) all[a].removeAttribute(all[a].attributes[k].name);
      }
      var keep = [];
      for (var c = tpl.content.firstElementChild; c; c = c.nextElementSibling) if (c.hasAttribute('data-nt-v6-turn')) keep.push(c);
      return keep.length ? keep : null;
    }
    function feedHold(host) {
      if (!fdHoldAt || Date.now() - fdHoldAt > FD_HOLD_MS) return false;
      if (!fdOld && host.querySelector('[data-agent-chat-user-step-id]')) return false;  // Notion shows this chat: its own list
      feedEnsure(host); root.classList.add('nt-v6-feed-on'); feedPlace();
      return true;
    }
    function feedSnapShow(host, th) {
      var keep = fdCache.get(th) || feedLsGet(th);
      if (!keep || !keep.length) return false;
      fdCache.set(th, keep);
      feedEnsure(host);
      fdIn.textContent = '';
      for (var i = 0; i < keep.length; i++) fdIn.appendChild(keep[i]);
      fdSnap = keep; fdSnapAt = Date.now(); fdThread = th; fdStick = true; fdSnapTh = th;
      root.classList.add('nt-v6-feed-on');
      feedPlace(); fdSc.scrollTop = fdSc.scrollHeight;
      return true;
    }
    function feedSnapDrop() {
      if (!fdSnap) return;
      for (var i = 0; i < fdSnap.length; i++) if (!fdSnap[i].__ntOrig && fdSnap[i].parentNode) fdSnap[i].parentNode.removeChild(fdSnap[i]);
      fdSnap = null;
    }
    var FD_SEL = '[role="button"],button,a,[aria-expanded]';
    // round 32 (user 18:35): levels from a user row up to Notion's turn list (4 today, learned from 2+ rows)
    var fdDepth = 4, fdSnapTh = '', fdBusy = false, fdCost = 0;
    function feedList() {
      var lc = document.querySelector('.layout-content');
      var u = lc && lc.querySelector('[data-agent-chat-user-step-id]');
      if (!u) return null;
      for (var e = u.parentElement, i = 0; e && e !== lc && i < 12; i++, e = e.parentElement) {
        if (e.hasAttribute('data-nt-cv')) return e;
      }
      for (e = u.parentElement, i = 0; e && e !== lc && i < 12; i++, e = e.parentElement) {
        var n = 0;
        for (var k = 0; k < e.children.length && n < 2; k++) if (e.children[k].querySelector('[data-agent-chat-user-step-id]')) n++;
        if (n >= 2) { fdDepth = i + 1; return e; }
      }
      // round 32 (user 18:35: the newest message showed, then vanished): a chat with one user turn has no
      // second user row, so its list was never found and the kept old turns came back again and again.
      for (e = u, i = 0; e && i < fdDepth; i++) e = e.parentElement;
      if (e && e !== lc && lc.contains(e) && e.children.length >= 2 && !e.querySelector('.nt-desk-composer,[data-nt-v6-comp]')) return e;
      return null;
    }
    function feedSig(list) {
      var u = list && list.querySelector('[data-agent-chat-user-step-id]');
      return u ? u.getAttribute('data-agent-chat-user-step-id') || '' : '';
    }
    function feedScroller(el) {
      for (var e = el; e && e !== document.body; e = e.parentElement) {
        var s = getComputedStyle(e).overflowY;
        if ((s === 'auto' || s === 'scroll') && e.scrollHeight > e.clientHeight + 4) return e;
      }
      return null;
    }
    function feedScr() {  // cached: the getComputedStyle walk ran on every sync
      if (fdScr && fdScr.isConnected && fdList && fdScr.contains(fdList)) return fdScr;
      if (Date.now() - fdScrAt < 2000) return null;
      fdScrAt = Date.now();
      fdScr = fdList ? feedScroller(fdList) : null;
      return fdScr;
    }
    function feedVisible(list) {
      for (var i = 0; i < list.children.length; i++) {
        var n = list.children[i];
        if (!n.hasAttribute('data-nt-desk-own') && n.getClientRects().length) return true;
      }
      return false;
    }
    function feedEnsure(host) {
      if (fd && fd.parentNode === host) return;
      if (fd && fdSc) {  // round 22: moved, content and bottom kept (a rebuild lost the kept turns = blink)
        var fb = fdSc.scrollHeight - fdSc.scrollTop - fdSc.clientHeight;
        host.appendChild(fd); fdPlaced = ''; feedPlace();
        fdSc.scrollTop = fdStick ? fdSc.scrollHeight : fdSc.scrollHeight - fdSc.clientHeight - fb;
        root.classList.add('nt-v6-feed-on');
        return;
      }
      if (fd) fd.remove();
      fd = document.createElement('div');
      fd.className = 'nt-v6-feed';
      fd.setAttribute('data-nt-desk-own', 'v6');
      fd.innerHTML = '<div class="nt-v6-feed-sc"><button type="button" class="nt-v6-older">Load older messages</button><div class="nt-v6-feed-in"></div></div>' +
        '<button type="button" class="nt-v6-down" aria-label="Scroll to bottom"><svg viewBox="0 0 16 16" width="16" height="16"><path d="M4 6l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg></button>';
      host.appendChild(fd);
      fdSc = fd.querySelector('.nt-v6-feed-sc'); fdIn = fd.querySelector('.nt-v6-feed-in'); fdOlder = fd.querySelector('.nt-v6-older');
      fdMap = new Map(); fdWork = null; fdPlaced = '';
      if (window.ResizeObserver) {  // keep the bottom in view when images or code blocks grow
        if (fdRo) fdRo.disconnect();
        fdRo = new ResizeObserver(function () { if (fdStick && fdSc) fdSc.scrollTop = fdSc.scrollHeight; });
        fdRo.observe(fdIn);
      }
      fdSc.addEventListener('scroll', function () {
        var atBottom = fdSc.scrollHeight - fdSc.scrollTop - fdSc.clientHeight < 40;
        if (atBottom) fdStick = true;
        else if (fdUserDrag || Date.now() - fdUserAt < 700) fdStick = false;  // the user scrolled up
        else if (fdStick) fdSc.scrollTop = fdSc.scrollHeight;  // content swap moved it: back to the bottom
        fd.classList.toggle('nt-v6-away', !fdStick);
      }, { passive: true });
      ['wheel', 'touchmove'].forEach(function (n) { fdSc.addEventListener(n, function () { fdUserAt = Date.now(); }, { passive: true }); });
      fdSc.addEventListener('pointerdown', function () { fdUserDrag = true; fdUserAt = Date.now(); });
      fd.querySelector('.nt-v6-down').addEventListener('click', function () { fdSc.scrollTop = fdSc.scrollHeight; fdStick = true; fd.classList.remove('nt-v6-away'); });
      fdOlder.addEventListener('click', feedOlder);
      fd.addEventListener('click', feedClick, true);
      root.classList.add('nt-v6-feed-on');
    }
    function feedOlder() {
      if (!fdList) return;
      var h = fdSc.scrollHeight, tv = fdList.getAttribute('data-nt-tl2');
      if (tv) {
        var cur = parseInt(tv, 10) || 8, next = cur < 20 ? 20 : cur < 40 ? 40 : cur < 80 ? 80 : 0;
        if (next && fdList.children.length > next) fdList.setAttribute('data-nt-tl2', String(next)); else fdList.removeAttribute('data-nt-tl2');
      } else {
        var s = feedScroller(fdList);
        if (s) s.scrollTop = 0;
        else { toast('This is the start of the chat', 'info'); return; }
      }
      fdOlder.textContent = 'Loading…';
      setTimeout(function () { feedSync(); if (fdSc) fdSc.scrollTop += fdSc.scrollHeight - h; if (fdOlder) fdOlder.textContent = 'Load older messages'; }, tv ? 60 : 900);
    }
    function feedClick(ev) {
      var t = ev.target;
      if (!t || !t.closest) return;
      var cp = t.closest('.nt-v6-copy');
      if (cp) {
        ev.preventDefault(); ev.stopPropagation();
        var txt = cp.parentElement.getAttribute('data-nt-v6-code') || '';
        try { navigator.clipboard.writeText(txt); } catch (e) { /* ignore */ }
        cp.textContent = 'Copied'; setTimeout(function () { cp.textContent = 'Copy'; }, 1200);
        return;
      }
      var turn = t.closest('[data-nt-v6-turn]');
      var hit = t.closest(FD_SEL);
      if (!turn || !hit || !turn.contains(hit)) return;
      var orig = turn.__ntOrig;
      if (!orig || !orig.isConnected) return;
      var i = Array.prototype.indexOf.call(turn.querySelectorAll(FD_SEL), hit);
      var o = orig.querySelectorAll(FD_SEL)[i];
      if (!o) return;
      ev.preventDefault(); ev.stopPropagation();
      ['pointerdown', 'mousedown', 'pointerup', 'mouseup'].forEach(function (ty) {
        try {
          var C = ty.indexOf('pointer') === 0 && window.PointerEvent ? PointerEvent : MouseEvent;
          o.dispatchEvent(new C(ty, { bubbles: true, cancelable: true, view: window, button: 0 }));
        } catch (e) { /* ignore */ }
      });
      o.click();
      setTimeout(feedSync, 30); setTimeout(feedSync, 250);
    }
    function feedCode(c) {
      var pres = c.querySelectorAll('pre');
      for (var i = 0; i < pres.length; i++) {
        var p = pres[i];
        if (p.closest('.nt-v6-code')) continue;
        var txt = p.textContent || '';
        var n = txt.replace(/\n$/, '').split('\n').length, nums = [];
        for (var k = 1; k <= n; k++) nums.push(k);
        var w = document.createElement('div'); w.className = 'nt-v6-code'; w.setAttribute('data-nt-v6-code', txt);
        var b = document.createElement('button'); b.type = 'button'; b.className = 'nt-v6-copy'; b.textContent = 'Copy';
        var g = document.createElement('div'); g.className = 'nt-v6-ln'; g.textContent = nums.join('\n');
        p.parentNode.insertBefore(w, p); w.appendChild(b); w.appendChild(g); w.appendChild(p);
      }
    }
    function feedClone(n) {
      var c = n.cloneNode(true);
      var x = c.querySelectorAll('[contenteditable]');
      for (var i = 0; i < x.length; i++) x[i].removeAttribute('contenteditable');
      x = c.querySelectorAll('[id]');
      for (i = 0; i < x.length; i++) x[i].removeAttribute('id');
      x = c.querySelectorAll('[data-nt-desk-own]');
      for (i = 0; i < x.length; i++) x[i].remove();
      c.setAttribute('data-nt-v6-turn', '1');
      c.__ntOrig = n;
      feedCode(c);
      return c;
    }
    function feedObserve(list) {
      if (fdObs) fdObs.disconnect();
      fdObs = new MutationObserver(function (recs) {
        for (var i = 0; i < recs.length; i++) {
          var t = recs[i].target;
          if (t === list) { fdAll = true; continue; }
          while (t && t.parentNode !== list) t = t.parentNode;
          if (t) fdDirty.add(t); else fdAll = true;
        }
        if (!fdQ) fdQ = setTimeout(feedSync, Math.min(400, Math.max(80, Math.round(fdCost * 4))));  // round 32: a heavy turn syncs less often (lag)
      });
      fdObs.observe(list, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ['aria-expanded', 'data-nt-v6-dur', 'data-nt-v6', 'class', 'data-nt-tl2'] });
    }
    function feedOff() {
      feedStash(); fdSnap = null;
      if (fdObs) { fdObs.disconnect(); fdObs = null; }
      if (fdQ) { clearTimeout(fdQ); fdQ = 0; }
      if (fdList) { fdList.removeAttribute('data-nt-v6-src'); fdList = null; }
      fdMap = new Map(); fdDirty = new Set(); fdAll = true; fdGone = 0; fdScr = null; fdPlaced = '';
      if (fdRo) { fdRo.disconnect(); fdRo = null; }
      if (fd) { fd.remove(); fd = null; fdIn = fdSc = fdOlder = fdWork = null; }
      root.classList.remove('nt-v6-feed-on');
    }
    // round 32 (user 18:35: a voice recording made the chat box vanish): Notion swaps the text box for its
    // recording bar, chrome.js then drops the composer mark and the chat mask hid the whole box. The last
    // composer keeps an own mark while it is on the page; with no box at all the chat area is shown
    // (html.nt-v6-nocomp) and the feed keeps the last bottom edge, so the recording bar stays free.
    var compKeep = null, compTop = 0;
    function composerNow() {
      var cp = document.querySelector('.nt-desk-composer');
      if (cp && compKeep !== cp) {
        if (compKeep) compKeep.removeAttribute('data-nt-v6-comp');
        compKeep = cp; cp.setAttribute('data-nt-v6-comp', '1');
      }
      if (!cp && compKeep && !compKeep.isConnected) compKeep = null;
      cp = cp || compKeep;
      var none = !cp && !!document.querySelector('.layout-chat');
      if (root.classList.contains('nt-v6-nocomp') !== none) root.classList.toggle('nt-v6-nocomp', none);
      return cp;
    }
    function feedPlace() {
      var s = sbRect(), h = document.querySelector('.nt-v6-hd'), cp = composerNow();
      var left = Math.round(s.left + s.width);
      var top = h ? Math.round(h.getBoundingClientRect().bottom + 2) : 0;
      var ct = cp ? cp.getBoundingClientRect() : null;
      if (ct && ct.height > 0) compTop = Math.round(ct.top);
      var bot = ct && ct.height > 0 ? Math.round(ct.top - 8) : (compTop ? compTop - 8 : window.innerHeight);
      var want = [left, top, Math.max(0, window.innerWidth - left), Math.max(0, bot - top)];
      var pk = want.join(',');
      if (pk === fdPlaced) return false;  // unchanged: no style writes, no forced layout
      fdPlaced = pk;
      fd.style.left = want[0] + 'px'; fd.style.top = want[1] + 'px'; fd.style.width = want[2] + 'px'; fd.style.height = want[3] + 'px';
      var r = fd.getBoundingClientRect();  // correct for a transformed / contained host
      if (Math.abs(r.left - want[0]) > 1 || Math.abs(r.top - want[1]) > 1) {
        fd.style.left = (want[0] * 2 - r.left) + 'px'; fd.style.top = (want[1] * 2 - r.top) + 'px';
      }
      return true;
    }
    function feedSync() {
      fdQ = 0;
      if (stopped) return;
      var off = false;
      try { off = localStorage.getItem('nt-v6-feed') === '0'; } catch (e) { /* ignore */ }
      var list = off ? null : feedList(), host = document.querySelector('.layout-chat'), th = currentThread();
      var hdOn = !!document.querySelector('.nt-v6-hd');
      if (fdPend) {  // round 22: the clicked chat is already shown; Notion has not switched the URL yet
        if (th === fdPend && Date.now() - fdPendAt < 3000) { if (fd && host && fd.parentNode !== host) feedEnsure(host); return; }
        fdPend = '';
      }
      if (list && fdOld) { if (feedSig(list) === fdOld) list = null; else fdOld = ''; }  // still the old chat's list
      if (th && fdThread && th !== fdThread) {  // switched chat: keep the old chat's last turns, show the new chat's at once
        fdOld = fdList ? feedSig(fdList) : ''; if (list && fdOld && feedSig(list) === fdOld) list = null;
        feedStash(); fdSnap = null; fdGone = 0;
        fdMap = new Map(); if (fdIn) fdIn.textContent = ''; fdWork = null; fdStick = true; fdAll = true; fdThread = th; fdHoldAt = Date.now();
        if (!off && host && hdOn) feedSnapShow(host, th);
      }
      if (!list || !host || !hdOn) {
        if (!off && fd && host && fd.parentNode === host && fdMap.size && th === fdThread) {
          if (!fdGone) fdGone = Date.now();
          if (Date.now() - fdGone < FEED_GRACE_MS) return;  // Notion is re-mounting the list: keep the mirror
        }
        // Notion still loads the chat: show its kept last turns meanwhile
        if (!off && host && hdOn && th && th === fdThread && !fdMap.size &&
            (fdSnap ? Date.now() - fdSnapAt < FD_SNAP_MS : fdSnapTh !== th && feedSnapShow(host, th))) return;  // round 25: a slow load never deletes the kept turns; round 32: once per visit, never again after they ended
        if (!off && host && hdOn && th && th === fdThread && !fdMap.size && feedHold(host)) return;  // still loading: never blank, never Notion
        if (!off && th && fd && fdHoldAt && Date.now() - fdHoldAt < FEED_GRACE_MS) {  // round 22: mid-switch
          root.classList.add('nt-v6-feed-on');
          if (host) feedEnsure(host); else if (!fd.isConnected) { document.body.appendChild(fd); fdPlaced = ''; feedPlace(); if (fdStick) fdSc.scrollTop = fdSc.scrollHeight; }
          return;
        }
        if (fd || fdList) feedOff();
        return;
      }
      feedEnsure(host);
      if (fdList !== list) {
        if (fdMap.size && th === fdThread && !feedVisible(list)) {
          if (!fdGone) fdGone = Date.now();
          if (Date.now() - fdGone < FEED_GRACE_MS) return;  // new list not painted yet: keep the old turns
        }
        if (fdList) fdList.removeAttribute('data-nt-v6-src');
        fdList = list; list.setAttribute('data-nt-v6-src', '1');
        fdMap = new Map(); if (!fdSnap) fdIn.textContent = ''; fdWork = null; fdStick = true; fdAll = true; fdScr = null; fdScrAt = 0;
        feedObserve(list);
      }
      fdThread = th;
      var now = Date.now(), full = fdAll || now - fdFullAt > FEED_FULL_MS;
      if (fdSnap) {  // the kept turns stay until Notion has painted this chat's own rows
        var pv = 0;
        for (var q = 0; q < list.children.length && pv < 2; q++) if (!list.children[q].hasAttribute('data-nt-desk-own') && list.children[q].getClientRects().length && /\S/.test(list.children[q].textContent)) pv++;
        if (pv < Math.min(fdSnap.length, 2) && now - fdSnapAt < FD_SNAP_MS) { feedPlace(); return; }
        feedSnapDrop();
      }
      var kids = list.children, prev = null, seen = new Set(), changed = false, vis = 0, t0 = performance.now();
      for (var i = 0; i < kids.length; i++) {
        var n = kids[i];
        if (n.hasAttribute('data-nt-desk-own') || !n.getClientRects().length) continue;  // tail mode hides old rows
        vis++;
        var e = fdMap.get(n);
        if (!e || full || fdDirty.has(n)) {  // only changed rows are serialized (was: every row, every sync)
          var cs = n.textContent.length + ':' + n.getElementsByTagName('*').length;  // user 04:41 (switch lagged): cheap check first
          var grown = !e || e.cs !== cs;  // round 32 (lag): a row that grew changed for sure, no innerHTML needed
          var sig = grown ? 'c' + cs : e && !fdDirty.has(n) && e.cs === cs ? e.sig : n.innerHTML.length + ':' + cs;  // full pass: unchanged rows are not serialized again
          if (!e || e.sig !== sig) {
            var c = feedClone(n);
            if (e && e.c.parentNode) e.c.parentNode.replaceChild(c, e.c);
            e = { sig: sig, cs: cs, c: c }; fdMap.set(n, e); changed = true;
          }
        }
        var want = prev ? prev.nextSibling : fdIn.firstChild;
        if (e.c !== want) { fdIn.insertBefore(e.c, want); changed = true; }
        prev = e.c; seen.add(n);
      }
      if (!vis && fdMap.size) {  // every row hidden for a moment (recording UI): keep the mirror
        if (!fdGone) fdGone = now;
        if (now - fdGone < FEED_GRACE_MS) { fdDirty.clear(); return; }
      }
      fdGone = 0; fdHoldAt = 0;
      fdMap.forEach(function (en, k) { if (!seen.has(k)) { en.c.remove(); fdMap.delete(k); changed = true; } });
      fdDirty.clear(); fdAll = false; fdCost = performance.now() - t0;
      if (full) fdFullAt = now;
      var busy = !!stopButton();
      if (busy !== fdBusy) {  // round 32 (user 18:35: a finished answer did not show): re-read every row once it ends
        fdBusy = busy;
        if (!busy) { fdAll = true; if (!fdQ) fdQ = setTimeout(feedSync, 80); }
      }
      if (busy) {
        if (!fdWork) { fdWork = document.createElement('div'); fdWork.className = 'nt-v6-fwork'; fdWork.innerHTML = '<i></i><span>Model is working…</span>'; }
        if (fdIn.lastChild !== fdWork) { fdIn.appendChild(fdWork); changed = true; }
      } else if (fdWork && fdWork.parentNode) { fdWork.remove(); changed = true; }
      var od = list.getAttribute('data-nt-tl2') || (feedScr() || {}).scrollTop > 0 ? '' : 'none';
      if (fdOlder.style.display !== od) fdOlder.style.display = od;
      var moved = feedPlace();
      if (fdStick && (changed || moved)) fdSc.scrollTop = fdSc.scrollHeight;
      fdTitle = currentTitle();  // feedStash skips coworker chats by this title
    }

    // New chat (user 2026-10-27 21:05): overlay.js asks v6 to open the chat it just created.
    window.__ntV6OpenThread = function (k) {
      k = key(k);
      if (!k) return false;
      var rows = document.querySelectorAll('[data-nt-desk="chat-row"]');
      for (var i = 0; i < rows.length; i++) if (key(rows[i].getAttribute('data-nt-desk-thread')) === k) return press(rows[i]);
      // Round 28: overlay rows are not marked under v6, so look at Notion's own rows (link holds the thread id).
      var own = notionRows();
      for (var j = 0; j < own.length; j++) {
        var a = own[j].el.querySelector('a[href]');
        if (a && key(a.getAttribute('href')).indexOf(k) >= 0) return press(own[j].el);
      }
      return false;
    };
    // Mic (user 2026-10-27 22:25): only Notion's own "Start voice recording" button is shown (marked 'mic'
    // in decorateChat). Notion hides it while an answer is running. There is no own dictation button.

    var hidTicks = 0;
    function tick() {
      if (stopped) return;
      // smooth (user 2026-10-27 22:25): while the window is hidden or covered only the sidebar is kept
      // current (every 4th tick, so done tags still appear); the chat mirror waits until it is shown again.
      if (document.hidden) {
        if (hidTicks++ % 4 === 0) { try { render(); } catch (e) { /* ignore */ } }
        return;
      }
      try { composerNow(); } catch (e) { /* ignore */ }
      try { feedSync(); } catch (e) { try { feedOff(); } catch (x) { /* ignore */ } }
      try { placeScrollBtn(); } catch (e) { /* ignore */ }
      try { draftTick(); } catch (e) { /* ignore */ }
      try { effortWatch(); } catch (e) { /* ignore */ }
      try { render(); } catch (e) {
        if (!errLogged) { errLogged = true; try { console.warn('[NotionTool v6]', e); } catch (x) { /* ignore */ } }
      }
    }
    var tickRaf = 0;
    function tickSoon() { if (!tickRaf) tickRaf = requestAnimationFrame(function () { tickRaf = 0; tick(); }); }
    function onVis() { if (!document.hidden) tickSoon(); }

    function openChatRow(title) {
      var go = function () {
        var row = findRow(title);
        if (!row) { toast('"' + title + '" is not in the sidebar right now', 'error'); return; }
        press(row);
      };
      if (root.classList.contains('nt-shell-view-open') && window.__ntShell && window.__ntShell.open) {
        window.__ntShell.open('chat');
        setTimeout(go, 120);
      } else go();
    }

    // Notion opens its model/effort menu at its own composer button (bottom of the window).
    // When the header pill opened it, move Notion's fixed popover anchor right under the pill.
    function placeMenu(pill) {
      if (!pill) return;
      var t0 = Date.now();
      (function look() {
        if (stopped) return;
        var all = document.querySelectorAll('.notion-overlay-container [role="dialog"]'), d = all[all.length - 1];
        if (!d) { if (Date.now() - t0 < 900) setTimeout(look, 30); return; }
        var anchor = null, flip = null, wrap = null, n = d.parentElement;
        while (n && !(n.classList && n.classList.contains('notion-overlay-container'))) {
          var st = n.getAttribute('style') || '';
          if (!flip && /bottom:\s*100%/.test(st)) flip = n;
          if (!wrap && /align-items:\s*flex-end/.test(st)) wrap = n;
          if (getComputedStyle(n).position === 'fixed') { anchor = n; break; }
          n = n.parentElement;
        }
        if (!anchor) return;
        var r = pill.getBoundingClientRect(), w = d.getBoundingClientRect().width || 300;
        var left = Math.max(12, Math.min(r.left, window.innerWidth - w - 12));
        anchor.style.setProperty('inset-inline-start', left + 'px', 'important');
        anchor.style.setProperty('left', left + 'px', 'important');
        anchor.style.setProperty('top', Math.round(r.bottom + 6) + 'px', 'important');
        if (flip) { flip.style.setProperty('bottom', 'auto', 'important'); flip.style.setProperty('top', '0', 'important'); }
        if (wrap) { wrap.style.setProperty('align-items', 'flex-start', 'important'); wrap.style.setProperty('justify-content', 'flex-start', 'important'); }
        mark(anchor, 'menu-top');
      })();
    }

    // Project picker: a small dropdown under the project card (switch project or manage projects).
    var projMenu = null;
    function outsideProj(e) {
      if (!projMenu) return;
      if (projMenu.contains(e.target) || (e.target.closest && e.target.closest('.nt-v6-proj'))) return;
      closeProjMenu();
    }
    function closeProjMenu() {
      if (projMenu && projMenu.parentNode) projMenu.parentNode.removeChild(projMenu);
      projMenu = null;
      document.removeEventListener('mousedown', outsideProj, true);
    }
    function openProjMenu(anchor) {
      if (projMenu) { closeProjMenu(); return; }
      var m = document.createElement('div'), r = anchor.getBoundingClientRect();
      m.className = 'nt-v6-pmenu';
      m.style.left = Math.round(r.left) + 'px';
      m.style.top = Math.round(r.bottom + 6) + 'px';
      m.style.width = Math.round(r.width) + 'px';
      m.innerHTML = '<div class="nt-v6-pm-empty">Loading projects...</div>';
      document.body.appendChild(m);
      projMenu = m;
      document.addEventListener('mousedown', outsideProj, true);
      call('projects_list', {}).then(function (res) {
        if (projMenu !== m) return;
        var all = list(res.projects).filter(function (p) { return !obj(p).archived; }), cur = str(res.current_project_id), h = [];
        if (!all.length) h.push('<div class="nt-v6-pm-empty">No projects yet</div>');
        all.forEach(function (p) {
          p = obj(p);
          h.push('<div class="nt-v6-pm-item nt-v6-btn' + (str(p.id) === cur ? ' on' : '') + '" data-pm="' + esc(p.id) + '">' + svg('folder') +
            '<span class="nt-v6-t1">' + esc(p.name || 'Untitled') + '</span></div>');
        });
        h.push('<div class="nt-v6-pm-sep"></div><div class="nt-v6-pm-item nt-v6-btn" data-pm="__manage">' + svg(all.length ? 'right' : 'plus') +
          '<span class="nt-v6-t1">' + (all.length ? 'Manage projects' : 'Create a project') + '</span></div>');
        m.innerHTML = h.join('');
      }).catch(function () { if (projMenu === m) m.innerHTML = '<div class="nt-v6-pm-empty">Projects could not be loaded</div>'; });
      m.addEventListener('click', function (e) {
        var it = e.target.closest ? e.target.closest('[data-pm]') : null;
        if (!it) return;
        var id = it.getAttribute('data-pm');
        closeProjMenu();
        if (id === '__manage') { if (window.__ntShell && window.__ntShell.open) window.__ntShell.open('projects'); return; }
        call('projects_open', { id: id }).then(function (res) {
          toast('Project "' + str(obj(res.project).name || 'Project') + '" is now current', 'success');
          loadProject(); lastSb = ''; lastHd = '';
        }).catch(function (exc) { toast('Could not switch the project: ' + str(exc && exc.message || exc), 'error'); });
      });
    }

    // Is a Notion menu really visible? A closed menu can linger in the DOM at opacity 0.
    function menuVisible() {
      var ds = document.querySelectorAll('.notion-overlay-container [role="dialog"]');
      for (var i = 0; i < ds.length; i++) {
        var n = ds[i], op = 1;
        while (n && n !== document.body) { op *= Number(getComputedStyle(n).opacity); n = n.parentElement; }
        if (op > 0.5) return true;
      }
      return false;
    }
    // Effort per chat (owner 2026-10-26): after a chat switch Notion's composer shows its own default
    // (Medium) although NotionTool keeps sending the chat's last pick (data/model_pin.log). Remember the
    // level the user picked in each chat and show that instead of Notion's reset.
    var EFFORT_RE = /^(minimal|low|medium|high|extra[\s-]?high|x-?high|max)/i;
    // Display label (e.g. 'Extra High', 'Maximum') -> canonical effort level the backend/pin accept.
    function effortLevel(label) {
      var s = str(label).toLowerCase().replace(/\s+/g, ' ');
      if (s.indexOf('extra') === 0 || s.indexOf('xhigh') === 0 || s === 'x-high') return 'xhigh';
      if (s.indexOf('max') === 0) return 'max';
      if (s.indexOf('high') === 0) return 'high';
      if (s.indexOf('medium') === 0 || s.indexOf('mid') === 0) return 'medium';
      if (s.indexOf('low') === 0) return 'low';
      if (s.indexOf('min') === 0) return 'low';
      return str(label);
    }
    // Header label = what the request pin really sends (user 2026-10-27: the Roblox chat showed "Maximum" while
    // data/model_pin.log sent xhigh). Truth order: the backend pick (chat_effort_get, enforced by the pin), then
    // the locally remembered pick; both shown as one canonical label.
    var EFFORT_LABEL = { low: 'Low', medium: 'Medium', high: 'High', xhigh: 'xHigh', max: 'Max' };
    var effortBackend = {}, effortAskAt = {};
    function effortLabel(v) { var l = effortLevel(v); return EFFORT_LABEL[l] || cap(v); }
    function askEffort(tid) {
      var now = Date.now();
      if (effortAskAt[tid] > now - 15000 || !ctx || typeof ctx.call !== 'function') return;
      effortAskAt[tid] = now;
      try {
        Promise.resolve(ctx.call('chat_effort_get', { thread_id: tid })).then(function (r) {
          var rec = obj(r), e = str(rec.effort || obj(rec.result).effort).toLowerCase();
          effortBackend[tid] = EFFORT_LABEL[e] ? e : '';
          // round 24: remember the backend level so the next switch to this chat shows it at once.
          try { if (EFFORT_LABEL[e]) localStorage.setItem('nt-effort-be:' + tid, e); else localStorage.removeItem('nt-effort-be:' + tid); } catch (err2) { /* ignore */ }
        }, function () {});
      } catch (err) { /* ignore */ }
    }
    function effortPick(tid) {
      if (!tid) return '';
      askEffort(tid);
      if (effortBackend[tid]) return EFFORT_LABEL[effortBackend[tid]];
      var v = '';
      try { v = str(localStorage.getItem('nt-effort-be:' + tid) || localStorage.getItem('nt-effort-pick:' + tid)); } catch (e) { v = ''; }
      return v ? effortLabel(v) : '';
    }
    function nativeEffort() {
      var b = effortButton();
      var m = str(b && b.getAttribute('aria-label')).match(/currently\s+([\w-]+(?:\s[\w-]+)*)/i);
      return m ? cap(m[1]) : '';
    }
    // round 31 (user 2026-10-28 16:08: every chat switch went back to Medium). Live: no pick of the new
    // chats was ever stored (no nt-effort key in localStorage, no thread_efforts record), so the menu check
    // never matched Notion's current effort menu. Now a press on the effort pill or on Notion's effort
    // button arms this chat; the pick in the menu, or else the next change of Notion's effort label in that
    // same chat (effortWatch in tick), is stored. A press anywhere else or a chat switch disarms it, so
    // Notion's own reset to Medium after a switch is never taken as a pick. Event-based, no timer.
    var effortArm = null;
    function onEffortPick(e) {
      // round 19 (user 2026-10-28 05:02): the level comes from the clicked menu row itself and is stored at once.
      var tid = '', lv = '', direct = e && e.ntPick;
      if (direct) { tid = direct.tid; lv = direct.lv; }
      else {
        var tg = e && e.target && e.target.closest ? e.target : null;
        if (!tg) return;
        var eb = effortButton();
        if (tg.closest('[data-act="effort"]') || (eb && (eb === tg || eb.contains(tg)))) { armEffort(); return; }
        var box = tg.closest('.notion-overlay-container');
        if (!box) { effortArm = null; return; }
        var it = tg.closest('[role^="menuitem"],[role="option"],[role="radio"]');
        if (!it || !EFFORT_RE.test(text(it))) return;
        tid = currentThread(); lv = effortLevel(text(it));
        if (!(effortArm && effortArm.tid === tid)) {
          var menu = it.closest('[role="menu"],[role="dialog"],[role="listbox"]') || box;
          if (!/\beffort\b/i.test(text(menu)) && !/\beffort\b/i.test(text(box))) return;
        }
      }
      if (!tid || !EFFORT_LABEL[lv]) return;
      effortArm = null;
      try { localStorage.setItem('nt-effort-pick:' + tid, lv); localStorage.setItem('nt-effort-be:' + tid, lv); } catch (err) { /* ignore */ }
      effortBackend[tid] = lv; effortAskAt[tid] = Date.now();
      // The pick is also stored on the backend and enforced by the request pin, so Notion's own
      // reset to 'medium' after a chat switch can never override the user's choice.
      try {
        if (ctx && typeof ctx.call === 'function') {
          Promise.resolve(ctx.call('chat_effort_pick', { thread_id: tid, effort: lv })).then(function () {
            if (!stopped) tick();
          }, function () { effortAskAt[tid] = 0; delete effortBackend[tid]; if (!stopped) tick(); });
        }
      } catch (err) { /* ignore */ }
      if (!stopped) tickSoon();
    }
    function armEffort() {
      var at = currentThread();
      effortArm = at ? { tid: at, from: effortLevel(nativeEffort()) } : null;
    }
    function effortWatch() {
      if (!effortArm) return;
      var tid = currentThread();
      if (!tid || tid !== effortArm.tid) { effortArm = null; return; }
      var lv = effortLevel(nativeEffort());
      if (!EFFORT_LABEL[lv] || lv === effortArm.from) return;
      onEffortPick({ ntPick: { tid: tid, lv: lv } });
    }
    var pillDownOpen = false;
    function onPillDown(e) {
      var t = e.target && e.target.closest ? e.target.closest('[data-act="effort"],[data-act="model"]') : null;
      pillDownOpen = t ? menuVisible() : false;
    }

    function onClick(e) {
      var t = e.target && e.target.closest ? e.target.closest('[data-act]') : null;
      if (!t) return;
      if (t.tagName === 'A' && t.getAttribute('data-act') === 'chat' && t.getAttribute('href')) {
        e.stopPropagation();
        // round 27 (user 07:46: an effort display still pops up for a moment on a chat switch): the link's own
        // navigation loaded the whole page again (measured: navigate, beforeunload, pagehide, new page) and
        // Notion drew its composer with the effort button before our styles were back. A plain click now
        // presses Notion's own sidebar row, which switches inside the page (measured: same page, no reload).
        // Ctrl, Shift, Alt, Meta and other buttons keep the normal link; a chat without a Notion row too.
        if (e.button || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
        var ownTitle = t.getAttribute('data-arg') || '';
        if (!findRow(ownTitle)) return;
        e.preventDefault();
        openChatRow(ownTitle);
        return;
      }
      e.preventDefault();
      e.stopPropagation();
      var act = t.getAttribute('data-act'), arg = t.getAttribute('data-arg') || '';
      if (act === 'proj') {
        if (t.classList.contains('nt-v6-proj')) { openProjMenu(t); return; }
        if (window.__ntShell && window.__ntShell.open) window.__ntShell.open('projects');
        return;
      }
      if (act === 'delegate') {
        if (!(window.__ntShell && window.__ntShell.open)) { toast('Live is not available right now', 'error'); return; }
        window.__ntShell.open('live');
        setTimeout(function () {
          if (!press(document.querySelector('.nt-desk-lv-new-request'))) toast('Use New request in Live to give a coworker a task', 'info');
        }, 200);
        return;
      }
      if (act === 'chat' || act === 'cw') { openChatRow(arg); return; }
      if (act === 'more') { expanded = !expanded; lastSb = ''; tick(); return; }
      if (act === 'cwidle' || act === 'arch') {
        if (act === 'cwidle') cwOpen = !cwOpen; else archOpen = !archOpen;
        try { localStorage.setItem(act === 'cwidle' ? CWOPEN_KEY : ARCHOPEN_KEY, (act === 'cwidle' ? cwOpen : archOpen) ? '1' : '0'); } catch (x) { /* ignore */ }
        lastSb = ''; tick(); return;
      }
      if (act === 'new') {
        var nb = document.querySelector('.nt-desk-newchat');
        if (!press(nb)) toast('New chat is not available right now', 'error');
        return;
      }
      if (act === 'back') { try { history.back(); } catch (x) { /* ignore */ } return; }
      if ((act === 'model' || act === 'effort') && pillDownOpen) {
        // second click on the pill closes the open menu instead of reopening it
        pillDownOpen = false;
        setTimeout(function () { if (menuVisible()) press(act === 'model' ? modelButton() : effortButton()); }, 80);
        return;
      }
      if (act === 'effort' && effortButton() && effortButton().getAttribute('aria-disabled') === 'true') {
        // Notion locks the effort picker while the chat is answering; say so instead of doing nothing
        toast('Effort can be changed once the answer is finished');
        return;
      }
      if (act === 'model') { if (!press(modelButton())) toast('The model menu is not available right now', 'error'); else placeMenu(t); return; }
      if (act === 'effort') { if (!press(effortButton())) toast('The effort menu is not available right now', 'error'); else placeMenu(t); return; }
      if (act === 'stop') { if (!press(stopButton())) toast('Nothing is running', 'info'); return; }
      if (act === 'archive') {
        if (archUntil <= Date.now()) { archUntil = Date.now() + CONFIRM_MS; lastHd = ''; tick(); setTimeout(function () { lastHd = ''; tick(); }, CONFIRM_MS + 50); return; }
        archUntil = 0; lastHd = '';
        var aTitle = currentTitle(), aw = switchTarget(aTitle);
        if (!aw || !aw.thread_id) { toast('Move to archive works for NotionTool chats only', 'error'); tick(); return; }
        var aName = 'Archived · ' + aTitle;
        toast('Moving "' + aTitle + '" to the archive...', 'info');
        call('app_chat_rename', { thread_id: aw.thread_id, name: aName }).then(function (res) {
          if (res && res.error) throw new Error(res.error);
          toast('Archived as "' + aName + '"', 'info'); lastHd = ''; tick();
        }).catch(function (err) { toast('Archive failed: ' + ((err && err.message) || err), 'error'); lastHd = ''; tick(); });
        return;
      }
      if (act === 'switch') {
        var sw0 = switchTarget(currentTitle());
        if (sw0 && sw0.purpose === 'private') { toast('This is a private chat. Open its settings, choose "Make this a Main", then Switch context works', 'info'); return; }
        if (confirmUntil <= Date.now()) { confirmUntil = Date.now() + CONFIRM_MS; lastHd = ''; tick(); setTimeout(function () { lastHd = ''; tick(); }, CONFIRM_MS + 50); return; }
        confirmUntil = 0; lastHd = '';
        var w = switchTarget(currentTitle());
        if (w && w.pending) { toast('This chat is still being set up. Try again in a moment', 'info'); tick(); return; }
        if (!w || (!w.id && !w.thread_id)) { toast('Switch context works for NotionTool chats only', 'error'); tick(); return; }
        if (w.purpose === 'private') { toast('This is a private chat. Open its settings, choose "Make this a Main", then Switch context works', 'info'); tick(); return; }
        call('chat_context_switch', { worker_id: str(w.id), chat_id: str(w.chat_id), thread_id: str(w.thread_id) }).then(function (res) {
          res = res || {};
          if (res.duplicate) toast(res.state === 'pending' || res.state === 'sending' ? 'A switch request is already waiting for the Main' : 'A context switch is already running', 'info');
          else if (res.kind === 'coworker') toast('Context switch started: the coworker gets a fresh chat before its next task', 'success');
          else toast('Switch requested: the Main writes the handoff and switches to a fresh chat', 'success');
        })
          .catch(function (exc) { toast('Context switch failed: ' + str(exc && exc.message || exc), 'error'); });
        tick();
      }
    }

    // ---- chat drafts (user 2026-10-28 04:49): the text in the chat box survives every restart, per chat,
    // up to the last typed letter. Saved right after each input locally and in data/desktop_drafts.json
    // (draft_save); after a restart or a chat switch an empty chat box gets its draft back, cursor at the end.
    // A draft is only cleared by the user (sending or deleting the text), never by Notion rebuilding the box.
    var DRAFT_LS = 'nt-v6-draft:', draftThread = null, draftLast = {}, draftWait = {}, draftShown = {}, draftSent = {}, draftLen = {}, draftT = 0;
    function draftEd() { var c = document.querySelector('.nt-desk-composer'); return c ? c.querySelector('[contenteditable="true"]') : null; }
    function draftText(ed) { return ed ? String(ed.innerText || '').replace(/\u200b/g, '').replace(/\n$/, '') : ''; }
    function draftLocal(t) { try { var v = JSON.parse(localStorage.getItem(DRAFT_LS + t) || 'null'); return v && typeof v.text === 'string' ? v : null; } catch (e) { return null; } }
    // Round 29 (user 09:07): a sent message never comes back as a draft. Whatever path sent it (Enter, a
    // button, a queue), it then is the newest user message in the feed; the same text counts as sent.
    function draftNorm(s) { return String(s || '').replace(/[\u200b\s]+/g, ' ').trim(); }
    function draftLastSent() {
      var lc = document.querySelector('.layout-content'), us = lc ? lc.querySelectorAll('[data-agent-chat-user-step-id]') : [];
      if (!us.length) return '';
      var u = us[us.length - 1], b = u.querySelector('[data-nt-v6="ububble"]') || u;
      return draftNorm(b.innerText || b.textContent);
    }
    function draftIsSent(s) { var n = draftNorm(s); return !!n && n === draftLastSent(); }
    function draftClear(ed) {
      try { ed.focus(); var sel = window.getSelection(), r = document.createRange(); r.selectNodeContents(ed); sel.removeAllRanges(); sel.addRange(r); document.execCommand('delete'); } catch (e) { /* ignore */ }
    }
    function draftSave() {
      if (draftT) { clearTimeout(draftT); draftT = 0; }
      var t = currentThread(), ed = draftEd();
      if (!t || !ed || draftWait[t]) return;
      var s = draftText(ed);
      if (draftLast[t] === s) return;
      draftLast[t] = s; draftShown[t] = 0;
      try { if (s) localStorage.setItem(DRAFT_LS + t, JSON.stringify({ text: s, ts: Date.now() })); else localStorage.removeItem(DRAFT_LS + t); } catch (e) { /* ignore */ }
      if (ctx && typeof ctx.call === 'function') ctx.call('draft_save', { thread: t, text: s }).catch(function () { /* the local copy stays */ });
    }
    function draftSoon() { if (!draftT) draftT = setTimeout(draftSave, 120); }
    function draftIn(e) { var n = e && e.target; return !!(n && n.closest && n.closest('.nt-desk-composer')); }
    function draftKeep() {  // the local copy right at the keystroke; the host copy follows within 120 ms
      var t = currentThread(), ed = draftEd();
      if (!t || !ed || draftWait[t]) return;
      var s = draftText(ed);
      try { if (s) localStorage.setItem(DRAFT_LS + t, JSON.stringify({ text: s, ts: Date.now() })); } catch (e) { /* ignore */ }
    }
    function onDraftInput(e) { if (draftIn(e)) { draftKeep(); draftSoon(); } }
    function onDraftKey(e) { if (draftIn(e) && e.key === 'Enter' && !e.shiftKey && !e.isComposing) { var t = currentThread(); if (t) draftSent[t] = true; } }
    function onDraftClick(e) { var n = e && e.target; if (n && n.closest && n.closest('[data-nt-v6="send"]')) { var t = currentThread(); if (t) draftSent[t] = true; } }
    function draftPut(ed, value) {
      try {
        ed.focus();
        var sel = window.getSelection(), r = document.createRange();
        r.selectNodeContents(ed); r.collapse(false); sel.removeAllRanges(); sel.addRange(r);
        var lines = String(value).split('\n');
        for (var i = 0; i < lines.length; i++) {
          if (i) document.execCommand('insertLineBreak');
          if (lines[i]) document.execCommand('insertText', false, lines[i]);
        }
      } catch (e) { /* ignore */ }
    }
    function draftFetch(t) {
      var local = draftLocal(t);
      function done(host) {
        draftWait[t] = 0;
        var pick = host && Number(host.ts || 0) >= Number((local && local.ts) || 0) ? host : local;
        var ed = draftEd();
        if (currentThread() !== t || !ed) return;
        var cur = draftText(ed);
        if (cur || !pick || !pick.text) { draftLast[t] = cur; return; }
        if (draftIsSent(pick.text)) { draftLast[t] = null; draftSave(); return; }  // round 29: already sent, clear it
        draftLast[t] = pick.text; draftShown[t] = 1;
        draftPut(ed, pick.text);
      }
      if (ctx && typeof ctx.call === 'function') ctx.call('draft_load', { thread: t }).then(done, function () { done(null); });
      else done(null);
    }
    function draftTick() {
      var t = currentThread();
      if (t !== draftThread) { draftThread = t; if (t) draftWait[t] = 1; }
      if (!t) return;
      var ed = draftEd();
      if (!ed) return;
      if (draftWait[t] === 1) { draftWait[t] = 2; draftFetch(t); return; }
      if (draftWait[t]) return;
      var raw = String(ed.textContent || '').replace(/\u200b/g, '');
      if (raw.trim()) {
        // round 29: a restored draft (no input since) that is the newest sent message is taken out again
        if (draftShown[t] && draftIsSent(draftText(ed))) { draftShown[t] = 0; draftClear(ed); draftLen[t] = 0; draftLast[t] = null; draftSave(); return; }
        if (raw.length !== draftLen[t]) { draftLen[t] = raw.length; draftSoon(); }
        return;  // also dictation
      }
      draftLen[t] = 0;
      if (draftSent[t]) { draftSent[t] = false; draftSave(); return; }  // sent: the draft is gone
      if (draftLast[t] && draftIsSent(draftLast[t])) { draftShown[t] = 0; draftSave(); return; }  // round 29: sent by any other path
      // Notion rebuilt the chat box right after a restore (no user input since): put the draft back
      if (draftShown[t] && draftShown[t] < 4 && draftLast[t]) { draftShown[t]++; draftPut(ed, draftLast[t]); }
    }
    document.addEventListener('input', onDraftInput, true);
    document.addEventListener('keydown', onDraftKey, true);
    document.addEventListener('click', onDraftClick, true);
    window.addEventListener('pagehide', draftSave);
    function draftOff() {
      if (draftT) draftSave();
      document.removeEventListener('input', onDraftInput, true);
      document.removeEventListener('keydown', onDraftKey, true);
      document.removeEventListener('click', onDraftClick, true);
      window.removeEventListener('pagehide', draftSave);
      unfreeze();
      if (window.__ntDeskFreeze === freezeState) { window.__ntDeskFreeze = null; window.__ntDeskUnfreeze = null; }
    }

    // ---- restart freeze + resume (user 2026-10-28 05:20): right before a restart closes the window,
    // work/freeze_window.py takes the exact state - open chat, scroll position, chat box text up to the last
    // letter - and the box turns read-only, so nothing typed after that moment can get lost. The new window
    // opens the same chat (desktop_shell resume_url) and gets the scroll position back here (resume_take).
    var resumeWant = null, freezeEd = null, freezeT = 0;
    function freezeState() {
      var t = currentThread(), ed = draftEd();
      var out = { url: String(location.href), thread: t || '', text: null, fromBottom: 0 };
      try { feedStash(); } catch (e) { /* ignore */ }  // round 21: the open chat is there at once after the restart
      if (draftT) { clearTimeout(draftT); draftT = 0; }
      if (t && ed && !draftWait[t]) out.text = draftText(ed);  // draft still loading: the stored one stays
      if (ed) {
        try { ed.setAttribute('contenteditable', 'false'); ed.blur(); } catch (e) { /* ignore */ }
        freezeEd = ed;
        if (freezeT) clearTimeout(freezeT);
        freezeT = setTimeout(unfreeze, 20000);  // safety net only: the window was not closed after all
      }
      var sc = fdSc && fdSc.isConnected ? fdSc : feedScr();
      if (sc) out.fromBottom = Math.max(0, Math.round(sc.scrollHeight - sc.scrollTop - sc.clientHeight));
      return out;
    }
    function unfreeze() {
      if (freezeT) { clearTimeout(freezeT); freezeT = 0; }
      if (freezeEd) { try { freezeEd.setAttribute('contenteditable', 'true'); } catch (e) { /* ignore */ } freezeEd = null; }
    }
    window.__ntDeskFreeze = freezeState;
    window.__ntDeskUnfreeze = unfreeze;
    if (ctx && typeof ctx.call === 'function') {
      Promise.resolve(ctx.call('resume_take', {})).then(function (r) {
        if (r && r.thread && Number(r.from_bottom) > 40) resumeWant = { thread: String(r.thread).replace(/-/g, ''), fromBottom: Number(r.from_bottom) };
      }, function () { /* no resume */ });
    }
    function resumeTick() {
      if (!resumeWant) return;
      var t = String(currentThread() || '').replace(/-/g, '');
      if (t && t !== resumeWant.thread) { resumeWant = null; return; }
      if (!fdSc || !fdSc.isConnected || !fdIn || !fdIn.children.length || !root.classList.contains('nt-v6-feed-on')) return;
      var top = fdSc.scrollHeight - fdSc.clientHeight - resumeWant.fromBottom;
      resumeWant = null;
      fdStick = false;
      fdSc.scrollTop = Math.max(0, top);
    }

    if (ctx && typeof ctx.onState === 'function') {
      offState = ctx.onState(function (s) { state = obj(s); });
    }
    tick();
    timer = setInterval(tick, TICK_MS);
    window.addEventListener('resize', tickSoon);
    document.addEventListener('visibilitychange', onVis);
      startGone();
    document.addEventListener('pointerdown', onEffortPick, true);

    function stop() {
      stopped = true;
      closeProjMenu();
      if (timer) clearInterval(timer);
      window.removeEventListener('resize', tickSoon);
      document.removeEventListener('visibilitychange', onVis);
      if (tickRaf) { cancelAnimationFrame(tickRaf); tickRaf = 0; }
      stopGone();
      if (rz) { rz.remove(); rz = null; }
      if (wk) { wk.remove(); wk = null; }
      feedOff();
      try { draftOff(); } catch (x) { /* ignore */ }
      try { delete window.__ntV6OpenThread; } catch (x) { /* ignore */ }
      root.style.removeProperty('--nt-v6-sbw');
      document.removeEventListener('pointerdown', onEffortPick, true);
      if (typeof offState === 'function') { try { offState(); } catch (e) { /* ignore */ } }
      [sb, hd, defs, dg, document.querySelector('.nt-v6-badge')].forEach(function (n) { if (n && n.parentNode) n.parentNode.removeChild(n); });
      var rowEl = document.querySelector('.' + CLS_TOPROW);
      if (rowEl) rowEl.classList.remove(CLS_TOPROW);
      root.classList.remove(CLS_ON);
      root.classList.remove('nt-v6-mask');
      document.removeEventListener('click', feedRowClick, true);
      document.removeEventListener('keydown', feedUserKey, true);
      window.removeEventListener('pointerup', feedUserUp, true);
    }
    return { stop: stop };
  };

  function mergePrivateChats(existing, privateRows) {
    var rows = existing.slice();
    privateRows.forEach(function (chat) {
      if (!chat || (chat.role && chat.role !== 'private') || chat.purpose === 'worker') return;
      var found = rows.find(function (row) { return row.title === chat.title; });
      if (found) { found.thread_id = chat.thread_id; found.pending = !!chat.pending; found.created_at = chat.created_at || 0; }
      else rows.push({ title: chat.title || 'New chat', thread_id: chat.thread_id || '', pending: !!chat.pending,
                       created_at: chat.created_at || 0, age: '', live: false });
    });
    return rows.sort(function (a, b) { return Number(!!b.pending) - Number(!!a.pending) || Number(b.created_at || 0) - Number(a.created_at || 0); });
  }
  window.__ntDesk.v6Util = { fmtTok: fmtTok, ageText: ageText, agoText: agoText, COWORKER_RE: COWORKER_RE, MODEL_RE: MODEL_RE, morph: morph, mergePrivateChats: mergePrivateChats };
})();
