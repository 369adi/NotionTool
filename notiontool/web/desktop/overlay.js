/* NotionTool desktop overlay - sidebar part (docs/DESKTOP_APP.md, overlay part contract).
 *
 * window.__ntDesk.sidebar(ctx) -> { stop }
 * ctx = { call(action, args) -> Promise, onState(fn) -> unsub, getState() -> state|null,
 *         el(tag, props, kids), toast(text, kind) }
 * State: state.mains = [{ chat_id, thread_id, title, operator }], state.hidden_threads = [thread ids],
 *        state.app_chat_filter.
 * - Sidebar chat rows are hidden (class nt-desk-hidden) when their thread id is in hidden_threads,
 *   or when it is not a Main and app_chat_filter !== 'hide_coworkers'.
 *   Notion AI chat rows are menu items without a link: their thread id is read (read only) from
 *   the row's React props (threadStore.id, rowThread) and filtered by the same rule (applyRows).
 *   Titles are never used: state.mains[].title is the harness label ('Operator Main'), not the
 *   Notion chat title ('Main'). A spaceId=<uuid> in a link is never taken as a thread id.
 *   No state, or state.connected === false (harness offline): nothing is hidden or re-labelled and
 *   marks from earlier runs are undone (resetMarks).
 * - The sidebar New chat control ('New chat' / 'Neuer Chat') never starts a normal New Chat; it asks
 *   the harness for a Welcome chat (new_welcome_chat) and opens it.
 * - Branding: a 'NotionTool' header with a gradient logo mark at the top of the sidebar, the
 *   'Notion AI' / 'Notion-KI' section heading shown as 'Mains' (original hidden, our label added),
 *   Main rows styled as cards (active row accent bar, 'Operator' pill), 'New agent' tiles and date
 *   group headings hidden unless they belong to a Main row. The 'New agent' tile is found by its
 *   label text on any element type (labelTargets/tileBlock), not only on buttons and links.
 * - Wrappers whose element children are all hidden are collapsed too (collapseEmpty), so hidden
 *   tiles and date headings leave no empty gaps.
 * - Sidebar v2 (per-Main status): Main rows carry a card line (data-nt-desk="card"): status dot
 *   (running = pulsing accent, waiting for the user = amber, idle = grey) from mainStatus(), the newest
 *   open run's goal and a done/total task progress bar. The 'Mains' heading is inserted exactly once
 *   (ensureHeading: reused, never re-added; stray headings of any build are dropped). A collapsible
 *   'Coworkers' section (state in localStorage) lists coworker chats with model chip, current task or
 *   'Idle' and an age badge (amber >= 20 h, red >= 23 h). Its rows hand the click to Notion's own row.
 *   Harness offline: everything unfiltered, only the brand header with an 'offline' chip.
 * Notion nodes are never removed, only marked with data-nt-desk attributes and hidden by class.
 * Only our own inserted nodes (data-nt-desk-own) are ever taken out again (dropOwn).
 * Self-contained: no imports, no network, no timers besides the 250 ms debounce.
 * Colours come from the theme part (--nt-* CSS variables) with fallbacks in overlay.css.
 */
(function () {
  'use strict';
  window.__ntDesk = window.__ntDesk || {};

  var DEBOUNCE_MS = 250;
  var NEW_CHAT_LABELS = ['new chat', 'neuer chat'];
  var NEW_AGENT_LABELS = ['new agent', 'neuer agent'];
  var AI_HEADING_LABELS = ['notion ai', 'notion-ki', 'notion ki'];
  var MONTHS = 'january|february|march|april|may|june|july|august|september|october|november|december|' +
    'januar|februar|m\u00e4rz|mai|juni|juli|oktober|dezember';
  var DATE_GROUP_RE = new RegExp('^(?:today|yesterday|this week|last week|this month|last month|older|' +
    '(?:previous|last|past) \\d+ days|heute|gestern|diese woche|letzte woche|dieser monat|letzter monat|' +
    '\u00e4lter|(?:vorherige|letzte) \\d+ tage|(?:' + MONTHS + ')(?: \\d{4})?)$');
  var CONTROL_SELECTOR = '[role="button"],[role="link"],[role="treeitem"],button,a';
  var SIDEBAR_SELECTOR = 'nav,[role="navigation"],.notion-sidebar,.notion-sidebar-container,aside';
  var SIDEBAR_ROOTS = ['.notion-sidebar', '.notion-sidebar-container', 'nav', '[role="navigation"]', 'aside'];
  var ROW_SELECTOR = '[role="treeitem"],[role="listitem"],li,[role="link"],[role="button"]';
  var THREAD_RE = /([0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12})/ig;
  var MAX_LABEL = 40;
  var CLS_HIDDEN = 'nt-desk-hidden';
  var CLS_NEW_CHAT = 'nt-desk-newchat';
  var CLS_BUSY = 'nt-desk-busy';
  var CLS_SIDEBAR = 'nt-desk-sidebar';
  var CLS_BRAND = 'nt-desk-brand';
  var CLS_BRAND_MARK = 'nt-desk-brand-mark';
  var CLS_BRAND_NAME = 'nt-desk-brand-name';
  var CLS_HEADING = 'nt-desk-heading';
  var CLS_MAIN_ROW = 'nt-desk-main-row';
  var CLS_ACTIVE = 'nt-desk-active';
  var CLS_OPERATOR = 'nt-desk-operator';
  var CLS_PILL = 'nt-desk-pill';
  var CLS_GROUP = 'nt-desk-chat-group';
  var OUR_CLASSES = [CLS_HIDDEN, CLS_NEW_CHAT, CLS_BUSY, CLS_SIDEBAR, CLS_MAIN_ROW, CLS_ACTIVE, CLS_OPERATOR, CLS_GROUP];
  var THREAD_ID_RE = /^[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}$/i;
  var SPACE_PARAM_RE = /[?&#]space_?id=[^&#]*/ig;
  var FIBER_DEPTH = 8;
  var ROW_ITEM_SELECTOR = '[role="menuitem"]';
  var LABEL_DEPTH = 4;
  var LOADING_LABELS = ['loading\u2026', 'loading...', 'loading', 'wird geladen\u2026', 'wird geladen...', 'wird geladen'];
  var HEADING_SKIP_SELECTOR = '[data-nt-desk="chat-row"],[data-nt-desk="clutter"],[role="list"],a[href],[role="menuitem"]';
  var COWORKERS_KEY = 'ntDesk.coworkersCollapsed';
  var AGE_AMBER_H = 20;
  var AGE_RED_H = 23;
  var FINAL_RUN_STATES = ['done', 'cancelled', 'canceled', 'failed', 'completed', 'aborted', 'error', 'archived'];
  var WAITING_RUN_STATES = ['awaiting_main', 'awaiting_user', 'waiting_user', 'needs_user', 'needs_main'];
  var BUSY_WORKER_STATES = ['busy', 'running', 'sending'];

  function norm(text) { return String(text || '').replace(/[\u2018\u2019]/g, "'").replace(/\s+/g, ' ').trim().toLowerCase(); }

  function threadKey(value) { return String(value || '').replace(/-/g, '').toLowerCase(); }

  function message(err) { return (err && err.message) || String(err || 'Unknown error'); }

  function list(value) { return Array.isArray(value) ? value : []; }

  // Last thread id in a link (chat links end with the thread id; dashes are optional). The
  // workspace id ('/ai?spaceId=72fa1b13-...') is not a thread and is dropped first.
  function threadFromHref(href) {
    var found = String(href || '').replace(SPACE_PARAM_RE, '').match(THREAD_RE);
    return found ? threadKey(found[found.length - 1]) : '';
  }

  // Thread id of a link-less chat row, read from Notion's React props (read only, never written).
  // Live shape: fiber.memoizedProps.icon.props.threadStore.id (also .title.props / .threadStore).
  function storeId(props) {
    var store = props && typeof props === 'object' ? props.threadStore : null;
    var id = store && (store.id || (store._pointer && store._pointer.id));
    return typeof id === 'string' && THREAD_ID_RE.test(id) ? threadKey(id) : '';
  }

  function rowThread(row) {
    try {
      var keys = Object.keys(row);
      var fiber = null;
      for (var i = 0; i < keys.length && !fiber; i++) {
        if (keys[i].indexOf('__reactFiber') === 0) fiber = row[keys[i]];
      }
      for (var up = 0; fiber && up < FIBER_DEPTH; up++, fiber = fiber.return) {
        var props = fiber.memoizedProps;
        var id = storeId(props);
        if (!id && props && typeof props === 'object') {
          var names = Object.keys(props);
          for (var j = 0; j < names.length && !id; j++) {
            var value = props[names[j]];
            if (value && typeof value === 'object') id = storeId(value.props) || storeId(value);
          }
        }
        if (id) return id;
      }
    } catch (e) { /* unreadable props: leave the row alone */ }
    return '';
  }

  // Harness offline: no state, or a state that says it is not connected.
  function online(state) { return !!(state && typeof state === 'object' && state.connected !== false); }

  function visibleLabel(node) {
    var text = norm(node.textContent);
    if (text) return text;
    var aria = node.getAttribute && node.getAttribute('aria-label');
    return norm(aria);
  }

  function isNewChatLabel(label) { return NEW_CHAT_LABELS.indexOf(norm(label)) >= 0; }

  function isOwn(node) { return !!(node && node.getAttribute && node.getAttribute('data-nt-desk-own') === '1'); }

  // The only removal in this part: nodes we inserted ourselves. Notion nodes are never removed.
  function dropOwn(node) {
    if (isOwn(node) && node.parentNode) node.parentNode.removeChild(node);
  }

  // ---- Sidebar v2: pure helpers (window.__ntDesk.sidebarUtil, used by the tests) ------------
  // Runs of one Main: by chat_id, else main_worker, else (older state) the operator flag.
  function runsOfMain(state, main) {
    if (!main) return [];
    return list(state && state.runs).filter(function (run) {
      if (!run) return false;
      if (run.chat_id) return String(run.chat_id) === String(main.chat_id || '');
      if (run.main_worker) return String(run.main_worker) === String(main.worker_id || '');
      return !!run.operator === !!main.operator;
    });
  }

  function awaitingIds(state) {
    return list(state && state.awaiting_main).map(function (item) {
      return item && typeof item === 'object' ? String(item.run_id || item.id || '') : String(item || '');
    });
  }

  // 'waiting' (the newest open run needs the user/Main, or the Main asks about its context),
  // 'running' (an open run, or a busy Main worker) or 'idle'. Progress is the run's done/total tasks.
  function mainStatus(state, main) {
    var runs = runsOfMain(state, main);
    var run = null;
    for (var i = runs.length - 1; i >= 0 && !run; i--) {
      if (FINAL_RUN_STATES.indexOf(String(runs[i].state || '').toLowerCase()) < 0) run = runs[i];
    }
    var worker = null;
    list(state && state.workers).forEach(function (w) {
      if (w && main && main.worker_id && w.id === main.worker_id) worker = w;
    });
    var status = 'idle';
    if (run) {
      var needs = Array.isArray(run.needs_main) ? run.needs_main.length > 0 : !!run.needs_main;
      var waiting = needs || WAITING_RUN_STATES.indexOf(String(run.state || '').toLowerCase()) >= 0 ||
        awaitingIds(state).indexOf(String(run.id || '')) >= 0;
      status = waiting ? 'waiting' : 'running';
    }
    var ask = state && state.context_ask;
    if (ask && worker && ask.worker_id === worker.id) status = 'waiting';
    if (status === 'idle' && worker && BUSY_WORKER_STATES.indexOf(String(worker.state || '').toLowerCase()) >= 0) {
      status = 'running';
    }
    var total = run ? Math.max(0, Number(run.tasks_total) || 0) : 0;
    var done = run ? Math.min(total, Math.max(0, Number(run.tasks_done) || 0)) : 0;
    return { status: status, run: run, title: run ? String(run.goal || run.title || 'Run') : '', done: done, total: total };
  }

  function seconds(value) {
    var n = typeof value === 'string' && isNaN(Number(value)) ? Date.parse(value) / 1000 : Number(value);
    if (!isFinite(n) || n <= 0) return 0;
    return n > 1e12 ? n / 1000 : n;
  }

  // Age in hours from created_at (seconds, ms or ISO) to now (state.ts); -1 when unknown.
  function ageHours(created, now) {
    var c = seconds(created);
    var n = seconds(now) || Date.now() / 1000;
    return c > 0 && n >= c ? (n - c) / 3600 : -1;
  }

  function ageClass(hours) {
    if (hours >= AGE_RED_H) return 'red';
    if (hours >= AGE_AMBER_H) return 'amber';
    return hours >= 0 ? 'ok' : '';
  }

  function ageLabel(hours) {
    if (hours < 0) return '';
    return hours < 1 ? Math.max(1, Math.floor(hours * 60)) + 'm' : Math.floor(hours) + 'h';
  }

  // Coworker chats (workers with purpose 'worker'). An age helper of the team run
  // (window.__ntDesk.ageInfo(created, now) -> { label, level }) is used when present.
  function coworkerRows(state) {
    var now = state && state.ts;
    var shared = typeof window.__ntDesk.ageInfo === 'function' ? window.__ntDesk.ageInfo : null;
    return list(state && state.workers).filter(function (w) { return w && w.purpose === 'worker'; }).map(function (w) {
      var hours = ageHours(w.created_at, now);
      var age = { label: ageLabel(hours), level: ageClass(hours) };
      if (shared) {
        try { var info = shared(w.created_at, now); if (info && info.label) age = { label: String(info.label), level: String(info.level || '') }; } catch (e) { /* keep ours */ }
      }
      var task = String(w.task_title || '');
      return { id: String(w.id || ''), label: String(w.label || 'Coworker'), thread: threadKey(w.thread_id),
        url: String(w.url || ''), model: String(w.model || ''), task: task || 'Idle', busy: !!task,
        age: age.label, ageCls: age.level };
    });
  }

  // Exactly one 'Mains' heading, directly after anchor (the hidden Notion heading). One already
  // there is reused; headings anywhere else (also from a replaced build) are dropped.
  function ensureHeading(scope, anchor, make) {
    var all = scope.querySelectorAll('[data-nt-desk="heading"]');
    var keep = null;
    for (var i = 0; i < all.length; i++) {
      if (!keep && anchor && all[i].previousSibling === anchor && !all[i].hasAttribute('data-nt-desk-dead')) keep = all[i];
    }
    for (var j = 0; j < all.length; j++) if (all[j] !== keep) dropOwn(all[j]);
    if (!keep && anchor && anchor.parentNode) {
      keep = make();
      anchor.parentNode.insertBefore(keep, anchor.nextSibling);
    }
    return keep;
  }

  window.__ntDesk.sidebarUtil = { mainStatus: mainStatus, runsOfMain: runsOfMain, ageHours: ageHours,
    ageClass: ageClass, ageLabel: ageLabel, coworkerRows: coworkerRows, ensureHeading: ensureHeading };

  window.__ntDesk.sidebar = function (ctx) {
    var timer = 0;
    var busy = false;
    var stopped = false;
    var observer = null;
    var unsub = null;
    var chatGroups = [];
    var applied = false;

    // ctx.el(tag, props, kids) is the shared helper; this fallback keeps the part usable alone.
    function el(tag, props, kids) {
      var node = null;
      if (typeof ctx.el === 'function') {
        try { node = ctx.el(tag, props || {}, kids || []); } catch (e) { node = null; }
      }
      if (!node) {
        node = document.createElement(tag);
        props = props || {};
        Object.keys(props).forEach(function (key) {
          if (key === 'cls') node.className = props[key];
          else if (key === 'text') node.textContent = props[key];
          else node.setAttribute(key, String(props[key]));
        });
        (kids || []).forEach(function (kid) { if (kid) node.appendChild(kid); });
      }
      node.setAttribute('data-nt-desk-own', '1');
      return node;
    }

    function sidebarRoot() {
      for (var i = 0; i < SIDEBAR_ROOTS.length; i++) {
        var nodes = document.querySelectorAll(SIDEBAR_ROOTS[i]);
        for (var j = 0; j < nodes.length; j++) {
          if (!isOwn(nodes[j]) && nodes[j].querySelector('a[href]')) return nodes[j];
        }
      }
      return null;
    }

    // Text-bearing elements inside the sidebar whose exact trimmed label passes test().
    function labelled(root, test) {
      var hits = [];
      if (!root) return hits;
      var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
      var node;
      while ((node = walker.nextNode())) {
        var text = node.nodeValue;
        if (!text || text.length > MAX_LABEL * 2) continue;
        var parent = node.parentElement;
        if (!parent || isOwn(parent) || parent.closest('[data-nt-desk-own]')) continue;
        var label = norm(parent.textContent);
        if (label.length <= MAX_LABEL && test(label)) hits.push({ node: parent, label: label });
      }
      return hits;
    }

    // Widest ancestor that still shows only this label and holds no link (the whole heading line).
    function headingBlock(node, label, root) {
      var block = node;
      while (block.parentElement && block.parentElement !== root &&
             norm(block.parentElement.textContent) === label && !block.parentElement.querySelector('a[href]')) {
        block = block.parentElement;
      }
      return block;
    }

    // Elements whose whole visible text is exactly one of labels, on any element type. A label
    // split over several nodes ('Neuer' + 'Agent') matches on the nearest ancestor holding all parts.
    function labelTargets(root, labels) {
      var hits = [];
      if (!root) return hits;
      var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
      var node;
      while ((node = walker.nextNode())) {
        if (!node.nodeValue || !node.nodeValue.trim()) continue;
        var cur = node.parentElement;
        for (var depth = 0; cur && cur !== root && depth < LABEL_DEPTH; depth++) {
          if (isOwn(cur) || cur.closest('[data-nt-desk-own]')) break;
          var label = norm(cur.textContent);
          if (label.length > MAX_LABEL) break;
          if (labels.indexOf(label) >= 0) {
            if (hits.indexOf(cur) < 0) hits.push(cur);
            break;
          }
          cur = cur.parentElement;
        }
      }
      return hits;
    }

    // The whole tile around a label: the widest ancestor that still shows only this label,
    // holds no link and is not a chat row.
    function tileBlock(node, root) {
      var label = norm(node.textContent);
      var tile = node;
      while (tile.parentElement && tile.parentElement !== root &&
             norm(tile.parentElement.textContent) === label &&
             !tile.parentElement.querySelector('a[href]') &&
             tile.parentElement.getAttribute('data-nt-desk') !== 'chat-row') {
        tile = tile.parentElement;
      }
      return tile;
    }

    // ---- branding ------------------------------------------------------------------------
    function applyBrand(root) {
      if (!root) return;
      if (!root.classList.contains(CLS_SIDEBAR)) {
        root.classList.add(CLS_SIDEBAR);
        root.setAttribute('data-nt-desk', 'sidebar');
      }
      var brand = root.querySelector('[data-nt-desk="brand"]');
      if (brand && brand.parentNode === root && root.firstChild === brand) return;
      if (brand) dropOwn(brand);
      brand = el('div', { cls: CLS_BRAND, 'data-nt-desk': 'brand', title: 'NotionTool' }, [
        el('span', { cls: CLS_BRAND_MARK, 'aria-hidden': 'true' }),
        el('span', { cls: CLS_BRAND_NAME, text: 'NotionTool' })
      ]);
      root.insertBefore(brand, root.firstChild);
    }

    function applyHeading(root) {
      var anchor = null;
      labelled(root, function (label) { return AI_HEADING_LABELS.indexOf(label) >= 0; }).forEach(function (hit) {
        // Only the section heading: never the 'Notion AI' agent tile (a list link) or a chat row.
        if (anchor || hit.node.closest(HEADING_SKIP_SELECTOR)) return;
        var block = headingBlock(hit.node, hit.label, root);
        if (block.closest(HEADING_SKIP_SELECTOR) || block.querySelector('a[href]')) return;
        anchor = block;
      });
      var stale = root ? root.querySelectorAll('[data-nt-desk="heading-orig"]') : [];
      for (var i = 0; i < stale.length; i++) {
        if (stale[i] === anchor) continue;
        stale[i].classList.remove(CLS_HIDDEN);
        stale[i].removeAttribute('data-nt-desk');
      }
      if (anchor) {
        anchor.setAttribute('data-nt-desk', 'heading-orig');
        anchor.classList.add(CLS_HIDDEN);
      }
      ensureHeading(document, anchor, function () {
        return el('div', { cls: CLS_HEADING, 'data-nt-desk': 'heading', text: 'Mains' });
      });
    }

    // 'New agent' tiles and date group headings ('Today', 'Gestern', ...), never inside a Main row.
    function applyClutter(root) {
      if (!root) return;
      var controls = root.querySelectorAll(CONTROL_SELECTOR);
      for (var i = 0; i < controls.length; i++) {
        var control = controls[i];
        if (isOwn(control) || NEW_AGENT_LABELS.indexOf(visibleLabel(control)) < 0) continue;
        var tile = control.closest(ROW_SELECTOR) || control;
        if (tile.getAttribute('data-nt-desk') === 'chat-row') continue;
        tile.setAttribute('data-nt-desk', 'clutter');
        tile.classList.add(CLS_HIDDEN);
      }
      // The round 'New agent' tile is a plain list item (no button/link role): match its label text.
      labelTargets(root, NEW_AGENT_LABELS).forEach(function (hit) {
        if (hit.closest('[data-nt-desk="chat-row"]')) return;
        var box = tileBlock(hit, root);
        if (box.getAttribute('data-nt-desk') === 'chat-row') return;
        box.setAttribute('data-nt-desk', 'clutter');
        box.classList.add(CLS_HIDDEN);
      });
      // Agent tiles next to 'New agent' (the 'Notion AI' tile, href '/ai?spaceId=...') are links
      // without a thread id: not chats, so they do not belong under Mains.
      var tiles = root.querySelectorAll('[role="list"] a[href]');
      for (var t = 0; t < tiles.length; t++) {
        if (isOwn(tiles[t]) || threadFromHref(tiles[t].getAttribute('href'))) continue;
        var tileBox = tiles[t].closest(ROW_SELECTOR) || tiles[t];
        tileBox.removeAttribute('data-nt-desk-thread');
        tileBox.setAttribute('data-nt-desk', 'clutter');
        tileBox.classList.add(CLS_HIDDEN);
      }
      // The 'Loading...' placeholder of a lazily loaded date group kept that group (a gap) visible.
      labelTargets(root, LOADING_LABELS).forEach(function (hit) {
        if (hit.closest('[data-nt-desk="chat-row"]')) return;
        var box = tileBlock(hit, root);
        if (box.getAttribute('data-nt-desk') === 'chat-row') return;
        box.setAttribute('data-nt-desk', 'clutter');
        box.classList.add(CLS_HIDDEN);
      });
      labelled(root, function (label) { return DATE_GROUP_RE.test(label); }).forEach(function (hit) {
        if (hit.node.closest('[data-nt-desk="chat-row"]')) return;
        var block = headingBlock(hit.node, hit.label, root);
        if (block.closest('[data-nt-desk="chat-row"]') || block.querySelector('a[href]')) return;
        block.setAttribute('data-nt-desk', 'clutter');
        block.classList.add(CLS_HIDDEN);
        var group = block.parentElement;
        if (group && group !== root && !isOwn(group) && chatGroups.indexOf(group) < 0) chatGroups.push(group);
      });
    }

    // Wrappers left behind by hidden content (tile grids, empty date groups) still take space.
    // Hide every ancestor whose child nodes are all hidden by us; recomputed on every run.
    function isShell(node) {
      if (!node.children.length) return false;
      for (var i = 0; i < node.childNodes.length; i++) {
        var kid = node.childNodes[i];
        if (kid.nodeType === 3 && kid.nodeValue.trim()) return false;
        if (kid.nodeType === 1 && !kid.classList.contains(CLS_HIDDEN)) return false;
      }
      return true;
    }

    // Runs before the clutter and row passes so they see the real Notion nodes, not last run's
    // collapsed wrappers (a collapsed date heading used to keep its whole group unfiltered).
    function uncollapse(root) {
      if (!root) return;
      var old = root.querySelectorAll('[data-nt-desk="collapsed"]');
      for (var i = 0; i < old.length; i++) {
        old[i].classList.remove(CLS_HIDDEN);
        old[i].removeAttribute('data-nt-desk');
      }
    }

    function collapseEmpty(root) {
      if (!root) return;
      var hiddenNodes = root.querySelectorAll('.' + CLS_HIDDEN);
      for (var j = 0; j < hiddenNodes.length; j++) {
        var cur = hiddenNodes[j].parentElement;
        while (cur && cur !== root && root.contains(cur) && !isOwn(cur)) {
          var mark = cur.getAttribute('data-nt-desk');
          if (mark === 'collapsed') { cur = cur.parentElement; continue; }
          if (mark && mark !== 'chat-group') break;
          if (!isShell(cur)) break;
          cur.setAttribute('data-nt-desk', 'collapsed');
          cur.classList.add(CLS_HIDDEN);
          cur = cur.parentElement;
        }
      }
    }

    // ---- sidebar filter ------------------------------------------------------------------
    function rowOf(link) {
      var row = link.closest(ROW_SELECTOR);
      return row && row !== document.body ? row : link;
    }

    function decide(state, key) {
      if (!state || !key) return false;
      var hidden = list(state.hidden_threads).map(threadKey);
      if (hidden.indexOf(key) >= 0) return true;
      var mains = list(state.mains).map(function (m) { return threadKey(m && m.thread_id); });
      return mains.indexOf(key) < 0 && state.app_chat_filter !== 'hide_coworkers';
    }

    function mainOf(state, key) {
      var mains = list(state && state.mains);
      for (var i = 0; i < mains.length; i++) {
        if (mains[i] && threadKey(mains[i].thread_id) === key) return mains[i];
      }
      return null;
    }

    function isActive(row, link, key) {
      if (threadFromHref(location.href) === key) return true;
      var flag = row.getAttribute('aria-selected') || link.getAttribute('aria-current') || row.getAttribute('aria-current');
      return flag === 'true' || flag === 'page';
    }

    function stylePill(link, operator) {
      var pill = link.querySelector('[data-nt-desk="pill"]');
      if (operator && !pill) link.appendChild(el('span', { cls: CLS_PILL, 'data-nt-desk': 'pill', text: 'Operator' }));
      if (!operator && pill) dropOwn(pill);
    }

    function applyFilter(state) {
      var links = document.querySelectorAll('a[href]');
      for (var i = 0; i < links.length; i++) {
        var link = links[i];
        if (!link.closest(SIDEBAR_SELECTOR) || link.closest('[data-nt-desk-own]')) continue;
        var key = threadFromHref(link.getAttribute('href'));
        if (!key) continue;
        var row = rowOf(link);
        row.setAttribute('data-nt-desk', 'chat-row');
        row.setAttribute('data-nt-desk-thread', key);
        row.classList.toggle(CLS_HIDDEN, decide(state, key));
        var main = mainOf(state, key);
        row.classList.toggle(CLS_MAIN_ROW, !!main);
        row.classList.toggle(CLS_ACTIVE, !!main && isActive(row, link, key));
        row.classList.toggle(CLS_OPERATOR, !!(main && main.operator));
        stylePill(link, !!(main && main.operator));
        styleCard(row, main, state);
      }
    }

    // ---- rows without links ----------------------------------------------------------------
    // Notion AI chat rows are menu items without a link (live: DIV[role=menuitem], text = title +
    // age). Their thread id comes from rowThread(); the decision is the same decide() as for links.
    function rowActive(row, key) {
      if (threadFromHref(location.href) === key) return true;
      var flag = row.getAttribute('aria-selected') || row.getAttribute('aria-current');
      return flag === 'true' || flag === 'page';
    }

    function applyRows(state, root) {
      if (!root) return;
      chatGroups.forEach(function (group) {
        if (!group.getAttribute('data-nt-desk')) group.setAttribute('data-nt-desk', 'chat-group');
        group.classList.add(CLS_GROUP);
      });
      var rows = root.querySelectorAll(ROW_ITEM_SELECTOR);
      for (var i = 0; i < rows.length; i++) {
        var row = rows[i];
        if (isOwn(row) || row.closest('[data-nt-desk-own]') || row.querySelector('a[href]')) continue;
        var mark = row.getAttribute('data-nt-desk');
        if (mark && mark !== 'chat-row') continue;
        var key = rowThread(row);
        if (!key) {
          // No readable thread id: never guess, show the row as Notion renders it.
          if (mark === 'chat-row') {
            for (var k = 0; k < OUR_CLASSES.length; k++) row.classList.remove(OUR_CLASSES[k]);
            row.removeAttribute('data-nt-desk');
            row.removeAttribute('data-nt-desk-thread');
          }
          continue;
        }
        var main = mainOf(state, key);
        row.setAttribute('data-nt-desk', 'chat-row');
        row.setAttribute('data-nt-desk-thread', key);
        row.classList.toggle(CLS_HIDDEN, decide(state, key));
        row.classList.toggle(CLS_MAIN_ROW, !!main);
        row.classList.toggle(CLS_ACTIVE, !!main && rowActive(row, key));
        row.classList.toggle(CLS_OPERATOR, !!(main && main.operator));
        stylePill(row, !!(main && main.operator));
        styleCard(row, main, state);
      }
    }

    // ---- Sidebar v2: Main cards, Coworkers, offline chip -------------------------------------
    // Writes only on change, so our own updates never re-trigger the observer in a loop.
    function setText(node, text) { if (node && node.textContent !== text) node.textContent = text; }

    function setCls(node, cls) { if (node && node.getAttribute('class') !== cls) node.setAttribute('class', cls); }

    function part(card, name) { return card.querySelector('[data-nt-desk="' + name + '"]'); }

    function styleCard(row, main, state) {
      var card = null;
      for (var i = 0; i < row.children.length; i++) {
        if (row.children[i].getAttribute('data-nt-desk') === 'card') card = row.children[i];
      }
      if (!main) { if (card) dropOwn(card); return; }
      var info = mainStatus(state, main);
      if (!card) {
        card = el('div', { cls: 'nt-desk-card', 'data-nt-desk': 'card' }, [
          el('span', { cls: 'nt-desk-dot', 'data-nt-desk': 'dot' }),
          el('span', { cls: 'nt-desk-card-title', 'data-nt-desk': 'run-title' }),
          el('span', { cls: 'nt-desk-progress', 'data-nt-desk': 'progress' }, [
            el('span', { cls: 'nt-desk-progress-fill', 'data-nt-desk': 'progress-fill' })
          ]),
          el('span', { cls: 'nt-desk-progress-count', 'data-nt-desk': 'progress-count' })
        ]);
        row.appendChild(card);
      }
      var labels = { running: 'Running', waiting: 'Waiting for you', idle: 'Idle' };
      setCls(card, 'nt-desk-card nt-desk-card-' + info.status + (info.total ? '' : ' nt-desk-no-run'));
      var dot = part(card, 'dot');
      setCls(dot, 'nt-desk-dot nt-desk-dot-' + info.status);
      if (dot && dot.getAttribute('title') !== labels[info.status]) dot.setAttribute('title', labels[info.status]);
      setText(part(card, 'run-title'), info.title || (info.status === 'idle' ? 'No active run' : labels[info.status]));
      var width = info.total ? Math.round(100 * info.done / info.total) + '%' : '0%';
      var fill = part(card, 'progress-fill');
      if (fill && fill.style.width !== width) fill.style.width = width;
      setText(part(card, 'progress-count'), info.total ? info.done + '/' + info.total : '');
    }

    function coworkersCollapsed() {
      try { return window.localStorage.getItem(COWORKERS_KEY) === '1'; } catch (e) { return false; }
    }

    function setCoworkersCollapsed(value) {
      try { window.localStorage.setItem(COWORKERS_KEY, value ? '1' : '0'); } catch (e) { /* not stored */ }
    }

    // Navigation stays Notion's: the click goes to Notion's own (hidden) sidebar row of that thread;
    // without one the link is followed.
    function openThread(ev, row) {
      var target = row.thread ? document.querySelector('[data-nt-desk="chat-row"][data-nt-desk-thread="' + row.thread + '"]') : null;
      if (!target) return;
      ev.preventDefault();
      ev.stopPropagation();
      var link = target.matches('a[href]') ? target : target.querySelector('a[href]');
      (link || target).click();
    }

    function coworkerRow(row) {
      var kids = [
        el('span', { cls: 'nt-desk-dot nt-desk-dot-' + (row.busy ? 'running' : 'idle'), 'aria-hidden': 'true' }),
        el('span', { cls: 'nt-desk-coworker-main' }, [
          el('span', { cls: 'nt-desk-coworker-name', text: row.label }),
          el('span', { cls: 'nt-desk-coworker-task', text: row.task })
        ])
      ];
      if (row.model) kids.push(el('span', { cls: 'nt-desk-chip nt-desk-model', text: row.model }));
      if (row.age) kids.push(el('span', { cls: 'nt-desk-chip nt-desk-age nt-desk-age-' + (row.ageCls || 'ok'), text: row.age, title: 'Chat age' }));
      var props = { cls: 'nt-desk-coworker', 'data-nt-desk': 'coworker', 'data-nt-desk-thread': row.thread, title: row.task };
      if (row.url) props.href = row.url;
      var node = el(row.url ? 'a' : 'div', props, kids);
      node.addEventListener('click', function (ev) { openThread(ev, row); });
      return node;
    }

    function applyCoworkers(state, root) {
      var section = document.querySelector('[data-nt-desk="coworkers"]');
      var mainRow = root ? root.querySelector('.' + CLS_MAIN_ROW) : null;
      var anchor = mainRow ? (mainRow.closest('[role="menu"]') || mainRow.parentElement) : null;
      if (!anchor || anchor === root || !root.contains(anchor) || !anchor.parentNode) {
        if (section) dropOwn(section);
        return;
      }
      if (section && section.previousSibling !== anchor) { dropOwn(section); section = null; }
      if (!section) {
        section = el('div', { cls: 'nt-desk-coworkers', 'data-nt-desk': 'coworkers' });
        anchor.parentNode.insertBefore(section, anchor.nextSibling);
      }
      var collapsed = coworkersCollapsed();
      section.classList.toggle('nt-desk-collapsed', collapsed);
      var rows = coworkerRows(state);
      var sig = JSON.stringify(rows);
      if (section.getAttribute('data-nt-desk-sig') === sig) return;
      section.setAttribute('data-nt-desk-sig', sig);
      Array.prototype.slice.call(section.childNodes).forEach(dropOwn);
      var head = el('button', { cls: 'nt-desk-coworkers-head', 'data-nt-desk': 'coworkers-head', type: 'button',
        'aria-expanded': String(!collapsed) }, [
        el('span', { cls: 'nt-desk-chevron', 'aria-hidden': 'true' }),
        el('span', { cls: 'nt-desk-coworkers-title', text: 'Coworkers' }),
        el('span', { cls: 'nt-desk-chip nt-desk-count', text: String(rows.length) })
      ]);
      head.addEventListener('click', function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        var next = !coworkersCollapsed();
        setCoworkersCollapsed(next);
        section.classList.toggle('nt-desk-collapsed', next);
        head.setAttribute('aria-expanded', String(!next));
      });
      var body = el('div', { cls: 'nt-desk-coworker-list', 'data-nt-desk': 'coworker-list' });
      if (!rows.length) body.appendChild(el('div', { cls: 'nt-desk-coworker-empty', text: 'No coworker chats' }));
      rows.forEach(function (row) { body.appendChild(coworkerRow(row)); });
      section.appendChild(head);
      section.appendChild(body);
    }

    // Harness offline: nothing filtered or re-labelled; only the brand header with an 'offline' chip.
    function showOffline() {
      try {
        var root = sidebarRoot();
        applyBrand(root);
        applyOffline(root, true);
      } catch (e) { /* cosmetic */ }
    }

    function applyOffline(root, offline) {
      var brand = root ? root.querySelector('[data-nt-desk="brand"]') : null;
      var chip = brand ? brand.querySelector('[data-nt-desk="offline-chip"]') : null;
      if (offline && brand && !chip) {
        brand.appendChild(el('span', { cls: 'nt-desk-chip nt-desk-offline', 'data-nt-desk': 'offline-chip', text: 'offline',
          title: 'Harness offline: all chats are shown unfiltered' }));
      }
      if (!offline && chip) dropOwn(chip);
    }

    // Undo every mark of this part (harness offline, and stop): own nodes out, classes/marks off.
    function resetMarks() {
      var own = document.querySelectorAll('[data-nt-desk-own]');
      for (var j = 0; j < own.length; j++) dropOwn(own[j]);
      var marked = document.querySelectorAll('[data-nt-desk]');
      for (var i = 0; i < marked.length; i++) {
        for (var k = 0; k < OUR_CLASSES.length; k++) marked[i].classList.remove(OUR_CLASSES[k]);
        marked[i].removeAttribute('data-nt-desk');
        marked[i].removeAttribute('data-nt-desk-thread');
      }
    }

    // ---- New chat control ----------------------------------------------------------------
    function newChatControl(target) {
      var node = target && target.closest ? target.closest(CONTROL_SELECTOR) : null;
      if (!node || !node.closest(SIDEBAR_SELECTOR)) return null;
      if (node.getAttribute('data-nt-desk') === 'new-chat') return node;
      return isNewChatLabel(visibleLabel(node)) ? node : null;
    }

    function markNewChat() {
      var nodes = document.querySelectorAll(CONTROL_SELECTOR);
      for (var i = 0; i < nodes.length; i++) {
        var node = nodes[i];
        if (node.getAttribute('data-nt-desk') === 'new-chat') continue;
        if (!node.closest(SIDEBAR_SELECTOR)) continue;
        var label = visibleLabel(node);
        if (label.length > 20 || !isNewChatLabel(label)) continue;
        node.setAttribute('data-nt-desk', 'new-chat');
        node.setAttribute('title', 'New chat opens a Welcome chat for the harness');
        node.classList.add(CLS_NEW_CHAT);
      }
    }

    function block(ev) {
      ev.preventDefault();
      ev.stopPropagation();
      if (ev.stopImmediatePropagation) ev.stopImmediatePropagation();
    }

    function createWelcome(node) {
      if (busy) return;
      busy = true;
      node.classList.add(CLS_BUSY);
      Promise.resolve().then(function () { return ctx.call('new_welcome_chat', {}); }).then(function (result) {
        if (result && result.url) location.href = result.url;
      }, function (err) {
        ctx.toast('Could not create a Welcome chat: ' + message(err), 'error');
      }).then(function () {
        busy = false;
        node.classList.remove(CLS_BUSY);
      });
    }

    function onPress(ev) {
      if (!stopped && newChatControl(ev.target)) block(ev);
    }

    function onClick(ev) {
      if (stopped) return;
      var node = newChatControl(ev.target);
      if (!node) return;
      block(ev);
      createWelcome(node);
    }

    function onKey(ev) {
      if (stopped || (ev.key !== 'Enter' && ev.key !== ' ')) return;
      var node = newChatControl(ev.target);
      if (!node) return;
      block(ev);
      createWelcome(node);
    }

    // ---- scheduling ----------------------------------------------------------------------
    function run() {
      timer = 0;
      if (stopped || !document.body) return;
      var state = typeof ctx.getState === 'function' ? ctx.getState() : null;
      if (!online(state)) {
        // Harness offline: hide nothing and re-label nothing; undo what an earlier run did.
        if (applied) resetMarks();
        applied = false;
        markNewChat();
        showOffline();
        return;
      }
      applied = true;
      markNewChat();
      applyFilter(state);
      try {
        var root = sidebarRoot();
        uncollapse(root);
        applyBrand(root);
        applyOffline(root, false);
        applyHeading(root);
        chatGroups = [];
        applyClutter(root);
        applyRows(state, root);
        applyCoworkers(state, root);
        collapseEmpty(root);
      } catch (e) { /* branding is cosmetic; never break the filter or the New chat routing */ }
    }

    function schedule() {
      if (!stopped && !timer) timer = setTimeout(run, DEBOUNCE_MS);
    }

    document.addEventListener('pointerdown', onPress, true);
    document.addEventListener('mousedown', onPress, true);
    document.addEventListener('click', onClick, true);
    document.addEventListener('keydown', onKey, true);
    observer = new MutationObserver(schedule);
    // aria-selected / aria-current / href: the active row follows navigation. Our own class
    // and data-nt-desk changes are not observed, so run() never re-triggers itself.
    observer.observe(document.documentElement, { childList: true, subtree: true, attributes: true,
      attributeFilter: ['aria-selected', 'aria-current', 'href'] });
    unsub = ctx.onState(function () { schedule(); });
    run();

    return {
      stop: function () {
        stopped = true;
        if (timer) clearTimeout(timer);
        timer = 0;
        if (observer) observer.disconnect();
        if (typeof unsub === 'function') unsub();
        document.removeEventListener('pointerdown', onPress, true);
        document.removeEventListener('mousedown', onPress, true);
        document.removeEventListener('click', onClick, true);
        document.removeEventListener('keydown', onKey, true);
        resetMarks();
      }
    };
  };
})();
