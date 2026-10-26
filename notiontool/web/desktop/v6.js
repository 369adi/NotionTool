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
  var ROW_H = 34;
  var FIXED_H = 430;
  var CW_H = 70;
  var COWORKER_RE = /^cowork(er)?\s*\d+/i;
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
    users: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>'
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
    }
    return cmd && shown(cmd) ? cmd.getBoundingClientRect() : null;
  }

  window.__ntDesk.v6 = function (ctx) {
    var stopped = false, timer = null, offState = null;
    var state = obj(ctx && ctx.getState ? ctx.getState() : null);
    var ages = { byKey: {}, byTitle: {}, limit: LIMIT_H }, agesAt = 0;
    var project = null, projectCommitMs = NaN, projectsAt = 0;
    var expanded = false, confirmUntil = 0, errLogged = false;
    var sb = null, hd = null, defs = null, dg = null, lastSb = '', lastHd = '', lastBox = '', lastHdBox = '', lastDg = '';
    var root = document.documentElement;

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
    function ageFor(threadId, title) {
      var k = key(threadId);
      if (k && ages.byKey[k]) return ages.byKey[k].hours;
      var t = str(title).trim().toLowerCase();
      if (t && ages.byTitle[t]) return ages.byTitle[t].hours;
      return NaN;
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
        : '<i class="nt-v6-dot n"></i>' + esc(info.state || 'Ready');
      html.push('<div class="nt-v6-main' + (info.running ? ' run' : '') + '"><div class="nt-v6-main-top"><div class="nt-v6-ico">' + svg('msgText') + '</div>' +
        '<div class="nt-v6-col"><div class="nt-v6-t1">' + esc(info.title || 'Chat') + '</div>' +
        (info.model ? '<span class="nt-v6-chip"><i class="nt-v6-dot"></i>' + esc(info.model) + '</span>' : '') + '</div>' +
        (info.ageLabel ? '<div class="nt-v6-age" title="Chat age (sending locks at ' + ages.limit + ' h)">' + esc(info.ageLabel) + ring(info.ageFrac) + '</div>' : '') +
        '</div><div class="nt-v6-live">' + live + '</div></div>');

      var chats = info.chats, limit = expanded ? chats.length : Math.max(3, Math.min(chats.length, info.fit));
      html.push('<div class="nt-v6-scroll"><div class="nt-v6-h">Chats</div><div class="nt-v6-list' + (expanded ? ' exp' : '') + '">');
      if (!chats.length) html.push('<div class="nt-v6-empty">No other chats</div>');
      for (var i = 0; i < chats.length && i < limit; i++) {
        var c = chats[i];
        var href = !c.pending && /^[0-9a-f-]{32,36}$/i.test(c.thread_id || '') ? 'https://app.notion.com/chat?t=' + c.thread_id.replace(/-/g, '') : '';
        var tag = href ? 'a' : 'div';
        html.push('<' + tag + (href ? ' href="' + esc(href) + '" style="color:inherit;text-decoration:none"' : '') + ' class="nt-v6-row nt-v6-btn' + (c.live ? ' live' : '') + '" data-act="' + (c.pending ? 'creating' : 'chat') + '" data-arg="' + esc(c.title) + '" title="' +
          esc(c.title) + (c.live ? ' - thinking or answering right now' : '') + '"><span class="nt-v6-rico">' + svg('msgDots') + '</span>' +
          '<span class="nt-v6-t1">' + esc(c.title) + '</span>' +
          (c.pending ? '<span class="nt-v6-livetag"><i class="nt-v6-spin"></i>Creating...</span>' :
            (c.live ? '<span class="nt-v6-livetag"><i class="nt-v6-spin"></i>thinking</span>' : '<span class="nt-v6-when">' + esc(c.age) + '</span>')) + '</' + tag + '>');
      }
      html.push('</div>');
      if (chats.length > 3 && (expanded || limit < chats.length)) {
        html.push('<div class="nt-v6-more nt-v6-btn" data-act="more">' + (expanded ? 'Show less' : 'Show all (' + info.total + ')') + svg('right') + '</div>');
      }

      var cwBusy = info.coworkers.filter(function (w) { return !w.idle; }).length;
      html.push('<div class="nt-v6-sep"></div><div class="nt-v6-h">Coworkers' + (info.coworkers.length ? '<i class="nt-v6-hn' + (cwBusy ? ' on' : '') + '">' + (cwBusy ? cwBusy + ' active' : 'all idle') + '</i>' : '') + '</div>');
      if (!info.coworkers.length) html.push('<div class="nt-v6-empty">No coworkers right now</div>');
      info.coworkers.forEach(function (w) {
        if (w.idle) {
          html.push('<div class="nt-v6-cw idle nt-v6-btn" data-act="cw" data-arg="' + esc(w.label) + '" title="Open ' + esc(w.label) + '"><i class="nt-v6-dot n"></i>' + avatar(w.label) +
            '<div class="nt-v6-col"><div class="nt-v6-cw-top"><span class="nt-v6-t1">' + esc(w.label) + '</span><span class="nt-v6-when">' + esc(w.age) + '</span></div>' +
            '<div class="nt-v6-cw-sub">Idle</div>' +
            (w.ctxLabel ? '<div class="nt-v6-prog">' + bar(w.ctxFrac) + '<em>' + esc(w.ctxLabel) + '</em></div>' : '') + '</div></div>');
          return;
        }
        html.push('<div class="nt-v6-cw nt-v6-btn' + (w.live ? ' live' : '') + '" data-act="cw" data-arg="' + esc(w.label) + '" title="Open ' + esc(w.label) + '"><i class="nt-v6-dot ' + w.dot + '"></i>' + avatar(w.label) +
          '<div class="nt-v6-col"><div class="nt-v6-cw-top"><span class="nt-v6-t1">' + esc(w.label) + '</span><span class="nt-v6-when">' + esc(w.age) + '</span></div>' +
          (w.ask
            ? '<div class="nt-v6-cw-sub nt-v6-cw-ask">Needs your answer<span class="nt-v6-answer nt-v6-btn" data-act="cw" data-arg="' + esc(w.label) + '">Answer</span></div>'
            : '<div class="nt-v6-cw-sub">' + esc(w.sub) + '</div><div class="nt-v6-cw-tool">&gt; ' + esc(w.step) + '</div>' +
              (w.ctxLabel ? '<div class="nt-v6-prog">' + bar(w.ctxFrac) + '<em>' + esc(w.ctxLabel) + '</em></div>' : '')) +
          '</div></div>');
      });

      var running = info.tasks.run, queued = info.tasks.queued;
      html.push('</div><div class="nt-v6-sep"></div><div class="nt-v6-pcard nt-v6-btn" data-act="proj" title="Open Projects"><div class="nt-v6-ico">' + svg('folder') + '</div>' +
        '<div class="nt-v6-col"><div class="nt-v6-t1">' + esc(p ? (p.name || 'Project') : 'Tasks') + '</div><div class="nt-v6-t2">' + (running ? running + (running === 1 ? ' task running' : ' tasks running') : 'No tasks running') +
        (queued ? ' \u00b7 ' + queued + ' queued' : '') + (info.tasks.waiting ? ' \u00b7 ' + info.tasks.waiting + ' waiting' : '') + '</div>' +
        (isFinite(projectCommitMs) ? '<div class="nt-v6-t2">Last commit ' + esc(agoText(projectCommitMs)) + '</div>' : '') + '</div></div>');
      html.push('<div class="nt-v6-spacer"></div><div class="nt-v6-new nt-v6-btn" data-act="new" title="Start a new chat">' + svg('plus') + 'New chat</div>');
      html.push('</div>');
      return html.join('');
    }

    // ---- render: header ----------------------------------------------------------------------
    function headerHtml(info) {
      var limit = ages.limit, h = [];
      h.push('<div class="nt-v6-back nt-v6-btn" data-act="back" title="Back">' + svg('back') + '</div>');
      h.push('<div class="nt-v6-ttl"><b>' + esc(info.title || 'Chat') + '</b>' +
        '<span>' + svg('folder') + esc(project ? (project.name || 'Project') : 'No project') + '</span></div>');
      h.push('<div class="nt-v6-stat">');
      h.push('<div class="nt-v6-si nt-v6-btn" data-act="model" title="Model (pinned, never Auto)">' + svg('gauge') + esc(info.model || 'Model') + svg('lock', 'nt-v6-lock') + '</div>');
      h.push('<div class="nt-v6-si nt-v6-btn" data-act="effort" title="Effort">' + svg('zap') + esc(info.effort || 'Effort') + '</div>');
      h.push('<div class="nt-v6-si" title="Chat age (sending locks at ' + limit + ' h)">' + ring(info.ageFrac) + esc((info.ageLabel || '-') + ' / ' + limit + 'h') + '</div>');
      // Only coworkers have a context limit; the Main just shows its token count (user review 26.10. 03:21).
      var cf = info.isCoworker ? (Number(info.ctxFrac) || 0) : 0, cOver = cf >= 1, cWarn = !cOver && cf >= 0.8;
      h.push('<div class="nt-v6-si' + (cOver ? ' over' : cWarn ? ' warn' : '') + '" title="' + (cOver ? 'Context is over the limit - Switch context continues in a fresh chat with a handoff' : cWarn ? 'Context is almost full' : 'Context of this chat') + '"><div class="nt-v6-ctx"><small>Context</small><b>' + esc(info.ctxLabel || '-') + '</b>' + (info.isCoworker ? bar(info.ctxFrac) : '') + '</div></div>');
      h.push('<div class="nt-v6-si" title="Steps in the last hour">' + ring(info.stepsFrac) + '<div class="nt-v6-ctx"><small>Steps</small><b>' + esc(info.stepsLabel) + '</b></div></div>');
      var ask = confirmUntil > Date.now();
      h.push('<div class="nt-v6-si nt-v6-btn' + (cOver && !ask ? ' hint' : '') + '" data-act="switch" title="' + (cOver ? 'Context is over the limit: continue in a fresh chat with a handoff' : 'Continue in a fresh chat with a handoff') + '">' + svg('swap') +
        '<span class="nt-v6-sw' + (ask ? ' ask' : '') + '">' + (ask ? 'Click again to switch' : 'Switch context') + '</span></div>');
      h.push('<div class="nt-v6-si nt-v6-stop nt-v6-btn' + (info.running ? '' : ' off') + '" data-act="stop" title="' + (info.running ? 'Stop the answer' : 'Nothing is running') + '"><span><i></i>Stop</span></div>');
      h.push('</div>');
      return h.join('');
    }

    function collect() {
      var title = currentTitle();
      var w = currentWorker(title);
      var mb = modelButton(), eb = effortButton(), sbtn = stopButton();
      var hours = ageFor(w ? w.thread_id : currentThread(), title);
      if (!isFinite(hours) && w && Number(w.age_s) >= 0 && w.age_s != null) hours = Number(w.age_s) / 3600;
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
      var cwH = 0;
      cws.forEach(function (x) { cwH += x.idle ? 58 : (x.ask ? 62 : CW_H + 16); });
      var box = sidebarBox(), boxH = box ? box.getBoundingClientRect().height : innerHeight;
      var fit = Math.floor((boxH - FIXED_H - Math.max(44, cwH)) / ROW_H);
      var running = !!sbtn || rowLiveMap[title] === true;
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
        isCoworker: !!(w && isCoworker(w)),
        ctxLabel: (w && isCoworker(w)) ? (Number(wctx.limit) > 0 ? fmtTok(wctx.estimated_tokens) + ' / ' + fmtTok(wctx.limit) : '') :
          (wctx.estimated_tokens != null && isFinite(Number(wctx.estimated_tokens)) ? fmtTok(wctx.estimated_tokens) + ' tokens' : ''),
        ctxFrac: Number(wctx.limit) > 0 ? Number(wctx.estimated_tokens) / Number(wctx.limit) : 0,
        stepsLabel: stepsUsed + ' / ' + stepsLimit,
        stepsFrac: stepsUsed / stepsLimit,
        chats: chats, total: total, fit: fit, coworkers: cws, tasks: tasksRunning()
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
    var SBW_KEY = 'nt-v6-sbw', SBW_MIN = 220, SBW_MAX = 440, sbw = 270, rz = null, rzDrag = null;
    try { var sbv = parseInt(localStorage.getItem(SBW_KEY), 10); if (sbv >= SBW_MIN && sbv <= SBW_MAX) sbw = sbv; } catch (e) { /* ignore */ }
    function railRight() {
      var n = document.querySelector('nav.nt-rail'), r = n && n.getBoundingClientRect();
      return r && r.width ? Math.round(r.right) : 66;
    }
    function applySbw() {
      var v = sbw + 'px';
      if (root.style.getPropertyValue('--nt-v6-sbw') !== v) root.style.setProperty('--nt-v6-sbw', v);
    }
    function sbRect() { applySbw(); return { left: railRight(), top: 0, width: sbw, height: window.innerHeight }; }
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
    }
    function onRzUp() {
      if (!rzDrag) return;
      rzDrag = null;
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

    function render() {
      if (stopped || !document.body) return;
      if (!root.classList.contains(CLS_ON)) root.classList.add(CLS_ON);
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
    var FD_SEL = '[role="button"],button,a,[aria-expanded]';
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
        if (n >= 2) return e;
      }
      return null;
    }
    function feedScroller(el) {
      for (var e = el; e && e !== document.body; e = e.parentElement) {
        var s = getComputedStyle(e).overflowY;
        if ((s === 'auto' || s === 'scroll') && e.scrollHeight > e.clientHeight + 4) return e;
      }
      return null;
    }
    function feedEnsure(host) {
      if (fd && fd.parentNode === host) return;
      if (fd) fd.remove();
      fd = document.createElement('div');
      fd.className = 'nt-v6-feed';
      fd.setAttribute('data-nt-desk-own', 'v6');
      fd.innerHTML = '<div class="nt-v6-feed-sc"><button type="button" class="nt-v6-older">Load older messages</button><div class="nt-v6-feed-in"></div></div>' +
        '<button type="button" class="nt-v6-down" aria-label="Scroll to bottom"><svg viewBox="0 0 16 16" width="16" height="16"><path d="M4 6l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg></button>';
      host.appendChild(fd);
      fdSc = fd.querySelector('.nt-v6-feed-sc'); fdIn = fd.querySelector('.nt-v6-feed-in'); fdOlder = fd.querySelector('.nt-v6-older');
      fdMap = new Map(); fdWork = null;
      fdSc.addEventListener('scroll', function () {
        fdStick = fdSc.scrollHeight - fdSc.scrollTop - fdSc.clientHeight < 40;
        fd.classList.toggle('nt-v6-away', !fdStick);
      }, { passive: true });
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
      fdObs = new MutationObserver(function () { if (!fdQ) fdQ = setTimeout(feedSync, 90); });
      fdObs.observe(list, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ['aria-expanded', 'data-nt-v6-dur', 'data-nt-v6', 'class', 'data-nt-tl2'] });
    }
    function feedOff() {
      if (fdObs) { fdObs.disconnect(); fdObs = null; }
      if (fdQ) { clearTimeout(fdQ); fdQ = 0; }
      if (fdList) { fdList.removeAttribute('data-nt-v6-src'); fdList = null; }
      fdMap = new Map();
      if (fd) { fd.remove(); fd = null; fdIn = fdSc = fdOlder = fdWork = null; }
      root.classList.remove('nt-v6-feed-on');
    }
    function feedPlace() {
      var s = sbRect(), h = document.querySelector('.nt-v6-hd'), cp = document.querySelector('.nt-desk-composer');
      var left = Math.round(s.left + s.width);
      var top = h ? Math.round(h.getBoundingClientRect().bottom + 2) : 0;
      var bot = cp ? Math.round(cp.getBoundingClientRect().top - 8) : window.innerHeight;
      var want = [left, top, Math.max(0, window.innerWidth - left), Math.max(0, bot - top)];
      fd.style.left = want[0] + 'px'; fd.style.top = want[1] + 'px'; fd.style.width = want[2] + 'px'; fd.style.height = want[3] + 'px';
      var r = fd.getBoundingClientRect();  // correct for a transformed / contained host
      if (Math.abs(r.left - want[0]) > 1 || Math.abs(r.top - want[1]) > 1) {
        fd.style.left = (want[0] * 2 - r.left) + 'px'; fd.style.top = (want[1] * 2 - r.top) + 'px';
      }
    }
    function feedSync() {
      fdQ = 0;
      if (stopped) return;
      var off = false;
      try { off = localStorage.getItem('nt-v6-feed') === '0'; } catch (e) { /* ignore */ }
      var list = off ? null : feedList(), host = document.querySelector('.layout-chat');
      if (!list || !host || !document.querySelector('.nt-v6-hd')) { if (fd || fdList) feedOff(); return; }
      feedEnsure(host);
      if (fdList !== list) {
        if (fdList) fdList.removeAttribute('data-nt-v6-src');
        fdList = list; list.setAttribute('data-nt-v6-src', '1');
        fdMap = new Map(); fdIn.textContent = ''; fdWork = null; fdStick = true;
        feedObserve(list);
      }
      var kids = list.children, prev = null, seen = new Set();
      for (var i = 0; i < kids.length; i++) {
        var n = kids[i];
        if (n.hasAttribute('data-nt-desk-own') || !n.getClientRects().length) continue;  // tail mode hides old rows
        var sig = n.innerHTML.length + ':' + n.textContent.length;
        var e = fdMap.get(n);
        if (!e || e.sig !== sig) {
          var c = feedClone(n);
          if (e && e.c.parentNode) e.c.parentNode.replaceChild(c, e.c);
          e = { sig: sig, c: c }; fdMap.set(n, e);
        }
        var want = prev ? prev.nextSibling : fdIn.firstChild;
        if (e.c !== want) fdIn.insertBefore(e.c, want);
        prev = e.c; seen.add(n);
      }
      fdMap.forEach(function (en, k) { if (!seen.has(k)) { en.c.remove(); fdMap.delete(k); } });
      if (stopButton()) {
        if (!fdWork) { fdWork = document.createElement('div'); fdWork.className = 'nt-v6-fwork'; fdWork.innerHTML = '<i></i><span>Model is working…</span>'; }
        if (fdIn.lastChild !== fdWork) fdIn.appendChild(fdWork);
      } else if (fdWork && fdWork.parentNode) fdWork.remove();
      fdOlder.style.display = list.getAttribute('data-nt-tl2') || (feedScroller(list) || {}).scrollTop > 0 ? '' : 'none';
      feedPlace();
      if (fdStick) fdSc.scrollTop = fdSc.scrollHeight;
    }

    function tick() {
      if (stopped) return;
      try { feedSync(); } catch (e) { try { feedOff(); } catch (x) { /* ignore */ } }
      try { placeScrollBtn(); } catch (e) { /* ignore */ }
      try { render(); } catch (e) {
        if (!errLogged) { errLogged = true; try { console.warn('[NotionTool v6]', e); } catch (x) { /* ignore */ } }
      }
    }

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
    function effortPick(tid) {
      try { return tid ? str(localStorage.getItem('nt-effort-pick:' + tid)) : ''; } catch (e) { return ''; }
    }
    function nativeEffort() {
      var b = effortButton();
      var m = str(b && b.getAttribute('aria-label')).match(/currently\s+([\w-]+(?:\s[\w-]+)*)/i);
      return m ? cap(m[1]) : '';
    }
    function onEffortPick(e) {
      var it = e.target && e.target.closest ? e.target.closest('[role^="menuitem"],[role="option"]') : null;
      if (!it || !it.closest('.notion-overlay-container [role="dialog"]') || !EFFORT_RE.test(text(it))) return;
      var tid = currentThread();
      if (!tid) return;
      setTimeout(function () {
        var lvl = nativeEffort();
        if (!lvl || currentThread() !== tid) return;
        try { localStorage.setItem('nt-effort-pick:' + tid, lvl); } catch (err) { /* ignore */ }
        if (!stopped) tick();
      }, 400);
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
        e.stopPropagation(); // own private chat: keep the link's normal user-click navigation
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
      if (act === 'switch') {
        if (confirmUntil <= Date.now()) { confirmUntil = Date.now() + CONFIRM_MS; lastHd = ''; tick(); setTimeout(function () { lastHd = ''; tick(); }, CONFIRM_MS + 50); return; }
        confirmUntil = 0; lastHd = '';
        var w = currentWorker(currentTitle());
        if (!w || !w.id) { toast('Switch context works for NotionTool chats only', 'error'); tick(); return; }
        call('chat_context_switch', { worker_id: w.id }).then(function (res) {
          res = res || {};
          if (res.duplicate) toast(res.state === 'pending' || res.state === 'sending' ? 'A switch request is already waiting for the Main' : 'A context switch is already running', 'info');
          else if (res.kind === 'coworker') toast('Context switch started: the coworker gets a fresh chat before its next task', 'success');
          else toast('Switch requested: the Main writes the handoff and switches to a fresh chat', 'success');
        })
          .catch(function (exc) { toast('Context switch failed: ' + str(exc && exc.message || exc), 'error'); });
        tick();
      }
    }

    if (ctx && typeof ctx.onState === 'function') {
      offState = ctx.onState(function (s) { state = obj(s); });
    }
    tick();
    timer = setInterval(tick, TICK_MS);
    window.addEventListener('resize', tick);
      startGone();
    document.addEventListener('pointerdown', onEffortPick, true);

    function stop() {
      stopped = true;
      closeProjMenu();
      if (timer) clearInterval(timer);
      window.removeEventListener('resize', tick);
      stopGone();
      if (rz) { rz.remove(); rz = null; }
      if (wk) { wk.remove(); wk = null; }
      feedOff();
      root.style.removeProperty('--nt-v6-sbw');
      document.removeEventListener('pointerdown', onEffortPick, true);
      if (typeof offState === 'function') { try { offState(); } catch (e) { /* ignore */ } }
      [sb, hd, defs, dg, document.querySelector('.nt-v6-badge')].forEach(function (n) { if (n && n.parentNode) n.parentNode.removeChild(n); });
      var rowEl = document.querySelector('.' + CLS_TOPROW);
      if (rowEl) rowEl.classList.remove(CLS_TOPROW);
      root.classList.remove(CLS_ON);
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
