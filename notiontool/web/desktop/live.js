/*
 * NotionTool desktop part 'live': the Live view (window.__ntDesk.live = function (ctx) { ... return { stop, open, close } }).
 * Layout, top to bottom: Needs you (only what needs the user, one sentence + one button), Working now (one card
 * per open request with 'Step 3 of 6' and one line per step; click a step for its details), Team (coworker
 * avatars), History (collapsed, last 20 finished requests) and Details (collapsed technical numbers).
 * It registers as the 'Live' tab with window.__ntShell as soon as the shell exists (checked lazily); until then
 * it opens as the slide-in panel (#nt-live, Ctrl+Shift+L, Live chip in the brand header).
 * Actions use ctx.call: task_reconcile {id, resolution, note}, run_retry {id}, run_pause {id, paused},
 * run_cancel {id}, run_confirm {id}. Renders at most once per second and only sections whose input changed;
 * all timers stop while the document is hidden. Only removes its own nodes, never Notion nodes.
 */
(function () {
  'use strict';
  window.__ntDesk = window.__ntDesk || {};

  var HASH = '#nt-live';
  var RENDER_MS = 1000;
  var REFRESH_MS = 5000;
  var DOCK_MS = 1000;
  var ANIM_MS = 260;
  var STALE_MS = 15000;
  var HISTORY_LIMIT = 20;
  var EVENT_LIMIT = 10;
  var PILL_ID = 'nt-desk-live-pill';
  var PANEL_ID = 'nt-desk-live-panel';
  var STALE_SELECTORS = ['#' + PILL_ID, '#' + PANEL_ID, '[data-nt-desk="live-pill"]', '[data-nt-desk="live-panel"]',
    '[data-nt-desk="live-view"]'];
  var RUN_DONE = ['done', 'failed', 'cancelled', 'completed'];
  var BUSY = ['busy', 'running', 'sending'];

  function list(v) { return Array.isArray(v) ? v : []; }
  function obj(v) { return v && typeof v === 'object' && !Array.isArray(v) ? v : {}; }
  function str(v) { return v === null || v === undefined ? '' : String(v); }
  function num(v) {
    if (v === null || v === undefined || v === '' || typeof v === 'boolean') return null;
    var n = Number(v);
    return isFinite(n) ? n : null;
  }
  function token(v) { return String(v || '').toLowerCase().replace(/[^a-z0-9_-]/g, ''); }
  function stamp(v) {
    var n = num(v);
    if (n !== null) return n > 0 ? (n > 1e12 ? n : n * 1000) : null;
    var parsed = Date.parse(str(v));
    return isFinite(parsed) ? parsed : null;
  }
  function clip(v, limit) {
    var text = str(v).replace(/\s+/g, ' ').trim();
    limit = limit || 160;
    return text.length <= limit ? text : text.slice(0, limit - 1).replace(/\s+$/, '') + '\u2026';
  }
  function firstSentence(v, limit) {
    var text = clip(v, 100000);
    var match = /^(.*?[.!?])(\s|$)/.exec(text);
    return clip(match ? match[1] : text, limit || 160);
  }
  function span(seconds) {
    var s = Math.max(0, Math.floor(seconds || 0));
    if (s < 60) return s + 's';
    var m = Math.floor(s / 60);
    if (m < 60) return m + 'm';
    var h = Math.floor(m / 60);
    if (h < 24) return h + 'h' + (m % 60 ? ' ' + (m % 60) + 'm' : '');
    return Math.floor(h / 24) + 'd';
  }
  function ago(v, now) {
    var at = stamp(v);
    if (at === null) return '';
    var s = (now - at) / 1000;
    return s < 10 ? 'just now' : span(s) + ' ago';
  }
  function quote(text) { return '\u201c' + text + '\u201d'; }
  function byCreated(a, b) { return (stamp(obj(a).created) || 0) - (stamp(obj(b).created) || 0); }

  function taskSummary(t) { t = obj(t); return clip(t.summary) || firstSentence(t.title) || 'One step'; }
  function runSummary(r) { r = obj(r); return clip(r.summary) || firstSentence(r.goal || r.title) || 'A request'; }

  function taskStatus(t) {
    t = obj(t);
    var s = token(t.state);
    if (t.remote_unknown || s === 'uncertain' || s === 'failed' || s === 'blocked' || s === 'remote_unknown') return 'Problem';
    if (s === 'done' || s === 'completed' || s === 'accepted') return t.checked ? 'Checked' : 'Done';
    if (s === 'running' || s === 'sending' || s === 'reviewing' || s === 'cancelling') return 'Working';
    if (s === 'cancelled' || s === 'skipped') return 'Stopped';
    return 'Waiting';
  }

  function elapsedFor(t, now) {
    t = obj(t);
    var start = stamp(t.started) || stamp(t.started_at);
    if (start === null) return '';
    var status = taskStatus(t);
    if (status === 'Waiting') return '';
    var end = status === 'Working' ? now : (stamp(t.finished) || now);
    return span((end - start) / 1000);
  }

  function stepText(done, total) {
    if (!total) return 'Getting started';
    if (done >= total) return total === 1 ? 'The step is done' : 'All ' + total + ' steps done';
    return 'Step ' + (done + 1) + ' of ' + total;
  }

  function runTasks(s, run) {
    var id = obj(run).id;
    return list(obj(s).tasks).filter(function (t) { return obj(t).run_id === id; }).sort(byCreated);
  }

  function progressOf(run, tasks) {
    run = obj(run);
    var total = num(run.tasks_total) || tasks.length;
    var done = num(run.tasks_done);
    if (done === null) done = tasks.filter(function (t) { var w = taskStatus(t); return w === 'Done' || w === 'Checked'; }).length;
    done = Math.min(done, total);
    return { done: done, total: total, ratio: total ? done / total : 0, text: stepText(done, total) };
  }

  function openRuns(s) {
    return list(obj(s).runs).filter(function (r) { return obj(r).id && RUN_DONE.indexOf(token(r.state)) < 0; }).sort(byCreated);
  }

  function historyRows(s) {
    return list(obj(s).runs).filter(function (r) { return obj(r).id && RUN_DONE.indexOf(token(r.state)) >= 0; })
      .sort(function (a, b) { return (stamp(b.finished) || stamp(b.created) || 0) - (stamp(a.finished) || stamp(a.created) || 0); })
      .slice(0, HISTORY_LIMIT);
  }

  function outcomeText(r) {
    r = obj(r);
    var fallback = { done: 'Finished.', completed: 'Finished.', cancelled: 'Stopped before the end.', failed: 'Did not finish.' };
    return clip(r.outcome) || fallback[token(r.state)] || 'Finished.';
  }

  function outcomeClass(r) {
    var s = token(obj(r).state);
    return s === 'done' || s === 'completed' ? 'ok' : s === 'failed' ? 'bad' : 'off';
  }

  function acceptDef(t) {
    return { key: 'accept', label: 'Check & accept', action: 'task_reconcile', note: true,
      args: { id: t.id, resolution: 'accept', note: '' },
      title: 'Accept this answer?',
      text: 'Open the coworker chat and check the work first. Accepting keeps that work as the answer. Nothing is sent again.',
      done: 'Answer accepted' };
  }
  function confirmDef(r) {
    return { key: 'confirm', label: 'Confirm', action: 'run_confirm', args: { id: r.id },
      title: 'Confirm this request?', text: 'This marks the request as finished.', done: 'Request confirmed' };
  }
  function retryDef(r) {
    return { key: 'retry', label: 'Try again', action: 'run_retry', args: { id: r.id },
      title: 'Try the failed steps again?',
      text: 'The failed steps start again. Work that already reached Notion is not sent twice.', done: 'Trying again' };
  }
  function pauseDef(r) {
    var paused = !!r.paused;
    return { key: paused ? 'resume' : 'pause', label: paused ? 'Resume' : 'Pause', action: 'run_pause',
      args: { id: r.id, paused: !paused },
      title: paused ? 'Resume this request?' : 'Pause this request?',
      text: paused ? 'Coworkers pick up the open steps again.' : 'Running steps finish, no new steps start.',
      done: paused ? 'Request resumed' : 'Request paused' };
  }
  function cancelDef(r) {
    return { key: 'cancel', label: 'Stop', action: 'run_cancel', args: { id: r.id }, danger: true,
      title: 'Stop this request?', text: 'All open steps stop. This cannot be undone.', done: 'Request stopped' };
  }

  function runControls(r) {
    r = obj(r);
    var s = token(r.state);
    if (!r.id || RUN_DONE.indexOf(s) >= 0 || s === 'awaiting_main') return [];
    if (s === 'blocked') return [cancelDef(r)];
    return [pauseDef(r), cancelDef(r)];
  }

  function needsYou(s) {
    s = obj(s);
    var out = [];
    list(s.runs).forEach(function (r) {
      r = obj(r);
      if (!r.id) return;
      var st = token(r.state);
      if (st === 'awaiting_main') {
        out.push({ key: 'ok-' + r.id, text: quote(runSummary(r)) + ' is finished and waits for your OK.', def: confirmDef(r) });
      } else if (st === 'blocked') {
        out.push({ key: 'retry-' + r.id, text: 'Some steps of ' + quote(runSummary(r)) + ' ran into a problem.', def: retryDef(r) });
      }
    });
    list(s.tasks).forEach(function (t) {
      t = obj(t);
      if (!t.id) return;
      if (t.remote_unknown || token(t.state) === 'uncertain') {
        out.push({ key: 'accept-' + t.id, text: 'A coworker finished ' + quote(taskSummary(t)) + ', but the answer got lost.', def: acceptDef(t) });
      }
    });
    return out;
  }

  function initials(label) {
    var words = str(label).trim().split(/\s+/).filter(Boolean);
    if (!words.length) return '?';
    var lastWord = words.length > 1 ? words[words.length - 1] : '';
    var tail = /^\d+$/.test(lastWord) ? lastWord.slice(0, 2) : lastWord.charAt(0).toUpperCase();
    return words[0].charAt(0).toUpperCase() + tail;
  }

  function teamRows(s, now) {
    return list(obj(s).workers).filter(function (w) { return obj(w).purpose === 'worker'; }).map(function (w) {
      var st = token(w.state), word = 'Off', cls = 'off';
      if (BUSY.indexOf(st) >= 0 || w.task_id) { word = 'Busy'; cls = 'busy'; }
      else if (st === 'ready' || st === 'idle') { word = 'Idle'; cls = 'idle'; }
      else if (st === 'recovering' || st === 'starting' || st === 'creating') { word = 'Restarting'; cls = 'wait'; }
      else if (st === 'blocked' || st === 'failed' || st === 'error') { word = 'Problem'; cls = 'bad'; }
      var born = stamp(w.created_at);
      return { label: clip(w.label, 40) || 'Coworker', initials: initials(w.label), state: word, cls: cls,
        age: born === null ? '' : span((now - born) / 1000) };
    });
  }

  function connection(raw, now, lastPush) {
    if (!raw) return { cls: 'off', text: 'Waiting for the harness' };
    if (lastPush && now - lastPush > STALE_MS) return { cls: 'warn', text: 'No update for ' + span((now - lastPush) / 1000) };
    return { cls: 'on', text: 'Connected' };
  }

  function effortWord(v) { var t = str(v).trim(); return t ? t.charAt(0).toUpperCase() + t.slice(1) : 'Default'; }

  function humanKind(kind) {
    var text = str(kind).replace(/[_-]+/g, ' ').trim();
    return text ? text.charAt(0).toUpperCase() + text.slice(1) : 'Event';
  }

  window.__ntDesk.liveUtil = {
    HASH: HASH, PILL_ID: PILL_ID, PANEL_ID: PANEL_ID, RENDER_MS: RENDER_MS, STALE_SELECTORS: STALE_SELECTORS,
    taskStatus: taskStatus, taskSummary: taskSummary, runSummary: runSummary, firstSentence: firstSentence,
    elapsedFor: elapsedFor, stepText: stepText, progressOf: progressOf, openRuns: openRuns, historyRows: historyRows,
    outcomeText: outcomeText, needsYou: needsYou, runControls: runControls, teamRows: teamRows, initials: initials,
    connection: connection, span: span, ago: ago, humanKind: humanKind
  };

  window.__ntDesk.live = function (ctx) {
    ctx = ctx || {};
    var stopped = false, isOpen = false, shellShown = false, registered = false, shellHandle = null;
    var timer = null, refreshTimer = null, dockTimer = null, hideTimer = null;
    var last = 0, lastPush = 0, unsubscribe = null, owner = {};
    var slots = {}, sigs = {}, expanded = {}, folds = { history: false, details: false };
    var pending = null, busyAction = false;

    function el(tag, props, kids) {
      var node = document.createElement(tag);
      props = props || {};
      Object.keys(props).forEach(function (key) {
        var value = props[key];
        if (value === null || value === undefined || value === false) return;
        if (key === 'cls') node.className = value;
        else if (key === 'text') node.textContent = String(value);
        else node.setAttribute(key, String(value));
      });
      list(kids).forEach(function (kid) {
        if (kid) node.appendChild(typeof kid === 'string' ? document.createTextNode(kid) : kid);
      });
      return node;
    }
    function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
    function detach(node) { if (node && node.parentNode) node.parentNode.removeChild(node); }
    function docHidden() { return !!(typeof document !== 'undefined' && document.hidden); }
    function shown() { return !stopped && (isOpen || shellShown) && !docHidden(); }
    function state() { try { return typeof ctx.getState === 'function' ? ctx.getState() : null; } catch (e) { return null; } }
    function sig(value) { try { return JSON.stringify(value); } catch (e) { return String(Math.random()); } }

    var root = el('div', { cls: 'nt-desk-lv', 'data-nt-desk': 'live-view' });
    ['needs', 'working', 'team', 'history', 'details'].forEach(function (name) {
      slots[name] = root.appendChild(el('div', { cls: 'nt-desk-lv-slot nt-desk-lv-slot-' + name }));
    });
    var confirmBox = el('div', { cls: 'nt-desk-lv-confirm', role: 'alertdialog', 'aria-modal': 'true', 'aria-label': 'Please confirm' });
    confirmBox.hidden = true;
    root.appendChild(confirmBox);

    var connDot = el('span', { cls: 'nt-desk-lv-dot nt-desk-lv-dot-off', 'aria-hidden': 'true' });
    var connText = el('span', { cls: 'nt-desk-lv-conn-text', text: 'Waiting for the harness' });
    var closeButton = el('button', { cls: 'nt-desk-btn nt-desk-close', type: 'button', text: '\u2715', title: 'Close (Esc)', 'aria-label': 'Close Live' });
    var panelBody = el('div', { cls: 'nt-desk-live-body' }, [root]);
    var panel = el('div', { cls: 'nt-desk-live', id: PANEL_ID, 'data-nt-desk': 'live-panel', role: 'dialog', 'aria-label': 'NotionTool Live' }, [
      el('div', { cls: 'nt-desk-live-bar' }, [
        el('span', { cls: 'nt-desk-live-name', text: 'Live' }),
        el('span', { cls: 'nt-desk-lv-conn', role: 'status' }, [connDot, connText]),
        closeButton
      ]),
      panelBody
    ]);
    panel.hidden = true;
    var pillBadge = el('span', { cls: 'nt-desk-live-pill-badge', 'aria-hidden': 'true' });
    pillBadge.hidden = true;
    var pill = el('button', { cls: 'nt-desk-live-pill', id: PILL_ID, type: 'button', 'data-nt-desk': 'live-pill', title: 'Live (Ctrl+Shift+L)', 'aria-label': 'Open Live' }, [
      el('span', { cls: 'nt-desk-live-pill-dot', 'aria-hidden': 'true' }), el('span', { text: 'Live' }), pillBadge]);

    function section(name, title, kids, count) {
      return el('section', { cls: 'nt-desk-lv-section nt-desk-lv-' + name, 'aria-label': title }, [
        el('div', { cls: 'nt-desk-lv-head' }, [el('h3', { cls: 'nt-desk-lv-title', text: title }),
          count ? el('span', { cls: 'nt-desk-lv-count', text: String(count) }) : null])
      ].concat(kids));
    }
    function fold(name, title, count, build) {
      var openNow = !!folds[name];
      var toggle = el('button', { cls: 'nt-desk-lv-fold', type: 'button', 'aria-expanded': openNow ? 'true' : 'false' }, [
        el('span', { cls: 'nt-desk-lv-fold-icon', 'aria-hidden': 'true', text: openNow ? '\u25be' : '\u25b8' }),
        el('span', { cls: 'nt-desk-lv-title', text: title }),
        count ? el('span', { cls: 'nt-desk-lv-count', text: String(count) }) : null]);
      toggle.addEventListener('click', function () { folds[name] = !folds[name]; sigs[name] = null; renderNow(); });
      return el('section', { cls: 'nt-desk-lv-section nt-desk-lv-' + name + (openNow ? ' nt-desk-lv-folded-open' : ''), 'aria-label': title },
        [toggle].concat(openNow ? build() : []));
    }
    function empty(text) { return el('p', { cls: 'nt-desk-lv-empty', text: text }); }
    function detail(label, value) {
      return el('div', { cls: 'nt-desk-lv-detail' }, [el('span', { cls: 'nt-desk-lv-detail-label', text: label }),
        el('span', { cls: 'nt-desk-lv-detail-value', text: value || '-' })]);
    }
    function actionButton(def, primary) {
      var b = el('button', { cls: 'nt-desk-lv-btn' + (primary ? ' nt-desk-lv-btn-main' : '') + (def.danger ? ' nt-desk-lv-btn-danger' : ''), type: 'button', text: def.label });
      b.addEventListener('click', function (event) { if (event && event.stopPropagation) event.stopPropagation(); ask(def); });
      return b;
    }

    function needsSection(s) {
      var items = needsYou(s).map(function (item) {
        return el('div', { cls: 'nt-desk-lv-need' }, [el('p', { cls: 'nt-desk-lv-need-text', text: item.text }), actionButton(item.def, true)]);
      });
      return section('needs', 'Needs you', items.length ? items : [empty('Nothing needs you right now.')], items.length);
    }

    function taskDetails(t, workers) {
      var w = obj(workers[t.worker_id]);
      var files = list(t.files).map(str).filter(Boolean);
      return el('div', { cls: 'nt-desk-lv-details' }, [
        detail('Model', str(t.model) || 'Default'),
        detail('Effort', effortWord(t.effort)),
        detail('Coworker', clip(w.label, 40) || 'Not picked yet'),
        detail('Files', files.length ? files.join(', ') : 'None'),
        el('div', { cls: 'nt-desk-lv-detail nt-desk-lv-detail-result' }, [el('span', { cls: 'nt-desk-lv-detail-label', text: 'Result' }),
          el('pre', { cls: 'nt-desk-lv-result', text: str(t.result) || str(t.error) || 'No result yet.' })])
      ]);
    }

    function taskRow(t, workers, now) {
      var status = taskStatus(t), openNow = !!expanded[t.id];
      var head = el('button', { cls: 'nt-desk-lv-task-head', type: 'button', 'aria-expanded': openNow ? 'true' : 'false', title: openNow ? 'Hide details' : 'Show details' }, [
        el('span', { cls: 'nt-desk-lv-task-text', text: taskSummary(t) }),
        el('span', { cls: 'nt-desk-lv-status nt-desk-lv-status-' + status.toLowerCase(), text: status }),
        el('span', { cls: 'nt-desk-lv-time', text: elapsedFor(t, now) })]);
      head.addEventListener('click', function () {
        if (expanded[t.id]) delete expanded[t.id]; else expanded[t.id] = true;
        sigs.working = null;
        renderNow();
      });
      return el('li', { cls: 'nt-desk-lv-task' + (openNow ? ' nt-desk-lv-task-open' : '') }, [head, openNow ? taskDetails(t, workers) : null]);
    }

    function workingSection(s, now) {
      var workers = {};
      list(s.workers).forEach(function (w) { if (obj(w).id) workers[w.id] = w; });
      var cards = openRuns(s).map(function (r) {
        var own = runTasks(s, r).filter(function (t) { return obj(t).id; });
        var p = progressOf(r, own);
        var fill = el('div', { cls: 'nt-desk-lv-bar-fill' });
        fill.style.width = Math.round(p.ratio * 100) + '%';
        var controls = runControls(r).map(function (def) { return actionButton(def, false); });
        return el('article', { cls: 'nt-desk-lv-card' + (r.paused ? ' nt-desk-lv-card-paused' : '') }, [
          el('p', { cls: 'nt-desk-lv-goal', text: runSummary(r) }),
          el('div', { cls: 'nt-desk-lv-progress' }, [
            el('span', { cls: 'nt-desk-lv-step', text: p.text + (r.paused ? ' \u00b7 Paused' : '') }),
            el('div', { cls: 'nt-desk-lv-bar', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': String(p.total), 'aria-valuenow': String(p.done) }, [fill])]),
          el('ul', { cls: 'nt-desk-lv-tasks' }, own.length ? own.map(function (t) { return taskRow(t, workers, now); }) : [el('li', { cls: 'nt-desk-lv-empty', text: 'No steps yet.' })]),
          controls.length ? el('div', { cls: 'nt-desk-lv-controls' }, controls) : null]);
      });
      return section('working', 'Working now', cards.length ? cards : [empty('Nothing is running.')], cards.length);
    }

    function teamSection(s, now) {
      var team = teamRows(s, now);
      var busyCount = team.filter(function (m) { return m.cls === 'busy'; }).length;
      var row = team.map(function (m) {
        return el('div', { cls: 'nt-desk-lv-member nt-desk-lv-member-' + m.cls, title: m.label + ': ' + m.state + (m.age ? ', ' + m.age + ' old' : '') }, [
          el('span', { cls: 'nt-desk-lv-avatar', 'aria-hidden': 'true', text: m.initials }),
          el('span', { cls: 'nt-desk-lv-member-name', text: m.label }),
          el('span', { cls: 'nt-desk-lv-member-state', text: m.state + (m.age ? ' \u00b7 ' + m.age : '') })]);
      });
      return section('team', 'Team', row.length ? [el('div', { cls: 'nt-desk-lv-team' }, row)] : [empty('No coworkers yet.')],
        team.length ? busyCount + ' of ' + team.length + ' busy' : 0);
    }

    function historySection(s, now) {
      var rows = historyRows(s);
      return fold('history', 'History', rows.length, function () {
        if (!rows.length) return [empty('No finished requests yet.')];
        return [el('ul', { cls: 'nt-desk-lv-history' }, rows.map(function (r) {
          return el('li', { cls: 'nt-desk-lv-past nt-desk-lv-past-' + outcomeClass(r) }, [
            el('span', { cls: 'nt-desk-lv-past-goal', text: runSummary(r) }),
            el('span', { cls: 'nt-desk-lv-past-outcome', text: outcomeText(r) }),
            el('span', { cls: 'nt-desk-lv-time', text: ago(r.finished || r.created, now) })]);
        }))];
      });
    }

    function detailsSection(raw, s, now) {
      return fold('details', 'Details', 0, function () {
        var rate = obj(s.rate), team = list(s.workers).filter(function (w) { return obj(w).purpose === 'worker'; });
        var busyCount = team.filter(function (w) { return BUSY.indexOf(token(w.state)) >= 0 || w.task_id; }).length;
        var openSteps = list(s.tasks).filter(function (t) { var w = taskStatus(t); return w === 'Waiting' || w === 'Working'; }).length;
        var used = num(rate.used_per_min), limit = num(rate.limit_per_min);
        var kids = [
          detail('Connection', connection(raw, now, lastPush).text),
          detail('Requests per minute', used === null ? '-' : used + (limit ? ' of ' + limit : '')),
          detail('Coworkers busy', busyCount + ' of ' + team.length),
          detail('Open steps', String(openSteps))
        ];
        team.forEach(function (w) {
          var usage = obj(w.context_usage), tokens = num(usage.estimated_tokens);
          if (tokens !== null) kids.push(detail('Chat size ' + (clip(w.label, 30) || 'Coworker'), Math.round(tokens / 1000) + 'k tokens'));
        });
        list(s.attention).slice(0, 5).forEach(function (a) {
          var text = clip(obj(a).message || obj(a).text || a, 200);
          if (text) kids.push(detail('Note', text));
        });
        var events = list(s.events).slice(-EVENT_LIMIT).reverse();
        if (events.length) {
          kids.push(el('ul', { cls: 'nt-desk-lv-events' }, events.map(function (ev) {
            ev = obj(ev);
            return el('li', { cls: 'nt-desk-lv-event' }, [el('span', { cls: 'nt-desk-lv-event-kind', text: humanKind(ev.kind) }),
              el('span', { cls: 'nt-desk-lv-event-text', text: clip(ev.message, 140) }), el('span', { cls: 'nt-desk-lv-time', text: ago(ev.ts, now) })]);
          })));
        }
        return [el('div', { cls: 'nt-desk-lv-details nt-desk-lv-tech' }, kids)];
      });
    }

    function paint(name, key, build) {
      if (sigs[name] === key) return;
      sigs[name] = key;
      clear(slots[name]);
      slots[name].appendChild(build());
    }

    function updateConnection(raw, now) {
      var c = connection(raw, now, lastPush);
      connDot.className = 'nt-desk-lv-dot nt-desk-lv-dot-' + c.cls;
      connText.textContent = c.text;
    }

    function updatePill() {
      var count = needsYou(state()).length;
      pillBadge.hidden = !count;
      pillBadge.textContent = count ? String(count) : '';
      pill.className = 'nt-desk-live-pill' + (count ? ' nt-desk-live-pill-alert' : '') + (isOpen || shellShown ? ' nt-desk-live-pill-on' : '');
      pill.setAttribute('aria-label', count ? 'Open Live, ' + count + ' need you' : 'Open Live');
    }

    function render() {
      timer = null;
      last = Date.now();
      if (!shown()) return;
      var raw = state(), s = obj(raw), now = Date.now(), tick = Math.floor(now / REFRESH_MS);
      updateConnection(raw, now);
      try {
        paint('needs', sig([s.runs, s.tasks]), function () { return needsSection(s); });
        paint('working', sig([s.runs, s.tasks, s.workers, tick, Object.keys(expanded)]), function () { return workingSection(s, now); });
        paint('team', sig([s.workers, tick]), function () { return teamSection(s, now); });
        paint('history', sig([folds.history, s.runs, folds.history ? tick : 0]), function () { return historySection(s, now); });
        paint('details', sig([folds.details, folds.details ? [s.rate, s.workers, s.tasks, s.attention, list(s.events).slice(-EVENT_LIMIT), tick, !!raw] : 0]),
          function () { return detailsSection(raw, s, now); });
      } catch (e) {
        if (window.console && console.warn) console.warn('[NotionTool] Live view failed', e);
      }
      updatePill();
    }
    function schedule() {
      if (!shown() || timer) return;
      timer = setTimeout(render, Math.max(0, RENDER_MS - (Date.now() - last)));
    }
    function renderNow() {
      if (timer) { clearTimeout(timer); timer = null; }
      render();
    }
    function startRefresh() {
      if (refreshTimer || !shown() || typeof setInterval !== 'function') return;
      refreshTimer = setInterval(schedule, REFRESH_MS);
    }
    function stopRefresh() {
      if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; }
      if (timer) { clearTimeout(timer); timer = null; }
    }
    function startDock() {
      if (dockTimer || stopped || typeof setInterval !== 'function') return;
      dockTimer = setInterval(dock, DOCK_MS);
    }

    function hideConfirm() { pending = null; confirmBox.hidden = true; clear(confirmBox); }
    function ask(def) {
      if (busyAction) return;
      pending = def;
      clear(confirmBox);
      var noteBox = def.note ? el('textarea', { cls: 'nt-desk-lv-note', rows: '2', placeholder: 'Note for the record (optional)', 'aria-label': 'Note' }) : null;
      var ok = el('button', { cls: 'nt-desk-lv-btn nt-desk-lv-btn-main' + (def.danger ? ' nt-desk-lv-btn-danger' : ''), type: 'button', text: def.label });
      var cancel = el('button', { cls: 'nt-desk-lv-btn', type: 'button', text: 'Cancel' });
      ok.addEventListener('click', function () { perform(def, noteBox ? str(noteBox.value).slice(0, 500) : ''); });
      cancel.addEventListener('click', hideConfirm);
      confirmBox.appendChild(el('div', { cls: 'nt-desk-lv-confirm-card' }, [
        el('h4', { cls: 'nt-desk-lv-confirm-title', text: def.title }), el('p', { cls: 'nt-desk-lv-confirm-text', text: def.text }),
        noteBox, el('div', { cls: 'nt-desk-lv-controls' }, [cancel, ok])]));
      confirmBox.hidden = false;
      if (ok.focus) ok.focus();
    }
    function toast(text, bad) { if (typeof ctx.toast === 'function') { try { ctx.toast(text, bad ? 'error' : 'ok'); } catch (e) { /* toast is optional */ } } }
    function perform(def, noteText) {
      if (busyAction || typeof ctx.call !== 'function') { hideConfirm(); return; }
      var args = {};
      Object.keys(def.args).forEach(function (k) { args[k] = def.args[k]; });
      if (def.note) args.note = noteText || '';
      busyAction = true;
      hideConfirm();
      var done = function (ok, error) {
        busyAction = false;
        toast(ok ? def.done + '.' : 'That did not work: ' + clip(error && error.message ? error.message : error, 120), !ok);
        sigs = {};
        schedule();
      };
      try {
        Promise.resolve(ctx.call(def.action, args)).then(function () { done(true); }, function (e) { done(false, e); });
      } catch (e) { done(false, e); }
    }

    function attach(parent) { if (root.parentNode !== parent) { detach(root); parent.appendChild(root); } }

    function openPanel() {
      if (stopped) return;
      if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
      attach(panelBody);
      if (panel.parentNode !== document.body) document.body.appendChild(panel);
      isOpen = true;
      panel.hidden = false;
      void panel.offsetWidth;
      panel.classList.add('nt-desk-live-open');
      renderNow();
      startRefresh();
      updatePill();
      if (closeButton.focus) closeButton.focus();
    }
    function open() {
      if (stopped) return;
      tryRegister();
      var sh = window.__ntShell;
      if (registered && sh && typeof sh.open === 'function') {
        try { sh.open('live'); return; } catch (e) { /* fall back to the panel */ }
      }
      openPanel();
    }
    function close() {
      if (!isOpen) return;
      isOpen = false;
      hideConfirm();
      panel.classList.remove('nt-desk-live-open');
      if (!shellShown) stopRefresh();
      hideTimer = setTimeout(function () { hideTimer = null; if (!isOpen) panel.hidden = true; }, ANIM_MS);
      if (location.hash === HASH && window.history && typeof history.replaceState === 'function') {
        try { history.replaceState(null, '', location.pathname + location.search); } catch (e) { /* keep the hash */ }
      }
      updatePill();
    }

    function shellMount(host) {
      var node = host && typeof host.appendChild === 'function' ? host : (obj(host).el || obj(host).container);
      if (stopped || !node || typeof node.appendChild !== 'function') return null;
      if (isOpen) close();
      attach(node);
      shellShown = true;
      sigs = {};
      renderNow();
      startRefresh();
      updatePill();
      return { unmount: shellUnmount, stop: shellUnmount };
    }
    function shellUnmount() {
      if (!shellShown) return;
      shellShown = false;
      hideConfirm();
      if (root.parentNode !== panelBody) attach(panelBody);
      if (!isOpen) stopRefresh();
      updatePill();
    }
    function tryRegister() {
      if (registered || stopped) return registered;
      var sh = window.__ntShell;
      if (!sh) return false;
      var fn = null;
      ['registerTab', 'registerView', 'register'].forEach(function (k) { if (!fn && typeof sh[k] === 'function') fn = sh[k]; });
      if (!fn) return false;
      try {
        shellHandle = fn.call(sh, { id: 'live', label: 'Live', title: 'Live', order: 20,
          mount: shellMount, unmount: shellUnmount, show: shellMount, hide: shellUnmount });
        registered = true;
      } catch (e) { registered = false; }
      return registered;
    }

    function queryAll(selector) {
      try { return typeof document.querySelectorAll === 'function' ? Array.prototype.slice.call(document.querySelectorAll(selector)) : []; } catch (e) { return []; }
    }
    function sweep() {
      STALE_SELECTORS.forEach(function (selector) {
        queryAll(selector).forEach(function (node) { if (node !== panel && node !== pill && node !== root) detach(node); });
      });
    }
    function dock() {
      if (stopped) return;
      if (window.__ntDeskLiveOwner !== owner) { stop(); return; }
      tryRegister();
      var brand = queryAll('[data-nt-desk="brand"]')[0];
      if (brand && pill.parentNode !== brand) brand.appendChild(pill);
      sweep();
      updatePill();
    }

    function onKey(event) {
      if (stopped || !event) return;
      var key = String(event.key || '').toLowerCase();
      if (event.ctrlKey && event.shiftKey && key === 'l') {
        if (event.preventDefault) event.preventDefault();
        if (isOpen) close(); else open();
        return;
      }
      if (event.key === 'Escape') {
        if (pending) { hideConfirm(); return; }
        if (isOpen) close();
      }
    }
    function onHash() { if (location.hash === HASH) open(); }
    function onVisibility() {
      if (docHidden()) {
        stopRefresh();
        if (dockTimer) { clearInterval(dockTimer); dockTimer = null; }
        return;
      }
      startDock();
      if (shown()) { sigs = {}; startRefresh(); schedule(); }
    }
    function onPush() { lastPush = Date.now(); schedule(); if (!shown()) updatePill(); }

    function stop() {
      if (stopped) return;
      stopped = true;
      stopRefresh();
      if (dockTimer) clearInterval(dockTimer);
      if (hideTimer) clearTimeout(hideTimer);
      dockTimer = hideTimer = null;
      window.removeEventListener('keydown', onKey, true);
      window.removeEventListener('hashchange', onHash);
      if (typeof document.removeEventListener === 'function') document.removeEventListener('visibilitychange', onVisibility);
      if (typeof unsubscribe === 'function') { try { unsubscribe(); } catch (e) { /* already gone */ } }
      var sh = window.__ntShell;
      if (registered && sh) {
        try {
          if (shellHandle && typeof shellHandle.unregister === 'function') shellHandle.unregister();
          else if (typeof sh.unregister === 'function') sh.unregister('live');
        } catch (e) { /* shell already gone */ }
      }
      detach(root);
      detach(panel);
      detach(pill);
      if (window.__ntDeskLiveOwner === owner) window.__ntDeskLiveOwner = null;
    }

    window.__ntDeskLiveOwner = owner;
    sweep();
    closeButton.addEventListener('click', close);
    pill.addEventListener('click', function () { if (isOpen) close(); else open(); });
    document.body.appendChild(pill);
    document.body.appendChild(panel);
    window.addEventListener('keydown', onKey, true);
    window.addEventListener('hashchange', onHash);
    if (typeof document.addEventListener === 'function') document.addEventListener('visibilitychange', onVisibility);
    if (typeof ctx.onState === 'function') unsubscribe = ctx.onState(onPush);
    tryRegister();
    if (!docHidden()) startDock();
    dock();
    if (location.hash === HASH) open();
    return { stop: stop, open: open, close: close };
  };
})();
