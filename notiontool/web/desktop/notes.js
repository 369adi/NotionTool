/*
 * NotionTool desktop part 'notes': the Notes tab (window.__ntDesk.notes = function (ctx) { ... return { stop, open, close } }).
 * Plans and notes are Notion pages the Main creates and updates. Left: list (title, status chip Draft / Active /
 * Done, last updated, linked Main and run) with search and filters. Right: read-only reader of the page content.
 * Data comes from the harness (read-only): ctx.call('notes_list' {query,status,kind}), ctx.call('notes_page' {id}),
 * ctx.call('notes_draft' {mode,id,title,idea}) - see notiontool/notes_routes.py.
 * 'New plan' / 'Update plan' only write a draft message for the Main into an editable box. The user copies it or
 * puts it into the chat box and sends it there. This part never sends a chat and never presses Enter.
 * Registers as tab 'notes' with window.__ntShell (lazy: retries until the shell exists, or queues in
 * window.__ntShellQueue); without the shell it opens as a panel (#nt-notes, Ctrl+Shift+O).
 * Only adds own nodes (data-nt-desk), never removes Notion nodes. Text only via textContent.
 */
(function () {
  'use strict';
  window.__ntDesk = window.__ntDesk || {};

  var HASH = '#nt-notes';
  var REFRESH_MS = 30000;
  var DOCK_MS = 1000;
  var SEARCH_MS = 250;
  var STATUS_LABEL = { draft: 'Draft', active: 'Active', done: 'Done' };
  var FILTERS = [['', 'All'], ['draft', 'Draft'], ['active', 'Active'], ['done', 'Done']];
  var KINDS = [['', 'Plans and notes'], ['plan', 'Plans'], ['note', 'Notes']];
  var NOT_ALLOWED = /not allowed|unknown action/i;

  function str(v) { return v === null || v === undefined ? '' : String(v); }
  function list(v) { return Array.isArray(v) ? v : []; }
  function obj(v) { return v && typeof v === 'object' && !Array.isArray(v) ? v : {}; }

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

  function localDraft(args) {
    if (args.mode === 'update') {
      return ['Please update the plan "' + (args.title || 'Untitled') + '".',
        'Keep its structure and change only this:', args.idea || '- (write what should change)',
        'Update the "Status:" line (Draft, Active or Done) and the date, then reply with the page link.'].join('\n');
    }
    return ['Please create a new plan as a Notion page.', 'Title: ' + (args.title || '(write a short title)'),
      'Goal: ' + (args.idea || '(write what the plan is for)'),
      'Use these headings: Goal, Steps, Open questions. Put "Status: Draft" as the first line.',
      'Reply with the page link when it is done.'].join('\n');
  }

  window.__ntDesk.notes = function (ctx) {
    var el = ctx.el;
    var stopped = false, registered = false, shellHandle = null, shown = false, panelOpen = false;
    var notes = [], selected = '', page = null, filter = '', kind = '', query = '';
    var refreshTimer = null, dockTimer = null, searchTimer = null, loading = false, loadedOnce = false;
    var owner = {};

    // --- layout -----------------------------------------------------------------------------------
    var searchBox = el('input', { cls: 'nt-notes-search', type: 'search', placeholder: 'Search notes',
      'aria-label': 'Search notes' });
    var filterRow = el('div', { cls: 'nt-notes-filters', role: 'group', 'aria-label': 'Show' });
    var kindSelect = el('select', { cls: 'nt-notes-kind', 'aria-label': 'Type' });
    KINDS.forEach(function (k) { kindSelect.appendChild(el('option', { value: k[0], text: k[1] })); });
    var listBox = el('div', { cls: 'nt-notes-list', role: 'listbox', 'aria-label': 'Plans and notes' });
    var listNote = el('div', { cls: 'nt-notes-hint' });
    var newButton = el('button', { cls: 'nt-notes-btn nt-notes-primary', type: 'button', text: 'New plan',
      title: 'Write a message that asks the Main to create a plan. You send it yourself.' });
    var left = el('section', { cls: 'nt-notes-left' }, [
      el('div', { cls: 'nt-notes-head' }, [el('h2', { cls: 'nt-notes-h', text: 'Notes' }), newButton]),
      searchBox, el('div', { cls: 'nt-notes-tools' }, [filterRow, kindSelect]), listNote, listBox]);

    var openButton = el('button', { cls: 'nt-notes-btn', type: 'button', text: 'Open in Notion',
      title: 'Show this page in the normal Notion view' });
    var updateButton = el('button', { cls: 'nt-notes-btn', type: 'button', text: 'Update plan',
      title: 'Write a message that asks the Main to change this plan. You send it yourself.' });
    var refreshButton = el('button', { cls: 'nt-notes-btn nt-notes-quiet', type: 'button', text: 'Refresh' });
    var readerTitle = el('h1', { cls: 'nt-notes-title' });
    var readerMeta = el('div', { cls: 'nt-notes-meta' });
    var readerBody = el('article', { cls: 'nt-notes-body' });
    var reader = el('section', { cls: 'nt-notes-reader', 'aria-live': 'polite' }, [
      el('div', { cls: 'nt-notes-bar' }, [openButton, updateButton, refreshButton]),
      readerTitle, readerMeta, readerBody]);

    var draftText = el('textarea', { cls: 'nt-notes-draft-text', rows: '8', 'aria-label': 'Message for the Main' });
    var draftTitle = el('div', { cls: 'nt-notes-draft-title' });
    var copyButton = el('button', { cls: 'nt-notes-btn', type: 'button', text: 'Copy' });
    var putButton = el('button', { cls: 'nt-notes-btn nt-notes-primary', type: 'button', text: 'Put in chat box',
      title: 'Puts the text into the chat box. It is not sent.' });
    var draftClose = el('button', { cls: 'nt-notes-btn nt-notes-quiet', type: 'button', text: 'Close' });
    var draft = el('div', { cls: 'nt-notes-draft', hidden: true, role: 'dialog', 'aria-label': 'Message for the Main' }, [
      draftTitle,
      el('p', { cls: 'nt-notes-hint', text: 'Nothing is sent. Check the text, then send it yourself in Chat.' }),
      draftText, el('div', { cls: 'nt-notes-bar' }, [putButton, copyButton, draftClose])]);

    var root = el('div', { cls: 'nt-notes', 'data-nt-desk': 'notes-view' }, [left, reader, draft]);
    var panelClose = el('button', { cls: 'nt-notes-btn nt-notes-quiet nt-notes-panel-close', type: 'button',
      text: 'Close', 'aria-label': 'Close notes' });
    var panel = el('div', { cls: 'nt-notes-panel', 'data-nt-desk': 'notes-panel', hidden: true }, [panelClose]);

    // --- rendering --------------------------------------------------------------------------------
    function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
    function chip(status) {
      var s = STATUS_LABEL[status] ? status : 'active';
      return el('span', { cls: 'nt-notes-chip nt-notes-chip-' + s, text: STATUS_LABEL[s] });
    }
    function renderFilters() {
      clear(filterRow);
      FILTERS.forEach(function (f) {
        filterRow.appendChild(el('button', { type: 'button', text: f[1], 'aria-pressed': filter === f[0] ? 'true' : 'false',
          cls: 'nt-notes-filter' + (filter === f[0] ? ' nt-notes-on' : ''),
          onclick: function () { filter = f[0]; renderFilters(); renderList(); } }));
      });
    }
    function visible() {
      var q = query.toLowerCase();
      return notes.filter(function (n) {
        if (filter && n.status !== filter) return false;
        if (kind && n.kind !== kind) return false;
        if (q && (str(n.title) + ' ' + str(n.run_title) + ' ' + str(n.main_label)).toLowerCase().indexOf(q) < 0) return false;
        return true;
      });
    }
    function renderList() {
      clear(listBox);
      var rows = visible();
      if (!rows.length) {
        listNote.textContent = loading && !loadedOnce ? 'Loading notes...' : (notes.length ? 'No notes match.' :
          'No plans or notes yet. Use New plan to ask the Main for one.');
        return;
      }
      listNote.textContent = rows.length + (rows.length === 1 ? ' page' : ' pages');
      rows.forEach(function (n) {
        var link = [n.main_label ? 'Main: ' + n.main_label : '', n.run_title ? 'Request: ' + n.run_title : '']
          .filter(Boolean).join(' \u00b7 ');
        var item = el('button', { type: 'button', role: 'option', 'aria-selected': n.id === selected ? 'true' : 'false',
          cls: 'nt-notes-item' + (n.id === selected ? ' nt-notes-on' : ''), 'data-id': n.id,
          onclick: function () { select(n.id); } }, [
          el('div', { cls: 'nt-notes-item-top' }, [
            el('span', { cls: 'nt-notes-item-title', text: (n.icon ? n.icon + ' ' : '') + (n.title || 'Untitled') }),
            chip(n.status)]),
          el('div', { cls: 'nt-notes-item-sub', text: (n.kind === 'plan' ? 'Plan' : 'Note') + ' \u00b7 Updated ' + ago(n.updated) }),
          link ? el('div', { cls: 'nt-notes-item-link', text: link }) : null]);
        listBox.appendChild(item);
      });
    }
    function current() {
      for (var i = 0; i < notes.length; i++) if (notes[i].id === selected) return notes[i];
      return null;
    }
    function renderReader(message) {
      clear(readerBody);
      var n = current();
      var has = !!selected;
      openButton.disabled = updateButton.disabled = refreshButton.disabled = !has;
      if (!has) {
        readerTitle.textContent = 'Pick a page';
        readerMeta.textContent = '';
        readerBody.appendChild(el('p', { cls: 'nt-notes-hint', text: 'Choose a plan or note on the left to read it here.' }));
        return;
      }
      var p = page && page.id === selected ? page : null;
      readerTitle.textContent = (p && p.title) || (n && n.title) || 'Untitled';
      clear(readerMeta);
      readerMeta.appendChild(chip((p && p.status) || (n && n.status)));
      readerMeta.appendChild(el('span', { text: 'Updated ' + ago((p && p.updated) || (n && n.updated)) }));
      if (n && n.main_label) readerMeta.appendChild(el('span', { text: 'Main: ' + n.main_label }));
      if (n && n.run_title) readerMeta.appendChild(el('span', { text: 'Request: ' + n.run_title }));
      if (p && p.stale) readerMeta.appendChild(el('span', { cls: 'nt-notes-warn', text: 'Saved copy (Notion is busy)' }));
      if (message || !p) {
        readerBody.appendChild(el('p', { cls: 'nt-notes-hint', text: message || 'Loading page...' }));
        return;
      }
      var listNode = null, listTag = '';
      list(p.blocks).forEach(function (b) {
        var t = b.type, depth = Math.min(3, Number(b.depth) || 0);
        var tag = t === 'li' ? 'ul' : (t === 'ol' ? 'ol' : '');
        if (!tag || tag !== listTag || depth !== (listNode && listNode.__depth)) { listNode = null; listTag = ''; }
        var node;
        if (t === 'h1' || t === 'h2' || t === 'h3') node = el(t === 'h1' ? 'h2' : (t === 'h2' ? 'h3' : 'h4'), { text: b.text });
        else if (tag) {
          if (!listNode) { listNode = el(tag, { cls: 'nt-notes-list-block' }); listNode.__depth = depth; listTag = tag; readerBody.appendChild(listNode); }
          listNode.appendChild(el('li', { text: b.text, style: { marginLeft: depth * 18 + 'px' } }));
          return;
        } else if (t === 'todo') node = el('div', { cls: 'nt-notes-todo' + (b.checked ? ' nt-notes-checked' : '') }, [
          el('span', { cls: 'nt-notes-box', 'aria-hidden': 'true', text: b.checked ? '\u2713' : '' }), el('span', { text: b.text })]);
        else if (t === 'quote') node = el('blockquote', { text: b.text });
        else if (t === 'callout') node = el('div', { cls: 'nt-notes-callout', text: b.text });
        else if (t === 'code') node = el('pre', { cls: 'nt-notes-code' }, [el('code', { text: b.text })]);
        else if (t === 'divider') node = el('hr');
        else if (t === 'toggle') node = el('p', { cls: 'nt-notes-toggle', text: '\u25b8 ' + b.text });
        else if (t === 'subpage') node = el('p', { cls: 'nt-notes-subpage', text: '\ud83d\udcc4 ' + (b.text || 'Untitled') });
        else node = el('p', { text: b.text });
        if (depth && node.style) node.style.marginLeft = depth * 18 + 'px';
        readerBody.appendChild(node);
      });
      if (!list(p.blocks).length) readerBody.appendChild(el('p', { cls: 'nt-notes-hint', text: 'This page is empty.' }));
      if (p.truncated) readerBody.appendChild(el('p', { cls: 'nt-notes-hint', text: 'Only the first part is shown. Open in Notion to see all of it.' }));
    }

    // --- data -------------------------------------------------------------------------------------
    function explain(err) {
      var m = str(err && err.message);
      if (NOT_ALLOWED.test(m)) return 'Notes needs a NotionTool update before it can list pages.';
      if (/sign in/i.test(m)) return 'Sign in to Notion in NotionTool first.';
      if (/not connected|did not answer/i.test(m)) return 'NotionTool is not reachable right now. It will try again.';
      return m ? m.slice(0, 200) : 'Something went wrong. It will try again.';
    }
    function load() {
      if (stopped || loading) return;
      loading = true;
      renderList();
      ctx.call('notes_list', {}).then(function (res) {
        loading = false; loadedOnce = true;
        notes = list(obj(res).notes);
        renderList();
        if (!selected && notes.length) select(notes[0].id);
        else renderReader(page && page.id === selected ? '' : undefined);
      }, function (err) {
        loading = false; loadedOnce = true;
        renderList();
        listNote.textContent = explain(err);
      });
    }
    function select(id, refresh) {
      selected = id;
      renderList();
      if (!refresh && page && page.id === id) { renderReader(); return; }
      page = null;
      renderReader();
      ctx.call('notes_page', { id: id, refresh: !!refresh }).then(function (res) {
        if (stopped || selected !== id) return;
        page = obj(res);
        renderReader();
      }, function (err) { if (selected === id) renderReader(explain(err)); });
    }

    // --- drafts (never sent) ----------------------------------------------------------------------
    function showDraft(mode) {
      var n = current();
      var args = { mode: mode, id: mode === 'update' && n ? n.id : '', title: mode === 'update' && n ? n.title : '', idea: '' };
      draftTitle.textContent = mode === 'update' ? 'Message for the Main: update this plan' : 'Message for the Main: new plan';
      draftText.value = localDraft(args);
      draft.hidden = false;
      ctx.call('notes_draft', args).then(function (res) {
        var text = str(obj(res).text);
        if (text && !draft.hidden) draftText.value = text;
      }, function () { /* keep the local text */ });
      if (draftText.focus) draftText.focus();
    }
    function chatBox() {
      var boxes = [];
      try {
        boxes = Array.prototype.slice.call(document.querySelectorAll(
          '.nt-desk-composer [contenteditable="true"], .nt-desk-composer textarea'));
      } catch (e) { boxes = []; }
      return boxes.filter(function (b) { return !root.contains(b); })[0] || null;
    }
    function putInChat() {
      var text = draftText.value;
      var sh = window.__ntShell;
      if (sh && typeof sh.open === 'function') { try { sh.open('chat'); } catch (e) { /* stay */ } }
      setTimeout(function () {
        var box = chatBox();
        if (!box) { copy(); ctx.toast('Chat box not found. The text was copied; paste it in Chat.', 'info'); return; }
        try {
          box.focus();
          if (box.tagName === 'TEXTAREA') box.value = text;
          else document.execCommand('insertText', false, text);
          ctx.toast('The text is in the chat box. Check it and send it yourself.', 'info');
          draft.hidden = true;
        } catch (e) { copy(); }
      }, 300);
    }
    function copy() {
      var text = draftText.value;
      function fallback() {
        draftText.focus(); draftText.select();
        var ok = false;
        try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
        ctx.toast(ok ? 'Copied.' : 'Select the text and copy it.', ok ? 'info' : 'error');
      }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function () { ctx.toast('Copied.', 'info'); }, fallback);
      } else fallback();
    }
    function openInNotion() {
      if (!selected) return;
      var sh = window.__ntShell;
      ['showNotion', 'hideView', 'close'].some(function (k) {
        if (sh && typeof sh[k] === 'function') { try { sh[k](); return true; } catch (e) { return false; } }
        return false;
      });
      if (panelOpen) close();
      // A normal in-app link: Notion's own router opens the page (no reload).
      var a = el('a', { href: '/' + selected.replace(/-/g, ''), 'data-nt-desk': 'notes-open', style: { display: 'none' } });
      document.body.appendChild(a);
      try { a.click(); } finally { if (a.parentNode) a.parentNode.removeChild(a); }
    }

    // --- lifecycle --------------------------------------------------------------------------------
    function hidden() { return typeof document.hidden === 'boolean' && document.hidden; }
    function startRefresh() {
      if (refreshTimer || hidden()) return;
      refreshTimer = setInterval(function () { if (!hidden()) load(); }, REFRESH_MS);
    }
    function stopRefresh() { if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; } }
    function attach(node) { if (root.parentNode !== node) node.appendChild(root); }
    function mount(host) {
      var node = host && typeof host.appendChild === 'function' ? host : (obj(host).el || obj(host).container);
      if (stopped || !node) return null;
      if (panelOpen) close();
      attach(node);
      shown = true;
      load();
      startRefresh();
      return unmount;
    }
    function unmount() {
      if (!shown) return;
      shown = false;
      if (root.parentNode && !panelOpen) root.parentNode.removeChild(root);
      if (!panelOpen) stopRefresh();
    }
    var view = { id: 'notes', label: 'Notes', title: 'Notes', order: 30, mount: mount, unmount: unmount,
      badge: function () { return 0; } };
    function tryRegister() {
      if (registered || stopped) return registered;
      var sh = window.__ntShell;
      if (!sh || typeof sh.register !== 'function') return false;
      try {
        shellHandle = sh.register('notes', view);
        registered = true;
      } catch (e) { registered = false; }
      return registered;
    }
    function open() {
      if (stopped) return;
      tryRegister();
      var sh = window.__ntShell;
      if (registered && sh && typeof sh.open === 'function') { try { sh.open('notes'); return; } catch (e) { /* panel */ } }
      panelOpen = true;
      attach(panel);
      panel.hidden = false;
      load();
      startRefresh();
    }
    function close() {
      if (!panelOpen) return;
      panelOpen = false;
      panel.hidden = true;
      if (root.parentNode === panel) panel.removeChild(root);
      if (!shown) stopRefresh();
    }
    function onKey(event) {
      if (stopped || !event) return;
      if (event.ctrlKey && event.shiftKey && String(event.key || '').toLowerCase() === 'o') {
        if (event.preventDefault) event.preventDefault();
        if (panelOpen) close(); else open();
      } else if (event.key === 'Escape' && !draft.hidden) draft.hidden = true;
    }
    function onHash() { if (location.hash === HASH) open(); }
    function onVisibility() { if (hidden()) stopRefresh(); else if (shown || panelOpen) { load(); startRefresh(); } }
    function stop() {
      if (stopped) return;
      stopped = true;
      stopRefresh();
      if (dockTimer) clearInterval(dockTimer);
      if (searchTimer) clearTimeout(searchTimer);
      window.removeEventListener('keydown', onKey, true);
      window.removeEventListener('hashchange', onHash);
      if (typeof document.removeEventListener === 'function') document.removeEventListener('visibilitychange', onVisibility);
      try { if (typeof shellHandle === 'function') shellHandle(); else if (shellHandle && shellHandle.unregister) shellHandle.unregister(); }
      catch (e) { /* shell gone */ }
      if (root.parentNode) root.parentNode.removeChild(root);
      if (panel.parentNode) panel.parentNode.removeChild(panel);
      if (window.__ntDeskNotesOwner === owner) window.__ntDeskNotesOwner = null;
    }

    searchBox.addEventListener('input', function () {
      if (searchTimer) clearTimeout(searchTimer);
      searchTimer = setTimeout(function () { query = str(searchBox.value).trim(); renderList(); }, SEARCH_MS);
    });
    kindSelect.addEventListener('change', function () { kind = str(kindSelect.value); renderList(); });
    newButton.addEventListener('click', function () { showDraft('new'); });
    updateButton.addEventListener('click', function () { if (selected) showDraft('update'); });
    refreshButton.addEventListener('click', function () { if (selected) select(selected, true); load(); });
    openButton.addEventListener('click', openInNotion);
    copyButton.addEventListener('click', copy);
    putButton.addEventListener('click', putInChat);
    draftClose.addEventListener('click', function () { draft.hidden = true; });
    panelClose.addEventListener('click', close);

    window.__ntDeskNotesOwner = owner;
    renderFilters();
    renderList();
    renderReader();
    document.body.appendChild(panel);
    window.addEventListener('keydown', onKey, true);
    window.addEventListener('hashchange', onHash);
    if (typeof document.addEventListener === 'function') document.addEventListener('visibilitychange', onVisibility);
    if (!tryRegister()) {
      // Shell not loaded yet: queue for it and keep checking lazily (stops once registered).
      try { (window.__ntShellQueue = window.__ntShellQueue || []).push(['notes', view]); } catch (e) { /* ignore */ }
      dockTimer = setInterval(function () {
        if (stopped || window.__ntDeskNotesOwner !== owner) { stop(); return; }
        if (tryRegister() || (window.__ntShell && window.__ntShell.views && list(window.__ntShell.views()).indexOf('notes') >= 0)) {
          registered = true; clearInterval(dockTimer); dockTimer = null;
        }
      }, DOCK_MS);
    }
    if (location.hash === HASH) open();
    return { stop: stop, open: open, close: close };
  };
})();
