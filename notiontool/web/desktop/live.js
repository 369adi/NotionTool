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
    if (t.remote_working && s === 'uncertain') return 'Working';  // B46: still working in Notion after the stream ended
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
      if ((t.remote_unknown || token(t.state) === 'uncertain') && !t.remote_working) {
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
    var working = {};  // B46: coworkers still working in Notion after the live stream ended
    list(obj(s).tasks).forEach(function (t) { t = obj(t); if (t.remote_working && t.worker_id && token(t.state) === 'uncertain') working[t.worker_id] = 1; });
    return list(obj(s).workers).filter(function (w) { return obj(w).purpose === 'worker'; }).map(function (w) {
      var st = token(w.state), word = 'Off', cls = 'off';
      if (BUSY.indexOf(st) >= 0 || w.task_id || working[w.id]) { word = 'Busy'; cls = 'busy'; }
      else if (st === 'ready' || st === 'idle') { word = 'Idle'; cls = 'idle'; }
      else if (st === 'recovering' || st === 'starting' || st === 'creating') { word = 'Restarting'; cls = 'wait'; }
      else if (st === 'blocked' || st === 'failed' || st === 'error') { word = 'Problem'; cls = 'bad'; }
      var born = stamp(w.created_at);
      return { label: clip(w.label, 40) || 'Coworker', initials: initials(w.label), state: word, cls: cls,
        age: born === null ? '' : span((now - born) / 1000) };
    });
  }

  // Live map (reference v6 Roles / v7 Direct): Main on top, TEAM_MAX coworkers per row; more coworkers wrap to the next row.
  // Mode comes from the newest open run (allocation.mode or mode); without it, analyse/review roles mean Roles.
  var TEAM_MAX = 4;
  var ROLE_LABEL = { researcher: 'Researcher', architect: 'Architect', executor: 'Worker', quality_planner: 'Quality planner',
    reviewer: 'Quality planner' };
  var ROLE_STAGE = { researcher: 'analyse', architect: 'analyse', executor: 'build', quality_planner: 'review', reviewer: 'review' };
  var STAGES = [['analyse', 'Analyse'], ['build', 'Build'], ['review', 'Review']];
  // Roles mode: three labelled stage rows. Coworkers without a role step wait in Build; an empty Review shows an idle Quality planner.
  function liveStages(cards) {
    return STAGES.map(function (st) {
      var own = list(cards).filter(function (c) { return c.stage === st[0]; });
      if (!own.length && st[0] === 'review') own = [{ id: '', slot: true, label: 'Quality planner', kind: 'idle', text: 'Idle', role: 'Quality planner', stage: 'review' }];
      return { id: st[0], label: st[1], cards: own };
    });
  }
  function newestRun(s) { var runs = openRuns(s); return runs.length ? obj(runs[runs.length - 1]) : null; }
  function liveMode(s) {
    var run = newestRun(s);
    if (!run) return 'direct';
    var m = token(obj(run.allocation).mode || run.mode);
    if (m === 'direct' || m === 'roles') return m;
    return runTasks(s, run).some(function (t) { var r = token(obj(t).role); return !!ROLE_LABEL[r] && r !== 'executor'; }) ? 'roles' : 'direct';
  }
  function findTask(s, id) { return id ? list(obj(s).tasks).filter(function (t) { return obj(t).id === id; })[0] || null : null; }
  function findRun(s, id) { return id ? list(obj(s).runs).filter(function (r) { return obj(r).id === id; })[0] || null : null; }
  // The coworker's current step: its task_id, else its newest open step (working steps first).
  function workerTask(s, w) {
    w = obj(w);
    var own = findTask(s, w.task_id);
    if (own) return obj(own);
    var open = list(obj(s).tasks).filter(function (t) {
      t = obj(t);
      var st = taskStatus(t);
      return !!t.worker_id && t.worker_id === w.id && (st === 'Working' || st === 'Waiting' || st === 'Problem');
    }).sort(byCreated);
    var pick = open.filter(function (t) { return taskStatus(t) === 'Working'; });
    if (!pick.length) pick = open;
    return pick.length ? obj(pick[pick.length - 1]) : null;
  }
  // 'waits for Cowork 2': the coworker of the first unfinished step this step depends on.
  function waitsFor(s, t, labels) {
    t = obj(t); labels = obj(labels);
    var deps = list(t.deps).concat(list(t.depends_on));
    for (var i = 0; i < deps.length; i++) {
      var d = findTask(s, deps[i]);
      if (!d) continue;
      var st = taskStatus(d);
      if (st !== 'Done' && st !== 'Checked') return labels[obj(d).worker_id] || quote(taskSummary(d));
    }
    return '';
  }
  // Answer = what the coworker waits for from the user, through the existing endpoints:
  // lost answer -> task_reconcile accept, request waits for the OK -> run_confirm, failed steps -> run_retry.
  function answerDef(s, t) {
    t = obj(t);
    if (!t.id) return null;
    if ((t.remote_unknown || token(t.state) === 'uncertain') && !t.remote_working) return acceptDef(t);
    var run = obj(findRun(s, t.run_id)), st = token(run.state);
    if (run.id && st === 'awaiting_main') return confirmDef(run);
    if (run.id && st === 'blocked') return retryDef(run);
    return null;
  }
  // Stop = run_cancel of the coworker's request (POST /api/run/cancel).
  function stopDef(s, t) {
    return runControls(findRun(s, obj(t).run_id)).filter(function (d) { return d.key === 'cancel'; })[0] || null;
  }
  function liveCards(s, now) {
    s = obj(s);
    var mode = liveMode(s), labels = {}, rows = teamRows(s, now);
    var workers = list(s.workers).filter(function (w) { return obj(w).purpose === 'worker'; });
    workers.forEach(function (w) { labels[w.id] = clip(w.label, 40) || 'Coworker'; });
    return workers.map(function (w, i) {
      var t = workerTask(s, w), status = t ? taskStatus(t) : '', row = obj(rows[i]), r = t ? token(t.role) : '';
      var wait = status === 'Waiting' ? waitsFor(s, t, labels) : '';
      var answer = t ? answerDef(s, t) : null;
      var kind = answer ? 'ask' : wait ? 'wait' : row.cls;
      var text = answer ? 'Needs your answer' : wait ? 'waits for ' + wait : row.cls === 'busy' ? 'Working' : row.state;
      return { id: w.id, label: labels[w.id], kind: kind, text: text, task: t ? taskSummary(t) : '', taskId: t ? str(t.id) : '',
        role: mode === 'roles' && t ? ROLE_LABEL[r] || '' : '', stage: mode === 'roles' ? ROLE_STAGE[r] || 'build' : '',
        elapsed: t ? elapsedFor(t, now) : '',
        answer: answer, stop: t ? stopDef(s, t) : null };
    });
  }
  function mainInfo(s) {
    var main = obj(list(obj(s).workers).filter(function (w) { return obj(w).purpose === 'main'; })[0]);
    var run = newestRun(s);
    return { label: clip(main.label, 40) || 'Main', busy: BUSY.indexOf(token(main.state)) >= 0,
      goal: run ? runSummary(run) : '', step: run ? progressOf(run, runTasks(s, run)).text : '' };
  }
  function cardEvents(s, card) {
    var evs = list(obj(s).events);
    if (card) {
      evs = evs.filter(function (e) {
        e = obj(e);
        return (!!e.worker_id && e.worker_id === card.id) || (!!card.taskId && e.task_id === card.taskId) || (!!e.worker && e.worker === card.label);
      });
    }
    return evs.slice(-EVENT_LIMIT).reverse();
  }
  // Rail badge: coworkers that really run right now (it counted every attention item before).
  function runningCount(s) { return teamRows(s, Date.now()).filter(function (m) { return m.cls === 'busy'; }).length; }

  function connection(raw, now, lastPush) {
    if (!raw) return { cls: 'off', text: 'Waiting for the harness' };
    if (lastPush && now - lastPush > STALE_MS) return { cls: 'warn', text: 'No update for ' + span((now - lastPush) / 1000) };
    return { cls: 'on', text: 'Connected' };
  }

  // Problems (top of Live, merged with 'Needs you'): one list of everything that went wrong.
  // Each item: {key, what, todo, def (one action or null), at, count}. Identical problems are merged.
  var WARN_EVENTS = ['welcome_warning', 'welcome_failed', 'team_warning', 'team_grow_failed', 'team_growth_failed'];
  var PROBLEM_TASK_STATES = ['failed', 'blocked', 'error'];
  function problems(s, opts) {
    s = obj(s); opts = obj(opts);
    var dismissed = obj(opts.dismissed), byKey = {}, bySig = {}, out = [];
    var runs = {};
    list(s.runs).forEach(function (r) { r = obj(r); if (r.id) runs[r.id] = r; });
    function add(p) {
      var sigKey = p.what + '|' + p.todo, prev = byKey[p.key] || bySig[sigKey];
      if (prev) {
        prev.count += 1;
        if ((stamp(p.at) || 0) > (stamp(prev.at) || 0)) prev.at = p.at;
        return;
      }
      p.count = 1;
      byKey[p.key] = bySig[sigKey] = p;
      out.push(p);
    }
    list(s.runs).forEach(function (r) {
      r = obj(r);
      if (!r.id) return;
      var st = token(r.state), at = r.finished || r.created;
      if (st === 'awaiting_main') {
        var why = [];
        Object.keys(obj(r.needs_main_reasons)).forEach(function (k) {
          var v = String(obj(r.needs_main_reasons)[k] || '').trim();
          if (v && why.indexOf(v) < 0) why.push(v);
        });
        add({ key: 'ok-' + r.id, what: quote(runSummary(r)) + ' is finished and waits for your OK' +
            (why.length ? ': ' + clip(why.join('; '), 160) : '.'),
          todo: 'Check the result, then confirm it.', def: confirmDef(r), at: at });
      } else if (st === 'blocked') {
        add({ key: 'retry-' + r.id, what: 'Some steps of ' + quote(runSummary(r)) + ' ran into a problem.',
          todo: 'Try the failed steps again, or stop the request.', def: retryDef(r), at: at });
      }
    });
    list(s.tasks).forEach(function (t) {
      t = obj(t);
      if (!t.id) return;
      var st = token(t.state), at = t.finished || t.last_output_at || t.started || t.created;
      if ((t.remote_unknown || st === 'uncertain') && !t.checked && !t.remote_working) {
        add({ key: 'accept-' + t.id, what: 'A coworker finished ' + quote(taskSummary(t)) + ', but the answer got lost.',
          todo: 'Check the work, then accept it if it looks right. Nothing is sent again.', def: acceptDef(t), at: at });
      } else if (PROBLEM_TASK_STATES.indexOf(st) >= 0) {
        var reason = firstSentence(t.error);
        var blockedRun = token(obj(runs[t.run_id]).state) === 'blocked';
        add({ key: 'task-' + t.id, what: 'The step ' + quote(taskSummary(t)) + ' did not finish' + (reason ? ': ' + clip(reason, 120) : '.'),
          todo: blockedRun ? 'Use Try again on its request.' : 'Open the request under Working to see what went wrong.', def: null, at: at });
      }
      var proof = obj(t.proof);
      if (proof.tests_status === 'failed') {
        add({ key: 'proof-' + t.id, what: 'Tests failed in the proof of ' + quote(taskSummary(t)) + '.',
          todo: 'Open the step under Working to see which tests failed.', def: null, at: proof.created || at });
      }
    });
    list(s.events).forEach(function (ev) {
      ev = obj(ev);
      var kind = token(ev.kind), msg = clip(firstSentence(ev.message) || ev.message, 160);
      if (msg && !/[.!?]$/.test(msg)) msg += '.';
      if (WARN_EVENTS.indexOf(kind) >= 0) {
        add({ key: 'ev-' + kind + '-' + msg, what: msg || 'Creating a chat or coworker did not work.',
          todo: 'Check the chats under Team. NotionTool does not try again on its own.', def: null, at: ev.ts });
      } else if (kind === 'machine_check_failed') {
        add({ key: 'ev-mc-' + str(ev.task_id || msg), what: 'An automatic check failed: ' + (msg || 'a step did not pass the machine check.'),
          todo: 'Open the request under Working and check the result before you confirm it.', def: null, at: ev.ts });
      } else if (kind === 'ui_error') {
        add({ key: 'ev-ui-' + msg, what: msg || 'A part of the NotionTool overlay hit an error.',
          todo: 'Usually harmless. If Live looks wrong, close it and open it again.', def: null, at: ev.ts });
      }
    });
    // Attention lists from the harness (notion_shell attention): stalled steps and steps tied to a gone chat.
    function attentionItems(v) {
      if (Array.isArray(v)) return v;
      v = obj(v);
      return Object.keys(v).map(function (k) { var e = obj(v[k]); if (!e.task_id) e.task_id = k; return e; });
    }
    attentionItems(s.stalled).forEach(function (e) {
      e = obj(e);
      var id = str(e.task_id || e.id);
      if (!id) return;
      var mins = Math.round((Number(e.stall_age_s) || 0) / 60);
      add({ key: 'stall-' + id, what: 'The step ' + quote(e.title || id) + ' has written nothing' + (mins ? ' for ' + mins + ' min' : '') +
        (e.worker_label ? ' (' + clip(e.worker_label, 40) + ')' : '') + '.',
        todo: 'Open it under Working. If it stays stuck, stop its request.', def: null, at: null });
    });
    attentionItems(s.stale_context_tasks).forEach(function (e) {
      e = obj(e);
      var id = str(e.id || e.task_id);
      if (!id) return;
      add({ key: 'stale-' + id, what: 'The step ' + quote(e.title || id) + ' still belongs to an old coworker chat.',
        todo: 'Stop or retry its request so it runs in a current chat.', def: null, at: null });
    });
    var c = opts.connection;
    if (c && (c.cls === 'off' || c.cls === 'warn')) {
      add({ key: 'conn', what: c.cls === 'off' ? 'The harness is not connected.' : 'The harness sent ' + c.text.toLowerCase() + '.',
        todo: 'Wait a minute. If it stays like this, use the safe restart in Settings.', def: null, at: opts.now ? opts.now / 1000 : null });
    }
    return out.filter(function (p) { return !dismissed[p.key]; }).sort(function (a, b) {
      if (!!a.def !== !!b.def) return a.def ? -1 : 1;
      return (stamp(b.at) || 0) - (stamp(a.at) || 0);
    });
  }

  function effortWord(v) { var t = str(v).trim(); return t ? t.charAt(0).toUpperCase() + t.slice(1) : 'Default'; }

  function humanKind(kind) {
    var text = str(kind).replace(/[_-]+/g, ' ').trim();
    return text ? text.charAt(0).toUpperCase() + text.slice(1) : 'Event';
  }

  // Request templates: same texts as notiontool/templates.py (a test keeps them equal). Drafts are never sent.
  var REQUEST_FIELDS = [['what', 'What', 'What should change, in one or two sentences'],
    ['where', 'Where', 'Page, file, screen or link'], ['done_when', 'Done when', 'How we know it is finished']];
  var REQUEST_TEMPLATES = [
    { id: 'ui_bug', title: 'Fix a UI bug', hint: 'Something on screen looks or works wrong.', checks: [
      'Reproduce the bug first and describe what you saw.', 'Fix it with the smallest change that works.',
      'Run the related tests and the desktop tests, and node --check for changed scripts.', 'Check the screen again and say what changed.'] },
    { id: 'feature', title: 'Build a feature', hint: 'Add something new.', checks: [
      'Split the work into small steps before you start.', 'Keep existing behaviour and settings unchanged.',
      'Add tests for the new behaviour and run the related tests.', 'Explain how to use it in plain words.'] },
    { id: 'review', title: 'Review and test', hint: 'Check existing work and run the tests.', checks: [
      'Read the changed files and list problems you find.', 'Run the full test suite and report failures with the test name.',
      'Do not change code unless a fix is clearly needed; ask first for larger changes.', 'End with a short verdict: ready or not ready.'] },
    { id: 'research', title: 'Research', hint: 'Find out how something works or what to do.', checks: [
      'Collect the facts first and name your sources.', 'Do not change any files.',
      'Give two or three options with pros and cons.', 'End with one clear recommendation.'] }
  ];
  function requestTemplate(id) {
    var key = str(id).trim().toLowerCase();
    return REQUEST_TEMPLATES.filter(function (t) { return t.id === key || t.title.toLowerCase() === key; })[0] || null;
  }
  function cleanField(v) {
    return str(v).trim().split(/\r?\n/).map(function (line) { return line.split(/\s+/).filter(Boolean).join(' '); }).join('\n').trim().slice(0, 2000);
  }
  function requestDraft(id, values) {
    var t = requestTemplate(id);
    if (!t) return '';
    values = obj(values);
    var lines = ['New request: ' + t.title, ''];
    REQUEST_FIELDS.forEach(function (f) { lines.push(f[1] + ': ' + (cleanField(values[f[0]]) || '(fill in)')); });
    lines.push('', 'Checks before you report done:');
    t.checks.forEach(function (step, i) { lines.push((i + 1) + '. ' + step); });
    return lines.join('\n');
  }

  window.__ntDesk.liveUtil = {
    REQUEST_TEMPLATES: REQUEST_TEMPLATES, REQUEST_FIELDS: REQUEST_FIELDS, requestTemplate: requestTemplate, requestDraft: requestDraft,
    HASH: HASH, PILL_ID: PILL_ID, PANEL_ID: PANEL_ID, RENDER_MS: RENDER_MS, STALE_SELECTORS: STALE_SELECTORS,
    taskStatus: taskStatus, taskSummary: taskSummary, runSummary: runSummary, firstSentence: firstSentence,
    elapsedFor: elapsedFor, stepText: stepText, progressOf: progressOf, openRuns: openRuns, historyRows: historyRows,
    outcomeText: outcomeText, needsYou: needsYou, runControls: runControls, teamRows: teamRows, initials: initials,
    connection: connection, span: span, ago: ago, humanKind: humanKind, problems: problems,
    TEAM_MAX: TEAM_MAX, ROLE_LABEL: ROLE_LABEL, liveMode: liveMode, liveCards: liveCards, liveStages: liveStages, waitsFor: waitsFor,
    answerDef: answerDef, stopDef: stopDef, mainInfo: mainInfo, runningCount: runningCount
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
    // Reference layout: the team map (mode badge, Main, one coworker row) and a 340 px side panel.
    slots.map = root.appendChild(el('div', { cls: 'nt-desk-lv-slot nt-desk-lv-slot-map' }));
    var side = root.appendChild(el('div', { cls: 'nt-desk-lv-side' }));
    slots.focus = side.appendChild(el('div', { cls: 'nt-desk-lv-slot nt-desk-lv-slot-focus' }));
    ['reply', 'budget', 'needs', 'working', 'team', 'history', 'details'].forEach(function (name) {
      slots[name] = side.appendChild(el('div', { cls: 'nt-desk-lv-slot nt-desk-lv-slot-' + name }));
    });
    var confirmBox = el('div', { cls: 'nt-desk-lv-confirm', role: 'alertdialog', 'aria-modal': 'true', 'aria-label': 'Please confirm' });
    confirmBox.hidden = true;
    side.appendChild(confirmBox);

    // ---- New request: a template fills a draft into the Notion chat box. The user checks and sends it (never sent here).
    var requestBox = el('div', { cls: 'nt-desk-lv-request', role: 'dialog', 'aria-label': 'New request' });
    requestBox.hidden = true;
    side.appendChild(requestBox);
    var newRequestButton = el('button', { cls: 'nt-desk-lv-btn nt-desk-lv-btn-main nt-desk-lv-new-request', type: 'button',
      text: 'New request', title: 'Start a request from a template' });
    newRequestButton.addEventListener('click', function () { showRequest(''); });
    var requestBar = el('div', { cls: 'nt-desk-lv-toolbar' }, [newRequestButton]);
    if (typeof side.insertBefore === 'function' && side.firstChild) side.insertBefore(requestBar, side.firstChild); else side.appendChild(requestBar);
    function clearOwn(node) { clear(node); }
    var requestInputs = {}, requestText = null, requestId = '';
    function hideRequest() { requestBox.hidden = true; clearOwn(requestBox); requestInputs = {}; requestText = null; requestId = ''; }
    function draftValues() { var v = {}; Object.keys(requestInputs).forEach(function (k) { v[k] = requestInputs[k].value; }); return v; }
    function refreshDraft() { if (requestText) requestText.value = requestDraft(requestId, draftValues()); }
    function plainButton(text, cls, onClick) {
      var b = el('button', { cls: cls || 'nt-desk-lv-btn', type: 'button', text: text });
      b.addEventListener('click', function (event) { if (event && event.stopPropagation) event.stopPropagation(); onClick(); });
      return b;
    }
    function showRequest(id) {
      clearOwn(requestBox);
      requestInputs = {}; requestText = null;
      var t = requestTemplate(id), buttons = [];
      requestId = t ? t.id : '';
      var kids = [el('h3', { cls: 'nt-desk-lv-title', text: t ? 'New request: ' + t.title : 'New request' })];
      if (!t) {
        kids.push(el('p', { cls: 'nt-desk-lv-empty', text: 'Pick a template. The text goes into the chat box; you check it and send it yourself.' }));
        kids.push(el('div', { cls: 'nt-desk-lv-request-list' }, REQUEST_TEMPLATES.map(function (tpl) {
          var b = el('button', { cls: 'nt-desk-lv-btn nt-desk-lv-request-pick', type: 'button', 'data-template': tpl.id }, [
            el('span', { cls: 'nt-desk-lv-request-name', text: tpl.title }), el('span', { cls: 'nt-desk-lv-request-hint', text: tpl.hint })]);
          b.addEventListener('click', function () { showRequest(tpl.id); });
          return b;
        })));
      } else {
        REQUEST_FIELDS.forEach(function (f) {
          var big = f[0] === 'what';
          var input = el(big ? 'textarea' : 'input', { cls: 'nt-desk-lv-request-input', placeholder: f[2], 'aria-label': f[1],
            rows: big ? '3' : null, type: big ? null : 'text', 'data-field': f[0] });
          input.addEventListener('input', refreshDraft);
          input.addEventListener('keydown', function (event) { if (event && event.stopPropagation) event.stopPropagation(); });
          requestInputs[f[0]] = input;
          kids.push(el('label', { cls: 'nt-desk-lv-request-field' }, [el('span', { cls: 'nt-desk-lv-detail-label', text: f[1] }), input]));
        });
        kids.push(el('p', { cls: 'nt-desk-lv-detail-label', text: 'Checks (always added)' }));
        kids.push(el('ol', { cls: 'nt-desk-lv-request-checks' }, t.checks.map(function (c) { return el('li', { text: c }); })));
        requestText = el('textarea', { cls: 'nt-desk-lv-request-text', rows: '8', readonly: 'readonly', 'aria-label': 'Message for the Main' });
        kids.push(requestText);
        buttons.push(plainButton('Put in chat', 'nt-desk-lv-btn nt-desk-lv-btn-main', putRequest));
        buttons.push(plainButton('Copy', '', function () { copyText(requestDraft(requestId, draftValues())); }));
        buttons.push(plainButton('Other template', '', function () { showRequest(''); }));
      }
      buttons.push(plainButton('Cancel', '', hideRequest));
      kids.push(el('div', { cls: 'nt-desk-lv-request-bar' }, buttons));
      requestBox.appendChild(el('div', { cls: 'nt-desk-lv-request-card' }, kids));
      requestBox.hidden = false;
      refreshDraft();
      try { if (requestInputs.what && requestInputs.what.focus) requestInputs.what.focus(); } catch (e) { /* focus is optional */ }
    }
    function chatBox() {
      var boxes = [];
      try {
        boxes = Array.prototype.slice.call(document.querySelectorAll('.nt-desk-composer [contenteditable="true"], .nt-desk-composer textarea'));
      } catch (e) { boxes = []; }
      return boxes.filter(function (b) { return !root.contains(b) && !panel.contains(b); })[0] || null;
    }
    function copyText(text) {
      try {
        if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
          navigator.clipboard.writeText(text).then(function () { toast('Copied. Paste it in the chat and send it yourself.'); },
            function () { toast('Could not copy the text.', true); });
          return;
        }
      } catch (e) { /* fall through */ }
      toast('Could not copy the text.', true);
    }
    // Only puts text into the composer: no Enter key, no click on Send (I5: nothing is sent from here).
    function putRequest() {
      var text = requestDraft(requestId, draftValues());
      if (!text) return;
      var sh = window.__ntShell;
      if (registered && sh && typeof sh.open === 'function') { try { sh.open('chat'); } catch (e) { /* stay */ } }
      setTimeout(function () {
        var box = chatBox();
        if (!box) { copyText(text); toast('Chat box not found. The text was copied; paste it in the chat.'); return; }
        try {
          box.focus();
          if (box.tagName === 'TEXTAREA') box.value = text;
          else document.execCommand('insertText', false, text);
          toast('The draft is in the chat box. Check it and send it yourself.');
          hideRequest();
        } catch (e) { copyText(text); }
      }, 300);
    }
    function openRequest(id) { open(); showRequest(id || ''); }

    // ---- History search: ctx.call('history_search') -> templates.search (same as GET /api/history/search).
    // Input and results are kept as nodes so a repaint never takes the focus away while typing.
    var historyQuery = '', historyTimer = null, historySeq = 0, historyRecent = null;
    var historyInput = el('input', { cls: 'nt-desk-lv-history-search', type: 'search', placeholder: 'Search all requests and steps',
      'aria-label': 'Search history', autocomplete: 'off', spellcheck: 'false' });
    var historyFound = el('div', { cls: 'nt-desk-lv-history-found', role: 'status' });
    historyInput.addEventListener('input', function () {
      if (historyTimer) clearTimeout(historyTimer);
      historyTimer = setTimeout(runHistorySearch, 300);
    });
    historyInput.addEventListener('keydown', function (event) {
      if (event && event.stopPropagation) event.stopPropagation();
      if (event && event.key === 'Escape' && historyInput.value) { event.preventDefault(); historyInput.value = ''; runHistorySearch(); }
    });
    function paintFound(res) {
      clearOwn(historyFound);
      if (historyRecent) historyRecent.hidden = !!historyQuery;
      if (!historyQuery) return;
      res = obj(res);
      if (res.busy) { historyFound.appendChild(empty('Searching...')); return; }
      if (res.error) { historyFound.appendChild(empty(res.error)); return; }
      var rows = list(res.results), total = num(res.total) || rows.length;
      if (!rows.length) { historyFound.appendChild(empty('Nothing found.')); return; }
      historyFound.appendChild(el('p', { cls: 'nt-desk-lv-history-count', text: total > rows.length
        ? 'Showing ' + rows.length + ' of ' + total + ' results.' : rows.length + (rows.length === 1 ? ' result.' : ' results.') }));
      historyFound.appendChild(el('ul', { cls: 'nt-desk-lv-history nt-desk-lv-history-results' }, rows.map(function (h) {
        h = obj(h);
        var bad = /did not|blocked|lost|fail/i.test(str(h.outcome));
        return el('li', { cls: bad ? 'nt-desk-lv-past nt-desk-lv-hit nt-desk-lv-past-bad' : 'nt-desk-lv-past nt-desk-lv-hit nt-desk-lv-past-ok', 'data-kind': str(h.kind) }, [
          el('span', { cls: 'nt-desk-lv-past-goal', text: (h.kind === 'task' ? 'Step: ' : 'Request: ') + clip(h.title, 160) }),
          h.kind === 'task' && h.request ? el('span', { cls: 'nt-desk-lv-hit-request', text: 'In: ' + clip(h.request, 120) }) : null,
          el('span', { cls: 'nt-desk-lv-past-outcome', text: clip(h.outcome, 160) || 'No outcome yet.' }),
          h.snippet ? el('span', { cls: 'nt-desk-lv-hit-snippet', text: (h.field ? h.field + ': ' : '') + clip(h.snippet, 200) }) : null,
          el('span', { cls: 'nt-desk-lv-time', text: str(h.date) || '-' })]);
      })));
    }
    function runHistorySearch() {
      historyTimer = null;
      var q = cleanField(historyInput.value).slice(0, 200), seq = ++historySeq;
      historyQuery = q;
      if (!q) { paintFound(null); return; }
      if (typeof ctx.call !== 'function') { paintFound({ error: 'Search needs the harness.' }); return; }
      paintFound({ busy: true });
      try {
        Promise.resolve(ctx.call('history_search', { q: q, limit: 50 })).then(function (res) {
          if (seq === historySeq && !stopped) paintFound(res);
        }, function (e) {
          if (seq === historySeq && !stopped) paintFound({ error: 'Search did not work: ' + clip(e && e.message ? e.message : e, 120) });
        });
      } catch (e) { paintFound({ error: 'Search did not work.' }); }
    }
    function historyFrozen() {
      return !!historyQuery || (typeof document !== 'undefined' && document.activeElement === historyInput);
    }

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

    // Problems: dismissed keys live in memory (and localStorage when the page allows it).
    var DISMISS_KEY = 'nt-desk-problems-dismissed';
    var dismissed = (function () {
      try { var saved = JSON.parse(window.localStorage.getItem(DISMISS_KEY) || '{}'); return saved && typeof saved === 'object' ? saved : {}; } catch (e) { return {}; }
    })();
    function dismiss(key) {
      dismissed[key] = Date.now();
      var keys = Object.keys(dismissed).sort(function (a, b) { return dismissed[b] - dismissed[a]; });
      keys.slice(200).forEach(function (k) { delete dismissed[k]; });
      try { window.localStorage.setItem(DISMISS_KEY, JSON.stringify(dismissed)); } catch (e) { /* memory only */ }
      sigs.needs = null;
      renderNow();
      updatePill();
    }
    function problemList(s, now) {
      return problems(s, { dismissed: dismissed, now: now, connection: connection(s, now, lastPush) });
    }
    function needsSection(s, now) {
      var items = problemList(s, now).map(function (p) {
        var off = el('button', { cls: 'nt-desk-lv-btn nt-desk-lv-dismiss', type: 'button', text: 'Dismiss', title: 'Hide this problem' });
        off.addEventListener('click', function (event) { if (event && event.stopPropagation) event.stopPropagation(); dismiss(p.key); });
        var when = p.at ? ago(p.at, now) : '';
        return el('div', { cls: 'nt-desk-lv-need nt-desk-lv-problem', 'data-problem': p.key }, [
          el('p', { cls: 'nt-desk-lv-need-text', text: p.what + (p.count > 1 ? ' (' + p.count + ' times)' : '') }),
          el('p', { cls: 'nt-desk-lv-problem-todo', text: 'What to do: ' + p.todo }),
          el('div', { cls: 'nt-desk-lv-problem-foot' }, [
            when ? el('span', { cls: 'nt-desk-lv-time', text: when }) : null,
            p.def ? actionButton(p.def, true) : null, off])]);
      });
      return section('needs', 'Problems', items.length ? items : [empty('No problems right now.')], items.length);
    }

    // Token budget (token_budget.py): estimated tokens of Main + coworkers in the current 6-hour window.
    function mega(n) { n = num(n) || 0; return n >= 1e6 ? (n / 1e6).toFixed(2) + 'M' : n >= 1e3 ? Math.round(n / 1e3) + 'k' : String(n); }
    function budgetSection(s, now) {
      var b = obj(s.token_budget);
      if (!num(b.limit)) return section('budget', 'Token budget (6 h)', [empty('No token data yet.')]);
      var pct = Math.max(0, Math.min(100, num(b.percent) || 0));
      var left = Math.max(0, Math.round((num(b.window_end) * 1000 - now) / 60000));
      var bar = el('div', { cls: 'nt-desk-lv-budget-bar', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': String(Math.round(pct)),
        style: 'height:8px;border-radius:4px;background:rgba(255,255,255,.12);overflow:hidden;margin:6px 0' },
        [el('div', { style: 'height:100%;width:' + pct + '%;background:' + (b.blocked ? '#e5484d' : pct >= 80 ? '#f5a524' : '#7c5cff') })]);
      var kids = [
        el('p', { cls: 'nt-desk-lv-need-text', text: mega(b.used) + ' of ' + mega(b.limit) + ' tokens used (' + pct.toFixed(1) + '%)' }),
        bar,
        detail('Main', mega(b.main_tokens)), detail('Coworkers', mega(b.coworker_tokens)),
        detail('Turns', String(num(b.turns) || 0)), detail('Resets in', Math.floor(left / 60) + ' h ' + (left % 60) + ' min')];
      var bu = obj(b.burst);
      if (bu.level) {
        var bl = obj(bu.limits);
        kids.push(detail('Short-term', mega(bu.tokens_15m) + ' / 15 min \u00b7 ' + mega(bu.tokens_60m) + ' / 60 min \u00b7 ' +
          (num(bu.coworker_turns_5m) || 0) + ' coworker turns / 5 min'));
        kids.push(el('p', { cls: 'nt-desk-lv-problem-todo',
          style: 'color:' + (bu.level === 'pause' ? '#e5484d' : bu.level === 'throttle' ? '#f5a524' : '#46a758'),
          text: bu.level === 'pause' ? 'Burst guard: coworkers paused (' + bu.reason + ')' :
            bu.level === 'throttle' ? 'Burst guard: coworkers slowed (' + bu.reason + ')' :
            'Burst guard: ok (slows at ' + mega(bl.burst_soft_15m) + ' / 15 min, pauses at ' + mega(bl.burst_hard_15m) + ' / 15 min or ' + mega(bl.burst_hard_60m) + ' / 60 min)' }));
      }
      if (b.blocked) kids.unshift(el('p', { cls: 'nt-desk-lv-problem-todo', style: 'color:#e5484d', text: 'Limit reached: sending is blocked until the reset.' }));
      list(s.ai_disable).slice(0, 1).forEach(function (r) {
        r = obj(r);
        kids.push(el('p', { cls: 'nt-desk-lv-problem-todo', style: 'color:#f5a524',
          text: 'Notion AI was disabled' + (r.account ? ' (' + r.account + ')' : '') + (r.at ? ', ' + ago(r.at, now) : '') +
            (r.tokens_6h ? ' \u00b7 ' + mega(r.tokens_6h) + ' tokens in 6 h' : '') + (r.cause ? ' \u00b7 probable cause: ' + r.cause : '') }));
      });
      return section('budget', 'Token budget (6 h, estimate)', kids);
    }

    function taskDetails(t, workers) {
      var w = obj(workers[t.worker_id]);
      var files = list(t.files).map(str).filter(Boolean);
      return el('div', { cls: 'nt-desk-lv-details' }, [
        detail('Model', str(t.model) || 'Default'),
        detail('Effort', effortWord(t.effort)),
        detail('Coworker', clip(w.label, 40) || 'Not picked yet'),
        detail('Files', files.length ? files.join(', ') : 'None'),
        proofDetail(t.proof),
        el('div', { cls: 'nt-desk-lv-detail nt-desk-lv-detail-result' }, [el('span', { cls: 'nt-desk-lv-detail-label', text: 'Result' }),
          el('pre', { cls: 'nt-desk-lv-result', text: str(t.result) || str(t.error) || 'No result yet.' })])
      ]);
    }

    // Proof (proof.py record on the task): files with +/- lines, reported tests (green/red), screenshot path.
    function proofFileText(f) {
      f = obj(f);
      var name = str(f.path) || 'file';
      if (f.binary) return name + ' (binary)';
      var counts = '+' + (num(f.added) || 0) + ' -' + (num(f.removed) || 0);
      return name + ' ' + counts + (f.change === 'added' ? ' (new)' : f.change === 'removed' ? ' (deleted)' : '');
    }
    function proofTestText(r) {
      r = obj(r);
      var head = 'Ran ' + (num(r.ran) || 0) + ' tests: ';
      if (r.status === 'passed') return head + 'OK';
      if (r.status === 'failed') {
        var parts = [];
        if (num(r.failures)) parts.push(num(r.failures) + (num(r.failures) === 1 ? ' failure' : ' failures'));
        if (num(r.errors)) parts.push(num(r.errors) + (num(r.errors) === 1 ? ' error' : ' errors'));
        return head + 'FAILED' + (parts.length ? ' (' + parts.join(', ') + ')' : '');
      }
      return head + 'result not clear';
    }
    function proofLines(p) {
      var lines = [];
      var files = list(p.files);
      lines.push({ text: 'Files: ' + (!p.files_checked ? 'Not checked' : files.length ? files.map(proofFileText).join(', ') : 'No changes'),
        cls: p.files_checked ? '' : 'none' });
      if (p.files_checked && list(p.unknown).length) lines.push({ text: 'Not checked: ' + list(p.unknown).map(str).join(', '), cls: 'none' });
      var tests = list(p.tests);
      var tcls = p.tests_status === 'passed' ? 'pass' : p.tests_status === 'failed' ? 'fail' : 'none';
      lines.push({ text: 'Tests: ' + (tests.length ? tests.map(proofTestText).join('; ') : 'Not checked'), cls: tcls });
      if (str(p.screenshot)) lines.push({ text: 'Screenshot: ' + str(p.screenshot), cls: '' });
      return lines;
    }
    function proofDetail(raw) {
      var p = obj(raw);
      var lines = Object.keys(p).length ? proofLines(p) : [{ text: 'Not checked', cls: 'none' }];
      return el('div', { cls: 'nt-desk-lv-detail nt-desk-lv-detail-proof' }, [
        el('span', { cls: 'nt-desk-lv-detail-label', text: 'Proof' }),
        el('span', { cls: 'nt-desk-lv-proof-lines' }, lines.map(function (line) {
          return el('span', { cls: 'nt-desk-lv-proof-line' + (line.cls ? ' nt-desk-lv-proof-' + line.cls : ''), text: line.text });
        }))]);
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

    // ---- Live map + 340 px side panel (reference v6 Roles / v7 Direct). selected = coworker shown in the panel.
    var selected = '';
    var SVG_NS = 'http://www.w3.org/2000/svg';
    // Lucide icons: reply (Answer), square (Stop), bot (Main).
    var ICON_PATHS = { reply: ['M20 18v-2a4 4 0 0 0-4-4H4', 'M9 17l-5-5 5-5'], stop: ['M5 5h14v14H5z'],
      bot: ['M12 8V4H8', 'M6 8h12a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2z', 'M2 14h2', 'M20 14h2', 'M15 13v2', 'M9 13v2'] };
    function icon(name) {
      if (typeof document.createElementNS !== 'function') return null;
      var svg = document.createElementNS(SVG_NS, 'svg');
      svg.setAttribute('viewBox', '0 0 24 24');
      svg.setAttribute('aria-hidden', 'true');
      ICON_PATHS[name].forEach(function (d) {
        var p = document.createElementNS(SVG_NS, 'path');
        p.setAttribute('d', d);
        svg.appendChild(p);
      });
      return svg;
    }
    function pickCard(cards) {
      return cards.filter(function (c) { return c.id === selected; })[0] || cards.filter(function (c) { return c.kind === 'ask'; })[0] ||
        cards.filter(function (c) { return c.kind === 'busy'; })[0] || cards[0] || null;
    }
    // Answer box (right panel): the typed reply goes through the host action live_answer -> POST /api/send.
    // Drafts and send states are kept per coworker, so a repaint never wipes what the user typed.
    var replyDrafts = {}, replyStates = {};
    function replyStateOf(c) { return c ? obj(replyStates[c.id]) : {}; }
    function isReplyBox(node) { return !!(node && /(^|\s)nt-desk-lv-reply(\s|$)/.test(String(node.className || ''))); }
    function replyLabel(st) {
      if (st.state === 'sending') return 'Sending\u2026';
      if (st.state === 'sent') return 'Sent to the chat';
      if (st.state === 'error') return 'Could not send: ' + clip(st.error, 120);
      return '';
    }
    function sendReply(c, box, button, status) {
      if (!c || busyReply(c)) return;
      var text = str(box.value).trim();
      if (!text) { replyStates[c.id] = { state: 'error', error: 'Type a reply first' }; status.textContent = replyLabel(replyStates[c.id]); status.className = 'nt-desk-lv-reply-state nt-desk-lv-reply-error'; return; }
      if (typeof ctx.call !== 'function') return;
      var id = c.id;
      replyStates[id] = { state: 'sending' };
      button.disabled = true;
      box.disabled = true;
      status.textContent = replyLabel(replyStates[id]);
      status.className = 'nt-desk-lv-reply-state nt-desk-lv-reply-sending';
      var done = function (ok, error) {
        if (ok) { replyStates[id] = { state: 'sent' }; delete replyDrafts[id]; }
        else replyStates[id] = { state: 'error', error: error && error.message ? error.message : String(error || 'unknown error') };
        sigs.reply = null;
        toast(ok ? 'Reply sent.' : 'Reply not sent: ' + clip(replyStates[id].error, 120), !ok);
        renderNow();
      };
      try {
        Promise.resolve(ctx.call('live_answer', { worker_id: id, text: text.slice(0, 4000), request_id: 'live-' + Date.now().toString(36) }))
          .then(function () { done(true); }, function (e) { done(false, e); });
      } catch (e) { done(false, e); }
    }
    function busyReply(c) { return replyStateOf(c).state === 'sending'; }
    function replySection(s, now) {
      var c = pickCard(liveCards(s, now)), st = replyStateOf(c);
      var asked = c && c.answer ? c.answer.title + ': ' + c.answer.text : c ? c.text + (c.task ? ': ' + c.task : '') : 'No coworker selected.';
      var box = el('textarea', { cls: 'nt-desk-lv-reply', rows: '3', maxlength: '4000', 'aria-label': 'Reply to ' + (c ? c.label : 'coworker'),
        placeholder: c ? 'Type your answer for ' + c.label : 'Select a coworker to answer', disabled: c && st.state !== 'sending' ? null : 'disabled' });
      box.value = c ? str(replyDrafts[c.id]) : '';
      var send = el('button', { cls: 'nt-desk-lv-btn nt-desk-lv-btn-main nt-desk-lv-send', type: 'button', disabled: c && st.state !== 'sending' && box.value.trim() ? null : 'disabled' },
        [icon('reply'), el('span', { text: 'Answer' })]);
      var status = el('p', { cls: 'nt-desk-lv-reply-state' + (st.state ? ' nt-desk-lv-reply-' + st.state : ''), role: 'status', 'aria-live': 'polite', text: replyLabel(st) });
      box.addEventListener('input', function () {
        if (!c) return;
        replyDrafts[c.id] = box.value;
        send.disabled = !(box.value.trim() && st.state !== 'sending');
      });
      box.addEventListener('keydown', function (event) {
        if (!event || event.key === 'Escape') return;
        if (event.stopPropagation) event.stopPropagation();
        if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { if (event.preventDefault) event.preventDefault(); replyDrafts[c.id] = box.value; sendReply(c, box, send, status); }
      });
      send.addEventListener('click', function () { if (c) { replyDrafts[c.id] = box.value; sendReply(c, box, send, status); } });
      return el('section', { cls: 'nt-desk-lv-question', 'aria-label': 'Question and answer' }, [
        el('h4', { cls: 'nt-desk-lv-title', text: 'Question' }),
        el('p', { cls: 'nt-desk-lv-question-text', text: asked }),
        box,
        el('div', { cls: 'nt-desk-lv-reply-actions' }, [status, send])]);
    }
    function mapSection(s, now) {
      var mode = liveMode(s), cards = liveCards(s, now), pick = pickCard(cards), main = mainInfo(s);
      function cardButton(c) {
        if (c.slot) {
          return el('div', { cls: 'nt-desk-lv-cw nt-desk-lv-cw-idle nt-desk-lv-cw-slot', title: c.label + ': ' + c.text }, [
            el('span', { cls: 'nt-desk-lv-cw-top' }, [
              el('span', { cls: 'nt-desk-lv-cw-dot', 'aria-hidden': 'true' }),
              el('span', { cls: 'nt-desk-lv-cw-name', text: c.label })]),
            el('span', { cls: 'nt-desk-lv-cw-state', text: c.text })]);
        }
        var on = !!pick && c.id === pick.id;
        var b = el('button', { cls: 'nt-desk-lv-cw nt-desk-lv-cw-' + c.kind + (on ? ' nt-desk-lv-cw-on' : ''), type: 'button',
          'aria-pressed': on ? 'true' : 'false', title: c.label + ': ' + c.text }, [
          el('span', { cls: 'nt-desk-lv-cw-top' }, [
            el('span', { cls: 'nt-desk-lv-cw-dot', 'aria-hidden': 'true' }),
            el('span', { cls: 'nt-desk-lv-cw-name', text: c.label }),
            c.role ? el('span', { cls: 'nt-desk-lv-role', text: c.role }) : null]),
          el('span', { cls: 'nt-desk-lv-cw-state', text: c.text + (c.kind === 'busy' && c.elapsed ? ' \u00b7 ' + c.elapsed : '') }),
          c.task ? el('span', { cls: 'nt-desk-lv-cw-task', text: c.task }) : null]);
        b.addEventListener('click', function () { selected = c.id; sigs.map = null; sigs.focus = null; renderNow(); });
        return b;
      }
      var body = !cards.length ? empty('No coworkers yet.') : mode !== 'roles' ? el('div', { cls: 'nt-desk-lv-row' }, cards.map(cardButton)) :
        el('div', { cls: 'nt-desk-lv-stages' }, liveStages(cards).map(function (st) {
          return el('div', { cls: 'nt-desk-lv-stage nt-desk-lv-stage-' + st.id, role: 'group', 'aria-label': st.label }, [
            el('h3', { cls: 'nt-desk-lv-stage-label', text: st.label }),
            st.cards.length ? el('div', { cls: 'nt-desk-lv-row' }, st.cards.map(cardButton))
              : el('p', { cls: 'nt-desk-lv-stage-empty', text: 'No coworker in this stage.' })]);
        }));
      return el('section', { cls: 'nt-desk-lv-map', 'aria-label': 'Delegation' }, [
        el('div', { cls: 'nt-desk-lv-map-head' }, [
          el('h2', { cls: 'nt-desk-lv-map-title', text: 'Delegation' }),
          el('span', { cls: 'nt-desk-lv-mode nt-desk-lv-mode-' + mode, text: mode === 'roles' ? 'Mode: Roles' : 'Mode: Direct' })]),
        el('div', { cls: 'nt-desk-lv-main' + (main.busy ? ' nt-desk-lv-main-busy' : '') }, [
          el('span', { cls: 'nt-desk-lv-main-icon', 'aria-hidden': 'true' }, [icon('bot')]),
          el('span', { cls: 'nt-desk-lv-main-text' }, [
            el('span', { cls: 'nt-desk-lv-main-name', text: main.label }),
            el('span', { cls: 'nt-desk-lv-main-goal', text: main.goal ? main.goal + (main.step ? ' \u00b7 ' + main.step : '') : 'Waiting for a request' })])]),
        cards.length ? el('div', { cls: 'nt-desk-lv-link', 'aria-hidden': 'true' }) : null,
        body]);
    }
    function focusSection(s, now) {
      var c = pickCard(liveCards(s, now));
      var answer = el('button', { cls: 'nt-desk-lv-btn nt-desk-lv-answer', type: 'button', disabled: c && c.answer ? null : 'disabled',
        title: c && c.answer ? c.answer.title : 'Nothing to resolve right now' }, [icon('reply'), el('span', { text: 'Resolve' })]);
      var stopButton = el('button', { cls: 'nt-desk-lv-btn nt-desk-lv-stop', type: 'button', disabled: c && c.stop ? null : 'disabled',
        title: c && c.stop ? c.stop.title : 'Nothing to stop' }, [icon('stop'), el('span', { text: 'Stop' })]);
      answer.addEventListener('click', function () { if (c && c.answer) ask(c.answer); });
      stopButton.addEventListener('click', function () { if (c && c.stop) ask(c.stop); });
      var events = cardEvents(s, c).map(function (e) {
        e = obj(e);
        return el('li', { cls: 'nt-desk-lv-event' }, [
          el('span', { cls: 'nt-desk-lv-event-kind', text: humanKind(e.kind) }),
          el('span', { cls: 'nt-desk-lv-event-text', text: clip(e.message, 120) }),
          el('span', { cls: 'nt-desk-lv-time', text: ago(e.ts, now) })]);
      });
      return el('section', { cls: 'nt-desk-lv-focus', 'aria-label': 'Selected coworker' }, [
        el('div', { cls: 'nt-desk-lv-focus-head' }, [
          el('h3', { cls: 'nt-desk-lv-focus-name', text: c ? c.label : 'No coworker' }),
          c && c.role ? el('span', { cls: 'nt-desk-lv-role', text: c.role }) : null]),
        el('p', { cls: 'nt-desk-lv-focus-text', text: c ? c.text + (c.task ? ': ' + c.task : '') : 'No coworkers yet.' }),
        el('div', { cls: 'nt-desk-lv-focus-actions' }, [answer, stopButton]),
        el('h4', { cls: 'nt-desk-lv-title', text: 'Live events' }),
        el('ul', { cls: 'nt-desk-lv-events' }, events.length ? events : [el('li', { cls: 'nt-desk-lv-empty', text: 'No events yet.' })])]);
    }

    function historySection(s, now) {
      var rows = historyRows(s);
      return fold('history', 'History', rows.length, function () {
        historyRecent = !rows.length ? empty('No finished requests yet.') : el('ul', { cls: 'nt-desk-lv-history' }, rows.map(function (r) {
          return el('li', { cls: 'nt-desk-lv-past nt-desk-lv-past-' + outcomeClass(r) }, [
            el('span', { cls: 'nt-desk-lv-past-goal', text: runSummary(r) }),
            el('span', { cls: 'nt-desk-lv-past-outcome', text: outcomeText(r) }),
            el('span', { cls: 'nt-desk-lv-time', text: ago(r.finished || r.created, now) })]);
        }));
        historyRecent.hidden = !!historyQuery;
        return [el('div', { cls: 'nt-desk-lv-history-searchbar' }, [historyInput]), historyFound, historyRecent];
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
      var count = problemList(state(), Date.now()).length;
      pillBadge.hidden = !count;
      pillBadge.textContent = count ? String(count) : '';
      pill.className = 'nt-desk-live-pill' + (count ? ' nt-desk-live-pill-alert' : '') + (isOpen || shellShown ? ' nt-desk-live-pill-on' : '');
      pill.setAttribute('aria-label', count ? 'Open Live, ' + count + (count === 1 ? ' problem' : ' problems') : 'Open Live');
      tabBadge(runningCount(state()));
    }
    // Badge count on the Live tab of the v3 shell; every shell API is optional.
    var lastTabBadge = null;
    function tabBadge(count) {
      if (!registered || count === lastTabBadge) return;
      var sh = window.__ntShell, text = count ? String(count) : '';
      try {
        if (shellHandle && typeof shellHandle.setBadge === 'function') shellHandle.setBadge(text);
        else if (sh && typeof sh.setBadge === 'function') sh.setBadge('live', text);
        else if (sh && typeof sh.badge === 'function') sh.badge('live', text);
        else return;
        lastTabBadge = count;
      } catch (e) { /* the shell has no badges */ }
    }

    // Overlay errors (window error events from our parts only) -> ctx.call('ui_error') -> problems.py.
    // Deduped and at most ERROR_MAX reports per minute here; the server limits again.
    var ERROR_MAX = 3, errorTimes = [], errorSeen = {};
    function ourError(event) {
      var e = obj(event), err = obj(e.error), file = str(e.filename), stack = str(err.stack);
      if (!str(e.message) || /^script error\.?$/i.test(str(e.message))) return false;
      if (/__ntDesk|nt-desk|NotionTool/.test(stack)) return true;
      return !file || !/^(https?|chrome-extension|app):/i.test(file);
    }
    function reportError(message, source, line, part) {
      if (stopped || typeof ctx.call !== 'function') return false;
      var text = clip(message, 200), now = Date.now();
      if (!text || errorSeen[text]) return false;
      errorTimes = errorTimes.filter(function (t) { return now - t < 60000; });
      if (errorTimes.length >= ERROR_MAX) return false;
      errorTimes.push(now);
      errorSeen[text] = now;
      try {
        Promise.resolve(ctx.call('ui_error', { message: text, source: clip(source, 120), line: num(line) || 0, part: part || '' }))
          .then(null, function () { /* never retried */ });
      } catch (e) { /* reporting is best effort */ }
      return true;
    }
    function onError(event) {
      if (!ourError(event)) return;
      var e = obj(event);
      reportError(e.message, e.filename, e.lineno, 'overlay');
    }

    function render() {
      timer = null;
      last = Date.now();
      if (!shown()) return;
      var raw = state(), s = obj(raw), now = Date.now(), tick = Math.floor(now / REFRESH_MS);
      updateConnection(raw, now);
      try {
        paint('map', sig([s.runs, s.tasks, s.workers, selected, tick]), function () { return mapSection(s, now); });
        paint('focus', sig([s.runs, s.tasks, s.workers, selected, list(s.events).slice(-EVENT_LIMIT), tick]), function () { return focusSection(s, now); });
        var rc = pickCard(liveCards(s, now)), rs = replyStateOf(rc);
        paint('reply', sig([rc && rc.id, rc && rc.label, rc && rc.text, rc && rc.task, rc && rc.answer && rc.answer.title, rs.state, rs.error]), function () { return replySection(s, now); });
        paint('budget', sig([s.token_budget && [s.token_budget.used, s.token_budget.limit, s.token_budget.blocked, s.token_budget.window_start, s.token_budget.burst && [s.token_budget.burst.level, s.token_budget.burst.tokens_15m, s.token_budget.burst.coworker_turns_5m]], s.ai_disable, Math.floor(now / 60000)]),
          function () { return budgetSection(s, now); });
        paint('needs', sig([s.runs, s.tasks, s.stalled, s.stale_context_tasks, list(s.events).slice(-EVENT_LIMIT), connection(raw, now, lastPush).cls, Object.keys(dismissed).length, tick]),
          function () { return needsSection(raw, now); });
        paint('working', sig([s.runs, s.tasks, s.workers, tick, Object.keys(expanded)]), function () { return workingSection(s, now); });
        paint('team', sig([s.workers, tick]), function () { return teamSection(s, now); });
        paint('history', sig([folds.history, folds.history && historyFrozen() ? 'search' : [s.runs, folds.history ? tick : 0]]), function () { return historySection(s, now); });
        paint('details', sig([folds.details, folds.details ? [s.rate, s.workers, s.tasks, s.attention, list(s.events).slice(-EVENT_LIMIT), tick, !!raw] : 0]),
          function () { return detailsSection(raw, s, now); });
      } catch (e) {
        if (window.console && console.warn) console.warn('[NotionTool] Live view failed', e);
        reportError('Live view failed: ' + str(e && e.message ? e.message : e), 'live.js', 0, 'live');
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
      root.classList.add('nt-desk-lv-page');
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
      root.classList.remove('nt-desk-lv-page');
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
        var liveView = { id: 'live', label: 'Live', title: 'Live', order: 20,
          mount: shellMount, unmount: shellUnmount, show: shellMount, hide: shellUnmount,
          // Rail badge = coworkers running now (shell.js liveBadge counted every attention item: '6').
          badge: function () { return runningCount(state()); } };
        // shell.js register(tab, view) takes the tab id first; an object-only call registered nothing
        // (the Live tab then showed the 'still loading' placeholder with a dead Open button).
        shellHandle = fn === sh.register ? fn.call(sh, 'live', liveView) : fn.call(sh, liveView);
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
        if (pending) { hideConfirm(); if (event.preventDefault) event.preventDefault(); return; }
        if (isOpen) { close(); return; }
        if (shellShown && !event.defaultPrevented && (!typingIn(event.target) || isReplyBox(event.target))) {
          if (!requestBox.hidden) { hideRequest(); if (event.preventDefault) event.preventDefault(); return; }
          backToChat(event);
        }
      }
    }
    function typingIn(node) {
      var tag = String(node && node.tagName || '').toUpperCase();
      return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || !!(node && node.isContentEditable);
    }
    // Esc on the Live page goes back to Chat (the shell's back() did nothing with an empty trail).
    function backToChat(event) {
      var sh = window.__ntShell;
      if (!sh || typeof sh.open !== 'function') return;
      if (event.preventDefault) event.preventDefault();
      try { sh.open('chat'); } catch (e) { /* shell is gone */ }
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
      if (historyTimer) clearTimeout(historyTimer);
      dockTimer = hideTimer = historyTimer = null;
      if (window.__ntDesk.openRequestTemplate === openRequest) window.__ntDesk.openRequestTemplate = null;
      window.removeEventListener('keydown', onKey, true);
      window.removeEventListener('hashchange', onHash);
      window.removeEventListener('error', onError);
      if (typeof document.removeEventListener === 'function') document.removeEventListener('visibilitychange', onVisibility);
      if (typeof unsubscribe === 'function') { try { unsubscribe(); } catch (e) { /* already gone */ } }
      var sh = window.__ntShell;
      if (registered && sh) {
        try {
          if (typeof shellHandle === 'function') shellHandle();
          else if (shellHandle && typeof shellHandle.unregister === 'function') shellHandle.unregister();
          else if (typeof sh.unregister === 'function') sh.unregister('live');
        } catch (e) { /* shell already gone */ }
      }
      detach(root);
      detach(panel);
      detach(pill);
      if (window.__ntDeskLiveOwner === owner) window.__ntDeskLiveOwner = null;
    }

    window.__ntDeskLiveOwner = owner;
    window.__ntDesk.openRequestTemplate = openRequest; // used by Ctrl+K (extras.js)
    sweep();
    closeButton.addEventListener('click', close);
    pill.addEventListener('click', function () { if (isOpen) close(); else open(); });
    document.body.appendChild(pill);
    document.body.appendChild(panel);
    window.addEventListener('keydown', onKey, true);
    window.addEventListener('hashchange', onHash);
    window.addEventListener('error', onError);  // like window.onerror, without replacing Notion's handler
    if (typeof document.addEventListener === 'function') document.addEventListener('visibilitychange', onVisibility);
    if (typeof ctx.onState === 'function') unsubscribe = ctx.onState(onPush);
    tryRegister();
    if (!docHidden()) startDock();
    dock();
    if (location.hash === HASH) open();
    return { stop: stop, open: open, close: close };
  };
})();
