/* NotionTool desktop overlay part: declutter (docs/DESKTOP_APP.md, overlay part contract).
 *
 * Hides Notion extras the app does not need by adding the class nt-desk-hidden. Notion nodes are
 * never removed and get no attributes; only nodes where this part added the class lose it again.
 * Hidden: feedback buttons under chat answers, the "What's new" button and its popover, trial and
 * upgrade offers, "New agent" tiles, onboarding cards, template galleries, Notion AI banners, the
 * help button and the Meetings shortcut. Kept: chat, composer, model and effort menus, page editor,
 * search, copy and retry buttons (KEEP wins).
 * Matching uses aria-label, role and data-testid first, visible text only for short buttons and
 * small cards; English and German texts. state.declutter === false shows everything again.
 * A MutationObserver (childList only, so our class changes do not wake it) runs a throttled scan.
 */
(function () {
  'use strict';
  window.__ntDesk = window.__ntDesk || {};

  var HIDDEN = 'nt-desk-hidden';
  var THROTTLE_MS = 300;
  var CARD_MAX_TEXT = 280;
  var CARD_DEPTH = 5;
  var BUTTON_MAX_TEXT = 60;

  // Exact aria-labels (lower case), English and German. Hides the element itself.
  var LABELS = {
    feedback: ['positives feedback teilen', 'negatives feedback teilen', 'share positive feedback',
      'share negative feedback', 'good response', 'bad response', 'gute antwort', 'schlechte antwort',
      'thumbs up', 'thumbs down', 'share feedback', 'feedback teilen', 'give feedback', 'feedback geben'],
    whats_new: ['aktuelle und neue funktionen', "what's new", "what's new and upcoming", 'new and upcoming features',
      'new features', 'neue funktionen', 'neuigkeiten', 'was ist neu'],
    help: ['help', 'hilfe', 'help & support', 'hilfe & support', 'help and support', 'hilfe und support',
      'help, feedback, and more', 'hilfe, feedback und mehr'],
    meetings: ['meetings', 'besprechungen', 'ai meeting notes', 'ki-besprechungsnotizen']
  };
  // Exact visible texts of short buttons; their small card is hidden with them.
  var BUTTONS = {
    trial: ['probe-abo starten', 'start trial', 'start free trial', 'start a free trial', 'kostenlos testen',
      'try for free', 'upgrade', 'jetzt upgraden', 'upgrade now', 'upgrade plan', 'tarif upgraden'],
    new_agent: ['neuer agent', 'new agent', 'agent erstellen', 'create agent', 'create an agent'],
    templates: ['vorlagen durchsuchen', 'browse templates', 'vorlagengalerie', 'template gallery',
      'weitere vorlagen', 'more templates']
  };
  // Text parts of small cards and banners (lower case substrings).
  var CARDS = {
    onboarding: ['erste schritte mit', 'get started with', 'getting started', 'onboarding', 'quick start',
      'schnellstart', 'einrichtung abschlie', 'finish setting up'],
    ai_banner: ['neu in notion ai', 'new in notion ai', 'try notion ai', 'notion ai ausprobieren',
      'entdecke notion ai', 'discover notion ai', 'do more with notion ai', 'mehr mit notion ai'],
    trial: ['probe-abo starten', 'start a free trial', 'start free trial', 'kostenlos testen']
  };
  // Dialog / popover labels (exact) for galleries; What's new uses LABELS.whats_new.
  var DIALOGS = {
    templates: ['templates', 'vorlagen', 'template gallery', 'vorlagengalerie']
  };
  // Never hidden, and a card that contains one of these is not hidden either.
  var KEEP = ['antwort kopieren', 'text kopieren', 'kopieren', 'copy', 'copy response', 'copy text', 'retry',
    'erneut versuchen', 'wiederholen', 'try again', 'suchen', 'search', 'neuer chat', 'new chat',
    'neuen chat starten', 'ki-nachricht senden', 'send', 'send message', 'nach unten scrollen', 'scroll to bottom'];
  var KEEP_SEL = '[role="textbox"],[contenteditable="true"],textarea,input,[data-testid="agent-send-message-button"],' +
    '.notion-page-content,[aria-haspopup="menu"],[aria-haspopup="listbox"],[role="tablist"],[role="tree"],nav';
  var OURS_SEL = '[data-nt-desk],[data-nt-desk-own],[id^="nt-desk-"]';
  var LABEL_SKIP_SEL = OURS_SEL + ',.notion-page-content,[contenteditable="true"],[role="treeitem"]';
  var TEXT_SKIP_SEL = LABEL_SKIP_SEL + ',.nt-desk-message,[data-message-id],[data-turn-id]';
  var CLICK_SEL = '[role="button"],button,a,[role="tab"],[role="menuitem"],[role="link"]';
  var CARD_SEL = '[role="alert"],[role="status"],[role="region"],[role="note"],aside,section,' +
    '[class*="banner" i],[class*="callout" i],[class*="onboarding" i],[class*="upsell" i],' +
    '[data-testid*="banner" i],[data-testid*="onboarding" i],[data-testid*="upsell" i],[data-testid*="promo" i]';
  var DIALOG_SEL = '[role="dialog"],[role="menu"][aria-label]';

  function norm(text) {
    return String(text || '').replace(/[\u2018\u2019]/g, "'").replace(/\s+/g, ' ').trim().toLowerCase();
  }
  function index(groups) {
    var map = {};
    Object.keys(groups).forEach(function (id) { groups[id].forEach(function (l) { map[norm(l)] = id; }); });
    return map;
  }
  var BY_LABEL = index(LABELS);
  var BY_BUTTON = index(BUTTONS);
  var BY_DIALOG = index(DIALOGS);
  var BY_KEEP = index({ keep: KEEP });

  // ---- pure rules (also used by tests) -------------------------------------------------
  function keepLabel(text) { return !!BY_KEEP[norm(text)]; }
  function labelReason(label, role) {
    var l = norm(label);
    if (!l || BY_KEEP[l]) return '';
    var id = BY_LABEL[l] || '';
    // A page called "Meetings" in the page tree stays; only the shortcut tab/button goes.
    if (id === 'meetings' && role !== 'tab' && role !== 'button') return '';
    return id;
  }
  function cardReason(text) {
    var t = norm(text);
    if (!t || t.length > CARD_MAX_TEXT) return '';
    var ids = Object.keys(CARDS);
    for (var i = 0; i < ids.length; i++) {
      var parts = CARDS[ids[i]];
      for (var j = 0; j < parts.length; j++) if (t.indexOf(parts[j]) >= 0) return ids[i];
    }
    return '';
  }
  function buttonReason(text) {
    var t = norm(text);
    if (!t || t.length > BUTTON_MAX_TEXT || BY_KEEP[t]) return '';
    return BY_BUTTON[t] || cardReason(t);
  }
  function dialogReason(label, text) {
    var l = norm(label);
    if (l && BY_LABEL[l] === 'whats_new') return 'whats_new';
    if (l && BY_DIALOG[l]) return BY_DIALOG[l];
    var head = norm(text).slice(0, 80);
    var news = LABELS.whats_new;
    for (var i = 0; i < news.length; i++) if (head.indexOf(news[i]) === 0) return 'whats_new';
    return '';
  }
  window.__ntDesk.declutterMatch = {
    labelReason: labelReason, buttonReason: buttonReason, cardReason: cardReason,
    dialogReason: dialogReason, keepLabel: keepLabel
  };

  // ---- DOM helpers -----------------------------------------------------------------------
  function all(root, selector) {
    try { return Array.prototype.slice.call((root || document).querySelectorAll(selector)); } catch (err) { return []; }
  }
  function inside(node, selector) {
    try { return !!(node && node.closest && node.closest(selector)); } catch (err) { return false; }
  }
  function visibleText(node) { return String((node && (node.innerText || node.textContent)) || ''); }
  function hasKeep(node) {
    try { if (node.matches(KEEP_SEL) || node.querySelector(KEEP_SEL)) return true; } catch (err) { return true; }
    var labelled = all(node, '[aria-label]');
    for (var i = 0; i < labelled.length; i++) if (keepLabel(labelled[i].getAttribute('aria-label'))) return true;
    return false;
  }
  // The largest small ancestor (card) around a button; the button itself when none is safe.
  function cardOf(node) {
    var best = node;
    var cur = node.parentElement;
    for (var depth = 0; cur && depth < CARD_DEPTH; depth++, cur = cur.parentElement) {
      if (cur === document.body || cur === document.documentElement) break;
      if (norm(visibleText(cur)).length > CARD_MAX_TEXT || hasKeep(cur) || inside(cur, OURS_SEL)) break;
      best = cur;
    }
    return best;
  }

  window.__ntDesk.declutter = function (ctx) {
    ctx = ctx || {};
    var marked = [];   // nodes where this part added the class
    var reasons = {};  // reason -> count from the last scan
    var timer = 0;
    var stopped = false;
    var observer = null;
    var unsubscribe = null;

    function enabled() {
      var state = typeof ctx.getState === 'function' ? ctx.getState() : null;
      return !(state && state.declutter === false);
    }

    function hide(node, reason, keep) {
      if (!node || !node.classList) return;
      reasons[reason] = (reasons[reason] || 0) + 1;
      keep.push(node);
      if (marked.indexOf(node) >= 0 || node.classList.contains(HIDDEN)) return;
      node.classList.add(HIDDEN);
      marked.push(node);
    }

    function release(nodes) {
      nodes.forEach(function (node) { try { node.classList.remove(HIDDEN); } catch (err) {} });
    }

    function scan() {
      timer = 0;
      if (stopped) return;
      if (!enabled()) { release(marked); marked = []; reasons = {}; return; }
      reasons = {};
      var keep = [];
      all(document, CLICK_SEL + ',[aria-label]').forEach(function (node) {
        if (inside(node, LABEL_SKIP_SEL)) return;
        var label = node.getAttribute('aria-label') || '';
        var role = node.getAttribute('role') || (node.tagName === 'BUTTON' ? 'button' : '');
        var reason = labelReason(label, role);
        if (reason) { hide(node, reason, keep); return; }
        if (label && keepLabel(label)) return;
        var isClick = false;
        try { isClick = node.matches(CLICK_SEL); } catch (err) {}
        if (!isClick || inside(node, TEXT_SKIP_SEL)) return;
        reason = buttonReason(visibleText(node));
        if (reason) hide(cardOf(node), reason, keep);
      });
      all(document, CARD_SEL).forEach(function (node) {
        if (inside(node, TEXT_SKIP_SEL) || hasKeep(node)) return;
        var reason = cardReason(visibleText(node));
        if (reason) hide(node, reason, keep);
      });
      all(document, DIALOG_SEL).forEach(function (node) {
        if (inside(node, OURS_SEL) || String(node.className || '').indexOf('nt-desk-') >= 0) return;
        var reason = dialogReason(node.getAttribute('aria-label') || '', visibleText(node));
        if (reason) hide(node, reason, keep);
      });
      // Nodes that no longer match (or left the page) get their class back off.
      var gone = marked.filter(function (node) { return keep.indexOf(node) < 0; });
      if (gone.length) { release(gone); marked = marked.filter(function (node) { return keep.indexOf(node) >= 0; }); }
    }

    function schedule() {
      if (stopped || timer) return;
      timer = setTimeout(scan, THROTTLE_MS);
    }

    if (typeof MutationObserver === 'function' && document.body) {
      observer = new MutationObserver((window.__ntIdle || (window.__ntIdle = function (fn) { var w = window; if (!w.__ntScrollHook) { w.__ntScrollHook = true; var mark = function () { w.__ntLastScroll = Date.now(); }; w.addEventListener('scroll', mark, { capture: true, passive: true }); w.addEventListener('wheel', mark, { capture: true, passive: true }); } var t = 0; return function idleRun() { var wait = (w.__ntLastScroll || 0) + 400 - Date.now(); if (wait > 0) { if (!t) t = setTimeout(function () { t = 0; idleRun(); }, wait + 20); return; } fn.apply(this, arguments); }; }))(schedule));
      observer.observe(document.body, { childList: true, subtree: true });
    }
    if (typeof ctx.onState === 'function') unsubscribe = ctx.onState(schedule);
    scan();

    return {
      scan: scan,
      selfcheck: function () {
        return { ok: true, on: enabled(), hidden: marked.length, reasons: JSON.parse(JSON.stringify(reasons)) };
      },
      stop: function () {
        stopped = true;
        if (timer) clearTimeout(timer);
        timer = 0;
        if (observer) observer.disconnect();
        if (typeof unsubscribe === 'function') unsubscribe();
        release(marked);
        marked = [];
      }
    };
  };
})();