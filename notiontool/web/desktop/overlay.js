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
 *   Every Main in state.mains is always shown (I33), even if its thread is in hidden_threads;
 *   several Mains and user-renamed Mains ('Roblox') included. state.mains[].title (the user's own
 *   title when renamed) is only displayed (card tooltip 'Main: <title>'), never used for matching.
 *   A spaceId=<uuid> in a link is never taken as a thread id.
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

  var MONTHS_SHORT = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

  function pad2(n) { return (n < 10 ? '0' : '') + n; }

  // created_at of a worker (team_rotation.py: epoch seconds), else derived from age_s; 0 when unknown.
  function createdOf(w, now) {
    var c = seconds(w && w.created_at);
    if (c) return c;
    var age = w && w.age_s != null ? Number(w.age_s) : NaN;
    return isFinite(age) && age >= 0 ? (seconds(now) || Date.now() / 1000) - age : 0;
  }

  // 'Created 19 Oct 12:04 - 16h old' in local time; '' when the creation time is unknown.
  function createdText(created, now) {
    var c = seconds(created);
    var hours = ageHours(c, now);
    if (!c || hours < 0) return '';
    var d = new Date(c * 1000);
    return 'Created ' + d.getDate() + ' ' + MONTHS_SHORT[d.getMonth()] + ' ' + pad2(d.getHours()) + ':' +
      pad2(d.getMinutes()) + ' - ' + ageLabel(hours) + ' old';
  }

  function workerBusy(w) {
    return !!(w && (w.task_title || BUSY_WORKER_STATES.indexOf(String(w.state || '').toLowerCase()) >= 0));
  }

  // Age badge of one coworker chat: { text, level ('ok' | 'amber' | 'red'), title, created, rotating };
  // null while the creation time is unknown. Red and busy: 'rotating soon' (a fresh chat takes over).
  function ageBadge(w, now) {
    var c = createdOf(w, now);
    var hours = ageHours(c, now);
    if (!c || hours < 0) return null;
    var level = ageClass(hours);
    var rotating = level === 'red' && workerBusy(w);
    var created = createdText(c, now);
    var note = rotating ? ' - rotating soon: a fresh chat takes over after this task'
      : (level === 'red' ? ' - a fresh chat takes over soon' : '');
    return { text: ageLabel(hours) + (rotating ? ' - rotating soon' : ''), level: level, title: created + note,
      created: created, rotating: rotating };
  }

  // Thread key -> age badge for every coworker chat (purpose 'worker'). Mains are never in this map.
  function coworkerAges(state) {
    var out = {};
    var now = state && state.ts;
    list(state && state.workers).forEach(function (w) {
      if (!w || w.purpose !== 'worker' || !w.thread_id) return;
      var badge = ageBadge(w, now);
      if (badge) out[threadKey(w.thread_id)] = badge;
    });
    return out;
  }

  // Coworker chats (workers with purpose 'worker'). An age helper of the team run
  // (window.__ntDesk.ageInfo(created, now) -> { label, level }) is used when present.
  function coworkerRows(state) {
    var now = state && state.ts;
    var shared = typeof window.__ntDesk.ageInfo === 'function' ? window.__ntDesk.ageInfo : null;
    return list(state && state.workers).filter(function (w) { return w && w.purpose === 'worker'; }).map(function (w) {
      var hours = ageHours(createdOf(w, now), now);
      var age = { label: ageLabel(hours), level: ageClass(hours) };
      if (shared) {
        try { var info = shared(w.created_at, now); if (info && info.label) age = { label: String(info.label), level: String(info.level || '') }; } catch (e) { /* keep ours */ }
      }
      var task = String(w.task_title || '');
      var busy = !!task || !!w.task_id || workerBusy(w);
      var badge = ageBadge(w, now);
      var rotating = !!(badge && badge.rotating);
      return { id: String(w.id || ''), label: String(w.label || 'Coworker'), thread: threadKey(w.thread_id),
        url: String(w.url || ''), model: String(w.model || ''), task: task || (busy ? 'Working' : 'Idle'), busy: busy,
        age: age.label + (rotating ? ' - rotating soon' : ''), ageCls: age.level, rotating: rotating,
        created: badge ? badge.created : '', ageTitle: badge ? badge.title : '' };
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
    ageClass: ageClass, ageLabel: ageLabel, coworkerRows: coworkerRows,
    createdText: createdText, ageBadge: ageBadge, coworkerAges: coworkerAges, ensureHeading: ensureHeading };

  // ---- Named chats (own New chat dialog, Chats section, gear menu): pure helpers ------------
  // Actions go through ctx.call only: app_chat_create / app_chat_rename / app_chat_promote, which
  // the desktop shell maps to the app chat create, rename and promote routes.
  var CHAT_NAME_MAX = 80;
  var CHAT_NAME_COUNTER_AT = 60;
  var CHATS_KEY = 'ntDesk.chatsCollapsed';
  var OPEN_WAIT_MS = 15000;
  var PLUS_GLYPH = '+';
  var GEAR_GLYPH = '\u2699\uFE0E';

  // One uuid per dialog: a double click or a retry sends the same request_id, so the backend
  // never creates the chat twice.
  function newRequestId() {
    var c = window.crypto;
    try { if (c && typeof c.randomUUID === 'function') return c.randomUUID(); } catch (e) { /* fallback below */ }
    var b = [];
    try {
      if (c && typeof c.getRandomValues === 'function') b = Array.prototype.slice.call(c.getRandomValues(new Uint8Array(16)));
    } catch (e) { b = []; }
    for (var i = b.length; i < 16; i++) b.push(Math.floor(Math.random() * 256));
    b[6] = (b[6] & 15) | 64;
    b[8] = (b[8] & 63) | 128;
    var h = b.map(function (x) { return (x < 16 ? '0' : '') + x.toString(16); }).join('');
    return h.slice(0, 8) + '-' + h.slice(8, 12) + '-' + h.slice(12, 16) + '-' + h.slice(16, 20) + '-' + h.slice(20, 32);
  }

  function cleanName(value) { return String(value == null ? '' : value).replace(/\s+/g, ' ').trim(); }

  function nameError(name) {
    if (!name) return 'Enter a chat name.';
    if (name.length > CHAT_NAME_MAX) return 'Use at most ' + CHAT_NAME_MAX + ' characters.';
    return '';
  }

  function createArgs(name, main, requestId) {
    return { name: cleanName(name), main: !!main, request_id: String(requestId || '') };
  }

  function renameArgs(info, name) {
    return { thread_id: String((info && info.thread_id) || ''), name: cleanName(name) };
  }

  function promoteArgs(info, main) {
    var args = { main: !!main };
    if (info && info.chat_id) args.chat_id = String(info.chat_id);
    if (info && info.thread_id) args.thread_id = String(info.thread_id);
    return args;
  }

  // Private chats (purpose 'private'): state.private_chats, else workers with that purpose.
  function privateChatRows(state) {
    var src = list(state && state.private_chats);
    if (!src.length) src = list(state && state.workers).filter(function (w) { return w && w.purpose === 'private'; });
    var seen = {};
    return src.map(function (c) {
      c = c || {};
      return { kind: 'private', name: cleanName(c.name || c.title || c.label) || 'Untitled chat',
        chat_id: String(c.chat_id || ''), worker_id: String(c.worker_id || c.id || ''),
        thread_id: String(c.thread_id || ''), thread: threadKey(c.thread_id || ''), url: String(c.url || '') };
    }).filter(function (row) {
      if (!row.thread || seen[row.thread]) return false;
      seen[row.thread] = true;
      return true;
    });
  }

  function mainChatInfo(main) {
    main = main || {};
    return { kind: 'main', name: cleanName(main.title || main.label) || 'Main', chat_id: String(main.chat_id || ''),
      worker_id: String(main.worker_id || ''), thread_id: String(main.thread_id || ''), operator: !!main.operator };
  }

  // Gear menu of one chat row: Rename plus Make Main (private) or Remove Main rights (Main).
  function chatMenuItems(info) {
    var items = [{ id: 'rename', label: 'Rename' }];
    if (info && info.kind === 'main') {
      items.push({ id: 'demote', label: 'Remove Main rights', danger: true, disabled: !!info.operator,
        title: info.operator ? 'The Operator Main always keeps its Main rights' : '' });
    } else {
      items.push({ id: 'promote', label: 'Make Main' });
    }
    return items;
  }

  window.__ntDesk.newChatUtil = { newRequestId: newRequestId, cleanName: cleanName, nameError: nameError,
    createArgs: createArgs, renameArgs: renameArgs, promoteArgs: promoteArgs, privateChatRows: privateChatRows,
    mainChatInfo: mainChatInfo, chatMenuItems: chatMenuItems, CHAT_NAME_MAX: CHAT_NAME_MAX };

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
      // I33: every Main is always shown (also user-renamed ones like 'Roblox', several at once).
      if (mainOf(state, key)) return false;
      // The user's own chats (e.g. 'Sonstiges') stay in Notion's list, so a normal click opens them
      // (desktop JS never clicks or navigates). applyChats then leaves them out of our Chats section,
      // so each chat is shown exactly once.
      var own = list(state.private_chats).map(function (c) { return threadKey(c && c.thread_id); });
      if (own.indexOf(key) >= 0) return false;
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
        styleGear(row, main);
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
      var ages = coworkerAges(state);
      window.__ntDesk.coworkerAges = ages;  // donesound: coworker chats stay silent
      window.__ntDesk.doneSound = !(state && state.done_sound === false);  // Settings > App > Done sound
      window.__ntDesk.threadKey = threadKey;
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
        styleAge(row, main ? null : (ages[key] || null));
        styleGear(row, main);
      }
      try { stableOrder(root); } catch (err) { /* never throw */ }
    }

    // Fixed chat order (user request 2026-10-20): Notion re-sorts the sidebar chats by last activity, so they
    // jumped whenever another chat got a new step. The first-seen order is pinned with CSS order (no Notion
    // node is moved). A chat seen for the first time at the top (new chat) goes to the top, others below.
    var orderOf = {}, nextTop = 0, nextLow = 100000;
    function stableOrder(root) {
      var rows = root.querySelectorAll('[data-nt-desk="chat-row"]');
      var parents = [];
      for (var i = 0; i < rows.length; i++) {
        var p = rows[i].parentElement;
        if (p && parents.indexOf(p) < 0) parents.push(p);
      }
      var first = !Object.keys(orderOf).length;
      parents.forEach(function (parent) {
        var kids = parent.children;
        for (var k = 0; k < kids.length; k++) {
          var kid = kids[k];
          var key = kid.getAttribute('data-nt-desk') === 'chat-row' ? kid.getAttribute('data-nt-desk-thread') : '';
          var want = k * 10;  // headers and 'more' keep their place
          if (key) {
            if (!(key in orderOf)) orderOf[key] = first ? k * 10 + 5 : (k === 0 ? (nextTop -= 1) : (nextLow += 10));
            want = orderOf[key];
          }
          if (kid.style && kid.style.order !== String(want)) kid.style.order = String(want);
        }
      });
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
      var tip = 'Main: ' + String(main.title || 'Main');
      if (card.getAttribute('title') !== tip) card.setAttribute('title', tip);
      if (card.getAttribute('data-nt-role') !== 'main') card.setAttribute('data-nt-role', 'main');
      var dot = part(card, 'dot');
      setCls(dot, 'nt-desk-dot nt-desk-dot-' + info.status);
      if (dot && dot.getAttribute('title') !== labels[info.status]) dot.setAttribute('title', labels[info.status]);
      setText(part(card, 'run-title'), info.title || (info.status === 'idle' ? 'No active run' : labels[info.status]));
      var width = info.total ? Math.round(100 * info.done / info.total) + '%' : '0%';
      var fill = part(card, 'progress-fill');
      if (fill && fill.style.width !== width) fill.style.width = width;
      setText(part(card, 'progress-count'), info.total ? info.done + '/' + info.total : '');
    }

    // Age badge on Notion's own coworker row: an own child element; the row itself is not changed.
    function styleAge(row, info) {
      var badge = null;
      for (var i = 0; i < row.children.length; i++) {
        if (row.children[i].getAttribute('data-nt-desk') === 'age') badge = row.children[i];
      }
      if (!info) { if (badge) dropOwn(badge); return; }
      if (!badge) {
        badge = el('span', { cls: 'nt-desk-chip nt-desk-age', 'data-nt-desk': 'age' });
        row.appendChild(badge);
      }
      setCls(badge, 'nt-desk-chip nt-desk-age nt-desk-row-age nt-desk-age-' + (info.level || 'ok') +
        (info.rotating ? ' nt-desk-age-rotating' : ''));
      setText(badge, info.text);
      if (badge.getAttribute('title') !== info.title) badge.setAttribute('title', info.title);
      if (badge.getAttribute('aria-label') !== info.title) badge.setAttribute('aria-label', info.title);
    }

    function coworkersCollapsed() {
      try { return window.localStorage.getItem(COWORKERS_KEY) === '1'; } catch (e) { return false; }
    }

    function setCoworkersCollapsed(value) {
      try { window.localStorage.setItem(COWORKERS_KEY, value ? '1' : '0'); } catch (e) { /* not stored */ }
    }

    // Navigation stays Notion's: the click goes to Notion's own (hidden) sidebar row of that thread;
    // without one the link is followed.
    // Coworker chats are never opened in the app: Notion loads the whole (very long) history on
    // open and the app starts to lag. The row is status-only; progress lives in the Live panel.
    function openThread(ev, row) {
      ev.preventDefault();
      ev.stopPropagation();
      ctx.toast((row.label || 'Coworker') + ': ' + (row.task || (row.busy ? 'working' : 'idle')) +
        '. Coworker chats are not opened here (keeps the app fast).', 'info');
    }

    function coworkerRow(row) {
      var kids = [
        el('span', { cls: 'nt-desk-dot nt-desk-dot-' + (row.busy ? 'running' : 'idle'), 'aria-hidden': 'true' }),
        el('span', { cls: 'nt-desk-coworker-main' }, [
          el('span', { cls: 'nt-desk-coworker-name', text: row.label }),
          el('span', { cls: 'nt-desk-coworker-task', text: row.task }),
          row.created ? el('span', { cls: 'nt-desk-coworker-created', 'data-nt-desk': 'created', text: row.created }) : null
        ].filter(Boolean))
      ];
      if (row.model) kids.push(el('span', { cls: 'nt-desk-chip nt-desk-model', text: row.model }));
      if (row.age) kids.push(el('span', { cls: 'nt-desk-chip nt-desk-age nt-desk-age-' + (row.ageCls || 'ok') + (row.rotating ? ' nt-desk-age-rotating' : ''), text: row.age, title: row.ageTitle || 'Chat age' }));
      var props = { cls: 'nt-desk-coworker', 'data-nt-desk': 'coworker', 'data-nt-desk-thread': row.thread, title: row.task };
      props.role = 'button';
      props.tabindex = '0';
      var node = el('div', props, kids);
      node.addEventListener('click', function (ev) { openThread(ev, row); });
      return node;
    }

    function applyCoworkers(state, root) {
      var section = document.querySelector('[data-nt-desk="coworkers"]');
      var anchor = sectionAnchor(root);
      if (!anchor) {
        if (section) dropOwn(section);
        return;
      }
      // The Chats section (private chats) sits between the Mains and this section.
      var next = anchor.nextSibling;
      var before = next && next.getAttribute && next.getAttribute('data-nt-desk') === 'chats' ? next : anchor;
      if (section && section.previousSibling !== before) { dropOwn(section); section = null; }
      if (!section) {
        section = el('div', { cls: 'nt-desk-coworkers', 'data-nt-desk': 'coworkers' });
        anchor.parentNode.insertBefore(section, before.nextSibling);
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
        el('span', { cls: 'nt-desk-chip nt-desk-count', text: String(typeof window.__ntDesk.counts === 'function'
          ? window.__ntDesk.counts(state).coworkers : rows.length) })
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

    // ---- Named chats: own New chat dialog, Chats section, gear menu ------------------------
    // Only our own nodes (data-nt-desk-own). Opening a chat clicks Notion's own row; without one
    // the URL is pushed into history (same origin, no reload). Nothing here sends a chat message.
    var dialog = null;
    var menu = null;
    var pendingOpen = null;
    var pendingTimer = 0;
    var promoting = {};

    function mk(tag, attrs, kids) {
      var node = document.createElement(tag);
      attrs = attrs || {};
      Object.keys(attrs).forEach(function (name) {
        var value = attrs[name];
        if (value == null || value === false) return;
        if (name === 'cls') node.className = value;
        else if (name === 'text') node.textContent = String(value);
        else node.setAttribute(name, value === true ? '' : String(value));
      });
      node.setAttribute('data-nt-desk-own', '1');
      (kids || []).forEach(function (kid) { if (kid) node.appendChild(kid); });
      return node;
    }

    function stopEv(ev) { ev.stopPropagation(); }

    // Events inside our dialog/menu stay ours, so Notion's shortcuts and handlers never see them.
    function guard(node, types) { types.forEach(function (type) { node.addEventListener(type, stopEv); }); }

    function failText(err) {
      var text = message(err) || 'Unknown error';
      if (/not allowed|not available|unknown action/i.test(text)) {
        return 'NotionTool does not offer this action yet. Update and restart NotionTool.';
      }
      return text;
    }

    function checkResult(result) {
      if (result && (result.ok === false || result.error)) throw new Error(String(result.error || 'The request failed.'));
      if (result && String(result.state || '').toLowerCase() === 'failed') {
        throw new Error(String(result.error || 'Creating the chat failed. Close this dialog and try again.'));
      }
      return result || {};
    }

    function sectionAnchor(root) {
      var mainRow = root ? root.querySelector('.' + CLS_MAIN_ROW) : null;
      var anchor = mainRow ? (mainRow.closest('[role="menu"]') || mainRow.parentElement) : null;
      return anchor && anchor !== root && root.contains(anchor) && anchor.parentNode ? anchor : null;
    }

    // -- dialog (create, rename, confirm)
    function closeDialog(restore) {
      if (!dialog) return;
      var d = dialog;
      dialog = null;
      dropOwn(d.scrim);
      if (restore !== false && d.opener && typeof d.opener.focus === 'function' && document.contains(d.opener)) {
        try { d.opener.focus(); } catch (e) { /* focus is best effort */ }
      }
    }

    function openDialog(opts) {
      closeMenu(false);
      closeDialog(false);
      var opener = opts.opener || document.activeElement;
      var helpText = 'Up to ' + CHAT_NAME_MAX + ' characters.';
      var kids = [mk('h2', { cls: 'nt-desk-dialog-title', id: 'nt-desk-dlg-title', text: opts.title })];
      if (opts.sub) kids.push(mk('p', { cls: 'nt-desk-dialog-sub', id: 'nt-desk-dlg-sub', text: opts.sub }));
      var input = null;
      var msg = null;
      var box = null;
      if (opts.nameLabel) {
        kids.push(mk('label', { cls: 'nt-desk-label', 'for': 'nt-desk-dlg-name', text: opts.nameLabel }));
        input = mk('input', { cls: 'nt-desk-input', id: 'nt-desk-dlg-name', type: 'text', maxlength: CHAT_NAME_MAX,
          autocomplete: 'off', spellcheck: 'false', placeholder: opts.placeholder || null,
          'aria-describedby': 'nt-desk-dlg-msg' });
        input.value = opts.name || '';
        msg = mk('div', { cls: 'nt-desk-field-msg', id: 'nt-desk-dlg-msg', 'aria-live': 'polite', text: helpText });
        kids.push(input, msg);
      }
      if (opts.check) {
        box = mk('input', { cls: 'nt-desk-checkbox', id: 'nt-desk-dlg-main', type: 'checkbox',
          'aria-describedby': 'nt-desk-dlg-main-help' });
        box.checked = !!opts.check.checked;
        kids.push(mk('label', { cls: 'nt-desk-check', 'for': 'nt-desk-dlg-main' }, [box,
          mk('span', { cls: 'nt-desk-check-text' }, [
            mk('span', { cls: 'nt-desk-check-label', text: opts.check.label }),
            mk('span', { cls: 'nt-desk-check-help', id: 'nt-desk-dlg-main-help', text: opts.check.help })
          ])
        ]));
      }
      var banner = mk('div', { cls: 'nt-desk-banner', role: 'alert', hidden: true });
      var cancel = mk('button', { cls: 'nt-desk-btn nt-desk-btn-ghost', type: 'button', text: 'Cancel' });
      var ok = mk('button', { cls: 'nt-desk-btn ' + (opts.danger ? 'nt-desk-btn-danger' : 'nt-desk-btn-primary'),
        type: 'button', text: opts.submit, 'aria-disabled': 'false' });
      kids.push(banner, mk('div', { cls: 'nt-desk-actions' }, [cancel, ok]));
      var panel = mk('div', { cls: 'nt-desk-dialog', role: opts.danger ? 'alertdialog' : 'dialog', 'aria-modal': 'true',
        'aria-labelledby': 'nt-desk-dlg-title', 'aria-describedby': opts.sub ? 'nt-desk-dlg-sub' : null }, kids);
      var scrim = mk('div', { cls: 'nt-desk-scrim', 'data-nt-desk': 'dialog' }, [panel]);
      var self = { scrim: scrim, opener: opener, busy: false };

      function isOpen() { return dialog === self; }

      // aria-disabled instead of disabled: the focused button keeps focus while busy.
      function setBusy(on) {
        self.busy = on;
        ok.textContent = on ? opts.busyText : opts.submit;
        ok.setAttribute('aria-disabled', on ? 'true' : 'false');
        ok.setAttribute('aria-busy', on ? 'true' : 'false');
        if (input) input.readOnly = on;
        if (box) box.setAttribute('aria-disabled', on ? 'true' : 'false');
      }

      function fieldMsg() {
        if (!msg) return;
        var n = input.value.length;
        msg.classList.remove('nt-desk-field-error');
        input.removeAttribute('aria-invalid');
        msg.textContent = n >= CHAT_NAME_COUNTER_AT ? n + '/' + CHAT_NAME_MAX : helpText;
      }

      function showError(text, onField) {
        if (onField && msg) {
          msg.textContent = text;
          msg.classList.add('nt-desk-field-error');
          input.setAttribute('aria-invalid', 'true');
          return;
        }
        banner.textContent = text;
        banner.hidden = false;
      }

      function submit() {
        if (self.busy) return;
        var values = { name: input ? cleanName(input.value) : '', main: !!(box && box.checked) };
        if (input) {
          var err = nameError(values.name);
          if (err) { showError(err, true); input.focus(); return; }
        }
        banner.hidden = true;
        banner.textContent = '';
        setBusy(true);
        Promise.resolve().then(function () { return opts.onSubmit(values, isOpen); }).then(function () {
          if (isOpen()) closeDialog();
        }, function (err) {
          if (!isOpen()) { ctx.toast(failText(err), 'error'); return; }
          setBusy(false);
          showError(failText(err), false);
          try { ok.focus(); } catch (e) { /* best effort */ }
        });
      }

      if (input) input.addEventListener('input', fieldMsg);
      if (box) box.addEventListener('click', function (ev) { if (self.busy) ev.preventDefault(); });
      cancel.addEventListener('click', function (ev) { ev.preventDefault(); closeDialog(); });
      ok.addEventListener('click', function (ev) { ev.preventDefault(); submit(); });
      panel.addEventListener('keydown', function (ev) {
        if (ev.key === 'Escape') {
          ev.preventDefault();
          closeDialog();
        } else if (ev.key === 'Enter' && (ev.target === input || ev.target === box)) {
          ev.preventDefault();
          submit();
        } else if (ev.key === 'Tab') {
          // Focus trap: name -> Main -> Cancel -> Create -> name.
          var ring = [input, box, cancel, ok].filter(Boolean);
          var at = ring.indexOf(document.activeElement);
          var to = ev.shiftKey ? (at <= 0 ? ring.length - 1 : at - 1) : (at < 0 || at === ring.length - 1 ? 0 : at + 1);
          ev.preventDefault();
          ring[to].focus();
        }
      });
      scrim.addEventListener('mousedown', function (ev) { if (ev.target === scrim && !self.busy) closeDialog(); });
      guard(scrim, ['keydown', 'keyup', 'keypress', 'input', 'beforeinput', 'paste', 'copy', 'cut',
        'pointerdown', 'mousedown', 'mouseup', 'pointerup', 'click', 'wheel']);
      document.body.appendChild(scrim);
      dialog = self;
      var first = input || (opts.danger ? cancel : ok);
      try {
        first.focus();
        if (input && input.value) input.select();
      } catch (e) { /* best effort */ }
      return self;
    }

    function openCreate(opener) {
      // Generated once per dialog: every Create click of this dialog sends the same request_id.
      var requestId = newRequestId();
      openDialog({ title: 'New chat', sub: 'Creates a fresh Notion chat with this name.', nameLabel: 'Chat name',
        placeholder: 'e.g. Tax return 2026', check: { label: 'Main', help: 'Main chats can delegate work to coworkers' },
        submit: 'Create', busyText: 'Creating chat...', opener: opener,
        onSubmit: function (values, isOpen) {
          return ctx.call('app_chat_create', createArgs(values.name, values.main, requestId)).then(function (result) {
            result = checkResult(result);
            var key = threadKey(result.thread_id || '');
            schedule();
            if (!key) {
              ctx.toast('Chat "' + values.name + '" is being created. It appears in the sidebar when it is ready.', 'info');
              return;
            }
            if (isOpen()) openChat(key, result.url);
            else ctx.toast('Chat "' + values.name + '" created', 'info');
          });
        } });
    }

    // -- opening a chat without a reload
    function clickRow(key) {
      if (!key) return false;
      var rows = document.querySelectorAll('[data-nt-desk="chat-row"]');
      // Safety rule: desktop JS never clicks chat rows itself; it only highlights the new row.
      for (var i = 0; i < rows.length; i++) {
        if (rows[i].getAttribute('data-nt-desk-thread') === key) {
          try { rows[i].scrollIntoView({ block: 'nearest' }); } catch (e) {}
          ctx.toast('The chat is ready. Click it in the sidebar to open it.', 'info');
          return true;
        }
      }
      return false;
    }

    function softNavigate(url) {
      // Safety rule: no page loads or history changes from desktop JS.
      ctx.toast('The chat is ready. Open it from the sidebar.', 'info');
    }

    function openChat(key, url) {
      if (clickRow(key)) return;
      pendingOpen = { key: key, url: String(url || ''), until: Date.now() + OPEN_WAIT_MS };
      schedule();
    }

    // The new row shows up in Notion's list a little later: retry every second for up to 15 s.
    function applyPendingOpen() {
      if (!pendingOpen) return;
      if (clickRow(pendingOpen.key)) { pendingOpen = null; return; }
      if (Date.now() < pendingOpen.until) {
        if (!pendingTimer) pendingTimer = setTimeout(function () { pendingTimer = 0; schedule(); }, 1000);
        return;
      }
      var url = pendingOpen.url;
      pendingOpen = null;
      if (url) softNavigate(url);
      else ctx.toast('The chat is ready. Open it from the sidebar.', 'info');
    }

    // -- our New chat button in the MAINS header (Notion's own New chat stays as it is)
    function applyNewChatButton() {
      var heading = document.querySelector('[data-nt-desk="heading"]');
      if (!heading || heading.querySelector('[data-nt-desk="newchat-open"]')) return;
      var btn = mk('button', { cls: 'nt-desk-icon-btn nt-desk-newchat-open', 'data-nt-desk': 'newchat-open', type: 'button',
        'aria-label': 'New chat', title: 'New chat: choose a name', 'aria-haspopup': 'dialog' },
        [mk('span', { cls: 'nt-desk-plus', 'aria-hidden': 'true', text: PLUS_GLYPH })]);
      guard(btn, ['pointerdown', 'mousedown', 'mouseup', 'pointerup', 'keydown']);
      btn.addEventListener('click', function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        openCreate(btn);
      });
      heading.appendChild(btn);
    }

    // -- Chats section (private chats, purpose 'private'), between Mains and Coworkers
    function chatsCollapsed() {
      try { return window.localStorage.getItem(CHATS_KEY) === '1'; } catch (e) { return false; }
    }

    function setChatsCollapsed(value) {
      try { window.localStorage.setItem(CHATS_KEY, value ? '1' : '0'); } catch (e) { /* not persisted */ }
    }

    function privateRow(row) {
      var node = mk('div', { cls: 'nt-desk-chat', 'data-nt-desk': 'private-chat', 'data-nt-desk-thread': row.thread,
        role: 'link', tabindex: '0', title: row.name }, [
        mk('span', { cls: 'nt-desk-chat-icon', 'aria-hidden': 'true' }),
        mk('span', { cls: 'nt-desk-chat-name', text: row.name }),
        gearButton(row)
      ]);
      function go(ev) {
        ev.preventDefault();
        ev.stopPropagation();
        if (!clickRow(row.thread) && row.url) softNavigate(row.url);
      }
      node.addEventListener('click', go);
      node.addEventListener('keydown', function (ev) {
        if (ev.target === node && (ev.key === 'Enter' || ev.key === ' ')) go(ev);
      });
      return node;
    }

    function applyChats(state, root) {
      var section = document.querySelector('[data-nt-desk="chats"]');
      var anchor = sectionAnchor(root);
      if (!anchor) {
        if (section) dropOwn(section);
        return;
      }
      if (section && section.previousSibling !== anchor) { dropOwn(section); section = null; }
      if (!section) {
        section = mk('div', { cls: 'nt-desk-chats', 'data-nt-desk': 'chats' });
        anchor.parentNode.insertBefore(section, anchor.nextSibling);
      }
      var collapsed = chatsCollapsed();
      section.classList.toggle('nt-desk-collapsed', collapsed);
      // Chats that Notion already lists itself (own chats stay visible there) are not shown twice.
      var rows = privateChatRows(state).filter(function (r) {
        return !document.querySelector('[data-nt-desk="chat-row"][data-nt-desk-thread="' + r.thread + '"]');
      });
      var sig = JSON.stringify(rows);
      if (section.getAttribute('data-nt-desk-sig') === sig) return;
      if (menu && section.contains(menu.button)) closeMenu(false);
      section.setAttribute('data-nt-desk-sig', sig);
      Array.prototype.slice.call(section.childNodes).forEach(dropOwn);
      var head = mk('button', { cls: 'nt-desk-section-head', 'data-nt-desk': 'chats-head', type: 'button',
        'aria-expanded': String(!collapsed), 'aria-controls': 'nt-desk-chats-list' }, [
        mk('span', { cls: 'nt-desk-chevron', 'aria-hidden': 'true' }),
        mk('span', { cls: 'nt-desk-coworkers-title', text: 'Chats' }),
        mk('span', { cls: 'nt-desk-chip nt-desk-count', text: String(rows.length) })
      ]);
      head.addEventListener('click', function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        var next = !chatsCollapsed();
        setChatsCollapsed(next);
        section.classList.toggle('nt-desk-collapsed', next);
        head.setAttribute('aria-expanded', String(!next));
      });
      var body = mk('div', { cls: 'nt-desk-chat-list', id: 'nt-desk-chats-list', 'data-nt-desk': 'chats-list' });
      if (!rows.length) body.appendChild(mk('div', { cls: 'nt-desk-chat-empty', text: 'No private chats' }));
      rows.forEach(function (row) { body.appendChild(privateRow(row)); });
      section.appendChild(head);
      section.appendChild(body);
    }

    // -- gear button and settings menu
    function gearButton(info) {
      var btn = mk('button', { cls: 'nt-desk-gear', 'data-nt-desk': 'gear', type: 'button',
        'aria-label': 'Chat settings for ' + info.name, title: 'Chat settings', 'aria-haspopup': 'menu',
        'aria-expanded': 'false' }, [mk('span', { cls: 'nt-desk-gear-icon', 'aria-hidden': 'true', text: GEAR_GLYPH })]);
      btn.__ntChat = info;
      guard(btn, ['pointerdown', 'mousedown', 'mouseup', 'pointerup']);
      btn.addEventListener('click', function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        if (menu && menu.button === btn) closeMenu();
        else openMenu(btn);
      });
      btn.addEventListener('keydown', function (ev) {
        ev.stopPropagation();
        if (ev.key === 'Enter' || ev.key === ' ' || ev.key === 'ArrowDown') {
          ev.preventDefault();
          openMenu(btn);
        }
      });
      return btn;
    }

    // Main rows get a gear (Rename, Remove Main rights); other rows lose a stale one.
    function styleGear(row, main) {
      var gear = null;
      for (var i = 0; i < row.children.length; i++) {
        if (row.children[i].getAttribute('data-nt-desk') === 'gear') gear = row.children[i];
      }
      if (!main) {
        if (gear) {
          if (menu && menu.button === gear) closeMenu(false);
          dropOwn(gear);
        }
        return;
      }
      var info = mainChatInfo(main);
      if (!gear) {
        gear = gearButton(info);
        row.appendChild(gear);
      } else {
        gear.__ntChat = info;
      }
      var label = 'Chat settings for ' + info.name;
      if (gear.getAttribute('aria-label') !== label) gear.setAttribute('aria-label', label);
    }

    function closeMenu(restore) {
      if (!menu) return;
      var m = menu;
      menu = null;
      document.removeEventListener('mousedown', m.outside, true);
      window.removeEventListener('resize', m.dismiss);
      window.removeEventListener('blur', m.dismiss);
      window.removeEventListener('scroll', m.dismiss, true);
      m.button.setAttribute('aria-expanded', 'false');
      dropOwn(m.node);
      if (restore !== false && document.contains(m.button)) {
        try { m.button.focus(); } catch (e) { /* best effort */ }
      }
    }

    function openMenu(btn) {
      closeMenu(false);
      var info = btn.__ntChat || {};
      var node = mk('div', { cls: 'nt-desk-menu', role: 'menu', 'data-nt-desk': 'chat-menu', 'aria-label': 'Chat settings' });
      var items = chatMenuItems(info).map(function (item) {
        var b = mk('button', { cls: 'nt-desk-menu-item' + (item.danger ? ' nt-desk-menu-danger' : ''), type: 'button',
          role: 'menuitem', tabindex: '-1', text: item.label, title: item.title || null,
          'aria-disabled': item.disabled ? 'true' : null });
        b.addEventListener('click', function (ev) {
          ev.preventDefault();
          ev.stopPropagation();
          if (item.disabled) return;
          closeMenu(false);
          runMenuItem(item.id, info, btn);
        });
        node.appendChild(b);
        return b;
      });
      node.addEventListener('keydown', function (ev) {
        var at = items.indexOf(document.activeElement);
        var n = items.length;
        if (ev.key === 'ArrowDown') { ev.preventDefault(); items[(at + 1) % n].focus(); }
        else if (ev.key === 'ArrowUp') { ev.preventDefault(); items[(at - 1 + n) % n].focus(); }
        else if (ev.key === 'Home') { ev.preventDefault(); items[0].focus(); }
        else if (ev.key === 'End') { ev.preventDefault(); items[n - 1].focus(); }
        else if (ev.key === 'Escape' || ev.key === 'Tab') { ev.preventDefault(); closeMenu(); }
      });
      guard(node, ['keydown', 'keyup', 'pointerdown', 'mousedown', 'mouseup', 'pointerup', 'click']);
      document.body.appendChild(node);
      var r = btn.getBoundingClientRect();
      var top = r.bottom + 4;
      if (top + node.offsetHeight > window.innerHeight - 8) top = Math.max(8, r.top - node.offsetHeight - 4);
      node.style.top = Math.round(top) + 'px';
      node.style.left = Math.round(Math.max(8, Math.min(r.left, window.innerWidth - node.offsetWidth - 8))) + 'px';
      var dismiss = function () { closeMenu(false); };
      var outside = function (ev) { if (!node.contains(ev.target) && !btn.contains(ev.target)) closeMenu(false); };
      document.addEventListener('mousedown', outside, true);
      window.addEventListener('resize', dismiss);
      window.addEventListener('blur', dismiss);
      window.addEventListener('scroll', dismiss, true);
      menu = { node: node, button: btn, outside: outside, dismiss: dismiss };
      btn.setAttribute('aria-expanded', 'true');
      var enabled = items.filter(function (b) { return b.getAttribute('aria-disabled') !== 'true'; });
      (enabled[0] || items[0]).focus();
    }

    function promote(info, main) {
      var key = String(info.thread_id || info.chat_id || '');
      if (promoting[key]) return Promise.resolve();
      promoting[key] = true;
      return Promise.resolve().then(function () {
        return ctx.call('app_chat_promote', promoteArgs(info, main));
      }).then(function (result) {
        checkResult(result);
        schedule();
        ctx.toast(main ? '"' + info.name + '" is now a Main' : '"' + info.name + '" is now a private chat', 'info');
      }).then(function () { delete promoting[key]; }, function (err) { delete promoting[key]; throw err; });
    }

    function runMenuItem(id, info, btn) {
      if (id === 'rename') {
        openDialog({ title: 'Rename chat', nameLabel: 'Chat name', name: info.name, submit: 'Rename',
          busyText: 'Renaming...', opener: btn,
          onSubmit: function (values) {
            return ctx.call('app_chat_rename', renameArgs(info, values.name)).then(function (result) {
              checkResult(result);
              schedule();
              ctx.toast('Chat renamed to "' + values.name + '"', 'info');
            });
          } });
      } else if (id === 'promote') {
        ctx.toast('Making "' + info.name + '" a Main...', 'info');
        promote(info, true).then(null, function (err) {
          ctx.toast('Could not make "' + info.name + '" a Main: ' + failText(err), 'error');
        });
      } else if (id === 'demote') {
        // Confirm first: removing Main rights stops delegation to coworkers.
        openDialog({ title: 'Remove Main rights?', sub: '"' + info.name + '" becomes a private chat and can no longer'
          + ' delegate work to coworkers. You can make it a Main again later.', submit: 'Remove Main rights',
          busyText: 'Removing...', danger: true, opener: btn,
          onSubmit: function () { return promote(info, false); } });
      }
    }

    // ---- New chat control ----------------------------------------------------------------
    function newChatControl(target) {
      var node = target && target.closest ? target.closest(CONTROL_SELECTOR) : null;
      if (!node || !node.closest(SIDEBAR_SELECTOR)) return null;
      if (node.closest('[data-nt-desk-own]')) return null;  // our own buttons are never Notion's New chat
      if (node.getAttribute('data-nt-desk') === 'new-chat') return node;
      return isNewChatLabel(visibleLabel(node)) ? node : null;
    }

    function markNewChat() {
      var nodes = document.querySelectorAll(CONTROL_SELECTOR);
      for (var i = 0; i < nodes.length; i++) {
        var node = nodes[i];
        if (node.getAttribute('data-nt-desk') === 'new-chat') continue;
        if (!node.closest(SIDEBAR_SELECTOR) || node.closest('[data-nt-desk-own]')) continue;
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

    // ---- jump to the newest message after switching chats ------------------------------------
    // Notion opens a chat at the top of its history. After the chat in the URL changes, scroll the
    // chat's message list to the bottom (view scroll only: no clicks, no navigation, no history).
    // Retries while the messages load; stops at once when the user scrolls or presses a key.
    var SCROLL_WAIT_MS = 10000;
    var SCROLL_MIN_MS = 3000;  // loading state can look stable; keep pushing at least this long
    var lastChatKey = '';
    var pendingScroll = null;
    var scrollTimer = 0;

    var cachedScroller = null;
    var lastFindAt = 0;
    var lastFindHref = '';

    function chatScroller() {
      if (cachedScroller && cachedScroller.isConnected && cachedScroller.getClientRects().length &&
          cachedScroller.scrollHeight - cachedScroller.clientHeight >= 80) return cachedScroller;
      // findScroller walks every div with getComputedStyle (profiled: ~600 ms per 9 s while a reply
      // streams). Search at most every 2 s for the same chat; a chat switch searches immediately.
      var href = location.href;
      if (href === lastFindHref && Date.now() - lastFindAt < 2000) return cachedScroller;
      lastFindHref = href;
      lastFindAt = Date.now();
      cachedScroller = findScroller();
      return cachedScroller;
    }

    // ---- render only the visible chat messages ----------------------------------------------
    // Notion keeps every message of a chat in the DOM (live: 76 rows, 6400+ nodes, 38k px) and
    // paints all of them on every scroll frame. Marking the row list lets our CSS give each row
    // content-visibility:auto: off-screen messages are skipped by the browser (layout + paint)
    // and rendered again the moment they scroll into view. Attribute only; Notion's DOM is untouched.
    function messageList(scroller) {
      var c = scroller;
      for (var d = 0; d < 14 && c; d++) {
        var total = c.getElementsByTagName('*').length, big = null, most = -1;
        for (var k = 0; k < c.children.length; k++) {
          var n = c.children[k].getElementsByTagName('*').length;
          if (n > most) { most = n; big = c.children[k]; }
        }
        if (c.children.length >= 6 && most < total * 0.5) return c;
        c = big;
      }
      return null;
    }

    function applyRenderSkip() {
      var scroller = chatScroller();
      if (!scroller) return;
      var marked = scroller.querySelector('[data-nt-cv]');
      if (!marked) {
        var listNode = messageList(scroller);
        if (listNode && listNode.children.length >= 6) { listNode.setAttribute('data-nt-cv', '1'); marked = listNode; }
      }
      if (!marked) return;
      // Tail mode: only the newest TAIL_ROWS messages take part in style/layout (profiled: Notion's
      // style+layout of a long chat was ~25% of the main thread while a reply streamed, 3.4 s on a
      // switch to an 86-row chat). Scrolling up to the top shows the full history again.
      var key = threadFromHref(location.href) || '';
      if (tailKey !== key || tailList !== marked) {
        tailKey = key;
        tailList = marked;
        if (marked.children.length > TAIL_ROWS) marked.setAttribute('data-nt-tl2', '1');
      }
      var tv = marked.getAttribute('data-nt-tl2');
      if (tv && ['8', '20', '40', '80'].indexOf(tv) < 0) marked.setAttribute('data-nt-tl2', '8');  // stale value from an older build
      bindTail(scroller);
    }

    var TAIL_ROWS = 8;
    var tailKey = null;
    var tailList = null;

    var lastSwitchDone = 0;
    function expandTail(scroller) {
      var list = scroller.querySelector('[data-nt-tl2]');
      if (!list) return;
      var before = scroller.scrollHeight;
      var top = scroller.scrollTop;
      // Reveal in steps (8 -> 20 -> 40 -> 80 -> all): showing ~90 rows at once froze the page for
      // up to 9 s (measured), each step only lays out the next batch.
      var cur = parseInt(list.getAttribute('data-nt-tl2'), 10) || 8;
      var next = cur < 20 ? 20 : cur < 40 ? 40 : cur < 80 ? 80 : 0;
      if (next && list.children.length > next) list.setAttribute('data-nt-tl2', String(next));
      else list.removeAttribute('data-nt-tl2');
      scroller.scrollTop = top + (scroller.scrollHeight - before);  // keep the visible message in place
    }

    function bindTail(scroller) {
      if (scroller.__ntTailBound2) return;
      scroller.__ntTailBound2 = true;
      // Expand only on a real upward user gesture (wheel up / scrollbar drag), never while or right
      // after the switch scroll-to-bottom runs: programmatic scrolls during mount revealed rows and
      // jumped the view up, then the scroll-to-bottom pulled it down again (wobble).
      function userUp() {
        return Date.now() < (scroller.__ntUserUp || 0) && !pendingScroll && Date.now() - lastSwitchDone > 1200;
      }
      scroller.addEventListener('mousedown', function () { scroller.__ntUserUp = Date.now() + 4000; }, { passive: true });
      scroller.addEventListener('scroll', function () {
        if (scroller.scrollTop < 250 && userUp()) expandTail(scroller);
      }, { passive: true });
      scroller.addEventListener('wheel', function (ev) {
        if (ev.deltaY < 0) scroller.__ntUserUp = Date.now() + 900;
        if (ev.deltaY < 0 && scroller.scrollTop < 250 && userUp()) expandTail(scroller);
      }, { passive: true });
    }

    // The scroller that really holds the visible chat: nearest scrollable ancestor of the newest visible user
    // message. Random jumps to the top after a switch came from picking another big scroller while the new chat
    // was still mounting (it was pushed down, the chat itself stayed at the top).
    function chatScroller() {
      var nodes = document.querySelectorAll('[data-agent-chat-user-step-id]');
      for (var i = nodes.length - 1; i >= 0; i--) {
        var n = nodes[i];
        if (!n.getClientRects().length) continue;  // hidden (previous chat still in the DOM)
        var p = n.parentElement;
        while (p && p !== document.body) {
          if (p.scrollHeight - p.clientHeight > 20 && p.clientHeight >= 200) {
            var oy = '';
            try { oy = window.getComputedStyle(p).overflowY; } catch (e) { oy = ''; }
            if (oy === 'auto' || oy === 'scroll') return p;
          }
          p = p.parentElement;
        }
      }
      return null;
    }

    function findScroller() {
      var own = chatScroller();
      if (own) return own;
      var sidebar = sidebarRoot();
      var best = null;
      var all = document.querySelectorAll('main *, [role="main"] *, .notion-frame *, div');
      for (var i = 0; i < all.length; i++) {
        var el = all[i];
        if (el.scrollHeight - el.clientHeight < 80 || el.clientHeight < 200) continue;
        if (sidebar && (sidebar === el || sidebar.contains(el))) continue;
        if (el.closest && el.closest('[data-nt-desk]')) continue;
        var oy = '';
        try { oy = window.getComputedStyle(el).overflowY; } catch (e) { continue; }
        if (oy !== 'auto' && oy !== 'scroll') continue;
        if (!best || el.clientWidth * el.clientHeight > best.clientWidth * best.clientHeight) best = el;
      }
      return best;
    }

    var SWITCH_HIDE_MAX_MS = 2500;
    function revealChat() {
      var hidden = document.querySelectorAll('[data-nt-sw]');
      for (var i = 0; i < hidden.length; i++) hidden[i].removeAttribute('data-nt-sw');
    }
    function cancelScroll() { pendingScroll = null; revealChat(); }

    function applyChatScroll() {
      var key = threadFromHref(location.href);
      if (key !== lastChatKey) {
        lastChatKey = key;
        pendingScroll = key ? { key: key, start: Date.now(), until: Date.now() + SCROLL_WAIT_MS, height: -1, stable: 0 } : null;
        lastSwitchDone = Date.now();
        revealChat();
      }
      if (!pendingScroll) return;
      if (pendingScroll.key !== key || Date.now() > pendingScroll.until) { pendingScroll = null; revealChat(); return; }
      // Always search fresh after a chat switch: a cached scroller can belong to the previous chat
      // (still in the DOM, hidden) and the new chat then stayed at the top (regression 17d4482).
      var el = findScroller();
      cachedScroller = el;
      if (el) {
        var age = Date.now() - pendingScroll.start;
        if (!pendingScroll.revealed && age < SWITCH_HIDE_MAX_MS) el.setAttribute('data-nt-sw', '1');
        // Notion anchors a few px above the exact bottom; pushing to the exact bottom fought it
        // every tick (measured 4 px flip each 250 ms = the visible wobble).
        if (el.scrollHeight - el.clientHeight - el.scrollTop > 24) el.scrollTop = el.scrollHeight;
        // Only a scroller that already shows the new chat's messages counts as settled.
        var real = !!(el.querySelector && el.querySelector('[data-agent-chat-user-step-id]'));
        if (real && el.scrollHeight === pendingScroll.height) pendingScroll.stable += 1;
        else { pendingScroll.height = el.scrollHeight; pendingScroll.stable = 0; }
        var lg = window.__ntSwLog = window.__ntSwLog || [];
        if (lg.length > 60) lg.splice(0, lg.length - 40);
        lg.push({ k: String(key).slice(-6), t: age, real: real, top: Math.round(el.scrollTop), h: el.scrollHeight, c: el.clientHeight });
        if (!pendingScroll.revealed && ((pendingScroll.stable >= 2 && age >= 500) || age >= SWITCH_HIDE_MAX_MS)) {
          pendingScroll.revealed = true;
          revealChat();
        }
        if (pendingScroll.stable >= 3 && Date.now() - pendingScroll.start >= SCROLL_MIN_MS) { pendingScroll = null; lastSwitchDone = Date.now(); revealChat(); return; }
      }
      if (!scrollTimer) scrollTimer = setTimeout(function () { scrollTimer = 0; try { applyChatScroll(); } catch (e) { pendingScroll = null; revealChat(); } }, 250);
    }

    window.addEventListener('wheel', cancelScroll, { capture: true, passive: true });
    window.addEventListener('touchmove', cancelScroll, { capture: true, passive: true });
    document.addEventListener('keydown', cancelScroll, true);

    // ---- scheduling ----------------------------------------------------------------------
    function run() {
      timer = 0;
      if (stopped || !document.body) return;
      var state = typeof ctx.getState === 'function' ? ctx.getState() : null;
      if (!online(state)) {
        // Harness offline: hide nothing and re-label nothing; undo what an earlier run did.
        if (applied) {
          closeMenu(false);
          closeDialog(false);
          pendingOpen = null;
          resetMarks();
        }
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
        applyNewChatButton();
        chatGroups = [];
        applyClutter(root);
        applyRows(state, root);
        applyChats(state, root);
        applyCoworkers(state, root);
        collapseEmpty(root);
      } catch (e) { /* branding is cosmetic; never break the filter or the New chat routing */ }
      try { applyPendingOpen(); } catch (e) { pendingOpen = null; }
      try { applyChatScroll(); } catch (e) { pendingScroll = null; }
      try { applyRenderSkip(); } catch (e) { /* performance only */ }
    }

    function schedule() {
      if (!stopped && !timer) timer = setTimeout(run, DEBOUNCE_MS);
    }

    document.addEventListener('pointerdown', onPress, true);
    document.addEventListener('mousedown', onPress, true);
    document.addEventListener('click', onClick, true);
    document.addEventListener('keydown', onKey, true);
    observer = new MutationObserver((window.__ntIdle2 || (window.__ntIdle2 = function (fn) { var w = window; if (!w.__ntScrollHook) { w.__ntScrollHook = true; var mark = function () { w.__ntLastScroll = Date.now(); }; w.addEventListener('scroll', mark, { capture: true, passive: true }); w.addEventListener('wheel', mark, { capture: true, passive: true }); w.addEventListener('keydown', function () { w.__ntLastKey = Date.now(); }, { capture: true, passive: true }); } var t = 0, rowT = 0, lastRow = 0, lastArgs = []; var rowOnly = function (recs) { if (!recs || !recs.length) return false; for (var i = 0; i < recs.length; i++) { var n = recs[i] && recs[i].target; if (n && n.nodeType !== 1) n = n.parentElement; if (!n || !n.closest || !n.closest('[data-nt-cv] > div')) return false; } return true; }; return function idleRun() { var self = this; lastArgs = arguments; var wait = Math.max((w.__ntLastScroll || 0) + 400, (w.__ntLastKey || 0) + 1200) - Date.now(); if (wait > 0) { if (!t) t = setTimeout(function () { t = 0; idleRun.apply(self, lastArgs); }, wait + 20); return; } if (rowOnly(arguments[0])) { var due = lastRow + 4000 - Date.now(); if (due > 0) { if (!rowT) rowT = setTimeout(function () { rowT = 0; lastRow = Date.now(); fn.apply(self, lastArgs); }, due); return; } lastRow = Date.now(); } fn.apply(this, arguments); }; }))(schedule));
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
        closeMenu(false);
        closeDialog(false);
        pendingOpen = null;
        if (pendingTimer) clearTimeout(pendingTimer);
        pendingTimer = 0;
        resetMarks();
      }
    };
  };
})();

// Hide Notion's cookie notice ('Notion uses cookies ...') in the NotionTool window (user wish 2026-10-21).
// Only hides it (display:none); never clicks anything in it.
(function () {
  'use strict';
  if (window.__ntCookieHide) return;
  window.__ntCookieHide = true;
  var RX = /^(notion uses cookies|notion verwendet cookies|we use cookies)/i;
  // Notion's overlay layer (.notion-overlay-container) also holds every menu and popup, e.g. the
  // workspace/profile switcher. It must never be hidden, or those menus open invisibly (bug 2026-10-21).
  var OVERLAY = 'notion-overlay-container';
  function isOverlay(node) { return !!(node && node.classList && node.classList.contains(OVERLAY)); }
  function hide() {
    try {
      var layers = document.querySelectorAll('.' + OVERLAY);
      for (var j = 0; j < layers.length; j++) {
        if (layers[j].__ntCookieHidden || layers[j].style.getPropertyValue('display') === 'none') {
          layers[j].style.removeProperty('display');
          layers[j].__ntCookieHidden = false;
        }
      }
      var nodes = document.querySelectorAll('div,section,aside,[role=dialog],[role=region]');
      for (var i = 0; i < nodes.length; i++) {
        var el = nodes[i];
        if (el.__ntCookieHidden) continue;
        var t = (el.innerText || '').trim();
        if (!t || t.length > 600 || !RX.test(t)) continue;
        // Walk up to the fixed/sticky box that holds the notice, and hide that box.
        var box = el, p = el;
        for (var k = 0; k < 8 && p && p !== document.body; k++) {
          if (isOverlay(p)) break;
          var pos = getComputedStyle(p).position;
          if (pos === 'fixed' || pos === 'sticky') { box = p; break; }
          p = p.parentElement;
        }
        if ((box.innerText || '').length > 800) continue;
        // A modal notice (aria-modal dialog in Notion's overlay layer) makes Notion set `inert` on the
        // whole app until it is answered. Hiding it would leave the app unclickable (bug 2026-10-22),
        // so a modal notice stays visible; only non-modal banners are hidden.
        if (el.closest && el.closest('[aria-modal="true"], [role="dialog"], [role="alertdialog"], .' + OVERLAY)) continue;
        if (box.closest && box.closest('[aria-modal="true"], .' + OVERLAY)) continue;
        box.style.setProperty('display', 'none', 'important');
        box.__ntCookieHidden = true;
        el.__ntCookieHidden = true;
      }
    } catch (e) { /* never break the page */ }
  }
  var n = 0;
  // Re-check for 10 minutes (the notice can appear late). Only in the real app window, not in test runners.
  var realApp = typeof navigator !== 'undefined' && /Chrome|Electron/.test(String(navigator.userAgent || ''));
  (function tick() { hide(); if (realApp && ++n < 600) setTimeout(tick, 1000); })();
  if (document.readyState !== 'loading') hide(); else document.addEventListener('DOMContentLoaded', hide);
})();
