/* NotionTool desktop overlay part: chrome (docs/DESKTOP_APP.md, overlay part contract).
 *
 * Hides Notion entries the harness app does not use (Home, Meetings, What's new, Templates,
 * Marketplace, Inbox, Get started, Invite members) by adding the class nt-desk-hidden to the
 * nearest clickable row. Notion nodes are never removed. The account switcher, Settings,
 * Search and Trash are always kept (KEEP wins over HIDE).
 * state.app_hide (optional, id -> bool) turns single entries back on: false = show, default = hidden.
 *
 * Window chrome redesign: the tab title gets a 'NotionTool' prefix (restored on stop), and our own
 * classes mark the top bar, the chat composer, chat messages, code blocks and upsell banners
 * (quota banner above the composer, other upgrade banners). chrome.css styles only those
 * nt-desk- classes; Notion class names are used here for detection only. app_hide.upsell = false
 * shows upsell banners again.
 */
(function () {
  'use strict';
  window.__ntDesk = window.__ntDesk || {};

  // Visible labels, lower case, English and German.
  var HIDE = [
    { id: 'home', labels: ['home', 'startseite'] },
    { id: 'meetings', labels: ['meetings', 'besprechungen'] },
    { id: 'whats_new', labels: ["what's new", 'neuigkeiten', 'neu'] },
    { id: 'templates', labels: ['templates', 'vorlagen'] },
    { id: 'marketplace', labels: ['marketplace', 'marktplatz'] },
    { id: 'inbox', labels: ['inbox', 'posteingang'] },
    { id: 'get_started', labels: ['get started', 'erste schritte'] },
    { id: 'invite', labels: ['invite members', 'mitglieder einladen'] }
  ];
  // Always visible. The account switcher has no fixed label (it shows the workspace name),
  // so it is protected by selector.
  var KEEP = [
    { id: 'settings', labels: ['settings', 'einstellungen', 'settings & members', 'einstellungen & mitglieder'] },
    { id: 'account_switcher', labels: [],
      selector: '[class*="sidebar-switcher"],[aria-label*="switcher" i],[aria-label*="switch workspace" i],' +
        '[aria-label*="workspace wechseln" i],[aria-label*="account" i],[aria-label*="konto" i]' },
    { id: 'search', labels: ['search', 'suche'] },
    { id: 'trash', labels: ['trash', 'papierkorb'] }
  ];
  var ROW = '[role="button"],a,[role="treeitem"]';
  var HIDDEN = 'nt-desk-hidden';
  var DEBOUNCE_MS = 250;
  var MAX_LABEL = 40;

  // ---- window chrome redesign ---------------------------------------------------------
  var TITLE_PREFIX = 'NotionTool \u00b7 ';
  var ROOT_ON = 'nt-desk-chrome-on';
  var TOPBAR = 'nt-desk-topbar';
  var COMPOSER = 'nt-desk-composer';
  var COMPOSER_EMPTY = 'nt-desk-composer-empty';
  var MESSAGE = 'nt-desk-message';
  var CODE = 'nt-desk-code';
  var UPSELL = 'nt-desk-upsell';
  // Detection selectors (Notion DOM); styling is done only through the classes above.
  var TOPBAR_SEL = '.notion-topbar,[data-testid="topbar"]';
  var TEXTBOX_SEL = '[role="textbox"][contenteditable="true"],textarea';
  var PAGE_SEL = '.notion-page-content';
  var CHAT_SEL = '[class*="chat" i],[class*="assistant" i],[data-testid*="chat" i],[data-testid*="assistant" i]';
  var COMPOSER_SEL = '[class*="composer" i],[class*="chat-input" i],[class*="input-container" i],form';
  var COMPOSER_HINT = /ask|message|chat|frag|nachricht|anything|beliebig/i;
  var MESSAGE_SEL = '[data-testid*="message" i],[class*="chat-message" i],[class*="assistant-message" i],' +
    '[data-message-id],[data-turn-id]';
  var CODE_SEL = 'pre,.notion-code-block,[class*="code-block" i]';
  var UPSELL_SEL = '[class*="upsell" i],[class*="upgrade-banner" i],[class*="paywall" i],' +
    '[class*="trial-banner" i],[data-testid*="upsell" i]';
  // Quota / upsell banner texts (lower case substrings, English and German).
  var QUOTA = [
    'verbraucht dein ki-kontingent', 'verbraucht ihr ki-kontingent', 'verbraucht dein ki-guthaben',
    'uses your ai credits faster', 'use your ai credits faster', 'uses more of your ai credits'
  ];
  var BANNER_MAX_TEXT = 400;
  var BANNER_DEPTH = 6;

  function norm(text) {
    return String(text || '').replace(/[\u2018\u2019]/g, "'").replace(/\s+/g, ' ').trim().toLowerCase();
  }

  function labelMap(list) {
    var map = {};
    list.forEach(function (item) {
      item.labels.forEach(function (label) { map[norm(label)] = item.id; });
    });
    return map;
  }

  function isQuota(text) {
    var t = norm(text);
    for (var i = 0; i < QUOTA.length; i++) if (t.indexOf(QUOTA[i]) >= 0) return true;
    return false;
  }

  function all(root, selector) {
    try { return Array.prototype.slice.call((root || document).querySelectorAll(selector)); } catch (err) { return []; }
  }

  function inside(node, selector) {
    try { return !!(node && node.closest && node.closest(selector)); } catch (err) { return false; }
  }

  function textLength(node) { return String((node && node.textContent) || '').length; }

  function hasTextbox(node) { return all(node, TEXTBOX_SEL).length > 0; }

  var HIDE_BY_LABEL = labelMap(HIDE);
  var KEEP_BY_LABEL = labelMap(KEEP);
  var KEEP_SELECTOR = KEEP.filter(function (k) { return k.selector; }).map(function (k) { return k.selector; }).join(',');

  window.__ntDesk.chrome = function (ctx) {
    ctx = ctx || {};
    var hidden = [];      // rows we marked, so they can be shown again and cleaned up on stop
    var found = {};       // HIDE ids seen in the last scan
    var marks = {};       // our style class -> elements currently carrying it
    var quotaHits = [];   // text parents of quota banners from the last scan
    var timer = 0;
    var stopped = false;
    var observer = null;
    var unsubscribe = null;

    function appHide() {
      var state = typeof ctx.getState === 'function' ? ctx.getState() : null;
      var map = state && state.app_hide;
      return map && typeof map === 'object' ? map : {};
    }

    function rowLabel(row) {
      return norm(row.getAttribute('aria-label') || row.innerText || row.textContent);
    }

    function kept(row) {
      if (KEEP_BY_LABEL[rowLabel(row)]) return true;
      if (!KEEP_SELECTOR) return false;
      try {
        return !!(row.matches(KEEP_SELECTOR) || row.closest(KEEP_SELECTOR) || row.querySelector(KEEP_SELECTOR));
      } catch (err) {
        return false;
      }
    }

    // Rows whose exact trimmed visible label (text or aria-label) is on the HIDE list.
    // The same text walk also collects quota banner texts.
    function scan() {
      var hits = [];
      quotaHits = [];
      var body = document.body;
      if (!body) return hits;
      // The chat message list (overlay.js marks it data-nt-cv) holds thousands of text nodes and
      // never contains menu rows or quota banners: skip it, so scans stay cheap while a reply streams.
      var walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT, {
        acceptNode: function (n) {
          if (n.nodeType === 1) return n.hasAttribute('data-nt-cv') ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_SKIP;
          return NodeFilter.FILTER_ACCEPT;
        }
      });
      var node;
      while ((node = walker.nextNode())) {
        var text = node.nodeValue;
        if (!text) continue;
        if (text.length <= BANNER_MAX_TEXT && node.parentElement && isQuota(text)) quotaHits.push(node.parentElement);
        if (text.length > MAX_LABEL * 2) continue;
        var id = HIDE_BY_LABEL[norm(text)];
        if (!id || !node.parentElement) continue;
        var row = node.parentElement.closest(ROW);
        if (row && HIDE_BY_LABEL[rowLabel(row)] === id) hits.push({ id: id, row: row });
      }
      var labelled = body.querySelectorAll('[aria-label]');
      for (var i = 0; i < labelled.length; i++) {
        var el = labelled[i];
        var lid = HIDE_BY_LABEL[norm(el.getAttribute('aria-label'))];
        if (!lid) continue;
        var lrow = el.matches(ROW) ? el : el.closest(ROW);
        if (lrow) hits.push({ id: lid, row: lrow });
      }
      return hits;
    }

    // ---- branding: tab title -----------------------------------------------------------
    function brandTitle() {
      var title = String(document.title || '');
      if (title.indexOf(TITLE_PREFIX) === 0) return;
      document.title = TITLE_PREFIX + (title || 'Notion');
    }

    function restoreTitle() {
      var title = String(document.title || '');
      if (title.indexOf(TITLE_PREFIX) === 0) document.title = title.slice(TITLE_PREFIX.length);
    }

    // ---- style marks -------------------------------------------------------------------
    function setMarks(cls, list) {
      (marks[cls] || []).forEach(function (node) { if (list.indexOf(node) < 0) node.classList.remove(cls); });
      list.forEach(function (node) { node.classList.add(cls); });
      marks[cls] = list;
    }

    function unique(list) {
      var out = [];
      list.forEach(function (node) { if (node && out.indexOf(node) < 0) out.push(node); });
      return out;
    }

    // Smallest safe container around a quota text: climbs while the box stays small and holds
    // no text box, so the composer itself is never hidden.
    function bannerFor(node) {
      var best = null;
      var cur = node;
      for (var depth = 0; cur && depth < BANNER_DEPTH; depth++) {
        if (cur === document.body || cur === document.documentElement) break;
        if (hasTextbox(cur) || textLength(cur) > BANNER_MAX_TEXT) break;
        best = cur;
        cur = cur.parentElement;
      }
      return best;
    }

    function safeBanner(node) {
      return node && node !== document.body && !hasTextbox(node) && textLength(node) <= BANNER_MAX_TEXT && !kept(node);
    }

    function composerBox(box) {
      var named = null;
      try { named = box.closest(COMPOSER_SEL); } catch (err) { named = null; }
      if (named && all(named, TEXTBOX_SEL).length === 1) return named;
      var best = box.parentElement || box;
      var cur = best;
      for (var depth = 0; cur && depth < 4; depth++) {
        var parent = cur.parentElement;
        if (!parent || parent === document.body || all(parent, TEXTBOX_SEL).length !== 1) break;
        if (parent.offsetHeight && parent.offsetHeight > 360) break;
        best = parent;
        cur = parent;
      }
      return best;
    }

    function isChatBox(box) {
      if (inside(box, PAGE_SEL)) return false;
      var hint = (box.getAttribute('aria-label') || '') + ' ' + (box.getAttribute('placeholder') || '') + ' ' +
        (box.getAttribute('data-placeholder') || '');
      return inside(box, CHAT_SEL) || COMPOSER_HINT.test(hint);
    }

    function styleChrome(allow) {
      setMarks(TOPBAR, unique(all(document, TOPBAR_SEL)));
      var boxes = all(document, TEXTBOX_SEL).filter(isChatBox);
      var composers = unique(boxes.map(composerBox));
      setMarks(COMPOSER, composers);
      setMarks(COMPOSER_EMPTY, composers.filter(function (c) {
        var box = all(c, TEXTBOX_SEL)[0];
        return box && !norm(box.value !== undefined && box.tagName === 'TEXTAREA' ? box.value : box.textContent);
      }));
      var messages = unique(all(document, MESSAGE_SEL).filter(function (m) { return !hasTextbox(m); }));
      setMarks(MESSAGE, messages);
      var codes = [];
      messages.forEach(function (m) { codes = codes.concat(all(m, CODE_SEL)); });
      setMarks(CODE, unique(codes));
      var upsell = [];
      if (allow.upsell !== false) {
        upsell = all(document, UPSELL_SEL).filter(safeBanner).concat(quotaHits.map(bannerFor).filter(safeBanner));
      }
      setMarks(UPSELL, unique(upsell));
    }

    function apply() {
      timer = 0;
      if (stopped) return;
      apply.lastRun = Date.now();
      var allow = appHide();
      var want = [];
      found = {};
      scan().forEach(function (hit) {
        found[hit.id] = true;
        if (allow[hit.id] === false || kept(hit.row)) return;
        if (want.indexOf(hit.row) < 0) want.push(hit.row);
      });
      hidden.forEach(function (row) {
        if (want.indexOf(row) < 0) row.classList.remove(HIDDEN);
      });
      want.forEach(function (row) { row.classList.add(HIDDEN); });
      hidden = want;
      try {
        brandTitle();
        styleChrome(allow);
      } catch (err) {
        if (typeof ctx.toast === 'function') ctx.toast('NotionTool chrome styling failed: ' + (err && err.message), 'error');
      }
    }

    function schedule() {
      if (stopped || timer) return;
      timer = setTimeout(apply, Math.max(DEBOUNCE_MS, (apply.lastRun || 0) + 3000 - Date.now()));
    }

    if (document.documentElement) document.documentElement.classList.add(ROOT_ON);
    if (typeof MutationObserver === 'function' && document.documentElement) {
      // childList/characterData only: our own class changes do not trigger it. Title text
      // changes (Notion navigation) arrive here too, so the branding is re-applied.
      observer = new MutationObserver((window.__ntIdle2 || (window.__ntIdle2 = function (fn) { var w = window; if (!w.__ntScrollHook) { w.__ntScrollHook = true; var mark = function () { w.__ntLastScroll = Date.now(); }; w.addEventListener('scroll', mark, { capture: true, passive: true }); w.addEventListener('wheel', mark, { capture: true, passive: true }); w.addEventListener('keydown', function () { w.__ntLastKey = Date.now(); }, { capture: true, passive: true }); } var t = 0, rowT = 0, lastRow = 0, lastArgs = []; var rowOnly = function (recs) { if (!recs || !recs.length) return false; for (var i = 0; i < recs.length; i++) { var n = recs[i] && recs[i].target; if (n && n.nodeType !== 1) n = n.parentElement; if (!n || !n.closest || !n.closest('[data-nt-cv] > div')) return false; } return true; }; return function idleRun() { var self = this; lastArgs = arguments; var wait = Math.max((w.__ntLastScroll || 0) + 400, (w.__ntLastKey || 0) + 1200) - Date.now(); if (wait > 0) { if (!t) t = setTimeout(function () { t = 0; idleRun.apply(self, lastArgs); }, wait + 20); return; } if (rowOnly(arguments[0])) { var due = lastRow + 4000 - Date.now(); if (due > 0) { if (!rowT) rowT = setTimeout(function () { rowT = 0; lastRow = Date.now(); fn.apply(self, lastArgs); }, due); return; } lastRow = Date.now(); } fn.apply(this, arguments); }; }))(schedule));
      observer.observe(document.documentElement, { childList: true, subtree: true, characterData: true });
    }
    if (typeof ctx.onState === 'function') unsubscribe = ctx.onState(schedule);
    apply();

    return {
      stop: function () {
        stopped = true;
        if (timer) clearTimeout(timer);
        timer = 0;
        if (observer) observer.disconnect();
        observer = null;
        if (typeof unsubscribe === 'function') unsubscribe();
        unsubscribe = null;
        hidden.forEach(function (row) { row.classList.remove(HIDDEN); });
        hidden = [];
        Object.keys(marks).forEach(function (cls) { setMarks(cls, []); });
        if (document.documentElement) document.documentElement.classList.remove(ROOT_ON);
        restoreTitle();
      },
      selfcheck: function () {
        if (!stopped) apply();
        var ids = HIDE.map(function (item) { return item.id; });
        return {
          found: ids.filter(function (id) { return found[id]; }),
          missing: ids.filter(function (id) { return !found[id]; }),
          styled: {
            topbar: (marks[TOPBAR] || []).length,
            composer: (marks[COMPOSER] || []).length,
            message: (marks[MESSAGE] || []).length,
            code: (marks[CODE] || []).length,
            upsell: (marks[UPSELL] || []).length
          },
          title: String(document.title || '').indexOf(TITLE_PREFIX) === 0
        };
      }
    };
  };
})();
