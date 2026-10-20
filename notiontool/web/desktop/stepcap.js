/*
 * NotionTool desktop part 'stepcap' (no UI, nothing to mount). Notion re-processes every message of a chat on each
 * send and chat switch; on long chats (100+ messages) that froze the page for 15-27 s (measured 2026-10-20, gone
 * after capping). This caps the message stores Notion renders per chat to the last STEP_CAP. The chat's 'config'
 * step is always kept, so model and reasoning changes keep working (verified live in the Test chat and Main 7).
 * window.__ntStepCap[threadId] overrides one chat (0 = no cap); window.__ntStepCapOff = true disables it.
 * Safe on Notion updates: if the ThreadStore module is not found under the known id, nothing is patched.
 */
(function () {
  var VERSION = 2;
  var STEP_CAP = 30;
  var MODULE_ID = 174148;  // RecordStore bundle, export E = ThreadStore (Notion desktop 7.39)

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
      if (window.__ntStepCapOff) return all;
      var caps = window.__ntStepCap || {};
      var cap = Object.prototype.hasOwnProperty.call(caps, this.id) ? caps[this.id] : STEP_CAP;
      return capStores(all, cap);
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
    if (r === true || r === 'missing') return;
    if (++tries < 240) setTimeout(attempt, 500);
  }
  attempt();
})();
