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
  var VERSION = 8;
  var BASE_TURNS = 5;      // v6: default view = last 5 user messages with their answers
  var BASE_MAX = 400;      // v6: but never more than this many steps by default (freeze guard)
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
  var lastLen = {};        // v8: threadId -> store count seen last time
  var memo = {};           // v8: threadId -> last capped array (stable identity)

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
  // v6: by default a chat shows its last BASE_TURNS user messages with their answers (at least STEP_CAP, at most
  // BASE_MAX steps). window.__ntStepCap[id] holds only explicit overrides (button loads, 0 = everything).
  var baseCache = {};
  function baseCap(id, all) {
    var len = all ? all.length : 0;
    var hit = baseCache[id];
    if (hit && hit.len === len) return hit.cap;
    var users = 0;
    var stop = len - BASE_MAX;
    var i = len - 1;
    for (; i >= 0 && i >= stop; i--) {
      var st = null;
      try { st = all[i] && all[i].getStep && all[i].getStep(); } catch (e) { st = null; }
      if (st && st.type === 'user' && ++users >= BASE_TURNS) break;
    }
    var n = len - Math.max(i, 0);
    if (n < STEP_CAP) n = STEP_CAP;
    if (n > BASE_MAX) n = BASE_MAX;
    // v8: while the agent adds tool steps (no new user message) keep the top fixed and just grow the window.
    if (hit && len > hit.len && len - hit.len < 50) {
      var newUser = false;
      for (var k = hit.len; k < len; k++) {
        var s2 = null;
        try { s2 = all[k] && all[k].getStep && all[k].getStep(); } catch (e) { s2 = null; }
        if (s2 && s2.type === 'user') { newUser = true; break; }
      }
      if (!newUser) n = Math.max(n, hit.cap + (len - hit.len));
    }
    baseCache[id] = { len: len, cap: n };
    return n;
  }
  function origAll(ts) {
    var f = ts && ts.getMessageStores;
    try { return ((f && f.__orig) ? f.__orig.call(ts) : f.call(ts)) || []; } catch (e) { return []; }
  }
  function capOf(id) {
    var c = caps();
    if (Object.prototype.hasOwnProperty.call(c, id)) return c[id];
    return live[id] ? baseCap(id, origAll(live[id])) : STEP_CAP;
  }

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
    delete caps()[id];
    if (el && el.isConnected !== false && live[id]) emit(live[id]);
    return true;
  }

  // v5: older messages load only on a click of the 'Load older messages' button (user wish 2026-10-20).
  // The button shows when a capped chat is scrolled near its top and older messages exist.
  function shouldOffer(el) {
    if (!el || el.isConnected === false || window.__ntStepCapOff || !(el.scrollTop < TOP_PX)) return null;
    var id = threadForEl(el);
    if (!id || !live[id]) return null;
    var cur = capOf(id);
    if (!cur || totalOf(live[id]) <= cur + 1) return null;
    return id;
  }

  var btn = null;
  var btnEl = null;
  function button() {
    if (btn || typeof document === 'undefined' || !document.body) return btn;
    btn = document.createElement('button');
    btn.type = 'button';
    btn.setAttribute('data-nt-stepcap-more', '1');
    btn.textContent = 'Load older messages';
    btn.style.cssText = 'position:fixed;z-index:2147483000;display:none;padding:6px 14px;border:0;border-radius:14px;' +
      'background:#2383e2;color:#fff;font:500 13px/1.2 -apple-system,Segoe UI,sans-serif;cursor:pointer;' +
      'box-shadow:0 2px 8px rgba(0,0,0,.25);transform:translateX(-50%)';
    // v7: the first click was eaten by Notion's own document listeners (only the 2nd click arrived, seen live).
    // Handle the press on window in the capture phase, before Notion sees it; 'click' stays as a deduped fallback.
    var lastPress = 0;
    function press(e) {
      if (!btn || (e.target !== btn && !btn.contains(e.target))) return;
      e.preventDefault(); e.stopPropagation();
      if (e.stopImmediatePropagation) e.stopImmediatePropagation();
      var now = Date.now();
      if (now - lastPress < 700 || !btnEl) return;
      lastPress = now;
      lastLoad = 0;
      var r = expand(btnEl, now);
      btn.textContent = 'Loading\u2026';
      setTimeout(function () { btn.textContent = 'Load older messages'; updateButton(btnEl); }, ANCHOR_MS + 100);
      window.__ntExpClick = { result: r, at: now, via: e.type };
    }
    window.addEventListener('pointerdown', press, true);
    window.addEventListener('mousedown', press, true);
    window.addEventListener('click', press, true);
    document.body.appendChild(btn);
    return btn;
  }

  function updateButton(el) {
    var b = button();
    if (!b) return;
    var id = shouldOffer(el);
    if (!id) { if (!el || el === btnEl) { b.style.display = 'none'; btnEl = null; } return; }
    var r = el.getBoundingClientRect();
    btnEl = el;
    b.style.top = Math.round(r.top + 10) + 'px';
    b.style.left = Math.round(r.left + r.width / 2) + 'px';
    b.style.display = 'block';
  }

  function onScroll(e) {
    if (window.__ntStepExpV !== VERSION || window.__ntStepCapOff) return;
    var el = e && e.target;
    if (!el || el.nodeType !== 1 || !(el.scrollHeight > el.clientHeight)) return;
    if (el === btnEl || el.scrollTop < TOP_PX) updateButton(el);
    var now = Date.now();
    var userDown = now - gesture.down < GESTURE_MS || now - gesture.drag < GESTURE_MS;
    // v8: while older messages are loading Notion briefly re-mounts the list (height drops ~900 px, measured), the
    // scroller clamps to its bottom and the button press counted as a gesture -> this reset sent you to the bottom.
    var loading = now - lastLoad < ANCHOR_MS + 1500;
    if (userDown && !loading && el.scrollHeight - el.clientHeight - el.scrollTop < BOTTOM_PX) {
      for (var id in expanded) if (expanded[id] === el) reset(id);
    }
  }

  function sweep() {
    for (var id in expanded) {
      var el = expanded[id];
      if (!el || el.isConnected === false) { delete expanded[id]; delete caps()[id]; }
    }
  }

  function bind() {
    window.__ntStepExpV = VERSION;
    if (typeof document === 'undefined' || window.__ntStepExpBound8) return;
    window.__ntStepExpBound8 = true;
    window.__ntStepCap = {};  // drop the fixed 30-step caps of v3-v5; the 5-message default applies
    try {  // the v3/v4 builds had no button; drop a stale one if a part reinstalls
      var old = document.querySelectorAll('[data-nt-stepcap-more]');
      for (var q = 0; q < old.length; q++) if (old[q] !== btn) old[q].remove();
    } catch (e) { /* nothing to drop */ }
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
    setInterval(function () {
      if (window.__ntStepExpV !== VERSION) return;
      sweep();
      if (btnEl && btnEl.isConnected === false) updateButton(null);  // chat closed or switched
    }, 1000);
  }

  window.__ntStepExp = {
    nextCap: nextCap, nextCapFor: nextCapFor, capOf: capOf, baseCap: baseCap, shouldOffer: shouldOffer, updateButton: updateButton, threadForEl: threadForEl, expand: expand, reset: reset, sweep: sweep,
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
      var co = caps();
      var id = this.id;
      var has = Object.prototype.hasOwnProperty.call(co, id);
      var len = all ? all.length : 0;
      // v8: new tool steps must not push older steps out at the top (that shifted the view = glitch / jump up).
      // Keep the first shown step fixed: a loaded (override) window grows by the new steps.
      var pl = lastLen[id];
      if (has && co[id] && pl && len > pl && len - pl < 50) co[id] += len - pl;
      lastLen[id] = len;
      var cap = has ? co[id] : baseCap(id, all);
      // v8: same array while nothing changed, so Notion does not re-render the whole list on every update.
      var m = memo[id];
      if (m && m.len === len && m.cap === cap && m.last === all[len - 1] && m.first === all[0]) return m.out;
      var out = capStores(all, cap);
      memo[id] = { len: len, cap: cap, last: all[len - 1], first: all[0], out: out };
      return out;
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
