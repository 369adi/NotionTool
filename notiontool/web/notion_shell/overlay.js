/* NotionTool overlay for the Notion AI chat page (Phase 8b, part B).
 *
 * Contract (shared with notiontool/notion_shell.py):
 * - The page calls window.ntHarness(JSON.stringify({id, action, args})).
 * - Python answers with window.__ntHarnessReply(id, {ok, result|error}).
 * - Python pushes live state with window.__ntHarnessPush(state), at most once per second.
 * Live data comes only through this binding. The overlay never talks to the local host API
 * or to Notion itself, keeps no credentials and runs no timed refresh loop.
 */
(function () {
  'use strict';
  if (window.top !== window) return;
  if (window.__ntOverlayInstalled) return;
  window.__ntOverlayInstalled = true;

  var ACTIONS = ['state', 'runs', 'run_pause', 'run_cancel', 'run_retry', 'task_reconcile', 'settings_get',
    'settings_set', 'mcp_list', 'mcp_refresh', 'run_confirm', 'mcp_add', 'mcp_bridge', 'host_key',
    'bridge_tunnel'];
  var TABS = [['live', 'Live'], ['runs', 'Runs'], ['harness', 'Harness'], ['mcp', 'MCP'], ['settings', 'Settings']];
  // Sidebar and menu entries that are not part of the AI chat (matched on aria-label or visible text).
  var HIDE_LABELS = ['home', 'inbox', 'search', 'meetings', 'meeting notes', 'agents', 'new agent', 'help',
    'help & support', 'help and support', 'notifications', 'updates', "what's new", 'create work account',
    'join or create workspace', 'add another account', 'get mac app', 'get windows app', 'get the app',
    'invite members', 'upgrade plan', 'try notion mail', 'notion calendar', 'notion mail'];
  // Never hidden: chat list, conversation, composer, model picker, reasoning level, attachments.
  var KEEP_PREFIXES = ['new chat', 'chat', 'model', 'reasoning', 'attach', 'add attachment', 'upload', 'send',
    'stop', 'mention', 'add context'];
  var KEEP_SELECTOR = '[contenteditable="true"],[role="textbox"],textarea,input[type="file"],[role="log"],[role="feed"]';
  var CANDIDATES = '[role="button"],[role="link"],[role="treeitem"],[role="menuitem"],[role="tab"],button,a';
  var HARNESS_KEYS = ['harness_enabled', 'max_workers', 'per_account', 'max_attempts', 'task_timeout',
    'max_fix_rounds', 'max_runs_per_chat', 'coworker_fresh_context', 'context_token_limit',
    'context_turn_limit', 'context_handoff_chars', 'context_replace_mode', 'coworker_rotate_mode',
    'main_context_policy', 'coworker_delete_old_chat'];
  // Settings with a fixed set of values (validated again by POST /api/settings).
  var CHOICES = { coworker_rotate_mode: ['new_welcome', 'same_thread'], main_context_policy: ['hint', 'auto'] };
  // Shown in the Harness tab even before they were saved once (server defaults).
  var HARNESS_DEFAULTS = { coworker_rotate_mode: 'new_welcome', main_context_policy: 'hint', coworker_delete_old_chat: false };
  var ATTENTION_LIMIT = 10;
  var LONG_ACTIONS = { mcp_refresh: 60000 };
  var DEFAULT_WAIT = 30000;
  var STORE_KEY = 'nt-overlay-collapsed';

  var live = null;
  var pending = {};
  var seq = 0;
  var activeTab = 'live';
  var settings = null;
  var mcpServers = null;
  var ui = {};
  var scanTimer = 0;

  // ---- binding ---------------------------------------------------------------------------
  function call(action, args) {
    return new Promise(function (resolve, reject) {
      if (ACTIONS.indexOf(action) < 0) { reject(new Error('Action not allowed')); return; }
      if (typeof window.ntHarness !== 'function') { reject(new Error('The harness is not connected')); return; }
      var id = 'nt' + (++seq) + '-' + Date.now().toString(36);
      var timer = setTimeout(function () {
        if (!pending[id]) return;
        delete pending[id];
        reject(new Error('No answer from the harness'));
      }, LONG_ACTIONS[action] || DEFAULT_WAIT);
      pending[id] = { resolve: resolve, reject: reject, timer: timer };
      try {
        window.ntHarness(JSON.stringify({ id: id, action: action, args: args || {} }));
      } catch (err) {
        clearTimeout(timer);
        delete pending[id];
        reject(err);
      }
    });
  }

  window.__ntHarnessReply = function (id, reply) {
    var entry = pending[id];
    if (!entry) return false;
    delete pending[id];
    clearTimeout(entry.timer);
    if (reply && reply.ok) entry.resolve(reply.result);
    else entry.reject(new Error((reply && reply.error) || 'Harness error'));
    return true;
  };

  window.__ntHarnessPush = function (state) {
    if (!state || typeof state !== 'object') return false;
    live = state;
    renderHeader();
    if (activeTab === 'live' || activeTab === 'runs') renderBody();
    return true;
  };

  // ---- helpers ---------------------------------------------------------------------------
  function el(tag, props, kids) {
    var node = document.createElement(tag);
    Object.keys(props || {}).forEach(function (key) {
      var value = props[key];
      if (value === undefined || value === null || value === false) return;
      if (key === 'text') node.textContent = String(value);
      else if (key === 'cls') node.className = value;
      else if (key.slice(0, 2) === 'on') node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? '' : String(value));
    });
    (kids || []).forEach(function (kid) {
      if (kid) node.appendChild(typeof kid === 'string' ? document.createTextNode(kid) : kid);
    });
    return node;
  }

  function svg(tag, attrs) {
    var node = document.createElementNS('http://www.w3.org/2000/svg', tag);
    Object.keys(attrs || {}).forEach(function (key) { node.setAttribute(key, String(attrs[key])); });
    return node;
  }

  function norm(text) { return String(text || '').replace(/\s+/g, ' ').trim().toLowerCase(); }

  function labelOf(node) {
    var label = node.getAttribute('aria-label');
    return norm(label !== null && label !== '' ? label : node.textContent);
  }

  function message(err) { return (err && err.message) || String(err || 'Unknown error'); }

  function list(value) { return Array.isArray(value) ? value : []; }

  function parseTime(value) {
    if (value === undefined || value === null || value === '') return 0;
    if (typeof value === 'number') return value > 1e12 ? value : value * 1000;
    var asNumber = Number(value);
    if (!isNaN(asNumber)) return parseTime(asNumber);
    var parsed = Date.parse(String(value));
    return isNaN(parsed) ? 0 : parsed;
  }

  function duration(ms) {
    if (!(ms > 0)) return '';
    var s = Math.floor(ms / 1000);
    if (s < 60) return s + 's';
    var m = Math.floor(s / 60);
    if (m < 60) return m + 'm ' + (s % 60) + 's';
    return Math.floor(m / 60) + 'h ' + (m % 60) + 'm';
  }

  function clock(ms) {
    if (!ms) return '';
    var d = new Date(ms);
    function two(n) { return (n < 10 ? '0' : '') + n; }
    return two(d.getHours()) + ':' + two(d.getMinutes()) + ':' + two(d.getSeconds());
  }

  function humanize(key) {
    var text = String(key).replace(/_/g, ' ');
    return text.charAt(0).toUpperCase() + text.slice(1);
  }

  function stateClass(state) {
    var s = norm(state);
    if (/(fail|error|block|dead|refus)/.test(s)) return 'err';
    if (/(pause|wait|uncertain|unknown|cancel|queue|pending)/.test(s)) return 'warn';
    if (/(run|busy|active|work|exec|plan|review|stream)/.test(s)) return 'run';
    return 'idle';
  }

  // ---- toasts ----------------------------------------------------------------------------
  function toast(text, kind, closeAfter) {
    var item = el('div', { cls: 'nt-toast nt-toast-' + (kind || 'info'), role: 'status', text: text });
    if (ui.toasts) ui.toasts.appendChild(item);
    var handle = {
      update: function (nextText, nextKind) {
        item.textContent = nextText;
        item.className = 'nt-toast nt-toast-' + (nextKind || 'info');
      },
      close: function (after) {
        setTimeout(function () {
          item.classList.add('nt-toast-out');
          setTimeout(function () { if (item.parentNode) item.parentNode.removeChild(item); }, 250);
        }, after || 0);
      }
    };
    if (closeAfter !== 0) handle.close(closeAfter || 3500);
    return handle;
  }

  // ---- hide non-chat parts of the page ---------------------------------------------------
  function isProtected(node) {
    if (node.closest('#nt-overlay-root')) return true;
    if (node.matches(KEEP_SELECTOR) || node.querySelector(KEEP_SELECTOR)) return true;
    var label = labelOf(node);
    for (var i = 0; i < KEEP_PREFIXES.length; i++) {
      if (label.indexOf(KEEP_PREFIXES[i]) === 0) return true;
    }
    return false;
  }

  function scan() {
    scanTimer = 0;
    if (!document.body) return;
    if (!document.getElementById('nt-overlay-root')) mount();
    var nodes = document.querySelectorAll(CANDIDATES);
    for (var i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      if (node.getAttribute('data-nt-hidden') === '1') continue;
      var label = labelOf(node);
      if (!label || label.length > 40 || HIDE_LABELS.indexOf(label) < 0) continue;
      if (isProtected(node)) continue;
      node.setAttribute('data-nt-hidden', '1');
    }
  }

  function scheduleScan() {
    if (!scanTimer) scanTimer = setTimeout(scan, 120);
  }

  // ---- New chat: the harness never creates chats (Phase 8b); the button is disabled ------
  function newChatButton(target) {
    var node = target && target.closest ? target.closest('[role="button"],[role="link"],button,a') : null;
    if (!node || node.closest('#nt-overlay-root')) return null;
    var label = labelOf(node);
    if (label !== 'new chat' && label !== 'new ai chat') return null;
    var rect = node.getBoundingClientRect();
    if (rect.left > window.innerWidth / 2) return null;
    return node;
  }

  function block(ev) {
    ev.preventDefault();
    ev.stopImmediatePropagation();
  }

  function onPress(ev) {
    if (newChatButton(ev.target)) block(ev);
  }

  function onClick(ev) {
    if (!newChatButton(ev.target)) return;
    block(ev);
    newChatRefused();
  }

  function onKey(ev) {
    if (ev.key !== 'Enter' && ev.key !== ' ') return;
    if (!newChatButton(ev.target)) return;
    block(ev);
    newChatRefused();
  }

  function newChatRefused() {
    toast('New chats are not created here. Open an existing Welcome chat from the chat list.', 'info', 6000);
  }

  // ---- panel -----------------------------------------------------------------------------
  function collapsed() {
    try { return window.localStorage.getItem(STORE_KEY) === '1'; } catch (err) { return false; }
  }

  function setCollapsed(value) {
    if (!ui.root) return;
    ui.root.classList.toggle('nt-collapsed', value);
    ui.toggle.setAttribute('aria-expanded', value ? 'false' : 'true');
    ui.toggle.textContent = value ? '\u2039' : '\u203a';
    try { window.localStorage.setItem(STORE_KEY, value ? '1' : '0'); } catch (err) { /* private mode */ }
  }

  function mount() {
    if (!document.body || document.getElementById('nt-overlay-root')) return;
    ui.toggle = el('button', { cls: 'nt-toggle', type: 'button', title: 'Harness panel', 'aria-label': 'Toggle harness panel',
      onclick: function () { setCollapsed(!ui.root.classList.contains('nt-collapsed')); } });
    ui.status = el('div', { cls: 'nt-status' });
    ui.tabs = el('nav', { cls: 'nt-tabs', role: 'tablist' });
    TABS.forEach(function (tab) {
      ui.tabs.appendChild(el('button', { cls: 'nt-tab', type: 'button', role: 'tab', 'data-tab': tab[0], text: tab[1],
        onclick: function () { selectTab(tab[0]); } }));
    });
    ui.body = el('section', { cls: 'nt-body', role: 'tabpanel' });
    ui.panel = el('aside', { cls: 'nt-panel', 'aria-label': 'NotionTool harness' }, [
      el('header', { cls: 'nt-head' }, [el('div', { cls: 'nt-brand', text: 'NotionTool' }), ui.status]),
      ui.tabs, ui.body]);
    ui.toasts = el('div', { cls: 'nt-toasts', 'aria-live': 'polite' });
    ui.root = el('div', { id: 'nt-overlay-root' }, [ui.toggle, ui.panel, ui.toasts]);
    document.body.appendChild(ui.root);
    setCollapsed(collapsed());
    selectTab(activeTab);
    if (!live) {
      call('state').then(function (state) { if (!live) window.__ntHarnessPush(state); }).catch(function () { /* push follows */ });
    }
  }

  function selectTab(name) {
    activeTab = name;
    var buttons = ui.tabs.querySelectorAll('.nt-tab');
    for (var i = 0; i < buttons.length; i++) {
      var on = buttons[i].getAttribute('data-tab') === name;
      buttons[i].classList.toggle('nt-active', on);
      buttons[i].setAttribute('aria-selected', on ? 'true' : 'false');
    }
    if ((name === 'harness' || name === 'settings') && !settings) loadSettings();
    if (name === 'mcp' && !mcpServers) loadMcp();
    renderHeader();
    renderBody();
  }

  function renderHeader() {
    if (!ui.status) return;
    var s = live || {};
    var parts = [];
    if (s.account_id) parts.push('Account ' + String(s.account_id).slice(0, 4));
    if (s.ts) parts.push('Updated ' + clock(parseTime(s.ts)));
    ui.status.textContent = parts.length ? parts.join(' \u00b7 ') : 'Waiting for the harness';
  }

  function renderBody() {
    if (!ui.body) return;
    ui.body.textContent = '';
    if (activeTab === 'live') renderLive(ui.body);
    else if (activeTab === 'runs') renderRuns(ui.body);
    else if (activeTab === 'harness') renderSettings(ui.body, true);
    else if (activeTab === 'settings') renderSettings(ui.body, false);
    else if (activeTab === 'mcp') renderMcp(ui.body);
  }

  // ---- Live ------------------------------------------------------------------------------
  function renderMeter(body, limits) {
    var max = Number(limits && limits.requests_per_min) || 0;
    var used = Number(limits && limits.window_used) || 0;
    var share = max > 0 ? Math.min(1, used / max) : 0;
    var bar = el('div', { cls: 'nt-meter-fill' });
    bar.style.width = Math.round(share * 100) + '%';
    bar.classList.add(share >= 0.9 ? 'nt-hot' : share >= 0.6 ? 'nt-warm' : 'nt-cool');
    body.appendChild(el('div', { cls: 'nt-meter' }, [
      el('div', { cls: 'nt-meter-label' }, [el('span', { text: 'Requests this minute' }),
        el('span', { cls: 'nt-mono', text: used + ' / ' + (max || '-') })]),
      el('div', { cls: 'nt-meter-track' }, [bar])]));
  }

  function card(chat) {
    var started = parseTime(chat.started);
    var runtime = started ? duration(Date.now() - started) : '';
    var task = chat.task_title ? chat.task_title + (chat.task_role ? ' (' + chat.task_role + ')' : '') : 'No current task';
    var node = el('div', { cls: 'nt-card nt-role-' + (chat.role === 'main' ? 'main' : 'coworker') }, [
      el('div', { cls: 'nt-card-top' }, [
        el('span', { cls: 'nt-dot nt-' + stateClass(chat.state), title: String(chat.state || 'idle') }),
        el('span', { cls: 'nt-card-title', text: chat.title || (chat.role === 'main' ? 'Main' : 'Coworker') }),
        el('span', { cls: 'nt-badge', text: chat.role === 'main' ? 'Main' : 'Coworker' })]),
      el('div', { cls: 'nt-card-task', text: task }),
      el('div', { cls: 'nt-card-meta' }, [el('span', { text: String(chat.state || 'idle') }),
        runtime ? el('span', { cls: 'nt-mono', text: runtime }) : null])]);
    node.setAttribute('data-chat-id', String(chat.id || ''));
    return node;
  }

  function findCard(graph, id) {
    var cards = graph.querySelectorAll('.nt-card');
    for (var i = 0; i < cards.length; i++) {
      if (cards[i].getAttribute('data-chat-id') === String(id)) return cards[i];
    }
    return null;
  }

  function drawLinks(graph, layer, links) {
    var box = graph.getBoundingClientRect();
    layer.setAttribute('width', Math.max(1, Math.round(box.width)));
    layer.setAttribute('height', Math.max(1, Math.round(box.height)));
    links.forEach(function (link) {
      var a = findCard(graph, link.from);
      var b = findCard(graph, link.to);
      if (!a || !b || a === b) return;
      var ra = a.getBoundingClientRect();
      var rb = b.getBoundingClientRect();
      var x1 = ra.left + ra.width / 2 - box.left;
      var y1 = ra.top + ra.height / 2 - box.top;
      var x2 = rb.left + rb.width / 2 - box.left;
      var y2 = rb.top + rb.height / 2 - box.top;
      var bend = Math.max(24, Math.abs(y2 - y1) / 2);
      var kind = ['assign', 'review', 'result'].indexOf(link.kind) >= 0 ? link.kind : 'assign';
      layer.appendChild(svg('path', {
        d: 'M' + x1 + ' ' + y1 + ' C' + x1 + ' ' + (y1 + bend) + ' ' + x2 + ' ' + (y2 - bend) + ' ' + x2 + ' ' + y2,
        'class': 'nt-link nt-link-' + kind
      }));
    });
  }

  function renderLive(body) {
    var s = live || {};
    renderMeter(body, s.limits);
    renderAttention(body, s);
    var chats = list(s.chats);
    if (!chats.length) {
      body.appendChild(el('p', { cls: 'nt-empty', text: live ? 'No active chats.' : 'Waiting for live data from the harness.' }));
      return;
    }
    var layer = svg('svg', { 'class': 'nt-links', 'aria-hidden': 'true' });
    var mains = el('div', { cls: 'nt-row nt-row-main' });
    var coworkers = el('div', { cls: 'nt-row nt-row-coworker' });
    chats.forEach(function (chat) { (chat.role === 'main' ? mains : coworkers).appendChild(card(chat)); });
    var graph = el('div', { cls: 'nt-graph' }, [mains, coworkers]);
    graph.insertBefore(layer, graph.firstChild);
    body.appendChild(graph);
    body.appendChild(el('div', { cls: 'nt-legend' }, [
      el('span', { cls: 'nt-key nt-key-assign', text: 'assign' }),
      el('span', { cls: 'nt-key nt-key-review', text: 'review' }),
      el('span', { cls: 'nt-key nt-key-result', text: 'result' })]));
    window.requestAnimationFrame(function () { drawLinks(graph, layer, list(s.links)); });
  }

  // ---- Attention: runs waiting for the Main, stalled and stale tasks (hidden when empty) ---
  function renderAttention(body, s) {
    var awaiting = list(s.awaiting_main).slice(0, ATTENTION_LIMIT);
    var stalled = list(s.stalled).slice(0, ATTENTION_LIMIT);
    var stale = list(s.stale_context_tasks).slice(0, ATTENTION_LIMIT);
    if (!awaiting.length && !stalled.length && !stale.length) return;
    var box = el('div', { cls: 'nt-attention' }, [el('div', { cls: 'nt-attention-title', text: 'Needs attention' })]);
    awaiting.forEach(function (run) { box.appendChild(awaitingRow(run)); });
    stalled.forEach(function (task) {
      box.appendChild(el('div', { cls: 'nt-attention-line nt-attention-stalled',
        text: 'Stalled: ' + (task.title || task.task_id || 'Task') + ' \u00b7 no progress for ' +
          duration((Number(task.stall_age_s) || 0) * 1000) + (task.worker_label ? ' \u00b7 ' + task.worker_label : '') }));
    });
    stale.forEach(function (task) {
      box.appendChild(el('div', { cls: 'nt-attention-line nt-attention-stale',
        text: 'Stale context task: ' + (task.title || task.id || 'Task') + ' \u00b7 ' +
          duration((Number(task.age_seconds) || 0) * 1000) + ' old \u00b7 worker ' + (task.worker_state || 'unknown') }));
    });
    body.appendChild(box);
  }

  function awaitingRow(run) {
    var title = run.goal || run.id || 'Run';
    var confirmButton = el('button', { cls: 'nt-btn nt-primary', type: 'button', text: 'Confirm' });
    confirmButton.addEventListener('click', function () {
      if (!window.confirm('Confirm the run "' + title + '"? The Main accepts it and the run continues.')) return;
      act(confirmButton, function () { return call('run_confirm', { id: run.id }); }, 'Run confirmed');
    });
    var kids = [el('div', { cls: 'nt-run-top' }, [
      el('span', { cls: 'nt-dot nt-' + stateClass('awaiting_main') }),
      el('span', { cls: 'nt-run-title', text: 'Waiting for the Main: ' + title })])];
    list(run.needs_main).slice(0, ATTENTION_LIMIT).forEach(function (item) {
      kids.push(el('div', { cls: 'nt-attention-reason',
        text: (item.task_id ? item.task_id + ': ' : '') + (item.reason || 'Needs the Main') }));
    });
    kids.push(el('div', { cls: 'nt-actions' }, [confirmButton]));
    return el('div', { cls: 'nt-run nt-attention-run' }, kids);
  }

  // ---- Runs ------------------------------------------------------------------------------
  function act(button, work, done) {
    button.disabled = true;
    work().then(function () {
      toast(done, 'ok');
    }).catch(function (err) {
      toast(message(err), 'error', 6000);
    }).then(function () {
      button.disabled = false;
    });
  }

  function reconcileAll(ids) {
    return ids.reduce(function (chain, taskId) {
      return chain.then(function () { return call('task_reconcile', { id: taskId }); });
    }, Promise.resolve());
  }

  function runRow(run) {
    var state = norm(run.state);
    var finished = ['done', 'cancelled', 'canceled', 'completed'].indexOf(state) >= 0;
    var paused = state === 'paused';
    var total = Number(run.tasks_total) || 0;
    var done = Number(run.tasks_done) || 0;
    var fill = el('div', { cls: 'nt-progress-fill' });
    fill.style.width = (total ? Math.round(Math.min(1, done / total) * 100) : 0) + '%';
    var reconcileIds = list(run.reconcile_task_ids).filter(function (id) { return id !== null && id !== ''; });
    var title = run.title || run.id || 'Run';

    var pause = el('button', { cls: 'nt-btn', type: 'button', text: paused ? 'Resume' : 'Pause', disabled: finished });
    pause.addEventListener('click', function () {
      act(pause, function () { return call('run_pause', { id: run.id, paused: !paused }); }, paused ? 'Run resumed' : 'Run paused');
    });
    var cancel = el('button', { cls: 'nt-btn nt-danger', type: 'button', text: 'Cancel', disabled: finished });
    cancel.addEventListener('click', function () {
      if (!window.confirm('Cancel the run "' + title + '"?')) return;
      act(cancel, function () { return call('run_cancel', { id: run.id }); }, 'Run cancelled');
    });
    var canRetry = /(block|fail)/.test(state);
    var retry = el('button', { cls: 'nt-btn', type: 'button', text: 'Retry', disabled: !canRetry || reconcileIds.length > 0,
      title: reconcileIds.length ? 'Reconcile the unconfirmed tasks first' : null });
    retry.addEventListener('click', function () {
      act(retry, function () { return call('run_retry', { id: run.id }); }, 'Retry started');
    });
    var reconcile = el('button', { cls: 'nt-btn', type: 'button', disabled: !reconcileIds.length,
      text: reconcileIds.length ? 'Reconcile (' + reconcileIds.length + ')' : 'Reconcile' });
    reconcile.addEventListener('click', function () {
      act(reconcile, function () { return reconcileAll(reconcileIds); }, 'Reconcile sent for ' + reconcileIds.length + ' task(s)');
    });

    return el('div', { cls: 'nt-run' }, [
      el('div', { cls: 'nt-run-top' }, [
        el('span', { cls: 'nt-dot nt-' + stateClass(run.state) }),
        el('span', { cls: 'nt-run-title', text: title })]),
      el('div', { cls: 'nt-run-meta' }, [
        el('span', { text: [run.state, run.stage].filter(Boolean).join(' \u00b7 ') || 'unknown' }),
        el('span', { cls: 'nt-mono', text: done + ' / ' + total + ' tasks' })]),
      el('div', { cls: 'nt-progress' }, [fill]),
      el('div', { cls: 'nt-actions' }, [pause, cancel, retry, reconcile])]);
  }

  function renderRuns(body) {
    var runs = list((live || {}).runs);
    if (!runs.length) {
      body.appendChild(el('p', { cls: 'nt-empty', text: live ? 'No runs yet.' : 'Waiting for live data from the harness.' }));
      return;
    }
    runs.forEach(function (run) { body.appendChild(runRow(run)); });
  }

  // ---- Harness and Settings --------------------------------------------------------------
  function loadSettings() {
    return call('settings_get').then(function (values) {
      settings = values && typeof values === 'object' ? values : {};
      if (activeTab === 'harness' || activeTab === 'settings') renderBody();
    }).catch(function (err) {
      toast('Settings could not be loaded: ' + message(err), 'error', 6000);
    });
  }

  function renderSettings(body, harnessGroup) {
    if (!settings) {
      body.appendChild(el('p', { cls: 'nt-empty', text: 'Loading settings...' }));
      return;
    }
    var values = {};
    Object.keys(settings).forEach(function (key) { values[key] = settings[key]; });
    if (harnessGroup) {
      Object.keys(HARNESS_DEFAULTS).forEach(function (key) {
        if (!(key in values)) values[key] = HARNESS_DEFAULTS[key];
      });
    }
    var keys = Object.keys(values).filter(function (key) {
      return (HARNESS_KEYS.indexOf(key) >= 0) === harnessGroup;
    }).sort(function (a, b) {
      var ia = HARNESS_KEYS.indexOf(a), ib = HARNESS_KEYS.indexOf(b);
      return ia !== ib ? ia - ib : (a < b ? -1 : a > b ? 1 : 0);
    });
    if (!keys.length) {
      body.appendChild(el('p', { cls: 'nt-empty', text: 'No settings in this group.' }));
      return;
    }
    var inputs = {};
    var form = el('form', { cls: 'nt-form' });
    keys.forEach(function (key) {
      var value = values[key];
      var input;
      if (CHOICES[key]) {
        input = el('select', { cls: 'nt-input', name: key });
        CHOICES[key].forEach(function (option) {
          var opt = el('option', { text: option });
          opt.value = option;
          input.appendChild(opt);
        });
        input.value = CHOICES[key].indexOf(value) >= 0 ? value : CHOICES[key][0];
      } else if (typeof value === 'boolean') {
        input = el('input', { type: 'checkbox', cls: 'nt-check', name: key });
        input.checked = value;
      } else if (typeof value === 'number') {
        input = el('input', { type: 'number', cls: 'nt-input', name: key, step: 'any' });
        input.value = String(value);
      } else {
        input = el('input', { type: 'text', cls: 'nt-input', name: key });
        input.value = value === null || value === undefined ? '' : String(value);
      }
      inputs[key] = input;
      form.appendChild(el('label', { cls: 'nt-field' + (input.type === 'checkbox' ? ' nt-field-check' : '') },
        [el('span', { text: humanize(key) }), input]));
    });
    var save = el('button', { cls: 'nt-btn nt-primary', type: 'submit', text: 'Save' });
    var reload = el('button', { cls: 'nt-btn', type: 'button', text: 'Reload' });
    reload.addEventListener('click', function () { settings = null; renderBody(); loadSettings(); });
    form.appendChild(el('div', { cls: 'nt-actions' }, [save, reload]));
    form.addEventListener('submit', function (ev) {
      ev.preventDefault();
      var values = {};
      keys.forEach(function (key) {
        var old = values[key];
        var input = inputs[key];
        var next;
        if (typeof old === 'boolean') next = input.checked;
        else if (typeof old === 'number') next = Number(input.value);
        else next = input.value;
        if (typeof old === 'number' && isNaN(next)) return;
        if (next !== old) values[key] = next;
      });
      if (!Object.keys(values).length) { toast('Nothing changed', 'info'); return; }
      save.disabled = true;
      call('settings_set', { values: values }).then(function (result) {
        if (result && typeof result === 'object') settings = result;
        toast('Settings saved', 'ok');
        renderBody();
      }).catch(function (err) {
        toast('Saving failed: ' + message(err), 'error', 6000);
        save.disabled = false;
      });
    });
    body.appendChild(form);
  }

  // ---- MCP -------------------------------------------------------------------------------
  function loadMcp() {
    return call('mcp_list').then(function (servers) {
      mcpServers = list(servers);
      if (activeTab === 'mcp') renderBody();
    }).catch(function (err) {
      toast('MCP servers could not be loaded: ' + message(err), 'error', 6000);
    });
  }

  function renderMcp(body) {
    var refresh = el('button', { cls: 'nt-btn', type: 'button', text: 'Refresh' });
    refresh.addEventListener('click', function () {
      act(refresh, function () {
        return call('mcp_refresh').then(function () { return loadMcp(); });
      }, 'MCP servers refreshed');
    });
    body.appendChild(el('div', { cls: 'nt-actions nt-actions-top' }, [refresh]));
    if (!mcpServers) {
      body.appendChild(el('p', { cls: 'nt-empty', text: 'Loading MCP servers...' }));
      return;
    }
    if (!mcpServers.length) {
      body.appendChild(el('p', { cls: 'nt-empty', text: 'No MCP servers configured.' }));
      return;
    }
    mcpServers.forEach(function (server) {
      var status = server.state || server.status || (server.enabled === false ? 'disabled' : server.enabled ? 'enabled' : 'unknown');
      var count = server.tool_count !== undefined ? server.tool_count : server.tools_count;
      body.appendChild(el('div', { cls: 'nt-mcp' }, [
        el('div', { cls: 'nt-run-top' }, [
          el('span', { cls: 'nt-dot nt-' + (server.error ? 'err' : server.enabled === false ? 'idle' : 'run') }),
          el('span', { cls: 'nt-run-title', text: server.name || server.label || server.id || 'MCP server' })]),
        el('div', { cls: 'nt-run-meta' }, [el('span', { text: String(status) }),
          count !== undefined ? el('span', { cls: 'nt-mono', text: count + ' tools' }) : null]),
        server.error ? el('div', { cls: 'nt-error', text: String(server.error) }) : null]));
    });
  }

  // ---- start -----------------------------------------------------------------------------
  document.addEventListener('pointerdown', onPress, true);
  document.addEventListener('mousedown', onPress, true);
  document.addEventListener('click', onClick, true);
  document.addEventListener('keydown', onKey, true);
  new MutationObserver(scheduleScan).observe(document.documentElement, { childList: true, subtree: true });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', scan);
  else scan();
  window.addEventListener('resize', function () { if (activeTab === 'live') renderBody(); });
})();
