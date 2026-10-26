/*
 * NotionTool desktop part 'projects': the Projects tab (window.__ntDesk.projects = function (ctx) { ... return { stop } }).
 * Project list (search, Active / Archived / All filter, New project) and project detail (goal, repo path, latest
 * commits, linked chats, coworkers, Open / Archive / Unarchive). A project only links chats that already exist;
 * this part never creates a Notion chat.
 * Data: ctx.call('projects_list' | 'projects_detail' | 'projects_create' | 'projects_update' | 'projects_open' |
 * 'projects_archive' | 'projects_unarchive') -> notiontool/projects_routes.py (/api/ui/projects).
 * Registers as tab 'projects' with window.__ntShell (lazy: retries until the shell exists, or queues in
 * window.__ntShellQueue). Own nodes only (class nt-prj-*), text only via textContent, no network calls of its own.
 */
(function () {
  'use strict';
  window.__ntDesk = window.__ntDesk || {};

  var SVG_NS = 'http://www.w3.org/2000/svg';
  var REFRESH_MS = 30000;
  var DOCK_MS = 1000;
  var SEARCH_MS = 200;
  var FILTERS = [['active', 'Active'], ['archived', 'Archived'], ['all', 'All']];
  var ICONS = {
    plus: [['path', { d: 'M5 12h14' }], ['path', { d: 'M12 5v14' }]],
    refresh: [['path', { d: 'M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8' }], ['path', { d: 'M21 3v5h-5' }],
      ['path', { d: 'M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16' }], ['path', { d: 'M8 16H3v5' }]],
    search: [['circle', { cx: 11, cy: 11, r: 8 }], ['path', { d: 'm21 21-4.3-4.3' }]],
    back: [['path', { d: 'm12 19-7-7 7-7' }], ['path', { d: 'M19 12H5' }]],
    folder: [['path', { d: 'M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z' }]],
    commit: [['circle', { cx: 12, cy: 12, r: 3 }], ['path', { d: 'M3 12h6' }], ['path', { d: 'M15 12h6' }]],
    chat: [['path', { d: 'M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z' }]],
    archive: [['rect', { x: 2, y: 3, width: 20, height: 5, rx: 1 }], ['path', { d: 'M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8' }],
      ['path', { d: 'M10 12h4' }]],
    restore: [['path', { d: 'M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8' }], ['path', { d: 'M3 3v5h5' }]],
    check: [['path', { d: 'M20 6 9 17l-5-5' }]],
    edit: [['path', { d: 'M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z' }], ['path', { d: 'm15 5 4 4' }]],
    users: [['path', { d: 'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2' }], ['circle', { cx: 9, cy: 7, r: 4 }],
      ['path', { d: 'M22 21v-2a4 4 0 0 0-3-3.87' }], ['path', { d: 'M16 3.13a4 4 0 0 1 0 7.75' }]],
    x: [['path', { d: 'M18 6 6 18' }], ['path', { d: 'm6 6 12 12' }]],
    star: [['path', { d: 'M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z' }]]
  };

  function str(v) { return v === null || v === undefined ? '' : String(v); }
  function list(v) { return Array.isArray(v) ? v : []; }
  function obj(v) { return v && typeof v === 'object' && !Array.isArray(v) ? v : {}; }

  function icon(name) {
    var svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('width', '16');
    svg.setAttribute('height', '16');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '1.75');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('class', 'nt-prj-icon');
    list(ICONS[name]).forEach(function (part) {
      var node = document.createElementNS(SVG_NS, part[0]);
      Object.keys(part[1]).forEach(function (k) { node.setAttribute(k, String(part[1][k])); });
      svg.appendChild(node);
    });
    return svg;
  }

  // h('div', { class, text, title, type, value, placeholder, on: { click } }, [children])
  function h(tag, attrs, kids) {
    var node = document.createElement(tag);
    var a = attrs || {};
    Object.keys(a).forEach(function (k) {
      var v = a[k];
      if (v === undefined || v === null || v === false) return;
      if (k === 'class') node.className = v;
      else if (k === 'text') node.textContent = str(v);
      else if (k === 'on') Object.keys(v).forEach(function (ev) { node.addEventListener(ev, v[ev]); });
      else if (k === 'value') node.value = str(v);
      else if (k === 'disabled' || k === 'checked' || k === 'hidden') node[k] = !!v;
      else node.setAttribute(k, str(v));
    });
    list(kids).forEach(function (kid) { if (kid) node.appendChild(typeof kid === 'string' ? document.createTextNode(kid) : kid); });
    return node;
  }

  function button(label, iconName, kind, onClick, extra) {
    var kids = [];
    if (iconName) kids.push(icon(iconName));
    if (label) kids.push(h('span', { text: label }));
    var attrs = { type: 'button', class: 'nt-prj-btn nt-prj-btn-' + (kind || 'ghost'), on: { click: onClick } };
    Object.keys(extra || {}).forEach(function (k) { attrs[k] = extra[k]; });
    return h('button', attrs, kids);
  }

  function ago(seconds) {
    var t = Number(seconds);
    if (!isFinite(t) || t <= 0) return 'Not known';
    var d = Math.max(0, Date.now() / 1000 - t);
    if (d < 60) return 'Just now';
    if (d < 3600) return Math.floor(d / 60) + ' min ago';
    if (d < 86400) return Math.floor(d / 3600) + ' h ago';
    if (d < 86400 * 30) return Math.floor(d / 86400) + ' d ago';
    try { return new Date(t * 1000).toLocaleDateString(); } catch (e) { return 'Long ago'; }
  }

  function stateLabel(value) {
    var s = str(value).replace(/_/g, ' ').trim();
    if (!s) return 'Unknown';
    if (s === 'busy') return 'Working';
    return s.charAt(0).toUpperCase() + s.slice(1);
  }

  function commitDate(value) {
    var text = str(value);
    var ms = Date.parse(text);
    if (!isFinite(ms)) return text;
    try { return new Date(ms).toLocaleDateString(); } catch (e) { return text; }
  }

  function unwrap(reply) {
    var r = obj(reply);
    return r.result && typeof r.result === 'object' && !r.projects && !r.project ? obj(r.result) : r;
  }

  window.__ntDesk.projects = function (ctx) {
    var stopped = false, registered = false, shellHandle = null, shown = false, owner = {};
    var projects = [], chats = [], currentId = '';
    var mode = 'list';            // 'list' | 'detail' (New project and confirmations are dialogs)
    var detail = null;            // project (with commits) shown in 'detail'
    var filter = 'active', query = '';
    var loading = false, loadedOnce = false, busy = false, error = '';
    var refreshTimer = null, dockTimer = null, searchTimer = null;
    var modal = null, modalError = null, modalOpener = null;

    var root = h('div', { class: 'nt-prj', 'data-nt-desk': 'projects' });
    var head = h('div', { class: 'nt-prj-head' });
    var toolbar = h('div', { class: 'nt-prj-toolbar' });
    var banner = h('div', { class: 'nt-prj-banner', role: 'alert', hidden: true });
    var body = h('div', { class: 'nt-prj-body' });
    root.appendChild(head);
    root.appendChild(toolbar);
    root.appendChild(banner);
    root.appendChild(body);

    function toast(text, kind) {
      try { if (ctx && typeof ctx.toast === 'function') ctx.toast(text, kind || 'info'); } catch (e) { /* no toast */ }
    }
    function fail(exc) {
      error = str(exc && exc.message ? exc.message : exc) || 'Something went wrong';
      renderBanner();
      setModalError(error);
      toast(error, 'error');
    }
    function call(action, args) {
      if (!ctx || typeof ctx.call !== 'function') return Promise.reject(new Error('NotionTool is not connected'));
      return Promise.resolve(ctx.call(action, args || {})).then(unwrap);
    }
    function freeChats(projectId) {
      return chats.filter(function (c) { return !c.project_id || c.project_id === projectId; });
    }

    // --- data -------------------------------------------------------------------------------------
    function load(quiet) {
      if (stopped || loading) return Promise.resolve();
      loading = true;
      if (!quiet && !loadedOnce) renderBody();
      return call('projects_list', { archived: true }).then(function (r) {
        projects = list(r.projects);
        chats = list(r.chats);
        currentId = str(r.current_project_id);
        error = '';
      }).catch(fail).then(function () {
        loading = false;
        loadedOnce = true;
        if (stopped) return;
        renderBanner();
        updateBadge();
        if (mode === 'list') { renderToolbar(); renderBody(); }
      });
    }
    function openDetail(id) {
      mode = 'detail';
      detail = null;
      error = '';
      render();
      return call('projects_detail', { id: id, n: 10 }).then(function (r) {
        detail = obj(r.project);
        currentId = str(r.current_project_id);
      }).catch(function (exc) { mode = 'list'; fail(exc); }).then(render);
    }
    // Resolves true when the action worked. A failed action only shows the error: the form the user
    // typed in (dialog or detail) is not rebuilt, so nothing typed is lost.
    function mutate(action, args, done) {
      if (busy) return Promise.resolve(false);
      busy = true;
      error = '';
      renderBanner();
      setModalError('');
      var ok = false;
      return call(action, args).then(function (r) {
        if (r.current_project_id !== undefined) currentId = str(r.current_project_id);
        if (done) done(r);
        ok = true;
      }).catch(fail).then(function () {
        busy = false;
        if (!stopped) { if (ok) render(); load(true); }
        return ok;
      });
    }
    function refreshDetail(project) {
      // Keep the commits the detail call returned when an update answers without them.
      var keep = detail && detail.id === project.id ? list(detail.commits) : [];
      detail = Object.assign({}, project, { commits: project.commits ? project.commits : keep });
    }

    // --- badge ------------------------------------------------------------------------------------
    // --- dialogs ----------------------------------------------------------------------------------
    function setModalError(text) {
      if (modalError) { modalError.textContent = text; modalError.hidden = !text; }
    }
    function closeModal() {
      if (!modal) return false;
      if (modal.parentNode) modal.parentNode.removeChild(modal);
      var back = modalOpener;
      modal = null; modalError = null; modalOpener = null;
      try { if (back && typeof back.focus === 'function' && document.contains(back)) back.focus(); } catch (e) { /* ignore */ }
      return true;
    }
    // A modal dialog over the page. 'err' is the node that shows failures of the dialog's action.
    function openModal(title, kids, err) {
      closeModal();
      modalOpener = document.activeElement;
      modalError = err || null;
      var panel = h('div', { class: 'nt-prj-dialog', role: 'dialog', 'aria-modal': 'true', 'aria-label': title },
        [h('h2', { class: 'nt-prj-dialog-title', text: title })].concat(kids));
      panel.addEventListener('keydown', function (ev) {
        if (ev.key !== 'Tab') return;
        var items = Array.prototype.filter.call(panel.querySelectorAll('button, input, select, textarea'), function (n) { return !n.disabled; });
        if (!items.length) return;
        var first = items[0], last = items[items.length - 1];
        if (ev.shiftKey && document.activeElement === first) { ev.preventDefault(); last.focus(); }
        else if (!ev.shiftKey && document.activeElement === last) { ev.preventDefault(); first.focus(); }
      });
      modal = h('div', { class: 'nt-prj-scrim', on: { mousedown: function (ev) { if (ev.target === modal) closeModal(); } } }, [panel]);
      root.appendChild(modal);
      return panel;
    }
    function confirmArchive(p) {
      var cancel = button('Cancel', null, 'ghost', closeModal);
      var go = button('Archive project', 'archive', 'danger', function () {
        closeModal();
        mutate('projects_archive', { id: p.id }, function (r) {
          if (mode === 'detail' && detail && detail.id === p.id) refreshDetail(obj(r.project));
          toast('Project archived', 'success');
        });
      });
      openModal('Archive "' + (str(p.name) || 'project') + '"?', [
        h('p', { class: 'nt-prj-dialog-text', text: 'It leaves the Active list. Its chats stay as they are and you can unarchive it any time.' }),
        h('div', { class: 'nt-prj-form-actions' }, [cancel, go])
      ]);
      try { cancel.focus(); } catch (e) { /* ignore */ }
    }

    // --- opening chats ----------------------------------------------------------------------------
    // The desktop layer never clicks chat rows or loads pages itself (overlay.js safety rule). So 'open a
    // chat' = switch to the Chat tab and bring that chat's sidebar row into view; the user clicks it.
    function chatKey(c) { return str(c && c.thread_id).replace(/-/g, '').toLowerCase(); }
    function openChat(c) {
      var sh = window.__ntShell;
      try { if (sh && typeof sh.open === 'function') sh.open('chat'); } catch (e) { fail(e); return; }
      var key = chatKey(c), title = str(c && c.title);
      if (!key) { toast('Chat "' + title + '" has no sidebar entry yet.', 'info'); return; }
      setTimeout(function () {
        var rows = document.querySelectorAll('[data-nt-desk="chat-row"]');
        for (var i = 0; i < rows.length; i++) {
          if (rows[i].getAttribute('data-nt-desk-thread') === key) {
            try { rows[i].scrollIntoView({ block: 'center' }); } catch (e) { /* ignore */ }
            toast('Chat "' + title + '" is in the sidebar. Click it to open.', 'info');
            return;
          }
        }
        toast('Chat "' + title + '" is not in the sidebar right now.', 'info');
      }, 150);
    }
    function openProjectChat(project) {
      var main = list(project && project.chats).filter(function (c) { return c.main && !c.missing; })[0];
      if (main) openChat(main);
      else toast('This project has no main chat yet. Link one in its details.', 'info');
    }

    function updateBadge() {
      var sh = window.__ntShell;
      try { if (sh && typeof sh.badge === 'function') sh.badge('projects', 0); } catch (e) { /* no badge */ }
    }

    // --- render -----------------------------------------------------------------------------------
    function visible() {
      var q = query.toLowerCase();
      return projects.filter(function (p) {
        if (filter === 'active' && p.archived) return false;
        if (filter === 'archived' && !p.archived) return false;
        if (!q) return true;
        return [p.name, p.goal, p.repo_path, p.main_chat_title].join(' ').toLowerCase().indexOf(q) >= 0;
      });
    }
    function renderBanner() {
      banner.textContent = error;
      banner.hidden = !error;
    }
    function renderHead() {
      head.textContent = '';
      var titleText = mode === 'detail' ? str(detail && detail.name) || 'Project' : 'Projects';
      var sub = mode === 'list' ? 'Goals, repo, chats and coworkers in one place.' : '';
      var left = h('div', { class: 'nt-prj-head-text' }, [h('h1', { class: 'nt-prj-title', text: titleText })]);
      if (sub) left.appendChild(h('p', { class: 'nt-prj-sub', text: sub }));
      var actions = h('div', { class: 'nt-prj-actions' });
      if (mode === 'list') {
        actions.appendChild(button('Refresh', 'refresh', 'ghost', function () { load(); }, { 'aria-label': 'Refresh projects' }));
        actions.appendChild(button('New project', 'plus', 'primary', function () { openCreate(); }));
      } else {
        actions.appendChild(button('Back', 'back', 'ghost', function () { mode = 'list'; detail = null; error = ''; render(); load(true); }));
      }
      head.appendChild(left);
      head.appendChild(actions);
    }
    function renderToolbar() {
      toolbar.hidden = mode !== 'list';
      if (mode !== 'list') return;
      var focused = document.activeElement && toolbar.contains(document.activeElement) && document.activeElement.tagName === 'INPUT';
      toolbar.textContent = '';
      var input = h('input', { class: 'nt-prj-input nt-prj-search', type: 'search', placeholder: 'Search projects', 'aria-label': 'Search projects',
        value: query, on: { input: function () {
          if (searchTimer) clearTimeout(searchTimer);
          searchTimer = setTimeout(function () { query = str(input.value).trim(); renderBody(); renderCount(); }, SEARCH_MS);
        } } });
      var box = h('label', { class: 'nt-prj-searchbox' }, [icon('search'), input]);
      var seg = h('div', { class: 'nt-prj-seg', role: 'group', 'aria-label': 'Filter projects' });
      FILTERS.forEach(function (f) {
        seg.appendChild(h('button', { type: 'button', class: 'nt-prj-segbtn' + (filter === f[0] ? ' is-on' : ''), text: f[1],
          'aria-pressed': filter === f[0] ? 'true' : 'false',
          on: { click: function () { filter = f[0]; renderToolbar(); renderBody(); } } }));
      });
      toolbar.appendChild(box);
      toolbar.appendChild(seg);
      toolbar.appendChild(h('span', { class: 'nt-prj-count', 'data-nt-count': '1' }));
      renderCount();
      if (focused) { try { input.focus(); } catch (e) { /* ignore */ } }
    }
    function renderCount() {
      var node = toolbar.querySelector('[data-nt-count]');
      if (node) { var n = visible().length; node.textContent = n + (n === 1 ? ' project' : ' projects'); }
    }
    function emptyState(title, text, action) {
      var box = h('div', { class: 'nt-prj-empty' }, [icon('folder'), h('h2', { text: title }), h('p', { text: text })]);
      if (action) box.appendChild(action);
      return box;
    }
    function badge(text, kind) { return h('span', { class: 'nt-prj-badge nt-prj-badge-' + kind, text: text }); }

    function card(p) {
      var kids = [];
      var top = h('div', { class: 'nt-prj-card-top' }, [icon('folder'), h('span', { class: 'nt-prj-card-name', text: str(p.name) || 'Untitled' })]);
      if (p.id === currentId) top.appendChild(badge('Current', 'accent'));
      top.appendChild(badge(p.archived ? 'Archived' : 'Active', p.archived ? 'muted' : 'ok'));
      kids.push(top);
      kids.push(h('p', { class: 'nt-prj-goal', text: str(p.goal) || 'No goal yet' }));
      var meta = h('div', { class: 'nt-prj-meta' }, [
        h('span', { class: 'nt-prj-meta-item', title: str(p.repo_path) || 'No repo path' }, [icon('commit'), h('span', { text: str(p.repo_path) || 'No repo' })]),
        h('span', { class: 'nt-prj-meta-item' }, [icon('chat'), h('span', { text: list(p.chat_ids).length + ' chats' })]),
        h('span', { class: 'nt-prj-meta-item' }, [icon('users'), h('span', { text: list(p.coworker_ids).length + ' coworkers' })]),
        h('span', { class: 'nt-prj-meta-item nt-prj-meta-time', text: 'Updated ' + ago(p.updated) })
      ]);
      kids.push(meta);
      var acts = h('div', { class: 'nt-prj-card-actions' }, [
        p.archived ? null : button('Archive', 'archive', 'ghost', function (ev) { ev.stopPropagation(); confirmArchive(p); }),
        p.archived
          ? button('Unarchive', 'restore', 'ghost', function (ev) { ev.stopPropagation(); mutate('projects_unarchive', { id: p.id }, function () { toast('Project is active again', 'success'); }); })
          : button('Open', 'chat', 'primary', function (ev) {
            ev.stopPropagation();
            mutate('projects_open', { id: p.id }, function (r) { toast('Project "' + str(p.name) + '" is now current', 'success'); openProjectChat(obj(r.project)); });
          })
      ]);
      kids.push(acts);
      return h('article', { class: 'nt-prj-card' + (p.id === currentId ? ' is-current' : ''), tabindex: '0', role: 'button',
        'aria-label': 'Open details of ' + str(p.name),
        on: { click: function () { openDetail(p.id); },
          keydown: function (ev) { if ((ev.key === 'Enter' || ev.key === ' ') && ev.target === ev.currentTarget) { ev.preventDefault(); openDetail(p.id); } } } }, kids);
    }

    function field(label, control, help) {
      var kids = [h('span', { class: 'nt-prj-label', text: label }), control];
      if (help) kids.push(h('span', { class: 'nt-prj-help', text: help }));
      return h('label', { class: 'nt-prj-field' }, kids);
    }
    function chatSelect(options, selected) {
      var sel = h('select', { class: 'nt-prj-input', 'aria-label': 'Main chat' });
      sel.appendChild(h('option', { value: '', text: 'No main chat' }));
      options.forEach(function (c) {
        var opt = h('option', { value: c.id, text: str(c.title) || c.id });
        if (c.id === selected) opt.selected = true;
        sel.appendChild(opt);
      });
      return sel;
    }

    function openCreate() {
      var name = h('input', { class: 'nt-prj-input', type: 'text', maxlength: '120', placeholder: 'e.g. Website relaunch', 'aria-label': 'Project name' });
      var goal = h('textarea', { class: 'nt-prj-input nt-prj-area', rows: '3', placeholder: 'What should this project achieve?', 'aria-label': 'Goal' });
      var repo = h('input', { class: 'nt-prj-input', type: 'text', placeholder: 'C:\\path\\to\\repo (optional)', 'aria-label': 'Repo path' });
      var main = chatSelect(freeChats(''), '');
      var checks = [];
      var chatBox = h('div', { class: 'nt-prj-checks' });
      freeChats('').forEach(function (c) {
        var box = h('input', { type: 'checkbox', value: c.id });
        checks.push(box);
        chatBox.appendChild(h('label', { class: 'nt-prj-check' }, [box, h('span', { text: str(c.title) || c.id })]));
      });
      if (!checks.length) chatBox.appendChild(h('p', { class: 'nt-prj-help', text: 'No free chats yet. You can link chats later.' }));
      var err = h('p', { class: 'nt-prj-dialog-error', role: 'alert', hidden: true });
      var form = h('form', { class: 'nt-prj-form', novalidate: 'novalidate', on: { submit: function (ev) {
        ev.preventDefault();
        var title = str(name.value).trim();
        if (!title) { setModalError('Enter a project name'); try { name.focus(); } catch (e) { /* ignore */ } return; }
        var ids = checks.filter(function (c) { return c.checked; }).map(function (c) { return c.value; });
        if (main.value && ids.indexOf(main.value) < 0) ids.push(main.value);
        mutate('projects_create', { name: title, goal: goal.value, repo_path: str(repo.value).trim(), main_chat_id: main.value, chat_ids: ids },
          function (r) {
            var made = obj(r.project);
            closeModal();
            toast('Project "' + str(made.name) + '" created', 'success');
            mode = 'list'; filter = 'active'; query = '';
            projects = [made].concat(projects.filter(function (p) { return p.id !== made.id; }));
          });
      } } }, [
        field('Name', name), field('Goal', goal), field('Repo path', repo, 'A git repository shows its latest commits here.'),
        field('Main chat', main), h('div', { class: 'nt-prj-field' }, [h('span', { class: 'nt-prj-label', text: 'Linked chats' }), chatBox]),
        err,
        h('div', { class: 'nt-prj-form-actions' }, [
          button('Cancel', null, 'ghost', closeModal),
          h('button', { type: 'submit', class: 'nt-prj-btn nt-prj-btn-primary' }, [icon('plus'), h('span', { text: 'Create project' })])
        ])
      ]);
      openModal('New project', [h('p', { class: 'nt-prj-sub', text: 'Link existing chats. Nothing is created in Notion.' }), form], err);
      try { name.focus(); } catch (e) { /* ignore */ }
    }

    function section(title, iconName, content, count) {
      var headNode = h('div', { class: 'nt-prj-sec-head' }, [icon(iconName), h('h2', { text: title })]);
      if (count !== undefined) headNode.appendChild(h('span', { class: 'nt-prj-count', text: String(count) }));
      return h('section', { class: 'nt-prj-panel nt-prj-sec' }, [headNode].concat(content));
    }

    function renderDetail() {
      if (!detail) { body.appendChild(h('div', { class: 'nt-prj-loading', text: 'Loading project…' })); return; }
      var p = detail;
      var name = h('input', { class: 'nt-prj-input', type: 'text', maxlength: '120', value: p.name, 'aria-label': 'Project name' });
      var goal = h('textarea', { class: 'nt-prj-input nt-prj-area', rows: '3', value: p.goal, 'aria-label': 'Goal' });
      var repo = h('input', { class: 'nt-prj-input', type: 'text', value: p.repo_path, placeholder: 'C:\\path\\to\\repo', 'aria-label': 'Repo path' });
      var status = [badge(p.archived ? 'Archived' : 'Active', p.archived ? 'muted' : 'ok')];
      if (p.id === currentId) status.unshift(badge('Current', 'accent'));
      var save = function () {
        var title = str(name.value).trim();
        if (!title) { error = 'Enter a project name'; renderBanner(); return; }
        mutate('projects_update', { id: p.id, name: title, goal: goal.value, repo_path: str(repo.value).trim() },
          function (r) { refreshDetail(obj(r.project)); toast('Project saved', 'success'); });
      };
      var topActions = h('div', { class: 'nt-prj-actions' }, status);
      topActions.appendChild(button('Edit', 'edit', 'secondary', function () { try { name.focus(); name.select(); } catch (e) { /* ignore */ } }));
      if (p.archived) {
        topActions.appendChild(button('Unarchive', 'restore', 'secondary', function () {
          mutate('projects_unarchive', { id: p.id }, function (r) { refreshDetail(obj(r.project)); toast('Project is active again', 'success'); });
        }));
      } else {
        topActions.appendChild(button('Open', 'chat', 'primary', function () {
          mutate('projects_open', { id: p.id }, function (r) { refreshDetail(obj(r.project)); toast('Project is now current', 'success'); openProjectChat(obj(r.project)); });
        }));
        topActions.appendChild(button('Archive', 'archive', 'danger', function () {
          confirmArchive(p);
        }));
      }
      body.appendChild(h('div', { class: 'nt-prj-detail-top' }, [h('span', { class: 'nt-prj-sub', text: 'Updated ' + ago(p.updated) }), topActions]));

      var form = h('form', { class: 'nt-prj-panel nt-prj-form', novalidate: 'novalidate', on: { submit: function (ev) { ev.preventDefault(); save(); } } }, [
        field('Name', name), field('Goal', goal), field('Repo path', repo),
        h('div', { class: 'nt-prj-form-actions' }, [
          button('Revert', null, 'ghost', function () { name.value = str(p.name); goal.value = str(p.goal); repo.value = str(p.repo_path); }),
          h('button', { type: 'submit', class: 'nt-prj-btn nt-prj-btn-primary', disabled: busy }, [icon('check'), h('span', { text: 'Save changes' })])
        ])
      ]);
      body.appendChild(form);

      // commits
      var commits = list(p.commits);
      var commitRows = commits.length ? commits.map(function (c) {
        return h('li', { class: 'nt-prj-row' }, [h('code', { class: 'nt-prj-sha', text: str(c.sha).slice(0, 7) }),
          h('span', { class: 'nt-prj-row-main', text: str(c.subject) }),
          h('span', { class: 'nt-prj-row-meta', text: str(c.author) + ' · ' + commitDate(c.date) })]);
      }) : [h('li', { class: 'nt-prj-row-empty', text: p.repo_path ? 'No commits found. Is this a git repository?' : 'Add a repo path to see the latest commits.' })];
      body.appendChild(section('Latest commits', 'commit', [h('ul', { class: 'nt-prj-rows' }, commitRows)], commits.length));

      // chats
      var linked = list(p.chats);
      var chatRows = linked.length ? linked.map(function (c) {
        var row = h('li', { class: 'nt-prj-row' }, [icon('chat'), h('span', { class: 'nt-prj-row-main', text: str(c.title) || c.id })]);
        if (c.main) row.appendChild(badge('Main', 'accent'));
        if (c.missing) row.appendChild(badge('Missing', 'warn'));
        var acts = h('span', { class: 'nt-prj-row-actions' });
        if (!c.main && !c.missing) {
          acts.appendChild(button('Set as main', 'star', 'ghost', function () {
            mutate('projects_update', { id: p.id, main_chat_id: c.id }, function (r) { refreshDetail(obj(r.project)); });
          }));
        }
        if (!c.missing) {
          acts.appendChild(button('Open chat', 'chat', 'ghost', function () {
            openChat(c);
          }));
        }
        acts.appendChild(button('Unlink', 'x', 'ghost', function () {
          var ids = list(p.chat_ids).filter(function (id) { return id !== c.id; });
          var args = { id: p.id, chat_ids: ids };
          if (c.main) args.main_chat_id = '';
          mutate('projects_update', args, function (r) { refreshDetail(obj(r.project)); });
        }, { 'aria-label': 'Unlink chat ' + str(c.title) }));
        row.appendChild(acts);
        return row;
      }) : [h('li', { class: 'nt-prj-row-empty', text: 'No chats linked yet.' })];
      var options = freeChats(p.id).filter(function (c) { return list(p.chat_ids).indexOf(c.id) < 0; });
      var pick = h('select', { class: 'nt-prj-input', 'aria-label': 'Chat to link' });
      pick.appendChild(h('option', { value: '', text: options.length ? 'Choose a chat to link' : 'No free chats' }));
      options.forEach(function (c) { pick.appendChild(h('option', { value: c.id, text: str(c.title) || c.id })); });
      var linkRow = h('div', { class: 'nt-prj-linkrow' }, [pick, button('Link chat', 'plus', 'secondary', function () {
        if (!pick.value) { error = 'Choose a chat to link'; renderBanner(); return; }
        var ids = list(p.chat_ids).concat([pick.value]);
        mutate('projects_update', { id: p.id, chat_ids: ids }, function (r) { refreshDetail(obj(r.project)); });
      }, { disabled: !options.length })]);
      body.appendChild(section('Chats', 'chat', [h('ul', { class: 'nt-prj-rows' }, chatRows), linkRow], linked.length));

      // coworkers
      var crew = list(p.coworkers);
      var crewRows = crew.length ? crew.map(function (w) {
        return h('li', { class: 'nt-prj-row' }, [icon('users'), h('span', { class: 'nt-prj-row-main', text: str(w.label) }),
          badge(stateLabel(w.state), w.state === 'ready' ? 'ok' : 'muted')]);
      }) : [h('li', { class: 'nt-prj-row-empty', text: 'No coworker has worked on this project yet.' })];
      body.appendChild(section('Coworkers', 'users', [h('ul', { class: 'nt-prj-rows' }, crewRows)], crew.length));
    }

    function renderBody() {
      body.textContent = '';
      // aria-modal marks the detail page as a layer above the tabs: the shell then leaves Esc to us (back to the list).
      if (mode === 'detail') body.setAttribute('aria-modal', 'true'); else body.removeAttribute('aria-modal');
      if (mode === 'detail') { renderDetail(); return; }
      if (!loadedOnce) { body.appendChild(h('div', { class: 'nt-prj-loading', text: 'Loading projects…' })); return; }
      var rows = visible();
      if (!rows.length) {
        var none = !projects.length;
        body.appendChild(emptyState(none ? 'No projects yet' : 'No matching projects',
          none ? 'Create a project to group a goal, a repo and your chats.' : 'Try another search or filter.',
          none ? button('New project', 'plus', 'primary', function () { openCreate(); }) : button('Clear filters', null, 'secondary', function () { query = ''; filter = 'all'; renderToolbar(); renderBody(); })));
        return;
      }
      var grid = h('div', { class: 'nt-prj-grid' });
      rows.forEach(function (p) { grid.appendChild(card(p)); });
      body.appendChild(grid);
    }
    function render() {
      if (stopped) return;
      renderHead();
      renderToolbar();
      renderBanner();
      renderBody();
    }

    // --- lifecycle --------------------------------------------------------------------------------
    function hidden() { return typeof document.hidden === 'boolean' && document.hidden; }
    function startRefresh() {
      if (refreshTimer || hidden()) return;
      refreshTimer = setInterval(function () { if (!hidden() && mode === 'list' && !busy) load(true); }, REFRESH_MS);
    }
    function stopRefresh() { if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; } }
    // Same rule as isTyping in shell.js: Esc must not leave the page while the user types in a field.
    function typing(node) {
      if (!node || node.nodeType !== 1) return false;
      var tag = str(node.tagName).toUpperCase();
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true;
      if (node.isContentEditable) return true;
      var ce = typeof node.getAttribute === 'function' ? node.getAttribute('contenteditable') : null;
      if (ce !== null && ce !== undefined && ce !== 'false') return true;
      return typeof node.getAttribute === 'function' && node.getAttribute('role') === 'textbox';
    }
    function onKey(event) {
      if (stopped || !shown || !event || event.key !== 'Escape') return;
      if (modal) { closeModal(); event.preventDefault(); event.stopPropagation(); return; }
      if (event.defaultPrevented || event.isComposing || event.ctrlKey || event.altKey || event.shiftKey || event.metaKey) return;
      if (typing(event.target) || typing(document.activeElement)) return;
      if (mode !== 'list') { mode = 'list'; detail = null; error = ''; render(); load(true); }
    }
    function onVisibility() { if (hidden()) stopRefresh(); else if (shown) { load(true); startRefresh(); } }
    function mount(host) {
      var node = host && typeof host.appendChild === 'function' ? host : obj(host).el || obj(host).container;
      if (stopped || !node) return null;
      if (root.parentNode !== node) node.appendChild(root);
      shown = true;
      render();
      load();
      startRefresh();
      return unmount;
    }
    function unmount() {
      closeModal();
      if (!shown) return;
      shown = false;
      stopRefresh();
      if (root.parentNode) root.parentNode.removeChild(root);
    }
    function stop() {
      if (stopped) return;
      unmount();
      stopped = true;
      if (dockTimer) clearInterval(dockTimer);
      if (searchTimer) clearTimeout(searchTimer);
      window.removeEventListener('keydown', onKey, true);
      if (typeof document.removeEventListener === 'function') document.removeEventListener('visibilitychange', onVisibility);
      try { if (typeof shellHandle === 'function') shellHandle(); else if (shellHandle && shellHandle.unregister) shellHandle.unregister(); }
      catch (e) { /* shell gone */ }
      if (window.__ntDeskProjectsOwner === owner) window.__ntDeskProjectsOwner = null;
    }

    var view = { id: 'projects', label: 'Projects', title: 'Projects', order: 20, mount: mount, unmount: unmount,
      badge: function () { return 0; } };
    function tryRegister() {
      if (registered || stopped) return registered;
      var sh = window.__ntShell;
      if (!sh || typeof sh.register !== 'function') return false;
      try { shellHandle = sh.register('projects', view); registered = true; } catch (e) { registered = false; }
      return registered;
    }

    window.__ntDeskProjectsOwner = owner;
    window.addEventListener('keydown', onKey, true);
    if (typeof document.addEventListener === 'function') document.addEventListener('visibilitychange', onVisibility);
    if (!tryRegister()) {
      // Shell not loaded yet: queue for it and keep checking lazily (stops once registered).
      try { (window.__ntShellQueue = window.__ntShellQueue || []).push(['projects', view]); } catch (e) { /* ignore */ }
      dockTimer = setInterval(function () {
        if (stopped || window.__ntDeskProjectsOwner !== owner) { stop(); return; }
        if (tryRegister() || (window.__ntShell && window.__ntShell.views && list(window.__ntShell.views()).indexOf('projects') >= 0)) {
          registered = true; clearInterval(dockTimer); dockTimer = null;
        }
      }, DOCK_MS);
    }
    return { stop: stop };
  };
})();
