/*
 * NotionTool desktop part 'progress' (progress.js): window.__ntDesk.progress = function (ctx) { ... return { stop } }.
 * Shows state.progress (progress_feed.py) for every coworker:
 * - Live view: a 'Team progress' grid of coworker cards right after the Live view's Team section. The section is
 *   looked up lazily on every tick, so this works inside the window.__ntShell 'Live' tab and in the slide-in panel,
 *   whenever they appear. Card: name, plain task summary, progress bar with the phase (Exploring / Editing /
 *   Testing / Answering) or 'Step x of y', the current step ('Writing file overlay.js', 'Running tests'), step
 *   count, 'Last activity 20 s ago' and an amber warning after 5 minutes without a new step. A card opens the
 *   coworker chat in Notion only on a real user click.
 * - Sidebar: a thin progress bar under each busy coworker row plus its current phase.
 * Renders at most once per second, updates only changed nodes and stops its timer while the page is hidden.
 * Reads state only: nothing here calls the harness, reads Notion or sends a message. Only own nodes
 * (data-nt-desk-own="1") are added or removed.
 */
(function () {
  'use strict';
  window.__ntDesk = window.__ntDesk || {};

  var TICK_MS = 1000;
  var STALL_S = 300;
  var OWN = 'data-nt-desk-own';
  var PHASES = { starting: 'Starting', exploring: 'Exploring', editing: 'Editing', testing: 'Testing',
    answering: 'Answering', done: 'Done' };
  var BUSY = ['busy', 'running', 'recovering'];
  var TEST_RE = /(unittest|pytest|npm (run )?test|node --test|jest|vitest)/i;

  function obj(v) { return v && typeof v === 'object' ? v : {}; }
  function list(v) { return Array.isArray(v) ? v : []; }
  function text(v) { return String(v == null ? '' : v).replace(/\s+/g, ' ').trim(); }
  function secs(v) {
    var n = Number(v);
    if (!isFinite(n) || n <= 0) return 0;
    return n > 1e11 ? n / 1000 : n;
  }

  function agoText(last, now) {
    var t = secs(last);
    if (!t) return 'No activity yet';
    var d = Math.max(0, Math.round(now - t));
    if (d < 60) return 'Last activity ' + d + ' s ago';
    if (d < 3600) return 'Last activity ' + Math.floor(d / 60) + ' min ago';
    return 'Last activity ' + Math.floor(d / 3600) + ' h ago';
  }
  function stepLine(p) {
    p = obj(p);
    var step = text(p.step), target = text(p.target);
    if (!step) return p.completed ? 'Finished' : 'Waiting for the first step';
    if (/^running command/i.test(step) && TEST_RE.test(target)) return 'Running tests';
    return target ? step + ' ' + target : step;
  }
  function label(p) {
    p = obj(p);
    var g = obj(p.progress);
    if (p.completed) return 'Done';
    if ((g.source === 'steps' || g.source === 'checklist') && text(g.label)) return text(g.label);
    return PHASES[text(g.phase).toLowerCase()] || text(g.label) || 'Working';
  }
  function percent(p) {
    p = obj(p);
    if (p.completed) return 100;
    var n = Number(obj(p.progress).percent);
    return isFinite(n) ? Math.max(0, Math.min(100, Math.round(n))) : 0;
  }
  function stepsText(n) {
    n = Array.isArray(n) ? n.length : Math.max(0, Math.floor(Number(n) || 0));
    return n === 1 ? '1 step' : n + ' steps';
  }
  function taskSummary(s, w) {
    if (!w.task_id) return '';
    var t = obj(list(s.tasks).filter(function (x) { return obj(x).id === w.task_id; })[0]);
    return text(t.plain_summary || t.plain || t.summary || t.title || w.task_title).slice(0, 160);
  }
  function findProgress(s, w) {
    var all = obj(s.progress), hit = null;
    if (w.task_id && all[w.task_id]) return obj(all[w.task_id]);
    Object.keys(all).forEach(function (k) { if (!hit && obj(all[k]).worker_id === w.id) hit = obj(all[k]); });
    return hit;
  }
  function cardModel(s, w, now) {
    s = obj(s); w = obj(w);
    var p = findProgress(s, w);
    var busy = !!(w.task_id || BUSY.indexOf(text(w.state).toLowerCase()) >= 0);
    var m = { id: w.id, thread: text(w.thread_id), url: text(w.url), name: text(w.label) || 'Coworker',
      task: taskSummary(s, w), busy: busy, has: !!p, stalled: false, warn: '' };
    if (!p) {
      m.label = busy ? 'Starting' : 'Idle';
      m.percent = 0;
      m.step = busy ? 'Waiting for the first step' : 'No task';
      m.steps = '';
      m.ago = '';
      return m;
    }
    var last = secs(p.last_activity);
    m.label = label(p);
    m.percent = percent(p);
    m.step = stepLine(p);
    m.steps = stepsText(p.steps);
    m.ago = agoText(last, now);
    m.stalled = !p.completed && (!!p.stalled || (last > 0 && now - last >= STALL_S));
    if (m.stalled) {
      var quiet = last > 0 ? now - last : (Number(p.stalled_s) || STALL_S);
      m.warn = 'Stalled: no new step for ' + Math.max(5, Math.floor(quiet / 60)) + ' min';
    } else if (text(p.error)) {
      m.warn = 'Progress could not be read right now';
    }
    return m;
  }
  function cards(s, now) {
    s = obj(s);
    return list(s.workers).filter(function (w) { return obj(w).purpose === 'worker' && obj(w).id; })
      .map(function (w) { return cardModel(s, w, now); });
  }

  window.__ntDesk.progressCore = { agoText: agoText, stepLine: stepLine, label: label, percent: percent,
    stepsText: stepsText, cardModel: cardModel, cards: cards };

  window.__ntDesk.progress = function (ctx) {
    ctx = obj(ctx);
    var state = typeof ctx.getState === 'function' ? obj(ctx.getState()) : {};
    var timer = null, stopped = false, renders = 0, lastRender = 0;
    var grid = null, gridCount = null, gridBody = null, gridEmpty = null;
    var cardsById = {}, sideByThread = {};

    function nowS() { return Date.now() / 1000; }
    function all(sel) {
      try { return Array.prototype.slice.call(document.querySelectorAll(sel)); } catch (e) { return []; }
    }
    function el(tag, cls, txt) {
      var n = document.createElement(tag);
      if (cls) n.className = cls;
      if (txt != null) n.textContent = txt;
      n.setAttribute(OWN, '1');
      return n;
    }
    function setText(n, v) { if (n.textContent !== v) n.textContent = v; }
    function setWidth(n, pct) { var v = pct + '%'; if (n.style.width !== v) n.style.width = v; }
    function attr(n, k, v) { if (n.getAttribute(k) !== v) n.setAttribute(k, v); }
    function hide(n, on) { if (!!n.hidden !== on) n.hidden = on; }
    function toggle(n, cls, on) {
      var padded = ' ' + (n.className || '') + ' ', has = padded.indexOf(' ' + cls + ' ') >= 0;
      if (on && !has) n.className = text(n.className + ' ' + cls);
      else if (!on && has) n.className = text(padded.replace(' ' + cls + ' ', ' '));
    }
    function detach(n) {
      if (n && n.parentNode && n.getAttribute && n.getAttribute(OWN) === '1') n.parentNode.removeChild(n);
    }
    function sidebarRow(thread) {
      if (!thread) return null;
      return all('[data-nt-desk="coworker"]').filter(function (r) {
        return r.getAttribute('data-nt-desk-thread') === thread;
      })[0] || null;
    }

    // Only a real user click opens the chat; it reuses the sidebar row's own open path when the row exists.
    function openChat(ev, m) {
      var prevent = function () { if (ev && typeof ev.preventDefault === 'function') ev.preventDefault(); };
      if (!ev || ev.isTrusted !== true || !m || !m.url) { prevent(); return; }
      var row = sidebarRow(m.thread);
      if (row && typeof row.click === 'function') { prevent(); row.click(); }
    }

    function ensureGrid() {
      if (grid) return grid;
      grid = el('section', 'nt-desk-pg-team');
      grid.setAttribute('data-nt-desk', 'progress-team');
      grid.setAttribute('aria-label', 'Team progress');
      var head = el('div', 'nt-desk-pg-head');
      head.appendChild(el('h3', 'nt-desk-pg-title', 'Team progress'));
      gridCount = el('span', 'nt-desk-pg-count', '');
      head.appendChild(gridCount);
      gridBody = el('div', 'nt-desk-pg-grid');
      gridEmpty = el('div', 'nt-desk-pg-empty', 'No coworkers yet.');
      grid.appendChild(head);
      grid.appendChild(gridBody);
      grid.appendChild(gridEmpty);
      return grid;
    }
    function placeGrid() {
      var anchor = all('section.nt-desk-lv-team')[0];
      if (!anchor || !anchor.parentNode) {
        if (grid && grid.parentNode) grid.parentNode.removeChild(grid);
        return false;
      }
      ensureGrid();
      if (grid.parentNode !== anchor.parentNode || anchor.nextSibling !== grid) {
        anchor.parentNode.insertBefore(grid, anchor.nextSibling);
      }
      return true;
    }
    function buildCard() {
      var c = { node: el('a', 'nt-desk-pg-card'), model: null };
      c.node.setAttribute('data-nt-desk', 'progress-card');
      var top = el('div', 'nt-desk-pg-top');
      c.name = el('span', 'nt-desk-pg-name', '');
      c.label = el('span', 'nt-desk-pg-label', '');
      top.appendChild(c.name);
      top.appendChild(c.label);
      c.task = el('div', 'nt-desk-pg-task', '');
      c.track = el('div', 'nt-desk-pg-track');
      c.track.setAttribute('role', 'progressbar');
      c.track.setAttribute('aria-valuemin', '0');
      c.track.setAttribute('aria-valuemax', '100');
      c.fill = el('div', 'nt-desk-pg-fill');
      c.track.appendChild(c.fill);
      c.step = el('div', 'nt-desk-pg-step', '');
      var meta = el('div', 'nt-desk-pg-meta');
      c.steps = el('span', 'nt-desk-pg-steps', '');
      c.ago = el('span', 'nt-desk-pg-ago', '');
      meta.appendChild(c.steps);
      meta.appendChild(c.ago);
      c.warn = el('div', 'nt-desk-pg-warn', '');
      [top, c.task, c.track, c.step, meta, c.warn].forEach(function (n) { c.node.appendChild(n); });
      c.node.addEventListener('click', function (ev) { openChat(ev, c.model); });
      return c;
    }
    function updateCard(c, m) {
      c.model = m;
      setText(c.name, m.name);
      setText(c.label, m.label);
      setText(c.task, m.task || (m.busy ? 'Working on a task' : 'No task'));
      setWidth(c.fill, m.percent);
      attr(c.track, 'aria-valuenow', String(m.percent));
      attr(c.track, 'aria-label', m.label);
      setText(c.step, m.step);
      setText(c.steps, m.steps);
      setText(c.ago, m.ago);
      setText(c.warn, m.warn);
      hide(c.warn, !m.warn);
      toggle(c.node, 'nt-desk-pg-stalled', m.stalled);
      toggle(c.node, 'nt-desk-pg-idle', !m.busy);
      if (m.url) attr(c.node, 'href', m.url);
      else if (c.node.getAttribute('href') != null) c.node.removeAttribute('href');
      attr(c.node, 'title', m.url ? 'Open ' + m.name + ' in Notion' : m.name);
    }
    function renderGrid(models) {
      if (!placeGrid()) return;
      var busy = models.filter(function (m) { return m.busy; }).length;
      setText(gridCount, models.length ? busy + ' of ' + models.length + ' busy' : '');
      hide(gridEmpty, models.length > 0);
      var seen = {};
      models.forEach(function (m, i) {
        seen[m.id] = true;
        var c = cardsById[m.id] || (cardsById[m.id] = buildCard());
        updateCard(c, m);
        var at = gridBody.childNodes[i] || null;
        if (at !== c.node) gridBody.insertBefore(c.node, at);
      });
      Object.keys(cardsById).forEach(function (id) {
        if (!seen[id]) { detach(cardsById[id].node); delete cardsById[id]; }
      });
    }

    function dropSide(thread) {
      var s = sideByThread[thread];
      if (!s) return;
      detach(s.bar);
      detach(s.phase);
      if (s.row) toggle(s.row, 'nt-desk-pg-row', false);
      delete sideByThread[thread];
    }
    function renderSide(models) {
      var byThread = {}, live = {};
      models.forEach(function (m) { if (m.thread) byThread[m.thread] = m; });
      all('[data-nt-desk="coworker"]').forEach(function (row) {
        var thread = row.getAttribute('data-nt-desk-thread') || '', m = byThread[thread];
        if (!m || !m.busy || live[thread]) return;
        live[thread] = true;
        var s = sideByThread[thread];
        if (!s) {
          s = sideByThread[thread] = { bar: el('span', 'nt-desk-pg-side'), phase: el('span', 'nt-desk-pg-phase', '') };
          s.fill = el('span', 'nt-desk-pg-side-fill');
          s.bar.setAttribute('aria-hidden', 'true');
          s.bar.appendChild(s.fill);
        }
        if (s.row && s.row !== row) toggle(s.row, 'nt-desk-pg-row', false);
        s.row = row;
        toggle(row, 'nt-desk-pg-row', true);
        if (s.phase.parentNode !== row) row.appendChild(s.phase);
        if (s.bar.parentNode !== row) row.appendChild(s.bar);
        setText(s.phase, m.label);
        attr(s.phase, 'title', m.warn || m.step);
        setWidth(s.fill, m.percent);
        toggle(s.bar, 'nt-desk-pg-stalled', m.stalled);
        toggle(s.phase, 'nt-desk-pg-stalled', m.stalled);
      });
      Object.keys(sideByThread).forEach(function (thread) { if (!live[thread]) dropSide(thread); });
    }

    function render() {
      if (stopped || Date.now() - lastRender < TICK_MS - 50) return false;  // at most one render per second
      lastRender = Date.now();
      renders += 1;
      var models = cards(state, nowS());
      try { renderGrid(models); } catch (e) { /* Live view changed under us; retry next tick */ }
      try { renderSide(models); } catch (e) { /* sidebar changed under us; retry next tick */ }
      return true;
    }
    function stopTimer() { if (timer != null) { clearInterval(timer); timer = null; } }
    function startTimer() { if (timer == null && !stopped && !document.hidden) timer = setInterval(tick, TICK_MS); }
    function tick() {
      if (stopped) return;
      if (document.hidden) { stopTimer(); return; }
      render();
    }
    function onVisibility() {
      if (stopped) return;
      if (document.hidden) { stopTimer(); return; }
      render();
      startTimer();
    }

    var off = typeof ctx.onState === 'function' ? ctx.onState(function (s) { state = obj(s); }) : null;
    document.addEventListener('visibilitychange', onVisibility);
    render();
    startTimer();

    function stop() {
      if (stopped) return;
      stopped = true;
      stopTimer();
      document.removeEventListener('visibilitychange', onVisibility);
      if (typeof off === 'function') { try { off(); } catch (e) { /* already gone */ } }
      Object.keys(sideByThread).forEach(dropSide);
      Object.keys(cardsById).forEach(function (id) { detach(cardsById[id].node); });
      cardsById = {};
      detach(grid);
      grid = null;
    }
    return { stop: stop, renderCount: function () { return renders; } };
  };
})();
