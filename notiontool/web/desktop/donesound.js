/*
 * NotionTool desktop part 'donesound' (no UI, nothing to mount). Plays a short, soft two-note chime when a Main chat,
 * the Test chat or one of the user's own chats finishes its answer. Coworker chats stay silent.
 * Source: Notion's own ThreadStore flag getIsInferenceLeaseActive() (true while the agent answers, verified live
 * 2026-10-20). localStorage 'ntDesk.doneSound' = 'off' turns the sound off.
 */
(function () {
  var VERSION = 1;
  var MODULE_ID = 174148;  // RecordStore bundle, export E = ThreadStore (Notion desktop 7.39), same as stepcap
  var POLL_MS = 1000;
  var MIN_RUN_MS = 2500;   // short blips never chime
  var GAP_MS = 2500;       // several chats finishing together = one chime
  window.__ntDoneSoundV = VERSION;
  var seen = window.__ntDoneSeen = window.__ntDoneSeen || {};  // threadId -> ThreadStore
  var runs = {};           // threadId -> start time while the agent answers
  var lastChime = 0;
  var ctx = null;

  function off() {
    try { return window.localStorage.getItem('ntDesk.doneSound') === 'off'; } catch (e) { return false; }
  }
  function coworker(id) {
    var d = window.__ntDesk || {};
    try { return !!(d.coworkerAges && d.threadKey && d.coworkerAges[d.threadKey(id)]); } catch (e) { return false; }
  }
  function audio() {
    var AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return null;
    try { ctx = ctx || new AC(); if (ctx.state === 'suspended' && ctx.resume) ctx.resume(); } catch (e) { ctx = null; }
    return ctx;
  }
  function chime(force) {
    var now = Date.now();
    if ((!force && off()) || now - lastChime < GAP_MS) return false;
    var a = audio();
    if (!a) return false;
    lastChime = now;
    try {
      var t = a.currentTime + 0.02;
      var master = a.createGain();
      master.gain.value = 0.14;
      master.connect(a.destination);
      [[659.25, 0], [987.77, 0.12]].forEach(function (n) {  // E5 -> B5, sine, soft attack, ~0.5 s fade
        var o = a.createOscillator();
        var g = a.createGain();
        o.type = 'sine';
        o.frequency.value = n[0];
        g.gain.setValueAtTime(0.0001, t + n[1]);
        g.gain.exponentialRampToValueAtTime(1, t + n[1] + 0.015);
        g.gain.exponentialRampToValueAtTime(0.0001, t + n[1] + 0.5);
        o.connect(g);
        g.connect(master);
        o.start(t + n[1]);
        o.stop(t + n[1] + 0.55);
      });
      window.__ntDoneLog = (window.__ntDoneLog || []).slice(-9).concat([{ at: now }]);
      return true;
    } catch (e) { return false; }
  }
  function active(ts) {
    var f = ts && ts.getIsInferenceLeaseActive;
    if (typeof f !== 'function') return false;
    try { return !!(f.__orig ? f.__orig.call(ts) : f.call(ts)); } catch (e) { return false; }
  }
  function poll() {
    if (window.__ntDoneSoundV !== VERSION) return;
    var now = Date.now();
    var live = window.__ntTSLive || {};
    var all = {};
    var k;
    for (k in live) all[k] = live[k];
    for (k in seen) all[k] = seen[k];
    for (var id in all) {
      var on = active(all[id]);
      if (on && !runs[id]) runs[id] = now;
      else if (!on && runs[id]) {
        var long = now - runs[id] >= MIN_RUN_MS;
        delete runs[id];
        if (long && !coworker(id)) chime(false);
      }
    }
  }
  // Register every ThreadStore Notion asks for its state (sidebar rows too), not only the open chat.
  function install() {
    var req = null;
    try {
      window.rspackChunknotion_next.push([[typeof Symbol === 'function' ? Symbol('nt-donesound') : 'nt-donesound'], {}, function (r) { req = r; }]);
    } catch (e) { return false; }
    if (!req) return false;
    var mod = null;
    try { mod = req(MODULE_ID); } catch (e) { return 'missing'; }
    var T = mod && mod.E;
    if (!T || !T.prototype || T.keyName !== 'ThreadStore') return 'missing';
    var proto = T.prototype;
    var cur = proto.getIsInferenceLeaseActive;
    if (typeof cur !== 'function') return 'missing';
    if (cur.__ntDone === VERSION) return true;
    var orig = cur.__orig || cur;
    var wrapped = function () {
      if (this && this.id) seen[this.id] = this;
      return orig.apply(this, arguments);
    };
    wrapped.__orig = orig;
    wrapped.__ntDone = VERSION;
    proto.getIsInferenceLeaseActive = wrapped;
    return true;
  }
  window.__ntDoneSound = { chime: function () { return chime(true); }, poll: poll, runs: runs, version: VERSION };
  if (typeof document === 'undefined' || window.__ntDoneBound1) return;
  window.__ntDoneBound1 = true;
  // Unlock audio on the first interaction in case the window blocks sound without one.
  document.addEventListener('pointerdown', function () { audio(); }, { capture: true, passive: true, once: true });
  var tries = 0;
  var timer = setInterval(function () {
    var r = install();
    if (r === true || r === 'missing' || ++tries > 60) clearInterval(timer);
  }, 2000);
  install();
  setInterval(poll, POLL_MS);
})();
