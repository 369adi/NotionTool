/* NotionTool desktop overlay part: theme (docs/DESKTOP_APP.md, overlay part contract).
 *
 * Turns on the NotionTool design system (theme.css) by adding the root class 'nt-theme' to
 * <html>. All theme rules are scoped to html.nt-theme, so removing the class switches the
 * theme off completely. Notion rewrites the root classes now and then (light/dark switch);
 * one MutationObserver on the root class attribute puts the class back (small debounce).
 * Studio v2: a throttled scan only toggles marker classes on Notion nodes (never adds, moves or
 * removes them): the top bar breadcrumb 'Notion AI' (shown as 'NotionTool' via CSS ::after, the
 * original text is hidden visually only), the chat title next to it and the 'N Schritte' step
 * labels in the chat (styled as pills). stop() removes every marker class again.
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
  var MARKS = ['nt-desk-crumb-host', 'nt-desk-crumb-src', 'nt-desk-chat-title', 'nt-desk-steps'];

  function isStepsLabel(text) { return STEPS_RE.test(String(text || '').trim()); }
  window.__ntDesk.themeUtil = { isStepsLabel: isStepsLabel, CRUMB_TEXT: CRUMB_TEXT, MARKS: MARKS };

  window.__ntDesk.theme = function (ctx) {
    ctx = ctx || {};
    var root = document.documentElement;
    var stopped = false;
    var timer = 0;
    var observer = null;
    var scanTimer = 0;
    var bodyObserver = null;

    function mark(node, cls) {
      if (node && node.classList && !node.classList.contains(cls)) node.classList.add(cls);
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
    function scan() {
      scanTimer = 0;
      if (stopped || typeof document.createTreeWalker !== 'function' || typeof document.querySelector !== 'function') return;
      try {
        var content = document.querySelector('.layout-content');
        if (content) scanHeader(content);
        var chat = document.querySelector('.layout-chat');
        if (chat) scanSteps(chat);
      } catch (err) { /* never throw */ }
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
        observer = new MutationObserver(schedule);
        observer.observe(root, { attributes: true, attributeFilter: ['class'] });
      }
      scan();
      if (document.body && typeof MutationObserver === 'function') {
        bodyObserver = new MutationObserver(scheduleScan);
        bodyObserver.observe(document.body, { childList: true, subtree: true });
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
