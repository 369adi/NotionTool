/* NotionTool desktop overlay part: shell - Studio v3 navigation (docs/DESKTOP_APP.md, overlay part contract).
 * Contract: window.__ntDesk.shell = function (ctx) { ... return { stop, open, current } }.
 *
 * - Tab bar (Chat, Live, Notes, Settings) at the place of Notion's top icon row in the sidebar
 *   (Home, Chat, Meetings, Inbox, Search). Notion's row is only hidden with a class, never removed.
 *   Live DOM: the row is the nearest ancestor of the search button
 *   ([aria-label="Search"] / [aria-label="Suchen"], inside .notion-sidebar) whose text holds at least
 *   two of the row labels; its items are [role="tab"] with aria-label Home / Chat / Meetings / Inbox.
 * - Router: window.__ntShell = { open(tab), current, register(tab, { mount, unmount, badge }), tabs }.
 *     mount(container, ctx)  renders the view into our container; may return a cleanup function.
 *     unmount(container)     called when the user leaves the tab (the container is removed after).
 *     badge(state)           number shown on the tab (Live default: items that need the user).
 *   Chat is Notion's own chat: our view layer closes and, if Notion's Chat tab is not selected, its
 *   hidden Chat tab is clicked (Notion's own navigation). Live / Notes / Settings render into our own
 *   view layer that covers the main Notion frame (right of .notion-sidebar-container); not a modal.
 *   Parts that load before this one can queue: (window.__ntShellQueue = []).push([tab, view]).
 * - Ctrl+1..4 switch tabs. Brand header: connection dot with plain status text on hover.
 * - shell.css makes the brand header compact and hides the composer 'NotionTool' label.
 */
(function () {
  'use strict';
  window.__ntDesk = window.__ntDesk || {};

  var TABS = [
    { id: 'chat', label: 'Chat', key: '1', hint: 'Chat with Notion AI' },
    { id: 'live', label: 'Live', key: '2', hint: 'See what your team is doing now' },
    { id: 'notes', label: 'Notes', key: '3', hint: 'Your Notion pages and notes' },
    { id: 'settings', label: 'Settings', key: '4', hint: 'NotionTool settings' }
  ];
  var TAB_IDS = TABS.map(function (t) { return t.id; });
  var TAB_LABEL = {};
  TABS.forEach(function (t) { TAB_LABEL[t.id] = t.label; });
  // Labels of Notion's top icon row (English and German UI).
  var TOP_ROW_LABELS = ['home', 'startseite', 'chat', 'meetings', 'besprechungen', 'inbox', 'posteingang'];
  var SEARCH_SELECTOR = '[aria-label="Search"],[aria-label="Suchen"]';
  var SIDEBAR_SELECTOR = '.notion-sidebar';
  var SIDEBAR_BOX_SELECTOR = '.notion-sidebar-container';
  var STALE_MS = 20000;
  var CLS_ON = 'nt-shell-on';
  var CLS_HIDE = 'nt-shell-hide';
  var CLS_VIEW_OPEN = 'nt-shell-view-open';
  var CLS_ACTIVE = 'nt-shell-tab-active';
  var CLS_HAS_BADGE = 'nt-shell-has-badge';
  var CLS_BRAND = 'nt-shell-brand';
  var CLS_TIGHT = 'nt-shell-tight';
  var ICONS = {
    chat: '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 4h12a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H9l-4 3v-3H4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z"/></svg>',
    live: '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M2 10h3l2-5 3 10 2-6 1.5 1H18"/></svg>',
    notes: '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M5 2h7l4 4v12H5zM12 2v4h4M8 10h5M8 13h5"/></svg>',
    settings: '<svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="2.6"/><path d="M10 2v2.5M10 15.5V18M2 10h2.5M15.5 10H18M4.3 4.3l1.8 1.8M13.9 13.9l1.8 1.8M4.3 15.7l1.8-1.8M13.9 6.1l1.8-1.8"/></svg>'
  };
  var PLACEHOLDER = {
    live: 'This view shows what your team is working on. It is still loading.',
    notes: 'This view shows your Notion pages and notes. It is still loading.',
    settings: 'This view shows the NotionTool settings. It is still loading.'
  };

  function norm(s) { return String(s == null ? '' : s).replace(/\s+/g, ' ').trim().toLowerCase(); }
  function isOwn(node) { return !!(node && node.getAttribute && node.getAttribute('data-nt-desk-own') === '1'); }
  function warn(msg, err) { try { console.warn('NotionTool shell: ' + msg, err); } catch (e) { /* no console */ } }

  // Items that need the user: the pushed attention list (waiting for the Main, stuck tasks, full
  // context) plus an open context question.
  function liveBadge(state) {
    if (!state || state.connected === false) return 0;
    var n = Array.isArray(state.attention) ? state.attention.length : 0;
    if (state.context_ask) n += 1;
    return n;
  }

  function connInfo(state, lastTs, now) {
    if (!state) return { level: 'wait', text: 'Connecting to NotionTool...' };
    if (state.connected === false || (lastTs && now - lastTs > STALE_MS)) {
      return { level: 'off', text: 'Not connected. NotionTool keeps trying.' };
    }
    return { level: 'on', text: 'Connected. NotionTool is running.' };
  }

  // Ctrl+1..4 (no Shift/Alt/Meta: Notion uses Ctrl+Shift+1..9 for block types).
  function keyTab(ev) {
    if (!ev || !ev.ctrlKey || ev.altKey || ev.shiftKey || ev.metaKey) return null;
    var i = ['1', '2', '3', '4'].indexOf(String(ev.key || ''));
    if (i < 0 && ev.code) i = ['Digit1', 'Digit2', 'Digit3', 'Digit4'].indexOf(String(ev.code));
    return i >= 0 ? TAB_IDS[i] : null;
  }

  function badgeText(n) {
    n = Number(n) || 0;
    if (n <= 0) return '';
    return n > 99 ? '99+' : String(Math.floor(n));
  }

  // Tab bar keyboard: arrows move (wrapping), Home/End jump. Returns the target tab id or null.
  function tabStep(current, key) {
    var n = TAB_IDS.length;
    var i = TAB_IDS.indexOf(current);
    if (i < 0) i = 0;
    key = String(key || '');
    if (key === 'ArrowRight' || key === 'ArrowDown') return TAB_IDS[(i + 1) % n];
    if (key === 'ArrowLeft' || key === 'ArrowUp') return TAB_IDS[(i + n - 1) % n];
    if (key === 'Home') return TAB_IDS[0];
    if (key === 'End') return TAB_IDS[n - 1];
    return null;
  }

  function hasTopLabels(node) {
    var text = norm(node.textContent);
    if (!text || text.length > 120) return false;
    var hits = 0;
    TOP_ROW_LABELS.forEach(function (label) { if (text.indexOf(label) >= 0) hits++; });
    return hits >= 2;
  }

  // Notion's top icon row: the nearest ancestor of the search button that also holds the row labels.
  function findTopRow(root) {
    if (!root || !root.querySelector) return null;
    var node = root.querySelector(SEARCH_SELECTOR);
    while (node && node !== root) {
      if (!isOwn(node) && hasTopLabels(node)) return node;
      node = node.parentElement;
    }
    return null;
  }

  // A [role="tab"] item of Notion's top row by its label ('chat', 'home', ...).
  function nativeTab(row, label) {
    if (!row || !row.querySelectorAll) return null;
    var items = row.querySelectorAll('[role="tab"]');
    for (var i = 0; i < items.length; i++) {
      // Labels can carry an unread count ('Chat 3', 'Chat, 3 unread'): match the leading word.
      var name = norm(items[i].getAttribute('aria-label')) || norm(String(items[i].textContent || '').replace(/\d+/g, ' '));
      if (name === label || name.indexOf(label + ' ') === 0 || name.indexOf(label + ',') === 0) return items[i];
    }
    return null;
  }

  // Pure router: which tab is current, which views are registered and mounted. All DOM work goes
  // through host = { ctx, viewBox(tab), clearView(tab), showView(tab), hideView(), showChat(),
  // placeholder(tab, box), chatBadge(), changed() }.
  function createRouter(host) {
    var views = {};
    var current = 'chat';
    var mounted = null;
    var dead = false;

    function safe(fn, args) {
      try { return fn.apply(null, args); } catch (e) { warn('view failed', e); return undefined; }
    }
    function unmountCurrent() {
      if (!mounted) return;
      var tab = mounted;
      var entry = views[tab];
      mounted = null;
      if (entry && typeof entry.unmount === 'function') safe(entry.unmount, [host.viewBox(tab)]);
      if (entry && typeof entry.cleanup === 'function') {
        var cleanup = entry.cleanup;
        entry.cleanup = null;
        safe(cleanup, []);
      }
      host.clearView(tab);
    }
    function mountTab(tab) {
      var entry = views[tab];
      var box = host.viewBox(tab);
      if (typeof host.pruneViews === 'function') host.pruneViews(tab);
      // Mark first: a view that opens another tab while mounting is unmounted, not orphaned.
      mounted = tab;
      if (entry && typeof entry.mount === 'function') {
        var cleanup = safe(entry.mount, [box, host.ctx]);
        if (typeof cleanup === 'function') {
          if (mounted === tab && views[tab] === entry) entry.cleanup = cleanup;
          else safe(cleanup, []);
        }
      } else {
        host.placeholder(tab, box);
      }
    }
    function open(tab) {
      tab = norm(tab);
      if (dead || TAB_IDS.indexOf(tab) < 0) return false;
      if (tab !== 'chat' && tab === current && mounted === tab) return true;
      unmountCurrent();
      current = tab;
      if (tab === 'chat') {
        host.hideView();
        host.showChat();
      } else {
        host.showView(tab);
        mountTab(tab);
      }
      host.changed();
      return true;
    }
    function register(tab, view) {
      tab = norm(tab);
      if (dead || !view || tab === 'chat' || TAB_IDS.indexOf(tab) < 0) return function () {};
      if (mounted === tab) unmountCurrent();
      var entry = { view: view, mount: view.mount, unmount: view.unmount, badge: view.badge, cleanup: null };
      views[tab] = entry;
      if (current === tab) mountTab(tab);
      host.changed();
      return function unregister() {
        if (dead || views[tab] !== entry) return;
        if (mounted === tab) unmountCurrent();
        delete views[tab];
        if (current === tab) mountTab(tab);
        host.changed();
      };
    }
    function badge(tab, state) {
      var entry = views[tab];
      if (entry && typeof entry.badge === 'function') {
        var n = safe(entry.badge, [state]);
        if (typeof n === 'number' || typeof n === 'string') return Number(n) || 0;
      }
      if (tab === 'live') return liveBadge(state);
      if (tab === 'chat' && typeof host.chatBadge === 'function') return host.chatBadge();
      return 0;
    }
    function registered() {
      var out = {};
      Object.keys(views).forEach(function (tab) { out[tab] = views[tab].view; });
      return out;
    }
    function destroy() {
      unmountCurrent();
      dead = true;
      views = {};
    }
    return {
      open: open, register: register, badge: badge, destroy: destroy, views: registered,
      get current() { return current; },
      get mounted() { return mounted; }
    };
  }

  // ---- Sidebar 'Coworkers' section toggle ------------------------------------------------------
  // The section and its header button are built by the overlay part (data-nt-desk="coworkers" /
  // "coworkers-head"). One delegated window listener in the capture phase handles the toggle: it runs
  // before Notion's sidebar handlers and before the header's own listener (which it stops, so there is
  // exactly one toggle), and it does not depend on the header node, so it keeps working when Notion
  // re-renders the sidebar or the overlay rebuilds the header. Only our own nodes are touched.
  var COWORKERS_KEY = 'ntDesk.coworkersCollapsed';
  // keydown is not listed: the shell's one window keydown listener (onKey) passes it to handle() first.
  var COWORKER_EVENTS = ['pointerdown', 'mousedown', 'click', 'keyup'];

  function createCoworkerToggle(win, doc) {
    var memo = null;
    var keyAt = 0;
    function read() {
      try {
        var v = win.localStorage.getItem(COWORKERS_KEY);
        if (v === '1' || v === '0') memo = v === '1';
      } catch (e) { /* storage blocked: memory only */ }
      return memo === true;
    }
    function write(value) {
      memo = !!value;
      try { win.localStorage.setItem(COWORKERS_KEY, value ? '1' : '0'); } catch (e) { /* not stored */ }
    }
    function headOf(target) {
      var node = target && target.nodeType === 3 ? target.parentNode : target;
      var head = node && node.closest ? node.closest('[data-nt-desk="coworkers-head"]') : null;
      return head && isOwn(head) ? head : null;
    }
    function sync(section, collapsed) {
      if (!section || !isOwn(section)) return;
      if (section.classList.contains('nt-desk-collapsed') !== collapsed) section.classList.toggle('nt-desk-collapsed', collapsed);
      var head = section.querySelector('[data-nt-desk="coworkers-head"]');
      if (!head) return;
      var expanded = String(!collapsed);
      if (head.getAttribute('aria-expanded') !== expanded) head.setAttribute('aria-expanded', expanded);
      var title = collapsed ? 'Show coworkers' : 'Hide coworkers';
      if (head.getAttribute('title') !== title) head.setAttribute('title', title);
    }
    function syncAll() {
      var collapsed = read();
      var list = doc.querySelectorAll('[data-nt-desk="coworkers"]');
      for (var i = 0; i < list.length; i++) sync(list[i], collapsed);
      return collapsed;
    }
    function toggle(head) {
      var next = !read();
      write(next);
      sync(head.closest('[data-nt-desk="coworkers"]'), next);
      return next;
    }
    function block(ev) {
      if (ev.preventDefault) ev.preventDefault();
      if (ev.stopPropagation) ev.stopPropagation();
      if (ev.stopImmediatePropagation) ev.stopImmediatePropagation();
    }
    function handle(ev) {
      var head = headOf(ev && ev.target);
      if (!head || ev.ntDeskCoworkers) return false;
      ev.ntDeskCoworkers = true;
      var type = ev.type;
      if (type === 'pointerdown' || type === 'mousedown') {
        // Keep Notion's sidebar (drag, focus moves) out of it; the button still gets focus.
        if (ev.stopPropagation) ev.stopPropagation();
        return true;
      }
      if (type === 'keydown' || type === 'keyup') {
        if (ev.key !== 'Enter' && ev.key !== ' ' && ev.key !== 'Spacebar') return false;
        block(ev);
        if (type === 'keydown' && !ev.repeat) { keyAt = Date.now(); toggle(head); }
        return true;
      }
      if (type !== 'click') return false;
      block(ev);
      // A click the browser makes from Enter/Space (detail 0) right after our key toggle is no second toggle.
      if (ev.detail === 0 && Date.now() - keyAt < 500) return true;
      toggle(head);
      return true;
    }
    function start() { COWORKER_EVENTS.forEach(function (t) { win.addEventListener(t, handle, true); }); }
    function stop() { COWORKER_EVENTS.forEach(function (t) { win.removeEventListener(t, handle, true); }); }
    return { handle: handle, read: read, write: write, sync: sync, syncAll: syncAll, toggle: toggle, start: start, stop: stop };
  }

  window.__ntDesk.shellUtil = {
    TABS: TABS, TAB_IDS: TAB_IDS, liveBadge: liveBadge, connInfo: connInfo, keyTab: keyTab,
    badgeText: badgeText, tabStep: tabStep, findTopRow: findTopRow, nativeTab: nativeTab, createRouter: createRouter,
    createCoworkerToggle: createCoworkerToggle, COWORKERS_KEY: COWORKERS_KEY
  };

  window.__ntDesk.shell = function (ctx) {
    ctx = ctx || {};
    // Mounted twice without stop (or an older build still around): stop it, keep its registrations.
    var prev = window.__ntShell;
    var carried = {};
    var prevTab = 'chat';
    if (prev && typeof prev._stop === 'function') {
      try { prevTab = prev.current || 'chat'; } catch (e) { prevTab = 'chat'; }
      try { carried = typeof prev._views === 'function' ? prev._views() : {}; } catch (e) { carried = {}; }
      try { prev._stop(); } catch (e) { warn('old shell stop failed', e); }
    }
    var doc = document;
    var html = doc.documentElement;
    var stopped = false;
    var lastTs = 0;
    var scheduled = null;
    var observer = null;
    var offState = null;
    var tick = null;
    var bar = null;
    var layer = null;
    var coworkers = createCoworkerToggle(window, doc);

    function mk(tag, attrs, text) {
      var node = doc.createElement(tag);
      Object.keys(attrs || {}).forEach(function (key) {
        if (key === 'cls') node.className = attrs[key];
        else node.setAttribute(key, attrs[key]);
      });
      if (text != null) node.textContent = text;
      return node;
    }
    function own(node, mark) {
      node.setAttribute('data-nt-desk', mark);
      node.setAttribute('data-nt-desk-own', '1');
      return node;
    }
    function dropAll(selector, keep) {
      var nodes = doc.querySelectorAll(selector);
      for (var i = 0; i < nodes.length; i++) {
        if (nodes[i] !== keep && isOwn(nodes[i]) && nodes[i].parentNode) nodes[i].parentNode.removeChild(nodes[i]);
      }
    }
    function sidebar() { return doc.querySelector(SIDEBAR_SELECTOR); }
    function state() { return typeof ctx.getState === 'function' ? ctx.getState() : null; }

    // ---- view layer (covers the main Notion frame, right of the sidebar) ----------------------
    function ensureLayer() {
      if (layer && layer.parentNode) return layer;
      dropAll('[data-nt-desk="shell-layer"]');
      layer = own(mk('div', { cls: 'nt-shell-layer', role: 'region', 'aria-label': 'NotionTool view' }), 'shell-layer');
      (doc.body || html).appendChild(layer);
      return layer;
    }
    function rightOf(node) {
      if (!node || typeof node.getBoundingClientRect !== 'function') return 0;
      return Math.max(0, Math.round(node.getBoundingClientRect().right || 0));
    }
    function placeLayer() {
      if (!layer) return;
      // No measurable sidebar: keep the last (or CSS default) left. A left: 0 layer would cover
      // the whole sidebar, its tab bar and its footer.
      var left = rightOf(doc.querySelector(SIDEBAR_BOX_SELECTOR)) || rightOf(sidebar());
      if (!left) return;
      var value = left + 'px';
      if (layer.style.left !== value) layer.style.left = value;
    }
    function viewOf(tab) {
      if (!layer) return null;
      var kids = layer.children || [];
      for (var i = 0; i < kids.length; i++) if (kids[i].getAttribute('data-nt-desk-view') === tab) return kids[i];
      return null;
    }

    // Only the current tab's view may live in the layer (fast switching, re-entrant mounts).
    function pruneViews(keep) {
      dropAll('[data-nt-desk="shell-view"]', viewOf(keep));
    }

    var host = {
      ctx: ctx,
      pruneViews: pruneViews,
      viewBox: function (tab) {
        var box = viewOf(tab);
        if (!box) {
          box = own(mk('div', { cls: 'nt-shell-view', 'data-nt-desk-view': tab }), 'shell-view');
          ensureLayer().appendChild(box);
        }
        return box;
      },
      clearView: function (tab) {
        var box = viewOf(tab);
        if (box && box.parentNode) box.parentNode.removeChild(box);
      },
      showView: function (tab) {
        pruneViews(tab);
        ensureLayer().setAttribute('data-nt-shell-tab', tab);
        placeLayer();
        html.classList.add(CLS_VIEW_OPEN);
      },
      hideView: function () {
        html.classList.remove(CLS_VIEW_OPEN);
        if (layer) layer.removeAttribute('data-nt-shell-tab');
      },
      showChat: function () {
        // Notion's own navigation: its (hidden) Chat tab, only if Notion does not show chat already.
        var tab = nativeTab(findTopRow(sidebar()), 'chat');
        if (tab && tab.getAttribute('aria-selected') !== 'true' && typeof tab.click === 'function') tab.click();
      },
      placeholder: function (tab, box) {
        box.appendChild(mk('div', { cls: 'nt-shell-empty-title' }, TAB_LABEL[tab] || ''));
        box.appendChild(mk('div', { cls: 'nt-shell-empty-text' }, PLACEHOLDER[tab] || ''));
        var part = window.__ntDeskMounted && window.__ntDeskMounted[tab];
        if (part && typeof part.open === 'function') {
          var btn = mk('button', { type: 'button', cls: 'nt-shell-btn' }, 'Open ' + (TAB_LABEL[tab] || tab));
          btn.addEventListener('click', function () { try { part.open(); } catch (e) { warn('open failed', e); } });
          box.appendChild(btn);
        }
      },
      chatBadge: function () {
        var tab = nativeTab(findTopRow(sidebar()), 'chat');
        var m = tab ? norm(tab.textContent).match(/\d+/) : null;
        return m ? Number(m[0]) : 0;
      },
      // Highlight follows the tab at once; the rest (badges, placement) on the next apply.
      changed: function () { renderBar(); schedule(); }
    };
    var router = createRouter(host);

    // ---- tab bar -----------------------------------------------------------------------------
    function buildBar() {
      var node = own(mk('div', { cls: 'nt-shell-tabs', role: 'tablist', 'aria-label': 'NotionTool sections' }), 'tabbar');
      TABS.forEach(function (t) {
        var btn = own(mk('button', { type: 'button', cls: 'nt-shell-tab', role: 'tab', 'data-nt-shell-tab': t.id,
          title: t.hint + ' (Ctrl+' + t.key + ')', 'aria-selected': 'false', tabindex: t.id === 'chat' ? '0' : '-1' }), 'tab');
        var icon = mk('span', { cls: 'nt-shell-tab-icon', 'aria-hidden': 'true' });
        icon.innerHTML = ICONS[t.id];
        btn.appendChild(icon);
        btn.appendChild(mk('span', { cls: 'nt-shell-tab-label' }, t.label));
        btn.appendChild(mk('span', { cls: 'nt-shell-tab-badge', 'aria-hidden': 'true' }));
        btn.addEventListener('click', function (ev) {
          if (ev && ev.preventDefault) ev.preventDefault();
          if (ev && ev.stopPropagation) ev.stopPropagation();
          router.open(t.id);
        });
        node.appendChild(btn);
      });
      function focusTab(id) {
        var kids = node.children || [];
        for (var i = 0; i < kids.length; i++) {
          if (kids[i].getAttribute('data-nt-shell-tab') === id && typeof kids[i].focus === 'function') { kids[i].focus(); return; }
        }
      }
      // Tablist keyboard: arrows/Home/End move and open, Enter/Space open the focused tab.
      node.addEventListener('keydown', function (ev) {
        if (!ev || ev.ctrlKey || ev.altKey || ev.metaKey) return;
        var key = String(ev.key || '');
        var el = ev.target && typeof ev.target.closest === 'function' ? ev.target.closest('[data-nt-shell-tab]') : null;
        var from = el ? el.getAttribute('data-nt-shell-tab') : router.current;
        var to = key === 'Enter' || key === ' ' || key === 'Spacebar' ? from : tabStep(from, key);
        if (!to) return;
        if (ev.preventDefault) ev.preventDefault();
        if (ev.stopPropagation) ev.stopPropagation();
        router.open(to);
        focusTab(to);
      });
      return node;
    }

    // Notion's header wrapper around the hidden top row keeps its padding/height: the empty gap
    // between the brand header and the tab bar. Collapse it with our own class (node is kept).
    function tightenWrap(sb, row) {
      var parent = row ? row.parentNode : null;
      var wrap = parent && parent !== sb && !isOwn(parent) && (parent.children || []).length <= 3 ? parent : null;
      var marked = doc.querySelectorAll('[data-nt-shell="wrap"]');
      for (var i = 0; i < marked.length; i++) {
        if (marked[i] === wrap) continue;
        marked[i].classList.remove(CLS_TIGHT);
        marked[i].removeAttribute('data-nt-shell');
      }
      if (!wrap || wrap.getAttribute('data-nt-shell')) return;
      wrap.classList.add(CLS_TIGHT);
      wrap.setAttribute('data-nt-shell', 'wrap');
    }

    function placeBar(sb) {
      var row = findTopRow(sb);
      var marked = doc.querySelectorAll('[data-nt-shell="toprow"]');
      for (var i = 0; i < marked.length; i++) {
        if (marked[i] === row) continue;
        marked[i].classList.remove(CLS_HIDE);
        marked[i].removeAttribute('data-nt-shell');
      }
      if (row) {
        if (!row.classList.contains(CLS_HIDE)) row.classList.add(CLS_HIDE);
        if (row.getAttribute('data-nt-shell') !== 'toprow') row.setAttribute('data-nt-shell', 'toprow');
      }
      // Never adopt a bar of an older mount: its click handlers talk to a dead router.
      if (!bar) bar = buildBar();
      dropAll('[data-nt-desk="tabbar"]', bar);
      tightenWrap(sb, row);
      if (row) {
        if (bar.parentNode !== row.parentNode || bar.nextSibling !== row) row.parentNode.insertBefore(bar, row);
      } else {
        // No Notion row found: right below our brand header.
        var brand = sb.querySelector('[data-nt-desk="brand"]');
        if (brand && brand.parentNode === sb) {
          if (bar.parentNode !== sb || bar.previousSibling !== brand) sb.insertBefore(bar, brand.nextSibling);
        } else if (bar.parentNode !== sb) {
          sb.insertBefore(bar, sb.firstChild);
        }
      }
    }

    function renderBar() {
      if (!bar) return;
      var s = state();
      var kids = bar.children || [];
      for (var i = 0; i < kids.length; i++) {
        var btn = kids[i];
        var id = btn.getAttribute('data-nt-shell-tab');
        var active = id === router.current;
        if (btn.classList.contains(CLS_ACTIVE) !== active) {
          if (active) btn.classList.add(CLS_ACTIVE); else btn.classList.remove(CLS_ACTIVE);
        }
        var sel = active ? 'true' : 'false';
        if (btn.getAttribute('aria-selected') !== sel) btn.setAttribute('aria-selected', sel);
        var ti = active ? '0' : '-1';
        if (btn.getAttribute('tabindex') !== ti) btn.setAttribute('tabindex', ti);
        var text = badgeText(router.badge(id, s));
        var badge = btn.lastChild;
        if (badge && badge.textContent !== text) badge.textContent = text;
        if (btn.classList.contains(CLS_HAS_BADGE) !== !!text) {
          if (text) btn.classList.add(CLS_HAS_BADGE); else btn.classList.remove(CLS_HAS_BADGE);
        }
      }
    }

    // ---- brand header: connection dot -------------------------------------------------------
    function applyConn(sb) {
      var brand = sb.querySelector('[data-nt-desk="brand"]');
      if (!brand) return;
      if (!brand.classList.contains(CLS_BRAND)) brand.classList.add(CLS_BRAND);
      var dot = null;
      var kids = brand.children || [];
      for (var i = 0; i < kids.length; i++) if (kids[i].getAttribute('data-nt-desk') === 'conn') { dot = kids[i]; break; }
      if (!dot) {
        dot = own(mk('span', { cls: 'nt-shell-conn', tabindex: '0' }), 'conn');
        dot.appendChild(mk('span', { cls: 'nt-shell-conn-text', role: 'status' }));
        var name = brand.querySelector('.nt-desk-brand-name');
        brand.insertBefore(dot, name && name.parentNode === brand ? name.nextSibling : null);
      }
      var info = connInfo(state(), lastTs, Date.now());
      if (dot.getAttribute('data-level') !== info.level) dot.setAttribute('data-level', info.level);
      if (dot.getAttribute('aria-label') !== info.text) dot.setAttribute('aria-label', info.text);
      if (dot.firstChild && dot.firstChild.textContent !== info.text) dot.firstChild.textContent = info.text;
    }

    function apply() {
      scheduled = null;
      if (stopped) return;
      try {
        if (!html.classList.contains(CLS_ON)) html.classList.add(CLS_ON);
        var sb = sidebar();
        if (sb) {
          placeBar(sb);
          applyConn(sb);
        }
        renderBar();
        if (router.current !== 'chat') placeLayer();
        coworkers.syncAll();
      } catch (e) {
        warn('apply failed', e);
      }
    }
    function schedule() {
      if (stopped || scheduled) return;
      scheduled = setTimeout(apply, 120);
    }

    function onKey(ev) {
      if (coworkers.handle(ev)) return;
      var tab = keyTab(ev);
      if (!tab) return;
      if (ev.preventDefault) ev.preventDefault();
      if (ev.stopPropagation) ev.stopPropagation();
      router.open(tab);
    }
    function onResize() { if (router.current !== 'chat') placeLayer(); }

    function stop() {
      if (stopped) return;
      stopped = true;
      if (scheduled) clearTimeout(scheduled);
      scheduled = null;
      if (tick) clearInterval(tick);
      if (observer) { try { observer.disconnect(); } catch (e) { /* gone */ } }
      if (typeof offState === 'function') offState();
      window.removeEventListener('keydown', onKey, true);
      window.removeEventListener('resize', onResize);
      coworkers.stop();
      router.destroy();
      dropAll('[data-nt-desk="tabbar"]');
      dropAll('[data-nt-desk="shell-layer"]');
      dropAll('[data-nt-desk="conn"]');
      var marked = doc.querySelectorAll('[data-nt-shell="toprow"]');
      for (var i = 0; i < marked.length; i++) {
        marked[i].classList.remove(CLS_HIDE);
        marked[i].removeAttribute('data-nt-shell');
      }
      var wraps = doc.querySelectorAll('[data-nt-shell="wrap"]');
      for (var k = 0; k < wraps.length; k++) {
        wraps[k].classList.remove(CLS_TIGHT);
        wraps[k].removeAttribute('data-nt-shell');
      }
      var brands = doc.querySelectorAll('[data-nt-desk="brand"]');
      for (var j = 0; j < brands.length; j++) brands[j].classList.remove(CLS_BRAND);
      html.classList.remove(CLS_ON);
      html.classList.remove(CLS_VIEW_OPEN);
      if (window.__ntShell === api) delete window.__ntShell;
      bar = null;
      layer = null;
    }

    var api = {
      open: function (tab) { return router.open(tab); },
      register: function (tab, view) { return router.register(tab, view); },
      tabs: TAB_IDS.slice(),
      refresh: function () { schedule(); },
      _stop: stop,
      _views: function () { return router.views(); }
    };
    Object.defineProperty(api, 'current', { enumerable: true, get: function () { return router.current; } });
    window.__ntShell = api;

    if (state()) lastTs = Date.now();
    if (typeof ctx.onState === 'function') offState = ctx.onState(function () { lastTs = Date.now(); schedule(); });
    window.addEventListener('keydown', onKey, true);
    window.addEventListener('resize', onResize);
    coworkers.start();
    tick = setInterval(schedule, 5000);
    if (typeof MutationObserver === 'function' && doc.body) {
      observer = new MutationObserver((window.__ntIdle || (window.__ntIdle = function (fn) { var w = window; if (!w.__ntScrollHook) { w.__ntScrollHook = true; var mark = function () { w.__ntLastScroll = Date.now(); }; w.addEventListener('scroll', mark, { capture: true, passive: true }); w.addEventListener('wheel', mark, { capture: true, passive: true }); } var t = 0; return function idleRun() { var wait = (w.__ntLastScroll || 0) + 400 - Date.now(); if (wait > 0) { if (!t) t = setTimeout(function () { t = 0; idleRun(); }, wait + 20); return; } fn.apply(this, arguments); }; }))(schedule));
      observer.observe(doc.body, { childList: true, subtree: true });
    }

    Object.keys(carried).forEach(function (tab) { router.register(tab, carried[tab]); });
    var queue = window.__ntShellQueue;
    if (Array.isArray(queue)) {
      queue.splice(0).forEach(function (item) { if (item && item.length > 1) router.register(item[0], item[1]); });
    }
    // Keep the open view after a re-mount (Chat needs nothing: Notion still shows it).
    if (prevTab !== 'chat') router.open(prevTab);
    apply();

    return {
      stop: stop,
      open: api.open,
      get current() { return router.current; }
    };
  };
})();
