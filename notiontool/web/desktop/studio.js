/*
 * NotionTool Studio part (studio.js): window.__ntDesk.studio = function (ctx) { ... return { stop } }.
 *
 * - Status strip above the composer: Main model + reasoning effort for the next turn (read-only,
 *   from state), 'Coworkers 1 of 10 busy' and 'Run 2 of 3 steps' (both from window.__ntDesk.counts).
 * - Notification center: bell chip in the brand header (unread count), drawer with the last 50
 *   toasts, native desktop notifications (asked once) when a run finishes, a task needs the Main
 *   (unclear) or the harness goes offline.
 * - Command palette entries (via window.__ntDesk.paletteExtra, read by extras.js): open Command
 *   Center, jump to any Main / coworker chat, toggle compact mode, copy last run summary,
 *   open notifications.
 * Only own nodes (data-nt-desk-own="1") are added; Notion nodes are never removed. Nothing here
 * sends a chat message or calls the harness.
 */
(function () {
  'use strict';
  window.__ntDesk = window.__ntDesk || {};

  var HISTORY_MAX = 50;
  var TICK_MS = 500;
  var OFFLINE_MS = 20000;
  var STRIP_GAP_PX = 10;
  var COMPACT_KEY = 'ntDeskCompact';
  var ASKED_KEY = 'ntDeskNotifyAsked';
  var STRIP_ON = 'nt-desk-strip-on';
  var COMPACT = 'nt-desk-compact';
  var ACTIVE_RUN = ['running', 'paused', 'planning', 'queued', 'starting', 'active'];
  var MODEL_LABEL = { opus: 'Opus 5.5', sonnet: 'Sonnet 5.5', haiku: 'Haiku 5.5' };

  function norm(text) { return String(text == null ? '' : text).replace(/\s+/g, ' ').trim().toLowerCase(); }
  function store(kind) { try { return window[kind]; } catch (e) { return null; } }
  function getKey(key) { var s = store('localStorage'); try { return s ? s.getItem(key) : null; } catch (e) { return null; } }
  function setKey(key, value) { var s = store('localStorage'); try { if (s) s.setItem(key, value); } catch (e) { /* ignore */ } }
  function modelLabel(code) {
    if (!code) return '';
    var key = String(code).toLowerCase();
    return MODEL_LABEL[key] || String(code);
  }
  var EFFORT_LABEL = { low: 'Low', medium: 'Medium', high: 'High', xhigh: 'Extra high', max: 'Max', auto: 'Auto' };
  function nextTurnText(nt) {
    var code = nt && nt.model ? String(nt.model) : '';
    var model = code && code.toLowerCase() !== 'auto' ? modelLabel(code) : 'Auto model';
    var e = nt && nt.effort ? String(nt.effort).toLowerCase() : 'auto';
    var effort = EFFORT_LABEL[e] || (e.charAt(0).toUpperCase() + e.slice(1)).replace(/_/g, ' ');
    return 'Next turn: ' + model + ' \u00b7 ' + effort + ' effort';
  }
  function nextTurn(state) {
    // Read-only: the harness may publish next_turn {model, effort}; older states only have it on the Main row.
    var s = state || {};
    var main = (Array.isArray(s.chats) ? s.chats : []).filter(function (c) { return c && c.role === 'main'; })[0] || {};
    var nt = s.next_turn || s.main_next_turn || main.next_turn || {};
    return { model: nt.model || main.model || '', effort: nt.effort || nt.reasoning_effort || main.effort || '' };
  }
  function coworkers(state) {
    // The one shared helper (extras.js): enabled workers of the active account, never chat rows.
    var c = typeof window.__ntDesk.counts === 'function' ? window.__ntDesk.counts(state) : null;
    return c ? { busy: c.busy, total: c.coworkers } : { busy: 0, total: 0 };
  }
  function currentRun(state) {
    var runs = state && Array.isArray(state.runs) ? state.runs : [];
    for (var i = runs.length - 1; i >= 0; i--) if (runs[i] && ACTIVE_RUN.indexOf(runs[i].state) >= 0) return runs[i];
    return runs.length ? runs[runs.length - 1] : null;
  }
  function runSummary(run) {
    if (!run) return '';
    return ['NotionTool run summary', 'Run: ' + (run.title || run.id), 'State: ' + (run.state || 'unknown') +
      (run.stage ? ' (' + run.stage + ')' : ''), 'Tasks: ' + (run.tasks_done || 0) + '/' + (run.tasks_total || 0) + ' done',
      'Run id: ' + run.id].join('\n');
  }
  window.__ntDesk.studioUtil = { nextTurn: nextTurn, coworkers: coworkers, currentRun: currentRun,
    runSummary: runSummary, modelLabel: modelLabel, nextTurnText: nextTurnText };

  window.__ntDesk.studio = function (ctx) {
    ctx = ctx || {};
    var state = null, lastPush = 0, stopped = false, unsub = null, tick = 0;
    var ui = {}, history = [], unread = 0, seeded = false, offline = false;
    var runStates = {}, unclearSeen = {};
    var prevToast = typeof ctx.toast === 'function' ? ctx.toast : null;

    function el(tag, props, kids) {
      var node = document.createElement(tag);
      node.setAttribute('data-nt-desk-own', '1');
      Object.keys(props || {}).forEach(function (key) {
        if (key === 'cls') node.className = props[key];
        else if (key === 'text') node.textContent = props[key];
        else node.setAttribute(key, props[key]);
      });
      (kids || []).forEach(function (kid) { if (kid) node.appendChild(kid); });
      return node;
    }
    function toast(text, kind) { try { if (typeof ctx.toast === 'function') ctx.toast(text, kind || 'info'); } catch (e) { /* ignore */ } }

    // ---- notification history ---------------------------------------------------------
    function record(text, kind) {
      history.unshift({ text: String(text == null ? '' : text).slice(0, 400), kind: kind || 'info', at: Date.now() });
      if (history.length > HISTORY_MAX) history.length = HISTORY_MAX;
      if (!drawerOpen()) unread += 1;
      renderBell();
      if (drawerOpen()) renderDrawer();
    }
    function wrappedToast(text, kind, code) {
      try { record(text, kind); } catch (e) { /* ignore */ }
      if (prevToast) return prevToast.apply(this, arguments);
    }
    function canNotify() { return typeof window.Notification === 'function'; }
    function askOnce() {
      if (!canNotify() || getKey(ASKED_KEY) === '1') return;
      if (Notification.permission !== 'default') { setKey(ASKED_KEY, '1'); return; }
      setKey(ASKED_KEY, '1');
      try { var p = Notification.requestPermission(); if (p && p.then) p.then(null, function () {}); } catch (e) { /* ignore */ }
    }
    function notify(title, body, tag, kind) {
      record(title + (body ? ': ' + body : ''), kind || 'info');
      if (!canNotify()) return;
      if (Notification.permission === 'default') askOnce();
      if (Notification.permission !== 'granted') return;
      try { new Notification(title, { body: body || '', tag: tag || title }); } catch (e) { /* ignore */ }
    }

    // ---- bell chip and drawer -------------------------------------------------------
    function drawerOpen() { return !!(ui.drawer && ui.drawer.isConnected && !ui.drawer.hidden); }
    function buildBell() {
      ui.count = el('span', { cls: 'nt-desk-bell-count', text: '0' });
      ui.bell = el('button', { cls: 'nt-desk-bell', id: 'nt-desk-bell', type: 'button', title: 'Notifications',
        'aria-label': 'Notifications' }, [el('span', { cls: 'nt-desk-bell-icon', text: '\u266a' }), ui.count]);
      ui.bell.firstChild.textContent = '\ud83d\udd14';
      ui.bell.addEventListener('click', function (ev) { ev.preventDefault(); ev.stopPropagation(); toggleDrawer(); });
    }
    function renderBell() {
      if (!ui.bell) return;
      ui.count.textContent = unread > 99 ? '99+' : String(unread);
      ui.count.hidden = unread <= 0;
      ui.bell.setAttribute('title', unread ? unread + ' unread notifications' : 'Notifications');
    }
    function placeBell() {
      var brand = document.querySelector('[data-nt-desk="brand"]');
      if (!ui.bell) buildBell();
      if (brand && ui.bell.parentNode !== brand) brand.appendChild(ui.bell);
      renderBell();
    }
    function buildDrawer() {
      ui.list = el('div', { cls: 'nt-desk-notify-list', role: 'list' });
      var clear = el('button', { cls: 'nt-desk-notify-clear', type: 'button', text: 'Clear' });
      clear.addEventListener('click', function () { history = []; unread = 0; renderBell(); renderDrawer(); });
      var close = el('button', { cls: 'nt-desk-notify-close', type: 'button', text: '\u00d7', 'aria-label': 'Close' });
      close.addEventListener('click', function () { toggleDrawer(false); });
      ui.drawer = el('div', { cls: 'nt-desk-notify', id: 'nt-desk-notify-drawer', role: 'dialog',
        'aria-label': 'Notifications' }, [
        el('div', { cls: 'nt-desk-notify-head' }, [el('span', { cls: 'nt-desk-notify-title', text: 'Notifications' }), clear, close]),
        ui.list]);
      ui.drawer.hidden = true;
      document.body.appendChild(ui.drawer);
    }
    function renderDrawer() {
      if (!ui.list) return;
      ui.list.textContent = '';
      if (!history.length) ui.list.appendChild(el('div', { cls: 'nt-desk-notify-empty', text: 'No notifications yet' }));
      history.forEach(function (item) {
        var time = new Date(item.at);
        ui.list.appendChild(el('div', { cls: 'nt-desk-notify-item nt-desk-notify-' + item.kind, role: 'listitem' }, [
          el('span', { cls: 'nt-desk-notify-time', text: time.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) }),
          el('span', { cls: 'nt-desk-notify-text', text: item.text })]));
      });
    }
    function toggleDrawer(force) {
      if (!ui.drawer) buildDrawer();
      var open = typeof force === 'boolean' ? force : ui.drawer.hidden;
      ui.drawer.hidden = !open;
      if (open) {
        askOnce();  // user gesture: the one permission prompt
        unread = 0;
        renderBell();
        renderDrawer();
        var r = ui.bell && ui.bell.isConnected ? ui.bell.getBoundingClientRect() : null;
        ui.drawer.style.top = Math.round(r ? r.bottom + 8 : 56) + 'px';
        ui.drawer.style.left = Math.round(r ? Math.max(8, Math.min(r.left, window.innerWidth - 376)) : 12) + 'px';
      }
    }
    function onDocDown(ev) {
      if (!drawerOpen()) return;
      if (ui.drawer.contains(ev.target) || (ui.bell && ui.bell.contains(ev.target))) return;
      toggleDrawer(false);
    }
    function onKey(ev) { if (ev.key === 'Escape' && drawerOpen()) toggleDrawer(false); }

    // ---- status strip -----------------------------------------------------------------
    function buildStrip() {
      ui.model = el('span', { cls: 'nt-desk-strip-item nt-desk-strip-model' });
      ui.crew = el('span', { cls: 'nt-desk-strip-item nt-desk-strip-crew' });
      ui.runText = el('span', { cls: 'nt-desk-strip-run-text' });
      ui.runFill = el('span', { cls: 'nt-desk-strip-fill' });
      ui.run = el('span', { cls: 'nt-desk-strip-item nt-desk-strip-run' }, [ui.runText,
        el('span', { cls: 'nt-desk-strip-bar' }, [ui.runFill])]);
      ui.tokens = el('span', { cls: 'nt-desk-strip-item nt-desk-strip-tokens' });
      // Idle mode button: on = after a NotionTool restart (build swap) the Main continues by itself.
      ui.idle = el('button', { cls: 'nt-desk-strip-item nt-desk-strip-idle', type: 'button' });
      ui.idle.style.pointerEvents = 'auto';
      ui.idle.style.cursor = 'pointer';
      ui.idle.addEventListener('click', toggleIdle);
      ui.strip = el('div', { cls: 'nt-desk-strip', id: 'nt-desk-strip', role: 'status', 'aria-live': 'polite' },
        [ui.model, ui.crew, ui.run, ui.tokens, ui.idle]);
      ui.strip.hidden = true;
      document.body.appendChild(ui.strip);
    }
    var idleWanted = null;  // value just clicked, until the next state shows it
    function idleOn() {
      if (idleWanted !== null) return idleWanted;
      return !(state && state.idle_mode === false);
    }
    function toggleIdle(ev) {
      if (ev) { ev.preventDefault(); ev.stopPropagation(); }
      var next = !idleOn();
      idleWanted = next;
      renderIdle();
      Promise.resolve(ctx.call('settings_set', { values: { idle_mode: next } })).then(function () {
        toast(next ? 'Idle mode on: the Main continues by itself after a restart' : 'Idle mode off', 'info');
      }, function (e) {
        idleWanted = null; renderIdle();
        toast('Idle mode could not be saved: ' + String((e && e.message) || e).slice(0, 120), 'warning');
      });
    }
    function renderIdle() {
      if (!ui.idle) return;
      if (idleWanted !== null && state && state.idle_mode === idleWanted) idleWanted = null;
      var on = idleOn();
      ui.idle.textContent = on ? 'Idle mode: on' : 'Idle mode: off';
      ui.idle.setAttribute('aria-pressed', on ? 'true' : 'false');
      ui.idle.setAttribute('title', on ? 'On: after a NotionTool restart (e.g. an update) the Main continues automatically. Click to turn off.'
        : 'Off: after a restart the Main waits for you. Click to turn on.');
      ui.idle.classList.toggle('nt-desk-strip-hot', on);
    }
    function composer() {
      var best = null, area = 0;
      Array.prototype.forEach.call(document.querySelectorAll('.nt-desk-composer'), function (node) {
        var r = node.getBoundingClientRect();
        if (r.width < 200 || r.height < 20 || r.bottom <= 0 || r.top >= window.innerHeight) return;
        if (r.width * r.height > area) { area = r.width * r.height; best = r; }
      });
      return best;
    }
    // ---- 6-hour token budget: shown in the strip; sending is blocked while it is used up ----
    function budget() { var b = state && state.token_budget; return b && typeof b === 'object' && b.limit ? b : null; }
    function millions(n) { n = Number(n) || 0; return n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e3 ? Math.round(n / 1e3) + 'k' : String(n); }
    function budgetBlocked() { var b = budget(); return !!(b && b.blocked && !document.documentElement.classList.contains('nt-desk-off')); }
    function inComposer(node) { return !!(node && node.closest && node.closest('.nt-desk-composer')); }
    var SEND_RE = /^(send|submit|senden|absenden)\b/i;
    function blockSend(ev) {
      ev.preventDefault(); ev.stopPropagation(); if (ev.stopImmediatePropagation) ev.stopImmediatePropagation();
      var b = budget();
      toast('Token limit of ' + millions(b && b.limit) + ' per 6 h is reached. Sending is blocked; resets in ' +
        Math.ceil(((b && b.resets_in) || 0) / 60) + ' min.', 'warning');
    }
    function onSendKey(ev) {
      if (ev.key !== 'Enter' || ev.shiftKey || ev.isComposing || !budgetBlocked() || !inComposer(ev.target)) return;
      blockSend(ev);
    }
    function onSendClick(ev) {
      if (!budgetBlocked() || !inComposer(ev.target)) return;
      var btn = ev.target.closest('button,[role="button"]');
      var label = btn ? (btn.getAttribute('aria-label') || btn.getAttribute('title') || '') : '';
      if (btn && SEND_RE.test(label.trim())) blockSend(ev);
    }
    window.addEventListener('keydown', onSendKey, true);
    window.addEventListener('click', onSendClick, true);
    window.addEventListener('pointerdown', onSendClick, true);

    // True only while a Notion AI chat is the visible view (Main or a coworker chat).
    function overlayOpen() {
      var nodes = document.querySelectorAll('[role="dialog"], [role="menu"], [role="listbox"], [aria-modal="true"]');
      for (var i = 0; i < nodes.length; i++) {
        var n = nodes[i];
        if (ui.strip && ui.strip.contains(n)) continue;
        if (ui.drawer && ui.drawer.contains(n)) continue;
        var rect = n.getBoundingClientRect();
        if (rect.width > 40 && rect.height > 40) return true;
      }
      return false;
    }
    function inChatView() {
      var sh = window.__ntShell;
      if (sh && sh.current && sh.current !== 'chat') return false;  // Live, Settings, ... tabs
      var href = String(location.href || '');
      return /\/(ai|chat)(?=[\/?#]|$)/i.test(href) || /[?&]t=[0-9a-f-]{32,36}/i.test(href);
    }
    var MODEL_RE = /^(Auto|Opus|Sonnet|Haiku|GPT|Gemini|o\d)[\w .\-]{0,24}$/i;
    function domModel(root) {
      var scope = root && root.querySelectorAll ? root : document;
      var btns = scope.querySelectorAll('[role="button"]');
      for (var i = 0; i < btns.length; i++) {
        var b = btns[i];
        if (b.closest && b.closest('[data-nt-desk]')) continue;
        var t = String(b.textContent || '').replace(/\s+/g, ' ').trim();
        if (!t || !MODEL_RE.test(t)) continue;
        var rc = b.getBoundingClientRect();
        if (rc.width > 0 && rc.height > 0) return t;
      }
      return '';
    }
    function renderStrip() {
      if (!ui.strip) buildStrip();
      if (!ui.strip.isConnected) document.body.appendChild(ui.strip);
      var root = document.documentElement;
      var r = composer();
      var off = root.classList.contains('nt-desk-off');
      // The strip belongs to the AI chat only: hidden on pages, databases, Live and every other view.
      // Bug 2026-10-21: the strip stayed over Notion's Settings dialog and over the model menu, so the
      // model could not be changed. Any open Notion dialog/menu/listbox hides the strip.
      if (!r || off || !inChatView() || overlayOpen()) { ui.strip.hidden = true; root.classList.remove(STRIP_ON); return; }
      var nt = nextTurn(state);
      // Bug 2026-10-21: the harness never publishes next_turn, so the strip always said 'Auto model'.
      // Without harness data, show the model Notion's own composer chip really uses.
      var dm = nt.model ? '' : domModel(r);
      if (dm) { ui.model.textContent = 'Model: ' + dm; nt = { model: dm, effort: '', fromNotion: true }; }
      else ui.model.textContent = nextTurnText(nt);
      ui.model.setAttribute('title', nt.fromNotion ? 'Model shown by Notion for this chat' : nt.model ? 'Model and reasoning effort the harness picked for the Main\'s next turn'
        : 'The harness has not published the next-turn model yet');
      // Every number comes from window.__ntDesk.counts, the same helper the Live tab and grid use.
      var c = typeof window.__ntDesk.counts === 'function' ? window.__ntDesk.counts(state) : null;
      ui.crew.textContent = c ? c.coworkersText : 'No coworkers';
      ui.crew.classList.toggle('nt-desk-strip-hot', !!(c && c.busy > 0));
      var run = c ? c.run : null;
      ui.runText.textContent = c ? c.runText : 'No run yet';
      if (run) ui.run.setAttribute('title', run.title || 'Current run');
      else ui.run.removeAttribute('title');
      ui.runFill.style.width = (run ? run.percent : 0) + '%';
      ui.run.classList.toggle('nt-desk-strip-idle', !(run && run.active));
      var tb = budget();
      ui.tokens.hidden = !tb;
      if (tb) {
        ui.tokens.textContent = (tb.blocked ? 'Token limit reached \u00b7 ' : 'Tokens ') + millions(tb.used) + ' of ' +
          millions(tb.limit) + ' (6 h)';
        ui.tokens.setAttribute('title', 'Estimated tokens of Main and coworkers in this 6-hour window; resets in ' +
          Math.ceil((tb.resets_in || 0) / 60) + ' min. Sending is blocked when the limit is reached.');
        ui.tokens.classList.toggle('nt-desk-strip-hot', !!tb.blocked || tb.percent >= 80);
      }
      renderIdle();
      ui.strip.hidden = false;
      var h = ui.strip.offsetHeight || 26;
      ui.strip.style.left = Math.round(r.left) + 'px';
      ui.strip.style.width = Math.round(r.width) + 'px';
      ui.strip.style.top = Math.round(Math.max(4, r.top - h - STRIP_GAP_PX)) + 'px';
      root.classList.add(STRIP_ON);  // hides the composer 'NotionTool' border label (it would touch the strip)
    }

    // ---- state watching: native notifications -------------------------------------------
    function watch() {
      var runs = state && Array.isArray(state.runs) ? state.runs : [];
      runs.forEach(function (run) {
        if (!run || !run.id) return;
        var before = runStates[run.id];
        if (seeded && before && ACTIVE_RUN.indexOf(before) >= 0 && ACTIVE_RUN.indexOf(run.state) < 0) {
          notify(run.state === 'failed' ? 'Run failed' : 'Run finished', String(run.title || run.id).slice(0, 120) +
            ' (' + (run.tasks_done || 0) + '/' + (run.tasks_total || 0) + ' tasks)', 'nt-run-' + run.id,
            run.state === 'failed' ? 'error' : 'success');
        }
        runStates[run.id] = run.state;
      });
      var waiting = state && Array.isArray(state.awaiting_main) ? state.awaiting_main : [];
      waiting.forEach(function (entry) {
        (Array.isArray(entry && entry.needs_main) ? entry.needs_main : []).forEach(function (need) {
          var id = need && need.task_id;
          if (!id || unclearSeen[id]) return;
          unclearSeen[id] = true;
          if (seeded) notify('Task is unclear', need.reason || 'A task needs the Main', 'nt-unclear-' + id, 'warning');
        });
      });
      seeded = true;
    }
    function checkOffline() {
      // Any harness message counts as alive: state pushes and the 3 s heartbeat (__ntDeskLastMessage).
      var beat = typeof window.__ntDeskLastMessage === 'number' ? window.__ntDeskLastMessage : 0;
      var seen = Math.max(lastPush || 0, beat);
      var down = (seen && Date.now() - seen > OFFLINE_MS) || !!(state && state.connected === false);
      if (down && !offline) notify('NotionTool harness is offline', 'No state update for 20 seconds', 'nt-offline', 'error');
      else if (!down && offline) record('NotionTool harness is back online', 'success');
      offline = !!down;
    }
    function onPush(next) {
      lastPush = Date.now();
      if (next && typeof next === 'object') state = next;
      try { watch(); } catch (e) { /* ignore */ }
    }
    function render() {
      if (stopped || !document.body) return;
      try { placeBell(); renderStrip(); checkOffline(); } catch (e) { /* never throw into the page */ }
    }

    // ---- palette entries -----------------------------------------------------------------
    function jump(chat) {
      var want = norm(chat.title), exact = null, loose = null;
      Array.prototype.forEach.call(document.querySelectorAll('.notion-sidebar a[href], nav a[href]'), function (a) {
        var text = norm(a.textContent);
        if (!text || a.closest('[data-nt-desk-own]')) return;
        if (text === want) exact = exact || a;
        else if (want && text.indexOf(want) >= 0) loose = loose || a;
      });
      var hit = exact || loose;
      if (!hit) { toast('Chat "' + chat.title + '" is not in the sidebar list', 'warning'); return; }
      hit.click();  // Notion's own in-app navigation; no reload, nothing is sent
    }
    function setCompact(on) {
      document.documentElement.classList.toggle(COMPACT, on);
      setKey(COMPACT_KEY, on ? '1' : '0');
    }
    function copyText(text) {
      if (navigator.clipboard && navigator.clipboard.writeText) return navigator.clipboard.writeText(text);
      return new Promise(function (resolve, reject) {
        var area = el('textarea', {});
        area.value = text;
        area.style.position = 'fixed'; area.style.opacity = '0';
        document.body.appendChild(area); area.select();
        var ok = false;
        try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
        area.parentNode.removeChild(area);
        if (ok) resolve(); else reject(new Error('Clipboard not available'));
      });
    }
    function copyRun() {
      var runs = state && Array.isArray(state.runs) ? state.runs : [];
      var run = runs[runs.length - 1];
      if (!run) { toast('There is no run to copy yet', 'warning'); return; }
      return copyText(runSummary(run)).then(function () { toast('Last run summary copied', 'success'); },
        function () { toast('Could not copy the run summary', 'error'); });
    }
    function paletteExtra() {
      var compact = document.documentElement.classList.contains(COMPACT);
      var list = [
        { title: 'Open Command Center', hint: 'Runs, coworkers and totals', keys: 'Ctrl+Shift+L',
          words: 'command center dashboard live', run: function () { location.hash = '#nt-live'; } },
        { title: compact ? 'Turn compact mode off' : 'Turn compact mode on', hint: 'Denser layout', keys: '',
          words: 'compact dense density toggle', run: function () { setCompact(!compact); } },
        { title: 'Copy last run summary', hint: 'Run title, state and task count to the clipboard', keys: '',
          words: 'copy clipboard run summary', run: copyRun },
        { title: 'Open notifications', hint: 'Last ' + HISTORY_MAX + ' notifications', keys: '',
          words: 'notifications bell history toasts', run: function () { toggleDrawer(true); } }
      ];
      (state && Array.isArray(state.chats) ? state.chats : []).forEach(function (chat) {
        if (!chat || !chat.title) return;
        var main = chat.role === 'main';
        list.push({ title: (main ? 'Jump to Main: ' : 'Jump to coworker: ') + chat.title,
          hint: chat.task_title ? 'Working on: ' + String(chat.task_title).slice(0, 80) : (chat.state || ''), keys: '',
          words: 'jump go open chat ' + (main ? 'main' : 'coworker'), run: function () { jump(chat); } });
      });
      return list;
    }

    try {
      if (getKey(COMPACT_KEY) === '1') document.documentElement.classList.add(COMPACT);
      ctx.toast = wrappedToast;
      window.__ntDesk.paletteExtra = paletteExtra;
      if (typeof ctx.getState === 'function') { try { state = ctx.getState(); } catch (e) { state = null; } }
      if (state) watch();
      if (typeof ctx.onState === 'function') { try { unsub = ctx.onState(onPush); } catch (e) { unsub = null; } }
      document.addEventListener('mousedown', onDocDown, true);
      window.addEventListener('keydown', onKey, true);
      window.addEventListener('resize', render);
      tick = setInterval(render, TICK_MS);
      render();
    } catch (e) { /* never throw into the page */ }

    return {
      stop: function () {
        stopped = true;
        try {
          if (tick) clearInterval(tick);
          if (typeof unsub === 'function') unsub();
          if (ctx.toast === wrappedToast) ctx.toast = prevToast;
          if (window.__ntDesk.paletteExtra === paletteExtra) delete window.__ntDesk.paletteExtra;
          document.removeEventListener('mousedown', onDocDown, true);
          window.removeEventListener('keydown', onKey, true);
          window.removeEventListener('resize', render);
          document.documentElement.classList.remove(STRIP_ON, COMPACT);
          [ui.bell, ui.drawer, ui.strip].forEach(function (n) { if (n && n.parentNode) n.parentNode.removeChild(n); });
        } catch (e) { /* ignore */ }
      },
      openNotifications: function () { toggleDrawer(true); }
    };
  };
})();
