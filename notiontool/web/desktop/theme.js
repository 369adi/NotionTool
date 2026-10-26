/* NotionTool desktop overlay part: theme (docs/DESKTOP_APP.md, overlay part contract).
 *
 * Turns on the NotionTool design system (theme.css) by adding the root class 'nt-theme' to
 * <html>. All theme rules are scoped to html.nt-theme, so removing the class switches the
 * theme off completely. Notion rewrites the root classes now and then (light/dark switch);
 * one MutationObserver on the root class attribute puts the class back (small debounce).
 * Studio v2: a throttled scan only toggles marker classes on Notion nodes (never adds, moves or
 * removes them): the top bar breadcrumb 'Notion AI' (shown as 'NotionTool' via CSS ::after, the
 * original text is hidden visually only), the chat title next to it and the 'N Schritte' step
 * labels in the chat (styled as pills).
 * Tool groups: runs of TOOL_MIN_GROUP or more consecutive tool-call rows ('NotionTool / nt_shell')
 * of one answer are folded into one compact line 'N tool steps' (CSS ::before on the first row,
 * the other rows are hidden by class only). A click on that line toggles the group; clicks on the
 * tool rows themselves go to Notion untouched, so Notion's own expand keeps working. The scan runs
 * again after every Notion re-render, the open state is kept per container.
 * stop() removes every marker class and data attribute again.
 * Never removes or moves Notion nodes, no network, no timers besides the debounce.
 */
(function () {
  'use strict';
  window.__ntDesk = window.__ntDesk || {};

  var ROOT_CLASS = 'nt-theme';
  var DEBOUNCE_MS = 50;
  var SCAN_MS = 400;            // marker scan throttle (DOM mutations)
  var HEADER_MAX_Y = 60;        // the Notion top bar is 44px high (live DOM)
  var SCAN_LIMIT = 20000;       // max text nodes visited per chat scan
  var CRUMB_TEXT = 'Notion AI';
  var STEPS_RE = /^\d+\s+(Schritte?|steps?)$/i;
  // Tool-call row label (live DOM): icon letter + 'NotionTool / nt_shell'.
  var TOOL_RE = /^.{0,3}[^\/\n]{1,40} \/ [\w.-]{1,60}$/;
  var TOOL_MIN_GROUP = 3;
  var TOOL_MAX_DEPTH = 6;       // max levels from the tool button up to its row item
  var TOOL_HEAD_PX = 30;        // height of the 'N tool steps' line on an open group
  var COUNT_ATTR = 'data-nt-tool-count';
  var GROUP_HEAD = 'nt-desk-toolgroup-head';
  var GROUP_MEMBER = 'nt-desk-toolgroup-member';
  var GROUP_OPEN = 'nt-desk-toolgroup-open';
  var MARKS = ['nt-desk-crumb-host', 'nt-desk-crumb-src', 'nt-desk-chat-title', 'nt-desk-steps',
    GROUP_HEAD, GROUP_MEMBER, GROUP_OPEN];

  function isStepsLabel(text) { return STEPS_RE.test(String(text || '').trim()); }
  function isToolLabel(text) { return TOOL_RE.test(String(text || '').replace(/\s+/g, ' ').trim()); }
  // Pure helper (tested): split a list of booleans into runs [start, length] of true values.
  function toolRuns(flags, min) {
    var runs = [], start = -1;
    for (var i = 0; i <= flags.length; i++) {
      if (i < flags.length && flags[i]) { if (start < 0) start = i; continue; }
      if (start >= 0 && i - start >= (min || TOOL_MIN_GROUP)) runs.push([start, i - start]);
      start = -1;
    }
    return runs;
  }
  window.__ntDesk.themeUtil = { isStepsLabel: isStepsLabel, isToolLabel: isToolLabel, toolRuns: toolRuns,
    CRUMB_TEXT: CRUMB_TEXT, MARKS: MARKS, TOOL_MIN_GROUP: TOOL_MIN_GROUP };

  window.__ntDesk.theme = function (ctx) {
    ctx = ctx || {};
    var root = document.documentElement;
    var stopped = false;
    var timer = 0;
    var observer = null;
    var scanTimer = 0;
    var bodyObserver = null;
    var clickBound = false;
    // Open tool groups per container: container -> { headIndex: true }. Survives re-renders of rows.
    var openGroups = typeof WeakMap === 'function' ? new WeakMap() : null;

    function mark(node, cls) {
      if (node && node.classList && !node.classList.contains(cls)) node.classList.add(cls);
    }
    function unmarkNode(node, cls) {
      if (node && node.classList && node.classList.contains(cls)) node.classList.remove(cls);
    }
    function roleButton(node) {
      while (node && node !== document.body) {
        if (node.getAttribute && node.getAttribute('role') === 'button') return node;
        node = node.parentElement;
      }
      return null;
    }
    // Top bar: walk only nodes in the first HEADER_MAX_Y px (subtrees further down are rejected).
    function scanHeader(content) {
      var walker = document.createTreeWalker(content, 5, { acceptNode: function (n) {
        if (n.nodeType === 1) {
          var r = n.getBoundingClientRect();
          return r.height > 0 && r.top > HEADER_MAX_Y ? 2 : 3;
        }
        return n.nodeValue && n.nodeValue.trim() === CRUMB_TEXT ? 1 : 3;
      } });
      var text = walker.nextNode();
      if (!text || !text.parentElement) return;
      var host = roleButton(text.parentElement);
      if (!host || host.getBoundingClientRect().top > HEADER_MAX_Y) return;
      mark(text.parentElement, 'nt-desk-crumb-src');
      mark(host, 'nt-desk-crumb-host');
      var row = host.parentElement ? host.parentElement.children : [];
      for (var i = 0; i < row.length; i++) {
        var item = row[i];
        if (item === host || !item.getAttribute) continue;
        // Live DOM: the chat title button sits in a wrapper div next to the crumb button.
        var btn = item.getAttribute('role') === 'button' ? item : item.querySelector('[role="button"]');
        var label = btn ? (btn.textContent || '').trim() : '';
        if (label && label !== '/' && label !== CRUMB_TEXT) mark(btn, 'nt-desk-chat-title');
      }
    }
    function scanSteps(chat) {
      var walker = document.createTreeWalker(chat, 4, null);
      var n, seen = 0;
      while ((n = walker.nextNode()) && seen < SCAN_LIMIT) {
        seen += 1;
        if (n.nodeValue && n.nodeValue.length < 24 && isStepsLabel(n.nodeValue)) mark(n.parentElement, 'nt-desk-steps');
      }
    }

    // ---- tool groups --------------------------------------------------------------------
    function toolButtonsIn(node) {
      var out = [];
      var list = node.querySelectorAll('[role="button"][aria-expanded]');
      for (var i = 0; i < list.length; i++) {
        var t = list[i].textContent || '';
        if (t.length < 120 && isToolLabel(t)) out.push(list[i]);
      }
      return out;
    }
    // A tool row item holds exactly one tool button and sits in a container (one answer) without
    // user messages. Live DOM: the button is 3-4 levels below the item.
    function isToolItem(node) {
      return !!(node && node.querySelectorAll && toolButtonsIn(node).length === 1);
    }
    function toolItem(button) {
      var node = button;
      for (var depth = 0; depth < TOOL_MAX_DEPTH && node && node.parentElement; depth++) {
        var parent = node.parentElement;
        if (parent.querySelector('[data-agent-chat-user-step-id]')) return null;
        if (isToolItem(node.previousElementSibling) || isToolItem(node.nextElementSibling)) {
          return toolButtonsIn(node).length === 1 ? node : null;
        }
        node = parent;
      }
      return null;
    }
    function openSet(parent, create) {
      if (!openGroups) return null;
      var set = openGroups.get(parent);
      if (!set && create) { set = {}; openGroups.set(parent, set); }
      return set || null;
    }
    function clearGroupMarks(node) {
      unmarkNode(node, GROUP_HEAD);
      unmarkNode(node, GROUP_MEMBER);
      unmarkNode(node, GROUP_OPEN);
      if (node.getAttribute && node.getAttribute(COUNT_ATTR) !== null) node.removeAttribute(COUNT_ATTR);
    }
    function groupParent(parent) {
      var kids = parent.children, flags = [];
      for (var i = 0; i < kids.length; i++) flags.push(isToolItem(kids[i]));
      var runs = toolRuns(flags, TOOL_MIN_GROUP);
      var inRun = {};
      var open = openSet(parent, false) || {};
      runs.forEach(function (run) {
        var start = run[0], len = run[1] - 1;  // the last row of the run stays visible (not folded)
        if (len < TOOL_MIN_GROUP - 1) return;
        var isOpen = !!open[start];
        for (var k = start; k < start + len; k++) {
          inRun[k] = true;
          var node = kids[k];
          if (k === start) {
            mark(node, GROUP_HEAD);
            unmarkNode(node, GROUP_MEMBER);
            if (node.getAttribute(COUNT_ATTR) !== String(len)) node.setAttribute(COUNT_ATTR, String(len));
          } else {
            mark(node, GROUP_MEMBER);
            unmarkNode(node, GROUP_HEAD);
            if (node.getAttribute(COUNT_ATTR) !== null) node.removeAttribute(COUNT_ATTR);
          }
          if (isOpen) mark(node, GROUP_OPEN); else unmarkNode(node, GROUP_OPEN);
        }
      });
      for (var j = 0; j < kids.length; j++) if (!inRun[j]) clearGroupMarks(kids[j]);
    }
    function scanToolGroups(chat) {
      var buttons = toolButtonsIn(chat);
      var parents = [];
      for (var i = 0; i < buttons.length; i++) {
        var item = toolItem(buttons[i]);
        if (item && item.parentElement && parents.indexOf(item.parentElement) < 0) parents.push(item.parentElement);
      }
      parents.forEach(groupParent);
      // Stale marks (a container that no longer qualifies) are cleared too.
      var stale = chat.querySelectorAll('.' + GROUP_HEAD + ', .' + GROUP_MEMBER);
      for (var s = 0; s < stale.length; s++) {
        if (parents.indexOf(stale[s].parentElement) < 0) clearGroupMarks(stale[s]);
      }
    }
    function toggleGroup(head) {
      var parent = head.parentElement;
      if (!parent) return;
      var index = Array.prototype.indexOf.call(parent.children, head);
      var next = !head.classList.contains(GROUP_OPEN);
      var set = openSet(parent, true);
      if (set) { if (next) set[index] = true; else delete set[index]; }
      var node = head;
      while (node && (node === head || node.classList.contains(GROUP_MEMBER))) {
        if (next) mark(node, GROUP_OPEN); else unmarkNode(node, GROUP_OPEN);
        node = node.nextElementSibling;
      }
    }
    // Only clicks on the 'N tool steps' line itself (target is the head item, i.e. its ::before
    // or, when closed, the whole folded line) are handled; every other click reaches Notion.
    function onClick(ev) {
      if (stopped || !root.classList.contains(ROOT_CLASS)) return;
      var head = ev.target;
      if (!head || !head.classList || !head.classList.contains(GROUP_HEAD)) return;
      if (head.classList.contains(GROUP_OPEN) && typeof ev.offsetY === 'number' && ev.offsetY > TOOL_HEAD_PX) return;
      ev.preventDefault();
      ev.stopPropagation();
      try { toggleGroup(head); } catch (err) { /* never throw */ }
    }

    function scan() {
      scanTimer = 0;
      if (stopped || typeof document.createTreeWalker !== 'function' || typeof document.querySelector !== 'function') return;
      try {
        var content = document.querySelector('.layout-content');
        if (content) scanHeader(content);
        var chat = document.querySelector('.layout-chat');
        if (chat) {
          scanSteps(chat);
          scanToolGroups(chat);
        }
      } catch (err) { /* never throw */ }
    }
    // Expanding a tool row: group the freshly rendered rows in a microtask, before the next paint,
    // so the expanded list never flashes ungrouped for the 400 ms scan throttle.
    var expandObs = null, expandUntil = 0;
    function onExpandClick(ev) {
      var t = ev && ev.target, b = t && t.closest ? t.closest('[role="button"][aria-expanded]') : null;
      if (!b || stopped || typeof MutationObserver !== 'function') return;
      var chat = document.querySelector('.layout-chat');
      if (!chat) return;
      expandUntil = Date.now() + 900;
      if (expandObs) return;
      expandObs = new MutationObserver(function () {
        if (Date.now() > expandUntil) { if (expandObs) expandObs.disconnect(); expandObs = null; return; }
        scan();
      });
      expandObs.observe(chat, { childList: true, subtree: true });
      setTimeout(function () { if (expandObs && Date.now() > expandUntil) { expandObs.disconnect(); expandObs = null; } }, 1000);
    }
    function scheduleScan() {
      if (stopped || scanTimer) return;
      scanTimer = setTimeout(scan, SCAN_MS);
    }
    function unmark() {
      if (typeof document.querySelectorAll !== 'function') return;
      MARKS.forEach(function (cls) {
        Array.prototype.slice.call(document.querySelectorAll('.' + cls)).forEach(function (node) {
          node.classList.remove(cls);
        });
      });
      Array.prototype.slice.call(document.querySelectorAll('[' + COUNT_ATTR + ']')).forEach(function (node) {
        node.removeAttribute(COUNT_ATTR);
      });
    }

    function apply() {
      timer = 0;
      if (stopped || !root) return;
      if (!root.classList.contains(ROOT_CLASS)) root.classList.add(ROOT_CLASS);
      root.setAttribute('data-nt-desk-theme', 'dark');
    }

    function schedule() {
      if (stopped || timer) return;
      timer = setTimeout(apply, DEBOUNCE_MS);
    }

    try {
      apply();
      if (root && typeof MutationObserver === 'function') {
        observer = new MutationObserver((window.__ntIdle2 || (window.__ntIdle2 = function (fn) { var w = window; if (!w.__ntScrollHook) { w.__ntScrollHook = true; var mark = function () { w.__ntLastScroll = Date.now(); }; w.addEventListener('scroll', mark, { capture: true, passive: true }); w.addEventListener('wheel', mark, { capture: true, passive: true }); w.addEventListener('keydown', function () { w.__ntLastKey = Date.now(); }, { capture: true, passive: true }); } var t = 0, rowT = 0, lastRow = 0, lastArgs = []; var rowOnly = function (recs) { if (!recs || !recs.length) return false; for (var i = 0; i < recs.length; i++) { var n = recs[i] && recs[i].target; if (n && n.nodeType !== 1) n = n.parentElement; if (!n || !n.closest || !n.closest('[data-nt-cv] > div')) return false; } return true; }; return function idleRun() { var self = this; lastArgs = arguments; var wait = Math.max((w.__ntLastScroll || 0) + 400, (w.__ntLastKey || 0) + 1200) - Date.now(); if (wait > 0) { if (!t) t = setTimeout(function () { t = 0; idleRun.apply(self, lastArgs); }, wait + 20); return; } if (rowOnly(arguments[0])) { var due = lastRow + 4000 - Date.now(); if (due > 0) { if (!rowT) rowT = setTimeout(function () { rowT = 0; lastRow = Date.now(); fn.apply(self, lastArgs); }, due); return; } lastRow = Date.now(); } fn.apply(this, arguments); }; }))(schedule));
        observer.observe(root, { attributes: true, attributeFilter: ['class'] });
      }
      scan();
      if (document.body && typeof MutationObserver === 'function') {
        bodyObserver = new MutationObserver((window.__ntIdle2 || (window.__ntIdle2 = function (fn) { var w = window; if (!w.__ntScrollHook) { w.__ntScrollHook = true; var mark = function () { w.__ntLastScroll = Date.now(); }; w.addEventListener('scroll', mark, { capture: true, passive: true }); w.addEventListener('wheel', mark, { capture: true, passive: true }); w.addEventListener('keydown', function () { w.__ntLastKey = Date.now(); }, { capture: true, passive: true }); } var t = 0, rowT = 0, lastRow = 0, lastArgs = []; var rowOnly = function (recs) { if (!recs || !recs.length) return false; for (var i = 0; i < recs.length; i++) { var n = recs[i] && recs[i].target; if (n && n.nodeType !== 1) n = n.parentElement; if (!n || !n.closest || !n.closest('[data-nt-cv] > div')) return false; } return true; }; return function idleRun() { var self = this; lastArgs = arguments; var wait = Math.max((w.__ntLastScroll || 0) + 400, (w.__ntLastKey || 0) + 1200) - Date.now(); if (wait > 0) { if (!t) t = setTimeout(function () { t = 0; idleRun.apply(self, lastArgs); }, wait + 20); return; } if (rowOnly(arguments[0])) { var due = lastRow + 4000 - Date.now(); if (due > 0) { if (!rowT) rowT = setTimeout(function () { rowT = 0; lastRow = Date.now(); fn.apply(self, lastArgs); }, due); return; } lastRow = Date.now(); } fn.apply(this, arguments); }; }))(scheduleScan));
        bodyObserver.observe(document.body, { childList: true, subtree: true });
      }
      if (typeof document.addEventListener === 'function') {
        document.addEventListener('click', onClick, true);
        document.addEventListener('click', onExpandClick, true);
        clickBound = true;
      }
    } catch (err) { /* never throw */ }

    return {
      stop: function () {
        stopped = true;
        if (timer) clearTimeout(timer);
        timer = 0;
        if (observer) observer.disconnect();
        observer = null;
        if (scanTimer) clearTimeout(scanTimer);
        scanTimer = 0;
        if (bodyObserver) bodyObserver.disconnect();
        bodyObserver = null;
        if (clickBound) { document.removeEventListener('click', onClick, true); document.removeEventListener('click', onExpandClick, true); }
        if (expandObs) { expandObs.disconnect(); expandObs = null; }
        clickBound = false;
        try { unmark(); } catch (err) { /* never throw */ }
        if (root) {
          root.classList.remove(ROOT_CLASS);
          root.removeAttribute('data-nt-desk-theme');
        }
      },
      active: function () { return !!(root && root.classList.contains(ROOT_CLASS)); }
    };
  };
})();
