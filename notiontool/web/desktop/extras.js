/* NotionTool desktop overlay part: extras (docs/DESKTOP_APP.md, overlay part contract).
 *
 * window.__ntDesk.extras = function (ctx) { ... return { stop } }.
 * - Floating bar: Live button (location.hash '#nt-live'), attention badge, status dot. It docks
 *   inside the sidebar, just above the footer row ('New chat'), so it never covers that row.
 * - Switch banner: offers a fresh Main chat when state.context_ask is set. A running switch is a
 *   short toast per status change (deduped by code), never a persistent banner.
 * - Disconnected banner: no state push for 20 s. Banners and toasts sit bottom right, above the
 *   composer area, never over the page header.
 * - Kill switch Ctrl+Alt+Shift+N: html.nt-desk-off turns the whole layer off (localStorage 'ntDeskOff').
 * - Toasts: ctx.toast is upgraded to stacked glass cards (kind icon + color bar, slide-in,
 *   auto-dismiss after 6 s, close button). Known status codes are shown as friendly English text.
 *   The same toast (same code, or same kind + text) is not added again while it is visible.
 *   stop() restores the original ctx.toast.
 * - Command palette Ctrl+K: fuzzy search over desk commands, arrow keys + Enter, Esc closes.
 *   It only calls harness actions listed in PALETTE_ACTIONS (all of them exist in DESK_ACTIONS).
 * DOM is built with ctx.el or textContent only. Nothing here throws into the page.
 */
(function () {
  'use strict';
  window.__ntDesk = window.__ntDesk || {};

  var LIVE_HASH = '#nt-live';
  var OFF_CLASS = 'nt-desk-off';
  var OFF_KEY = 'ntDeskOff';
  var LATER_KEY = 'ntDeskSwitchLaterUntil';
  var LATER_MS = 30 * 60 * 1000;
  var FRESH_MS = 15000;          // status dot green
  var DISCONNECTED_MS = 20000;   // disconnected banner
  var TICK_MS = 1000;
  var ACTIVE_SWITCH = ['brief_requested', 'brief_ready', 'prompt_sent'];
  var TOAST_MS = 6000;           // every toast auto-dismisses after 6 s
  var TOAST_ERROR_MS = 6000;
  var TOAST_OUT_MS = 200;
  var TOAST_MAX = 5;
  var BAR_GAP_PX = 8;            // Live bar spacing above the sidebar footer row
  var FOOTER_LABELS = ['new chat', 'neuer chat'];
  // Harness actions the palette may call. Every one exists in desktop_shell.DESK_ACTIONS.
  var PALETTE_ACTIONS = ['state', 'run_pause', 'mcp_refresh', 'new_welcome_chat', 'main_switch_start'];

  // Status codes -> friendly English text (toasts and the switch banner).
  var STATUS_TEXT = {
    brief_requested: 'Preparing handoff brief',
    brief_ready: 'Handoff brief ready',
    prompt_sent: 'Handing over to the new Main chat',
    done: 'Switch complete',
    failed: 'Switch failed',
    cancelled: 'Switch cancelled',
    awaiting_main: 'Waiting for the Main chat',
    remote_unknown: 'Remote state unknown',
    stale_context_tasks: 'Tasks with stale context',
    context_ask: 'Main chat context is getting long',
    main_switch_start: 'Main chat switch',
    new_welcome_chat: 'New Welcome chat',
    run_pause: 'Pause run',
    mcp_refresh: 'Refresh MCP tools'
  };

  var KIND_ICON = { info: 'i', success: '\u2713', warning: '!', error: '\u00d7' };

  function message(err) {
    return (err && err.message) || String(err || 'unknown error');
  }

  function statusText(code) {
    var key = String(code == null ? '' : code);
    if (Object.prototype.hasOwnProperty.call(STATUS_TEXT, key)) return STATUS_TEXT[key];
    return key.replace(/_/g, ' ');
  }

  // Replace known snake_case codes inside a message, e.g. 'Switching Main chat: brief_requested'.
  function friendly(text) {
    return String(text == null ? '' : text).replace(/\b[a-z]+(?:_[a-z]+)+\b/g, function (code) {
      return Object.prototype.hasOwnProperty.call(STATUS_TEXT, code) ? STATUS_TEXT[code] : code;
    });
  }

  function toastKind(kind) {
    var k = String(kind || 'info').toLowerCase();
    if (k === 'ok' || k === 'success' || k === 'done') return 'success';
    if (k === 'error' || k === 'danger' || k === 'fail') return 'error';
    if (k === 'warn' || k === 'warning') return 'warning';
    return 'info';
  }

  // Fuzzy score: substring match first, then an in-order subsequence with word-start bonuses.
  function fuzzyScore(query, text) {
    var q = String(query || '').toLowerCase().replace(/\s+/g, ' ').trim();
    var t = String(text || '').toLowerCase();
    if (!q) return 1;
    var at = t.indexOf(q);
    if (at >= 0) return 1000 - at + (at === 0 || t.charAt(at - 1) === ' ' ? 200 : 0);
    var score = 0, ti = 0, run = 0;
    for (var qi = 0; qi < q.length; qi++) {
      var ch = q.charAt(qi);
      if (ch === ' ') { run = 0; continue; }
      var found = t.indexOf(ch, ti);
      if (found < 0) return 0;
      run = found === ti ? run + 1 : 1;
      score += 1 + run * 2 + (found === 0 || t.charAt(found - 1) === ' ' ? 8 : 0);
      ti = found + 1;
    }
    return score;
  }

  function storageGet(kind, key) {
    try { return window[kind].getItem(key); } catch (e) { return null; }
  }

  function storageSet(kind, key, value) {
    try {
      if (value === null) window[kind].removeItem(key);
      else window[kind].setItem(key, String(value));
    } catch (e) { /* storage blocked: keep the in-memory value */ }
  }

  function formatTokens(tokens) {
    var n = Number(tokens);
    if (!isFinite(n) || n < 0) n = 0;
    return (Math.round(n / 100000) / 10).toFixed(1) + 'M';
  }

  function activeSwitch(state) {
    var list = state && Array.isArray(state.switches) ? state.switches : [];
    for (var i = 0; i < list.length; i++) {
      if (list[i] && ACTIVE_SWITCH.indexOf(list[i].state) >= 0) return list[i];
    }
    return null;
  }

  // Dedupe key for a toast: an explicit code wins, otherwise kind + friendly text.
  function toastKey(text, kind, code) {
    if (code !== undefined && code !== null && code !== '') return 'code:' + String(code);
    return 'text:' + toastKind(kind) + ':' + friendly(text);
  }

  // Status dedupe: returns true only when the code for a key changed. A null code resets the key,
  // so a status toasts once per change and never again while it stays unchanged.
  function makeStatusDedupe() {
    var last = {};
    return function (key, code) {
      if (code === null || code === undefined || code === '') { delete last[key]; return false; }
      if (last[key] === code) return false;
      last[key] = code;
      return true;
    };
  }

  // Where the Live bar docks: inside the sidebar, gap px above the footer row ('New chat').
  // Rects are plain {left, top, bottom, width}. Returns null when the sidebar is missing or
  // collapsed; the CSS fallback position is used then.
  function dockPosition(sidebar, footer, viewportHeight, gap) {
    if (!sidebar || !(sidebar.width >= 160) || !(viewportHeight > 0)) return null;
    var g = typeof gap === 'number' ? gap : BAR_GAP_PX;
    var anchor = Math.min(sidebar.bottom, viewportHeight);
    if (footer && footer.top >= viewportHeight * 0.5 && footer.top <= anchor) anchor = footer.top;
    return {
      left: Math.round(sidebar.left + 12),
      bottom: Math.max(g, Math.round(viewportHeight - anchor + g)),
      maxWidth: Math.max(0, Math.round(sidebar.width - 24))
    };
  }

  window.__ntDesk.extrasUtil = { fuzzyScore: fuzzyScore, friendly: friendly, statusText: statusText,
    toastKind: toastKind, toastKey: toastKey, makeStatusDedupe: makeStatusDedupe,
    dockPosition: dockPosition };

  window.__ntDesk.extras = function (ctx) {
    ctx = ctx || {};
    var lastPush = 0;          // ms of the last state push seen through ctx.onState
    var state = null;
    var later = Number(storageGet('sessionStorage', LATER_KEY)) || 0;
    var starting = false;
    var stopped = false;
    var unsub = null;
    var tick = 0;
    var ui = {};
    var pal = null;            // command palette nodes, built on first open
    var palItems = [];
    var palIndex = 0;
    var palReturnFocus = null;
    var originalToast = typeof ctx.toast === 'function' ? ctx.toast : null;
    var liveToasts = {};       // toastKey -> visible card (no duplicates while shown)
    var statusChanged = makeStatusDedupe();

    // ctx.el(tag, props, kids) is the shared helper; this fallback keeps the part usable alone.
    function el(tag, props, kids) {
      if (typeof ctx.el === 'function') {
        try { return ctx.el(tag, props || {}, kids || []); } catch (e) { /* fall back below */ }
      }
      var node = document.createElement(tag);
      props = props || {};
      Object.keys(props).forEach(function (key) {
        var value = props[key];
        if (value === null || value === undefined || value === false) return;
        if (key === 'cls') node.className = value;
        else if (key === 'text') node.textContent = value;
        else node.setAttribute(key, value === true ? '' : String(value));
      });
      (kids || []).forEach(function (kid) {
        if (kid === null || kid === undefined) return;
        node.appendChild(typeof kid === 'string' ? document.createTextNode(kid) : kid);
      });
      return node;
    }

    // ---- toasts -------------------------------------------------------------------------
    function toastBox() {
      var box = document.querySelector('.nt-desk-toasts');
      if (!box) {
        box = el('div', { cls: 'nt-desk-toasts' });
        document.body.appendChild(box);
      }
      box.classList.add('nt-desk-toasts-glass');
      box.setAttribute('aria-live', 'polite');
      return box;
    }

    function dismissCard(card) {
      if (!card || card.__ntGone) return;
      card.__ntGone = true;
      if (card.__ntKey && liveToasts[card.__ntKey] === card) delete liveToasts[card.__ntKey];
      if (card.__ntTimer) clearTimeout(card.__ntTimer);
      card.classList.add('nt-desk-tcard-out');
      setTimeout(function () { if (card.parentNode) card.parentNode.removeChild(card); }, TOAST_OUT_MS);
    }

    function fancyToast(text, kind, code) {
      try {
        if (!document.body) return;
        var k = toastKind(kind);
        var key = toastKey(text, kind, code);
        var shown = liveToasts[key];
        if (shown && !shown.__ntGone && shown.isConnected) return;  // same toast still visible
        var box = toastBox();
        var close = el('button', { cls: 'nt-desk-tcard-close', type: 'button', title: 'Dismiss',
          'aria-label': 'Dismiss notification', text: '\u00d7' });
        var card = el('div', { cls: 'nt-desk-toast nt-desk-tcard nt-desk-tcard-' + k,
          role: k === 'error' ? 'alert' : 'status' }, [
          el('span', { cls: 'nt-desk-tcard-bar' }),
          el('span', { cls: 'nt-desk-tcard-icon', 'aria-hidden': 'true', text: KIND_ICON[k] }),
          el('span', { cls: 'nt-desk-tcard-text', text: friendly(text) }),
          close
        ]);
        var ms = k === 'error' ? TOAST_ERROR_MS : TOAST_MS;
        var arm = function () { card.__ntTimer = setTimeout(function () { dismissCard(card); }, ms); };
        close.addEventListener('click', function () { dismissCard(card); });
        card.addEventListener('mouseenter', function () { if (card.__ntTimer) clearTimeout(card.__ntTimer); });
        card.addEventListener('mouseleave', function () { if (!card.__ntGone) arm(); });
        card.__ntKey = key;
        liveToasts[key] = card;
        box.appendChild(card);
        var cards = box.querySelectorAll('.nt-desk-tcard');
        for (var i = 0; i < cards.length - TOAST_MAX; i++) dismissCard(cards[i]);
        arm();
        layoutStack();
      } catch (e) {
        try { if (originalToast) originalToast(friendly(text), kind); } catch (x) { /* ignore */ }
      }
    }

    function toast(text, kind, code) {
      try { if (typeof ctx.toast === 'function') ctx.toast(text, kind || 'info', code); } catch (e) { /* ignore */ }
    }

    function show(node, visible) {
      if (node) node.hidden = !visible;
    }

    function openLive() {
      try { location.hash = LIVE_HASH; } catch (e) { /* ignore */ }
    }

    // ---- layout: Live bar docking and the bottom-right stack ------------------------------
    function rectOf(node) {
      try {
        var r = node.getBoundingClientRect();
        return { left: r.left, top: r.top, bottom: r.bottom, width: r.width };
      } catch (e) { return null; }
    }

    // The sidebar footer row: a row marked by the sidebar part, else the 'New chat' control.
    function sidebarFooter(root) {
      var node = root.querySelector('[data-nt-desk="sidebar-footer"]') || root.querySelector('.nt-desk-newchat');
      if (node) return node;
      var nodes = root.querySelectorAll('[role="button"],button,a');
      for (var i = 0; i < nodes.length; i++) {
        var label = String(nodes[i].textContent || '').trim().toLowerCase();
        if (FOOTER_LABELS.indexOf(label) >= 0) return nodes[i];
      }
      return null;
    }

    // Dock the Live bar just above the sidebar footer row so it never covers it.
    function dockBar() {
      if (!ui.bar) return;
      var root = document.querySelector('[data-nt-desk="sidebar"]') || document.querySelector('.notion-sidebar');
      var footer = root ? sidebarFooter(root) : null;
      var pos = root ? dockPosition(rectOf(root), footer ? rectOf(footer) : null, window.innerHeight || 0,
        BAR_GAP_PX) : null;
      ui.bar.classList.toggle('nt-desk-bar-docked', !!pos);
      ui.bar.style.left = pos ? pos.left + 'px' : '';
      ui.bar.style.bottom = pos ? pos.bottom + 'px' : '';
      ui.bar.style.maxWidth = pos ? pos.maxWidth + 'px' : '';
    }

    // Toasts stack above the visible banners (both bottom right, above the composer area).
    function layoutStack() {
      var box = document.querySelector('.nt-desk-toasts');
      if (!box || !ui.banners) return;
      var h = ui.banners.offsetHeight || 0;
      box.style.setProperty('--nt-desk-banner-h', h ? (h + 8) + 'px' : '0px');
    }

    function build() {
      ui.live = el('button', { cls: 'nt-desk-bar-live', type: 'button', title: 'Open the Live tab (Ctrl+Shift+L)',
        text: 'Live' });
      ui.badge = el('span', { cls: 'nt-desk-badge', role: 'status', text: '0' });
      ui.dot = el('span', { cls: 'nt-desk-dot', title: 'No state from the harness yet' });
      ui.bar = el('div', { cls: 'nt-desk-bar' }, [ui.live, ui.badge, ui.dot]);
      ui.live.addEventListener('click', openLive);
      ui.badge.addEventListener('click', openLive);
      show(ui.badge, false);

      ui.offText = el('span', { cls: 'nt-desk-banner-text',
        text: 'NotionTool lost connection to the harness - retrying' });
      ui.offBanner = el('div', { cls: 'nt-desk-banner nt-desk-banner-off', role: 'alert' }, [ui.offText]);

      ui.switchText = el('span', { cls: 'nt-desk-banner-text', text: '' });
      ui.switchNow = el('button', { cls: 'nt-desk-btn nt-desk-btn-primary', type: 'button', text: 'Switch now' });
      ui.switchLater = el('button', { cls: 'nt-desk-btn', type: 'button', text: 'Later' });
      ui.switchActions = el('span', { cls: 'nt-desk-banner-actions' }, [ui.switchNow, ui.switchLater]);
      ui.switchBanner = el('div', { cls: 'nt-desk-banner nt-desk-banner-switch', role: 'status' },
        [ui.switchText, ui.switchActions]);
      ui.switchNow.addEventListener('click', startSwitch);
      ui.switchLater.addEventListener('click', function () {
        later = Date.now() + LATER_MS;
        storageSet('sessionStorage', LATER_KEY, later);
        render();
      });

      ui.banners = el('div', { cls: 'nt-desk-banners' }, [ui.offBanner, ui.switchBanner]);
      show(ui.offBanner, false);
      show(ui.switchBanner, false);
    }

    function mount() {
      if (stopped || !document.body) return;
      if (!ui.bar) build();
      if (!ui.bar.isConnected) document.body.appendChild(ui.bar);
      if (!ui.banners.isConnected) document.body.appendChild(ui.banners);
      render();
    }

    // Harness calls from this part go through here; only PALETTE_ACTIONS are allowed.
    function call(action, args) {
      return Promise.resolve().then(function () {
        if (PALETTE_ACTIONS.indexOf(action) < 0) throw new Error('Unknown action: ' + action);
        if (typeof ctx.call !== 'function') throw new Error('The harness is not connected');
        return ctx.call(action, args || {});
      });
    }

    function startSwitch() {
      if (starting) return;
      starting = true;
      if (ui.switchNow) ui.switchNow.disabled = true;
      call('main_switch_start', {}).then(function () {
        toast('Switch started', 'ok');
      }, function (err) {
        toast('Could not start the switch: ' + message(err), 'error');
      }).then(function () {
        starting = false;
        if (ui.switchNow) ui.switchNow.disabled = false;
        render();
      });
    }

    function render() {
      if (stopped || !ui.bar) return;
      try {
        var now = Date.now();
        // Any message counts as alive: a state push or the harness heartbeat ({type:'heartbeat'}, every 3 s).
        var beat = typeof window.__ntDeskLastMessage === 'number' ? window.__ntDeskLastMessage : 0;
        var seen = Math.max(lastPush || 0, beat);
        var age = seen ? now - seen : Infinity;
        var count = state && Array.isArray(state.attention) ? state.attention.length : 0;
        ui.badge.textContent = String(count);
        ui.badge.setAttribute('title', count === 1 ? '1 item needs attention' : count + ' items need attention');
        show(ui.badge, count > 0);

        var fresh = age <= FRESH_MS && !(state && state.connected === false);
        ui.dot.classList.toggle('nt-desk-dot-ok', fresh);
        ui.dot.setAttribute('title', fresh ? 'Harness connected' : 'No recent state from the harness');

        show(ui.offBanner, age > DISCONNECTED_MS);

        var running = activeSwitch(state);
        var ask = state && state.context_ask;
        if (running) {
          // One short toast per status change (deduped by code), never a persistent banner.
          if (statusChanged('main_switch', running.state)) {
            toast('Switching Main chat: ' + statusText(running.state), 'info', 'main_switch:' + running.state);
          }
          show(ui.switchBanner, false);
        } else {
          statusChanged('main_switch', null);
          if (ask && now >= later) {
            ui.switchText.textContent = 'This Main chat is getting long (' + formatTokens(ask.tokens) +
              ' tokens). Switch to a fresh Main chat?';
            show(ui.switchActions, true);
            show(ui.switchBanner, true);
          } else {
            show(ui.switchBanner, false);
          }
        }
        layoutStack();
        dockBar();
      } catch (e) { /* never throw into the page */ }
    }

    function onPush(next) {
      lastPush = Date.now();
      if (next && next.type === 'heartbeat') { render(); return; }  // alive signal only, keep the last state
      state = next && typeof next === 'object' ? next : state;
      if (!state && typeof ctx.getState === 'function') {
        try { state = ctx.getState(); } catch (e) { state = null; }
      }
      mount();
    }

    function isOff() {
      var root = document.documentElement;
      return !!(root && root.classList.contains(OFF_CLASS));
    }

    function setOff(off, announce) {
      var root = document.documentElement;
      if (root) root.classList.toggle(OFF_CLASS, off);
      storageSet('localStorage', OFF_KEY, off ? '1' : null);
      if (announce) {
        toast(off ? 'NotionTool layer off - press Ctrl+Alt+Shift+N again to turn it on'
          : 'NotionTool layer on - press Ctrl+Alt+Shift+N again to turn it off', 'info');
      }
    }

    // ---- command palette ----------------------------------------------------------------
    function openSettings() {
      // The settings part listens for its own hotkey Ctrl+Shift+, (it has no shared handle).
      var ev = new KeyboardEvent('keydown', { key: ',', code: 'Comma', ctrlKey: true, shiftKey: true,
        bubbles: true, cancelable: true });
      (document.body || document.documentElement).dispatchEvent(ev);
    }

    function newWelcomeChat() {
      toast('Creating a Welcome chat...', 'info');
      return call('new_welcome_chat', {}).then(function (result) {
        if (result && result.url) location.href = result.url;
        else toast('Welcome chat created', 'ok');
      }, function (err) {
        toast('Could not create a Welcome chat: ' + message(err), 'error');
      });
    }

    function refreshState() {
      return call('state', {}).then(function (s) {
        if (s && typeof window.__ntHarnessPush === 'function') window.__ntHarnessPush(s);
        toast('Harness state refreshed', 'ok');
      }, function (err) { toast('Could not refresh the state: ' + message(err), 'error'); });
    }

    function refreshMcp() {
      return call('mcp_refresh', {}).then(function () {
        toast('MCP tools refreshed', 'ok');
      }, function (err) { toast('Could not refresh MCP tools: ' + message(err), 'error'); });
    }

    function pauseRun(run, paused) {
      return call('run_pause', { id: run.id, paused: paused }).then(function () {
        toast(paused ? 'Run paused' : 'Run resumed', 'ok');
      }, function (err) { toast('Could not update the run: ' + message(err), 'error'); });
    }

    function commands() {
      var off = isOff();
      var list = [
        { title: 'Open Live', hint: 'Runs, coworkers, context and events', keys: 'Ctrl+Shift+L',
          words: 'dashboard status monitor', run: openLive },
        { title: 'Open NotionTool settings', hint: 'Harness and app settings', keys: 'Ctrl+Shift+,',
          words: 'preferences options config', run: openSettings },
        { title: 'New Welcome chat', hint: 'Start a fresh Welcome chat for the Main', keys: '',
          words: 'create chat new', run: newWelcomeChat },
        { title: 'Switch to a fresh Main chat', hint: 'Hand the Main over with a handoff brief', keys: '',
          words: 'switch main rotate handoff context', run: startSwitch },
        { title: off ? 'Turn NotionTool layer on' : 'Emergency stop: turn NotionTool layer off',
          hint: off ? 'Show the NotionTool layer again' : 'Hide everything NotionTool adds to Notion',
          keys: 'Ctrl+Alt+Shift+N', words: 'emergency stop kill switch disable off on',
          danger: !off, run: function () { setOff(!isOff(), true); } },
        { title: 'Refresh harness state', hint: 'Reload state from the harness', keys: '',
          words: 'reload sync update', run: refreshState },
        { title: 'Refresh MCP tools', hint: 'Reload the MCP tool list', keys: '',
          words: 'mcp tools reload', run: refreshMcp }
      ];
      var runs = state && Array.isArray(state.runs) ? state.runs : [];
      runs.forEach(function (run) {
        if (!run || !run.id) return;
        var goal = String(run.goal || run.id).slice(0, 80);
        if (run.state === 'running') {
          list.push({ title: 'Pause run: ' + goal, hint: 'Run ' + String(run.id).slice(-8), keys: '',
            words: 'pause stop run', run: function () { return pauseRun(run, true); } });
        } else if (run.state === 'paused') {
          list.push({ title: 'Resume run: ' + goal, hint: 'Run ' + String(run.id).slice(-8), keys: '',
            words: 'resume continue run', run: function () { return pauseRun(run, false); } });
        }
      });
      // Other parts (studio.js) add entries here; a failing hook never breaks the palette.
      var extra = window.__ntDesk.paletteExtra;
      if (typeof extra === 'function') {
        try { list = list.concat(extra() || []); } catch (e) { /* ignore */ }
      }
      return list;
    }

    function buildPalette() {
      var input = el('input', { cls: 'nt-desk-pal-input', type: 'text', placeholder: 'Type a command...',
        'aria-label': 'Search commands', autocomplete: 'off', spellcheck: 'false' });
      var list = el('div', { cls: 'nt-desk-pal-list', role: 'listbox', id: 'nt-desk-pal-list' });
      var empty = el('div', { cls: 'nt-desk-pal-empty', text: 'No matching commands' });
      var foot = el('div', { cls: 'nt-desk-pal-foot',
        text: 'Up/Down to navigate \u00b7 Enter to run \u00b7 Esc to close' });
      var box = el('div', { cls: 'nt-desk-pal', role: 'dialog', 'aria-modal': 'true',
        'aria-label': 'NotionTool command palette' }, [
        el('div', { cls: 'nt-desk-pal-head' }, [el('span', { cls: 'nt-desk-pal-mark', text: 'NotionTool' }), input]),
        list, empty, foot
      ]);
      var backdrop = el('div', { cls: 'nt-desk-pal-backdrop' }, [box]);
      input.setAttribute('aria-controls', 'nt-desk-pal-list');
      input.addEventListener('input', function () { palIndex = 0; renderPalette(); });
      input.addEventListener('keydown', onPaletteKey);
      backdrop.addEventListener('mousedown', function (ev) { if (ev.target === backdrop) closePalette(); });
      pal = { backdrop: backdrop, box: box, input: input, list: list, empty: empty };
    }

    function renderPalette() {
      if (!pal) return;
      var query = pal.input.value;
      palItems = commands().map(function (cmd, order) {
        var score = Math.max(fuzzyScore(query, cmd.title) * 2, fuzzyScore(query, cmd.title + ' ' + cmd.words));
        return { cmd: cmd, score: score, order: order };
      }).filter(function (item) { return item.score > 0; });
      if (query.trim()) palItems.sort(function (a, b) { return b.score - a.score || a.order - b.order; });
      if (palIndex >= palItems.length) palIndex = palItems.length - 1;
      if (palIndex < 0) palIndex = 0;
      pal.list.textContent = '';
      palItems.forEach(function (item, i) {
        var row = el('div', { cls: 'nt-desk-pal-item' + (item.cmd.danger ? ' nt-desk-pal-danger' : ''),
          role: 'option', id: 'nt-desk-pal-opt-' + i, 'aria-selected': i === palIndex ? 'true' : 'false' }, [
          el('span', { cls: 'nt-desk-pal-main' }, [
            el('span', { cls: 'nt-desk-pal-title', text: item.cmd.title }),
            el('span', { cls: 'nt-desk-pal-hint', text: item.cmd.hint || '' })
          ]),
          item.cmd.keys ? el('span', { cls: 'nt-desk-pal-keys', text: item.cmd.keys }) : null
        ]);
        row.addEventListener('mousemove', function () { if (palIndex !== i) { palIndex = i; markSelected(); } });
        row.addEventListener('mousedown', function (ev) { ev.preventDefault(); });
        row.addEventListener('click', function () { palIndex = i; runSelected(); });
        pal.list.appendChild(row);
      });
      show(pal.empty, palItems.length === 0);
      markSelected();
    }

    function markSelected() {
      if (!pal) return;
      var rows = pal.list.children;
      for (var i = 0; i < rows.length; i++) {
        var on = i === palIndex;
        rows[i].setAttribute('aria-selected', on ? 'true' : 'false');
        rows[i].classList.toggle('nt-desk-pal-active', on);
        if (on && typeof rows[i].scrollIntoView === 'function') rows[i].scrollIntoView({ block: 'nearest' });
      }
      if (rows[palIndex]) pal.input.setAttribute('aria-activedescendant', rows[palIndex].id);
      else pal.input.removeAttribute('aria-activedescendant');
    }

    function paletteOpen() {
      return !!(pal && pal.backdrop.isConnected && !pal.backdrop.hidden);
    }

    function openPalette() {
      if (stopped || !document.body) return;
      if (!pal) buildPalette();
      palReturnFocus = document.activeElement;
      if (!pal.backdrop.isConnected) document.body.appendChild(pal.backdrop);
      pal.backdrop.hidden = false;
      pal.input.value = '';
      palIndex = 0;
      renderPalette();
      try { pal.input.focus(); } catch (e) { /* ignore */ }
    }

    function closePalette() {
      if (!pal) return;
      pal.backdrop.hidden = true;
      var back = palReturnFocus;
      palReturnFocus = null;
      try { if (back && back.isConnected && typeof back.focus === 'function') back.focus(); } catch (e) { /* ignore */ }
    }

    function runSelected() {
      var item = palItems[palIndex];
      closePalette();
      if (!item) return;
      Promise.resolve().then(function () { return item.cmd.run(); }).then(null, function (err) {
        toast('Command failed: ' + message(err), 'error');
      });
    }

    function onPaletteKey(event) {
      var key = event.key;
      var n = palItems.length;
      var handled = true;
      if (key === 'ArrowDown') palIndex = n ? (palIndex + 1) % n : 0;
      else if (key === 'ArrowUp') palIndex = n ? (palIndex - 1 + n) % n : 0;
      else if (key === 'Home') palIndex = 0;
      else if (key === 'End') palIndex = Math.max(0, n - 1);
      else if (key === 'Enter') { event.preventDefault(); event.stopPropagation(); runSelected(); return; }
      else if (key === 'Escape') { event.preventDefault(); event.stopPropagation(); closePalette(); return; }
      else if (key === 'Tab') { /* keep focus in the palette */ }
      else handled = false;
      event.stopPropagation();  // keep typing inside the palette away from Notion shortcuts
      if (handled) { event.preventDefault(); markSelected(); }
    }

    // Ctrl+K inside a text selection in the editor stays Notion's "add link".
    function isLinkContext() {
      try {
        if (paletteOpen()) return false;
        var sel = window.getSelection && window.getSelection();
        if (!sel || sel.isCollapsed) return false;
        var active = document.activeElement;
        return !!(active && (active.isContentEditable || /^(INPUT|TEXTAREA)$/.test(active.tagName)));
      } catch (e) { return false; }
    }

    function onKey(event) {
      try {
        var key = String(event.key).toLowerCase();
        if (event.ctrlKey && event.altKey && event.shiftKey) {
          if (key !== 'n' && event.code !== 'KeyN') return;
          event.preventDefault();
          event.stopPropagation();
          setOff(!isOff(), true);
          return;
        }
        if (event.ctrlKey && !event.altKey && !event.shiftKey && !event.metaKey &&
            (key === 'k' || event.code === 'KeyK')) {
          if (isLinkContext()) return;
          event.preventDefault();
          event.stopPropagation();
          if (paletteOpen()) closePalette(); else openPalette();
          return;
        }
        if (key === 'escape' && paletteOpen()) {
          event.preventDefault();
          event.stopPropagation();
          closePalette();
        }
      } catch (e) { /* ignore */ }
    }

    function onResize() {
      dockBar();
    }

    function onReady() {
      document.removeEventListener('DOMContentLoaded', onReady);
      mount();
    }

    try {
      ctx.toast = fancyToast;  // every part shares ctx, so all toasts get the new cards
      if (storageGet('localStorage', OFF_KEY) === '1') setOff(true, false);
      if (typeof ctx.getState === 'function') {
        try { state = ctx.getState(); } catch (e) { state = null; }
      }
      if (typeof ctx.onState === 'function') {
        try { unsub = ctx.onState(onPush); } catch (e) { unsub = null; }
      }
      window.addEventListener('keydown', onKey, true);
      window.addEventListener('resize', onResize);
      tick = setInterval(render, TICK_MS);  // dot, banner and bar docking depend on time and layout
      if (document.body) mount();
      else document.addEventListener('DOMContentLoaded', onReady);
    } catch (e) { /* never throw into the page */ }

    return {
      stop: function () {
        stopped = true;
        try {
          if (tick) clearInterval(tick);
          tick = 0;
          if (typeof unsub === 'function') unsub();
          unsub = null;
          if (ctx.toast === fancyToast && originalToast) ctx.toast = originalToast;
          window.removeEventListener('keydown', onKey, true);
          window.removeEventListener('resize', onResize);
          document.removeEventListener('DOMContentLoaded', onReady);
          // Only our own nodes are removed; Notion nodes are never touched.
          if (ui.bar && ui.bar.parentNode) ui.bar.parentNode.removeChild(ui.bar);
          if (ui.banners && ui.banners.parentNode) ui.banners.parentNode.removeChild(ui.banners);
          if (pal && pal.backdrop.parentNode) pal.backdrop.parentNode.removeChild(pal.backdrop);
        } catch (e) { /* ignore */ }
      },
      openPalette: openPalette,
      closePalette: closePalette
    };
  };
})();
