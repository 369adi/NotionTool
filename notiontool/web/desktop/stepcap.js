/*
 * NotionTool desktop part 'stepcap' (no UI, nothing to mount). Notion re-processes every message of a chat on each
 * send and chat switch; on long chats (100+ messages) that froze the page for 15-27 s (measured 2026-10-20, gone
 * after capping). This caps the message stores Notion renders per chat to the last STEP_CAP. The chat's 'config'
 * step is always kept, so model and reasoning changes keep working (verified live in the Test chat and Main 7).
 * window.__ntStepCap[threadId] overrides one chat (0 = no cap); window.__ntStepCapOff = true disables it.
 * Safe on Notion updates: if the ThreadStore module is not found under the known id, nothing is patched.
 *
 * v3: scroll-up loading. When the user really scrolls (wheel/keys/scrollbar) to the top of a capped chat, the cap of
 * that chat grows by STEP_ADD and Notion is told to re-render (inMemoryRecordCache.emitRecord(threadPointer, []),
 * the same call Notion uses itself; verified live: 1492 -> 2524 px within 1 s). The scroll position is kept on the
 * message the user was reading. Back at the bottom (user scroll down) or when the chat is closed, the cap returns
 * to STEP_CAP so the freeze cannot come back.
 */
(function () {
  var VERSION = 4;
  var STEP_CAP = 30;
  var STEP_ADD = 30;       // minimum growth per load
  var TURNS_ADD = 3;       // v4: one load adds this many older user turns (30 steps were often only tool steps)
  var MAX_ADD = 400;       // v4: upper bound per load so one load cannot bring the freeze back
  var MODULE_ID = 174148;  // RecordStore bundle, export E = ThreadStore (Notion desktop 7.39)
  var TOP_PX = 150;        // distance from the top that loads older messages
  var BOTTOM_PX = 40;      // distance from the bottom that resets the cap
  var GESTURE_MS = 900;    // a scroll counts as the user's only this long after a wheel/key/scrollbar gesture
  var COOLDOWN_MS = 1200;  // min time between two loads
  var ANCHOR_MS = 1500;    // how long the scroll position is held after a load

  var live = window.__ntTSLive = window.__ntTSLive || {};
  var expanded = {};       // threadId -> scroller element
  var gesture = { up: 0, down: 0, drag: 0 };
  var lastLoad = 0;

  function capStores(all, cap) {
    if (!cap || !all || all.length <= cap) return all;
    var tail = all.slice(-cap);
    var cfg = null;
    for (var i = 0; i < all.length - cap; i++) {
      var st = null;
      try { st = all[i] && all[i].getStep && all[i].getStep(); } catch (e) { st = null; }
      if (st && st.type === 'config') { cfg = all[i]; break; }
    }
    window.__ntCapHits = (window.__ntCapHits || 0) + 1;
    return cfg ? [cfg].concat(tail) : tail;
  }
  window.__ntCapStores = capStores;

  function caps() { return (window.__ntStepCap = window.__ntStepCap || {}); }
  function capOf(id) { var c = caps(); return Object.prototype.hasOwnProperty.call(c, id) ? c[id] : STEP_CAP; }

  function nextCap(cur, total) {
    if (!cur) return 0;                    // already uncapped
    var n = cur + STEP_ADD;
    return n >= total ? 0 : n;
  }

  function totalOf(ts) {
    var f = ts && ts.getMessageStores;
    try { return ((f && f.__orig) ? f.__orig.call(ts) : f.call(ts)).length; } catch (e) { return 0; }
  }

  // v4: grow the cap until TURNS_ADD more user messages are included (at least STEP_ADD, at most MAX_ADD steps).
  function nextCapFor(ts, cur) {
    if (!cur) return 0;
    var all = [];
    try { var f = ts.getMessageStores; all = ((f && f.__orig) ? f.__orig.call(ts) : f.call(ts)) || []; } catch (e) { return cur; }
    var total = all.length;
    var users = 0;
    var limit = total - cur - MAX_ADD;
    var i = total - cur - 1;
    for (; i >= 0 && i >= limit; i--) {
      var st = null;
      try { st = all[i].getStep(); } catch (e) { st = null; }
      if (st && st.type === 'user' && ++users >= TURNS_ADD) break;
    }
    var n = total - Math.max(i, 0);
    if (n < cur + STEP_ADD) n = cur + STEP_ADD;
    return n >= total ? 0 : n;
  }

  function emit(ts) {
    try { ts.internals.inMemoryRecordCache.emitRecord(ts.internals._pointer, []); return true; } catch (e) { return false; }
  }

  // Which chat does this scroller show? Match the user-message ids in the DOM against the live thread stores.
  function threadForEl(el) {
    var ids = {};
    var n = 0;
    try {
      var nodes = el.querySelectorAll('[data-agent-chat-user-step-id]');
      for (var i = 0; i < nodes.length && n < 6; i++) { ids[nodes[i].getAttribute('data-agent-chat-user-step-id')] = 1; n++; }
    } catch (e) { return null; }
    if (!n) return null;
    for (var id in live) {
      var ts = live[id];
      var stores = [];
      try { stores = ts.getMessageStores() || []; } catch (e) { stores = []; }
      for (var j = 0; j < stores.length; j++) {
        var st = null;
        try { st = stores[j].getStep(); } catch (e) { st = null; }
        if (st && ids[st.id]) return id;
      }
    }
    return null;
  }

  function hold(el, prevH, prevTop) {
    var t0 = Date.now();
    var raf = window.requestAnimationFrame || function (f) { return setTimeout(f, 16); };
    function tick() {
      var h = el.scrollHeight;
      if (h !== prevH) el.scrollTop = prevTop + (h - prevH);
      if (Date.now() - t0 < ANCHOR_MS && el.isConnected !== false) raf(tick);
    }
    raf(tick);
  }

  function expand(el, now) {
    now = now || Date.now();
    if (now - lastLoad < COOLDOWN_MS) return 'cooldown';
    var id = threadForEl(el);
    if (!id) return 'no-thread';
    var ts = live[id];
    var cur = capOf(id);
    var n = nextCapFor(ts, cur);
    if (n === cur) return 'all-loaded';
    window.__ntExpLast = { id: id, from: cur, to: n, at: now };
    lastLoad = now;
    caps()[id] = n;
    expanded[id] = el;
    var prevH = el.scrollHeight;
    var prevTop = el.scrollTop;
    emit(ts);
    hold(el, prevH, prevTop);
    return n;
  }

  function reset(id) {
    if (!expanded[id]) return false;
    var el = expanded[id];
    delete expanded[id];
    caps()[id] = STEP_CAP;
    if (el && el.isConnected !== false && live[id]) emit(live[id]);
    return true;
  }

  function onScroll(e) {
    if (window.__ntStepExpV !== VERSION || window.__ntStepCapOff) return;
    var el = e && e.target;
    if (!el || el.nodeType !== 1 || !(el.scrollHeight > el.clientHeight)) return;
    var now = Date.now();
    var userUp = now - gesture.up < GESTURE_MS || now - gesture.drag < GESTURE_MS;
    var userDown = now - gesture.down < GESTURE_MS || now - gesture.drag < GESTURE_MS;
    if (userUp && el.scrollTop < TOP_PX) { expand(el, now); return; }
    if (userDown && el.scrollHeight - el.clientHeight - el.scrollTop < BOTTOM_PX) {
      for (var id in expanded) if (expanded[id] === el) reset(id);
    }
  }

  function sweep() {
    for (var id in expanded) {
      var el = expanded[id];
      if (!el || el.isConnected === false) { delete expanded[id]; caps()[id] = STEP_CAP; }
    }
  }

  function bind() {
    window.__ntStepExpV = VERSION;
    if (typeof document === 'undefined' || window.__ntStepExpBound4) return;
    window.__ntStepExpBound4 = true;
    var opt = { capture: true, passive: true };
    document.addEventListener('wheel', function (e) {
      if (e.deltaY < 0) gesture.up = Date.now(); else if (e.deltaY > 0) gesture.down = Date.now();
    }, opt);
    document.addEventListener('keydown', function (e) {
      var k = e.key;
      if (k === 'PageUp' || k === 'ArrowUp' || k === 'Home') gesture.up = Date.now();
      else if (k === 'PageDown' || k === 'ArrowDown' || k === 'End') gesture.down = Date.now();
    }, opt);
    document.addEventListener('mousedown', function () { gesture.drag = Date.now(); }, opt);
    document.addEventListener('mousemove', function (e) { if (e.buttons & 1) gesture.drag = Date.now(); }, opt);
    document.addEventListener('scroll', onScroll, opt);
    setInterval(function () { if (window.__ntStepExpV === VERSION) sweep(); }, 1000);
  }

  window.__ntStepExp = {
    nextCap: nextCap, nextCapFor: nextCapFor, threadForEl: threadForEl, expand: expand, reset: reset, sweep: sweep,
    onScroll: onScroll, gesture: gesture, expanded: expanded, version: VERSION,
    _resetCooldown: function () { lastLoad = 0; }
  };

  function install() {
    var req = null;
    try {
      window.rspackChunknotion_next.push([[typeof Symbol === 'function' ? Symbol('nt-stepcap') : 'nt-stepcap'], {}, function (r) { req = r; }]);
    } catch (e) { return false; }
    if (!req) return false;
    var mod = null;
    try { mod = req(MODULE_ID); } catch (e) { return 'missing'; }
    var T = mod && mod.E;
    if (!T || !T.prototype || T.keyName !== 'ThreadStore') return 'missing';
    var proto = T.prototype;
    var cur = proto.getMessageStores;
    if (typeof cur !== 'function') return 'missing';
    if (cur.__ntv === VERSION) return true;
    var orig = cur.__orig || cur;
    var capped = function () {
      var all = orig.call(this);
      if (this && this.id) live[this.id] = this;
      if (window.__ntStepCapOff) return all;
      return capStores(all, capOf(this.id));
    };
    capped.__nt = true;
    capped.__ntv = VERSION;
    capped.__orig = orig;
    proto.getMessageStores = capped;
    return true;
  }

  var tries = 0;
  function attempt() {
    var r = install();
    window.__ntStepCapState = r === true ? 'active' : r === 'missing' ? 'module-missing' : 'waiting';
    if (r === true) bind();
    if (r === true || r === 'missing') return;
    if (++tries < 240) setTimeout(attempt, 500);
  }
  attempt();
})();
