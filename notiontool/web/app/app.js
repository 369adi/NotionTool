/* NotionTool standalone window (data/desktop_app.json app_ui_mode = 'standalone').
 *
 * This page IS the NotionTool app: own window (pywebview + WebView2), no Notion page. All data comes from the
 * Host API (/api/ui/app/*, notiontool/app_routes.py); the host talks to Notion only over its API (/api/v3).
 * The desktop parts settings.js and projects.js run unchanged on an emulated ctx and window.__ntShell.
 */
(function () {
  'use strict';

  var API = '/api/ui/app';
  var LIVE_MS = 400, STATE_MS = 5000, CHATS_MS = 45000, JOB_WAIT_MS = 20000;

  // ---------- access token (Host API auth: X-NotionTool-Token) ----------
  var token = '';
  try {
    token = decodeURIComponent(String(location.hash || '').replace(/^#/, ''));
    if (token) {
      sessionStorage.setItem('ntAppToken', token);
      history.replaceState(null, '', location.pathname);
    } else {
      token = sessionStorage.getItem('ntAppToken') || '';
    }
  } catch (e) { /* storage blocked: keep the hash token */ }

  function query(params) {
    var keys = Object.keys(params || {}).filter(function (k) {
      return params[k] !== undefined && params[k] !== null && params[k] !== '';
    });
    if (!keys.length) return '';
    return '?' + keys.map(function (k) { return encodeURIComponent(k) + '=' + encodeURIComponent(params[k]); }).join('&');
  }

  function request(method, name, data) {
    var opts = {method: method, headers: {'X-NotionTool-Token': token}, cache: 'no-store'};
    var url = API + '/' + name;
    if (method === 'GET') {
      url += query(data);
    } else {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(data || {});
    }
    return fetch(url, opts).then(function (res) {
      return res.text().then(function (text) {
        var body = {};
        try { body = text ? JSON.parse(text) : {}; } catch (e) { body = {error: String(text).slice(0, 300)}; }
        if (!res.ok) throw new Error(body.error || ('Request failed (' + res.status + ')'));
        return body;
      });
    });
  }
  function get(name, params) { return request('GET', name, params); }
  function post(name, body) { return request('POST', name, body); }

  // ---------- small DOM helpers (same contract as the desktop ctx) ----------
  function el(tag, props, kids) {
    var node = document.createElement(tag);
    props = props || {};
    Object.keys(props).forEach(function (key) {
      var value = props[key];
      if (value === undefined || value === null || value === false) return;
      if (key === 'cls' || key === 'className') node.className = value;
      else if (key === 'text') node.textContent = String(value);
      else if (key === 'style' && typeof value === 'object') Object.keys(value).forEach(function (k) { node.style[k] = value[k]; });
      else if (key.slice(0, 2) === 'on' && typeof value === 'function') node.addEventListener(key.slice(2).toLowerCase(), value);
      else node.setAttribute(key, value === true ? '' : String(value));
    });
    (Array.isArray(kids) ? kids : (kids == null ? [] : [kids])).forEach(function (kid) {
      if (kid === null || kid === undefined || kid === false) return;
      node.appendChild(typeof kid === 'object' ? kid : document.createTextNode(String(kid)));
    });
    return node;
  }

  var toastBox = null;
  function toast(text, kind) {
    if (!document.body) return;
    if (!toastBox || !toastBox.isConnected) { toastBox = el('div', {cls: 'nt-desk-toasts'}); document.body.appendChild(toastBox); }
    var item = el('div', {cls: 'nt-desk-toast nt-desk-toast-' + (kind || 'info'), text: String(text || '')});
    toastBox.appendChild(item);
    setTimeout(function () { if (item.parentNode) item.parentNode.removeChild(item); }, kind === 'error' ? 8000 : 4000);
  }

  // ---------- app state ----------
  var app = {
    state: null, offline: '', busy: '', switching: false,
    chats: [], chatsLoaded: false, chatsError: '', chatSpace: '',
    active: '', draftNew: false, messages: [], live: null, pending: {}, polling: {}, loadSeq: 0, chatsSeq: 0,
    tab: 'chat'
  };
  var dom = {}, views = {}, panes = {}, unmounts = {}, badges = {}, parts = {}, listeners = [];

  // ctx for the desktop parts: call() goes to the desk route (DESK_ACTIONS on the host).
  var ctx = {
    call: function (action, args) {
      return post('desk', {action: action, args: args || {}}).then(function (res) { return res.result; });
    },
    onState: function (fn) {
      listeners.push(fn);
      return function () { var i = listeners.indexOf(fn); if (i >= 0) listeners.splice(i, 1); };
    },
    getState: function () { return app.state; },
    el: el,
    toast: toast
  };
  window.__ntDesk = window.__ntDesk || {};
  window.__ntDeskCtx = ctx;

  // Tab shell for the desktop parts (projects.js registers its view here).
  var shell = {
    register: function (a, b) {
      var view = b || a, name = b ? a : (view && view.id);
      if (!name || !view) return function () {};
      views[name] = view;
      renderTabs();
      if (app.tab === name) showTab(name);
      return function () { if (views[name] === view) { delete views[name]; renderTabs(); } };
    },
    open: function (name) { showTab(name || 'chat'); },
    views: function () { return tabList().map(function (t) { return t.id; }); },
    badge: function (name, value) { badges[name] = value; renderTabs(); },
    active: function () { return app.tab; }
  };
  var queued = Array.isArray(window.__ntShellQueue) ? window.__ntShellQueue : [];
  window.__ntShell = shell;
  window.__ntShellQueue = {push: function (item) { register(item); }};
  function register(item) {
    if (Array.isArray(item)) shell.register(item[0], item[1]); else shell.register(item);
  }

  // ---------- helpers ----------
  function threadAttr(id) { return String(id || '').replace(/-/g, '').toLowerCase(); }
  function ago(ms) {
    ms = Number(ms) || 0;
    if (!ms) return '';
    if (ms < 1e12) ms *= 1000;
    var s = (Date.now() - ms) / 1000;
    if (s < 60) return 'now';
    if (s < 3600) return Math.floor(s / 60) + 'm';
    if (s < 86400) return Math.floor(s / 3600) + 'h';
    if (s < 604800) return Math.floor(s / 86400) + 'd';
    return new Date(ms).toLocaleDateString();
  }
  function initials(text) {
    var words = String(text || '?').replace(/@.*/, '').split(/[\s._-]+/).filter(Boolean);
    return ((words[0] || '?').charAt(0) + (words[1] || '').charAt(0)).toUpperCase();
  }
  function nearBottom(box) { return box.scrollHeight - box.scrollTop - box.clientHeight < 90; }
  function remember(id) {
    var space = (app.state && app.state.space_id) || app.chatSpace;
    try { if (space && id) localStorage.setItem('ntAppLast:' + space, id); } catch (e) {}
  }
  function lastChat() {
    var space = (app.state && app.state.space_id) || app.chatSpace;
    try { return localStorage.getItem('ntAppLast:' + space) || ''; } catch (e) { return ''; }
  }
  function isRunning(id) {
    id = id === undefined ? app.active : id;
    return !!(id && app.live && app.live.state === 'running' && app.active === id);
  }

  // Small, safe Markdown: code fences, lists, headings, bold, inline code, links. Text only, never HTML.
  function inline(text) {
    var out = [], re = /(`[^`]+`|\*\*[^*]+\*\*|\[[^\]]+\]\(https?:[^)\s]+\))/g, last = 0, m;
    text = String(text);
    while ((m = re.exec(text))) {
      if (m.index > last) out.push(document.createTextNode(text.slice(last, m.index)));
      var t = m[0];
      if (t.charAt(0) === '`') out.push(el('code', {text: t.slice(1, -1)}));
      else if (t.charAt(0) === '*') out.push(el('strong', {text: t.slice(2, -2)}));
      else {
        var cut = t.indexOf('](');
        out.push(el('a', {href: t.slice(cut + 2, -1), target: '_blank', rel: 'noopener noreferrer', text: t.slice(1, cut)}));
      }
      last = re.lastIndex;
    }
    if (last < text.length) out.push(document.createTextNode(text.slice(last)));
    return out;
  }
  function markdown(text) {
    var root = document.createDocumentFragment();
    String(text || '').split('```').forEach(function (part, i) {
      if (i % 2 === 1) {
        var nl = part.indexOf('\n');
        var code = nl >= 0 && /^[\w+.#-]*$/.test(part.slice(0, nl).trim()) ? part.slice(nl + 1) : part;
        root.appendChild(el('pre', {cls: 'nt-code'}, el('code', {text: code.replace(/\n$/, '')})));
        return;
      }
      part.split(/\n{2,}/).forEach(function (block) {
        if (!block.trim()) return;
        var lines = block.replace(/^\n+|\n+$/g, '').split('\n');
        var bullet = /^\s*([-*\u2022]|\d+[.)])\s+/;
        if (lines.every(function (l) { return bullet.test(l); })) {
          var list = el(/^\s*\d/.test(lines[0]) ? 'ol' : 'ul');
          lines.forEach(function (l) { list.appendChild(el('li', null, inline(l.replace(bullet, '')))); });
          root.appendChild(list);
          return;
        }
        var head = /^(#{1,4})\s+(.*)$/.exec(lines[0]);
        if (head && lines.length === 1) {
          root.appendChild(el('h' + Math.min(6, head[1].length + 2), null, inline(head[2])));
          return;
        }
        var p = el('p');
        lines.forEach(function (l, j) {
          if (j) p.appendChild(el('br'));
          inline(l).forEach(function (n) { p.appendChild(n); });
        });
        root.appendChild(p);
      });
    });
    return root;
  }
  function thinking() { return el('span', {cls: 'nt-thinking', title: 'Thinking'}, [el('i'), el('i'), el('i')]); }

  // ---------- layout ----------
  function build() {
    var root = document.getElementById('app');
    root.textContent = '';
    dom.space = el('button', {type: 'button', cls: 'nt-space', title: 'Account and workspace (Ctrl+,)',
      onclick: function () { showTab('settings'); }});
    dom.tabs = el('nav', {cls: 'nt-tabs'});
    dom.search = el('input', {cls: 'nt-search', type: 'search', placeholder: 'Search chats (Ctrl+K)', oninput: renderChats});
    dom.list = el('div', {cls: 'nt-chatlist'});
    dom.status = el('div', {cls: 'nt-status'});
    var side = el('aside', {cls: 'nt-side'}, [
      el('div', {cls: 'nt-brand'}, [el('span', {cls: 'nt-logo'}), el('span', {text: 'NotionTool'})]),
      dom.space,
      dom.tabs,
      el('button', {type: 'button', cls: 'nt-new', title: 'New chat (Ctrl+N)', onclick: newChat}, [el('span', {cls: 'nt-plus', text: '+'}), 'New chat']),
      dom.search,
      dom.list,
      dom.status
    ]);

    dom.title = el('h1', {cls: 'nt-title', text: 'Chat'});
    dom.meta = el('div', {cls: 'nt-meta'});
    dom.messages = el('div', {cls: 'nt-messages'});
    dom.input = el('textarea', {cls: 'nt-input', rows: '1', placeholder: 'Message Notion AI... (Enter sends, Shift+Enter new line)',
      onkeydown: onComposerKey, oninput: autosize});
    dom.send = el('button', {type: 'submit', cls: 'nt-send', text: 'Send'});
    dom.composer = el('form', {cls: 'nt-composer', onsubmit: function (e) { e.preventDefault(); send(); }}, [dom.input, dom.send]);
    dom.chatPane = el('section', {cls: 'nt-pane nt-pane-chat', 'data-tab': 'chat'}, [
      el('header', {cls: 'nt-head'}, [
        el('div', {cls: 'nt-head-text'}, [dom.title, dom.meta]),
        el('button', {type: 'button', cls: 'nt-ghost', text: 'Reload', title: 'Load this chat and the chat list again',
          onclick: function () { if (app.active) loadChat(app.active); loadChats(true); }})
      ]),
      dom.messages,
      el('div', {cls: 'nt-composer-wrap'}, dom.composer)
    ]);

    dom.settingsTop = el('div', {cls: 'nt-settings-top'});
    dom.settingsAll = el('div', {cls: 'nt-settings-all'});
    dom.settings = el('section', {cls: 'nt-pane nt-pane-settings', 'data-tab': 'settings'}, [
      el('div', {cls: 'nt-settings-inner'}, [
        el('h1', {cls: 'nt-title', text: 'Settings'}),
        dom.settingsTop,
        el('section', {cls: 'nt-card nt-card-all'}, [el('h2', {text: 'All settings'}), dom.settingsAll])
      ])
    ]);
    dom.views = el('div', {cls: 'nt-view-panes'});
    var main = el('main', {cls: 'nt-main'}, [dom.chatPane, dom.settings, dom.views]);
    root.appendChild(side);
    root.appendChild(main);
  }

  function tabList() {
    var extra = Object.keys(views).filter(function (k) { return k !== 'chat' && k !== 'settings'; }).map(function (k) {
      var v = views[k] || {};
      return {id: k, label: v.label || v.title || (k.charAt(0).toUpperCase() + k.slice(1)),
              order: typeof v.order === 'number' ? v.order : 50};
    }).sort(function (a, b) { return a.order - b.order; });
    return [{id: 'chat', label: 'Chats'}].concat(extra, [{id: 'settings', label: 'Settings'}]);
  }

  function renderTabs() {
    if (!dom.tabs) return;
    dom.tabs.textContent = '';
    tabList().forEach(function (t, i) {
      var badge = badges[t.id];
      dom.tabs.appendChild(el('button', {type: 'button', cls: 'nt-tab' + (app.tab === t.id ? ' is-active' : ''),
        title: t.label + ' (Ctrl+' + (i + 1) + ')', onclick: function () { showTab(t.id); }}, [
        el('span', {text: t.label}), badge ? el('span', {cls: 'nt-tab-badge', text: String(badge)}) : null
      ]));
    });
  }

  function paneFor(name) {
    if (name === 'chat') return dom.chatPane;
    if (name === 'settings') return dom.settings;
    if (!panes[name]) {
      panes[name] = el('section', {cls: 'nt-pane nt-pane-view', 'data-tab': name});
      dom.views.appendChild(panes[name]);
    }
    return panes[name];
  }

  function showTab(name) {
    if (!dom.chatPane) return;
    if (name !== 'chat' && name !== 'settings' && !views[name]) name = 'chat';
    var before = app.tab;
    if (before !== name && unmounts[before]) {
      try { unmounts[before](); } catch (e) { /* view cleanup only */ }
      delete unmounts[before];
    }
    app.tab = name;
    [dom.chatPane, dom.settings].concat(Object.keys(panes).map(function (k) { return panes[k]; }))
      .forEach(function (p) { p.hidden = true; });
    var pane = paneFor(name);
    pane.hidden = false;
    if (views[name] && name !== 'settings' && !unmounts[name]) {
      pane.textContent = '';
      try {
        var off = views[name].mount(pane);
        unmounts[name] = typeof off === 'function' ? off : function () {};
      } catch (e) {
        pane.appendChild(el('div', {cls: 'nt-empty', text: 'This view could not be shown: ' + e.message}));
      }
    }
    if (name === 'settings') { renderSettings(true); mountAllSettings(); }
    renderTabs();
  }

  // ---------- sidebar ----------
  function renderSpace() {
    var s = app.state || {};
    dom.space.textContent = '';
    dom.space.appendChild(el('span', {cls: 'nt-avatar', text: initials(s.space_name || s.user_name || 'N')}));
    dom.space.appendChild(el('span', {cls: 'nt-space-text'}, [
      el('b', {text: s.space_name || (s.account_id ? 'Choose a workspace' : 'Not signed in')}),
      el('small', {text: s.user_name || s.email || 'Open Settings to sign in'})
    ]));
  }

  function renderStatus() {
    var s = app.state || {};
    var running = (s.jobs || []).filter(function (j) { return j.state === 'running'; }).map(function (j) { return j.title; });
    var text = app.busy || (app.offline ? 'NotionTool host not reachable: ' + app.offline : running.join(' / '));
    dom.status.textContent = text || '';
    dom.status.className = 'nt-status' + (app.offline ? ' is-error' : text ? ' is-busy' : '');
  }

  function visibleChats() {
    var q = dom.search.value.trim().toLowerCase();
    var filter = app.state && app.state.chat_filter;
    return app.chats.filter(function (row) {
      if (q && String(row.title || '').toLowerCase().indexOf(q) < 0) return false;
      if (row.id === app.active || row.pending) return true;
      if (filter === 'hide_coworkers' && row.role === 'coworker') return false;
      if (filter === 'mains_only' && row.role !== 'main') return false;
      return true;
    });
  }

  function chatRow(row) {
    var running = (app.state && (app.state.running || []).indexOf(row.id) >= 0) || app.polling[row.id];
    return el('button', {type: 'button',
      cls: 'nt-chat' + (row.id === app.active ? ' is-active' : '') + (row.unread ? ' is-unread' : '') + (row.pending ? ' is-pending' : ''),
      'data-nt-desk': 'chat-row', 'data-nt-desk-thread': threadAttr(row.id), title: row.title || 'New chat',
      onclick: function () { openChat(row.id); }}, [
      el('span', {cls: 'nt-chat-title', text: row.title || 'New chat'}),
      row.role === 'main' ? el('span', {cls: 'nt-pill nt-pill-main', text: 'Main'}) :
        row.role === 'coworker' ? el('span', {cls: 'nt-pill', text: 'Coworker'}) : null,
      running ? el('span', {cls: 'nt-live-dot', title: 'Answering'}) : el('span', {cls: 'nt-chat-time', text: ago(row.updated)})
    ]);
  }

  function renderChats() {
    if (!dom.list) return;
    var box = dom.list, top = box.scrollTop;
    box.textContent = '';
    if (!app.chatsLoaded) {
      box.appendChild(el('div', {cls: 'nt-empty', text: app.chatsError ? 'Chats could not load: ' + app.chatsError : 'Loading chats...'}));
      return;
    }
    var rows = visibleChats();
    if (!rows.length) box.appendChild(el('div', {cls: 'nt-empty', text: dom.search.value ? 'No chat matches.' : 'No chats yet.'}));
    rows.forEach(function (row) { box.appendChild(chatRow(row)); });
    box.scrollTop = top;
  }

  function loadChats(fresh) {
    var seq = ++app.chatsSeq;
    return get('chats', fresh ? {fresh: 1} : null).then(function (data) {
      if (seq !== app.chatsSeq) return data;
      app.chats = data.chats || [];
      app.chatSpace = data.space_id || '';
      app.chatsLoaded = true;
      app.chatsError = '';
      renderChats();
      renderHead();
      return data;
    }, function (err) {
      if (seq === app.chatsSeq) { app.chatsError = err.message; renderChats(); }
      return null;
    });
  }

  function pickChat() {
    var ids = app.chats.map(function (r) { return r.id; });
    var main = app.state && app.state.main_thread_id;
    var last = lastChat();
    var target = ids.indexOf(last) >= 0 ? last : (main && ids.indexOf(main) >= 0 ? main : (ids[0] || ''));
    if (target) openChat(target); else newChat();
  }

  // ---------- chat ----------
  function renderHead() {
    if (!dom.title) return;
    var row = app.chats.filter(function (r) { return r.id === app.active; })[0];
    var s = app.state || {};
    dom.title.textContent = app.active ? ((row && row.title) || 'Chat') : 'New chat';
    dom.meta.textContent = [row && row.role === 'main' ? 'Main chat' : row && row.role === 'coworker' ? 'Coworker chat' : '',
      s.space_name, s.model ? 'Model: ' + s.model : ''].filter(Boolean).join('  \u00b7  ');
  }

  function bubble(role, text, live) {
    var body = el('div', {cls: 'nt-msg-body'});
    if (role === 'user') body.textContent = text;
    else if (text) body.appendChild(markdown(text));
    else body.appendChild(thinking());
    if (live) { dom.liveBody = body; body._text = text; }
    return el('div', {cls: 'nt-msg nt-msg-' + (role === 'user' ? 'user' : 'assistant') + (live ? ' is-live' : '')}, [
      el('div', {cls: 'nt-msg-role', text: role === 'user' ? 'You' : 'Notion AI'}), body
    ]);
  }

  function hasUserText(text) {
    var want = String(text || '').trim();
    for (var i = app.messages.length - 1; i >= 0; i--) {
      if (app.messages[i].role === 'user') return String(app.messages[i].text || '').trim() === want;
    }
    return false;
  }

  function renderMessages(notice) {
    var box = dom.messages, stick = nearBottom(box) || !!notice;
    box.textContent = '';
    dom.liveBody = null;
    var s = app.state || {};
    if (notice) {
      box.appendChild(el('div', {cls: 'nt-empty nt-empty-big', text: notice}));
    } else if (!s.account_id && app.state) {
      box.appendChild(el('div', {cls: 'nt-empty nt-empty-big'}, ['No Notion account is signed in. ',
        el('button', {type: 'button', cls: 'nt-link', text: 'Open Settings', onclick: function () { showTab('settings'); }})]));
    }
    app.messages.forEach(function (m) { box.appendChild(bubble(m.role, m.text)); });
    var live = app.live, wait = app.pending[app.active];
    if (live && (live.state === 'running' || live.show)) {
      if (wait && !hasUserText(wait)) box.appendChild(bubble('user', wait));
      if (live.state === 'error') box.appendChild(el('div', {cls: 'nt-msg nt-msg-error', text: live.error || 'The reply failed.'}));
      else box.appendChild(bubble('assistant', live.text || '', live.state === 'running'));
    }
    if (!notice && !app.messages.length && !(live && (live.state === 'running' || live.show)) && s.account_id) {
      box.appendChild(el('div', {cls: 'nt-empty nt-empty-big'}, [
        el('div', {cls: 'nt-hello', text: app.active ? 'This chat has no messages yet.' : 'New chat'}),
        el('div', {text: 'Write a message below. The reply streams in live.'})
      ]));
    }
    if (stick) box.scrollTop = box.scrollHeight;
    setComposer();
  }

  function renderLive() {
    var body = dom.liveBody;
    if (!body || !body.isConnected || !app.live || app.live.state !== 'running') { renderMessages(); return; }
    var text = app.live.text || '';
    if (body._text === text) return;
    var stick = nearBottom(dom.messages);
    body._text = text;
    body.textContent = '';
    body.appendChild(text ? markdown(text) : thinking());
    if (stick) dom.messages.scrollTop = dom.messages.scrollHeight;
  }

  function setComposer() {
    if (!dom.send) return;
    var running = isRunning();
    dom.send.disabled = app.sending || running || !(app.state && app.state.account_id);
    dom.send.textContent = app.sending ? 'Sending...' : running ? 'Answering...' : 'Send';
  }

  function autosize() {
    var box = dom.input;
    box.style.height = 'auto';
    box.style.height = Math.min(260, box.scrollHeight) + 'px';
  }

  function onComposerKey(e) {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
  }

  function newChat() {
    app.active = '';
    app.draftNew = true;
    app.messages = [];
    app.live = null;
    app.loadSeq++;
    showTab('chat');
    renderChats();
    renderHead();
    renderMessages();
    if (dom.input) dom.input.focus();
  }

  function openChat(id) {
    if (!id) { newChat(); return; }
    app.active = id;
    app.draftNew = false;
    app.messages = [];
    app.live = null;
    remember(id);
    showTab('chat');
    renderChats();
    renderHead();
    renderMessages('Loading chat...');
    loadChat(id);
    if (dom.input) dom.input.focus();
  }

  function loadChat(id, finished, retried) {
    var seq = ++app.loadSeq;
    return get('chat', {id: id, n: 100}).then(function (data) {
      if (seq !== app.loadSeq || app.active !== id) return;
      app.messages = data.messages || [];
      var live = data.live, last = app.messages[app.messages.length - 1];
      var done = finished || null;
      if (live && live.state === 'running') {
        app.live = live;
        poll(id);
      } else if (done && (done.state === 'error' || !last || last.role === 'user')) {
        app.live = Object.assign({show: true}, done);  // Notion has not stored the reply yet: keep the live text
        if (done.state !== 'error' && !retried) setTimeout(function () { if (app.active === id) loadChat(id, done, true); }, 2500);
      } else {
        app.live = null;
        if (hasUserText(app.pending[id])) delete app.pending[id];
      }
      renderMessages();
    }, function (err) {
      if (seq === app.loadSeq && app.active === id) renderMessages('This chat could not load: ' + err.message);
    });
  }

  function poll(id) {
    if (app.polling[id]) return;
    app.polling[id] = true;
    var misses = 0;
    (function tick() {
      get('live', {id: id}).then(function (data) {
        misses = 0;
        var live = data.live;
        if (!live) { delete app.polling[id]; return; }
        if (app.active === id) { app.live = live; renderLive(); }
        if (live.state === 'running') { setTimeout(tick, LIVE_MS); return; }
        delete app.polling[id];
        if (live.state === 'error') toast(live.error || 'The reply failed.', 'error');
        loadChats(true);
        if (app.active === id) loadChat(id, live);
      }, function () {
        if (++misses > 20) { delete app.polling[id]; renderChats(); return; }
        setTimeout(tick, 1500);
      });
    })();
  }

  function send() {
    var text = dom.input.value.trim();
    if (!text || app.sending || isRunning()) return;
    if (!app.state || !app.state.account_id) { toast('Sign in to a Notion account first (Settings).', 'error'); return; }
    var id = app.active;
    app.sending = true;
    setComposer();
    post('send', {thread_id: id || '', text: text}).then(function (res) {
      dom.input.value = '';
      autosize();
      var tid = res.thread_id;
      app.pending[tid] = text;
      if (!id || res.created) {
        app.active = tid;
        app.draftNew = false;
        app.messages = [];
        remember(tid);
      }
      if (app.active === tid) {
        app.live = {state: 'running', text: '', created: !!res.created};
        renderHead();
        renderMessages();
        dom.messages.scrollTop = dom.messages.scrollHeight;
      }
      poll(tid);
      loadChats(false);  // a new chat shows at once as a pending row
    }, function (err) {
      toast(err.message, 'error');
    }).then(function () {
      app.sending = false;
      setComposer();
    });
  }

  // ---------- host state, account and workspace ----------
  function loadState() {
    return get('state').then(function (s) {
      var before = app.state;
      app.state = s;
      app.offline = '';
      listeners.slice().forEach(function (fn) { try { fn(s); } catch (e) { /* part listener */ } });
      renderSpace();
      renderStatus();
      setComposer();
      if (app.tab === 'settings') renderSettings();
      if (before && !app.switching) {
        if (before.account_id !== s.account_id || before.space_id !== s.space_id) {
          resetWorkspace();
        } else {
          var was = {};
          (before.jobs || []).forEach(function (j) { was[j.id] = j.state; });
          var ended = (s.jobs || []).some(function (j) { return was[j.id] === 'running' && j.state !== 'running'; });
          if (ended) loadChats(true).then(function () { if (!app.active && !app.draftNew) pickChat(); });
        }
      }
      return s;
    }, function (err) {
      app.offline = err.message;
      renderStatus();
      return app.state;
    });
  }

  function resetWorkspace() {
    app.chats = [];
    app.chatsLoaded = false;
    app.active = '';
    app.draftNew = false;
    app.messages = [];
    app.live = null;
    app.loadSeq++;
    renderChats();
    renderHead();
    renderMessages('Loading workspace...');
    return loadChats(true).then(function () { pickChat(); });
  }

  function waitJob(id, maxMs) {
    if (!id) return Promise.resolve(null);
    var until = Date.now() + (maxMs || JOB_WAIT_MS);
    return new Promise(function (resolve) {
      (function check() {
        get('state').then(function (s) { return s; }, function () { return null; }).then(function (s) {
          var job = ((s && s.jobs) || []).filter(function (j) { return j.id === id; })[0];
          if (job && job.state !== 'running') { resolve(job); return; }
          if (Date.now() > until) { resolve(job || null); return; }
          setTimeout(check, 1000);
        });
      })();
    });
  }

  function setBusy(text) {
    app.busy = text || '';
    renderStatus();
    if (app.tab === 'settings') renderSettings(true);
  }

  function afterSwitch(message) {
    return loadState().then(function () { return resetWorkspace(); }).then(function () {
      if (message) toast(message, 'info');
    });
  }

  // Always switches for real: also the current workspace again (chat list, Main and data reload from it).
  function chooseSpace(spaceId) {
    if (app.busy) return;
    var s = app.state || {};
    var same = spaceId === s.space_id;
    var name = ((s.spaces || []).filter(function (sp) { return sp.id === spaceId; })[0] || {}).name || 'workspace';
    app.switching = true;
    setBusy(same ? 'Reloading workspace...' : 'Switching to ' + name + '...');
    post('space', {space_id: spaceId, account_id: s.account_id}).then(function (res) {
      return waitJob(res.job && res.job.id);
    }).then(function (job) {
      if (job && job.state === 'failed') toast(job.error || 'The workspace switch failed.', 'error');
      return afterSwitch(same ? 'Workspace reloaded: ' + name : 'Workspace: ' + name);
    }).then(null, function (err) {
      toast(err.message, 'error');
    }).then(function () {
      app.switching = false;
      setBusy('');
    });
  }

  function chooseAccount(accountId) {
    if (app.busy) return;
    app.switching = true;
    setBusy('Switching account...');
    post('account', {account_id: accountId}).then(function () {
      return afterSwitch('Account switched');
    }).then(null, function (err) {
      toast(err.message, 'error');
    }).then(function () {
      app.switching = false;
      setBusy('');
    });
  }

  function deskAction(action, args, busyText, doneText) {
    if (app.busy) return Promise.resolve(null);
    setBusy(busyText);
    return ctx.call(action, args || {}).then(function (result) {
      if (doneText) toast(doneText, 'info');
      return loadState().then(function () { return result; });
    }, function (err) {
      toast(err.message, 'error');
      return null;
    }).then(function (result) {
      setBusy('');
      return result;
    });
  }

  function card(title, hint, kids) {
    return el('section', {cls: 'nt-card'}, [el('h2', {text: title}), hint ? el('p', {cls: 'nt-hint', text: hint}) : null].concat(kids || []));
  }

  function renderSettings(force) {
    if (!dom.settingsTop) return;
    var s = app.state || {};
    var sig = JSON.stringify([s.account_id, s.space_id, s.spaces, s.accounts, s.ui_mode, s.jobs, s.main_thread_id, app.busy]);
    if (!force && sig === dom.settingsSig) return;
    dom.settingsSig = sig;
    var top = dom.settingsTop;
    top.textContent = '';
    var busy = !!app.busy;

    var accounts = s.accounts || [];
    var accList = el('div', {cls: 'nt-list'});
    accounts.forEach(function (a) {
      var current = a.id === s.account_id;
      accList.appendChild(el('button', {type: 'button', cls: 'nt-item' + (current ? ' is-current' : ''), disabled: busy,
        title: current ? 'Current account' : 'Use this account',
        onclick: function () { if (!current) chooseAccount(a.id); }}, [
        el('span', {cls: 'nt-avatar', text: initials(a.name || a.email)}),
        el('span', {cls: 'nt-item-text'}, [
          el('b', {text: a.name || a.email || 'Account'}),
          el('small', {text: [a.email, a.space_name].filter(Boolean).join('  \u00b7  ') || a.error || ''})
        ]),
        el('span', {cls: 'nt-badge' + (current ? ' is-ok' : ''), text: current ? 'Current' : (a.state || 'Use')})
      ]));
    });
    if (!accounts.length) accList.appendChild(el('div', {cls: 'nt-empty', text: s.account_id ? (s.email || 'Signed in') : 'No account yet.'}));
    top.appendChild(card('Account', 'Choose which Notion account NotionTool uses.', [accList, el('div', {cls: 'nt-row'}, [
      el('button', {type: 'button', cls: 'nt-btn', disabled: busy, text: 'Sign in another account',
        onclick: function () { deskAction('account_sign_in', {}, 'Opening Notion sign-in...', 'Finish the sign-in in the window that opened.'); }}),
      s.account_id ? el('button', {type: 'button', cls: 'nt-btn nt-btn-quiet', disabled: busy, text: 'Sign out',
        onclick: function () {
          if (!window.confirm('Sign out of ' + (s.email || 'this account') + '?')) return;
          deskAction('account_sign_out', {account_id: s.account_id}, 'Signing out...', 'Signed out').then(function () { resetWorkspace(); });
        }}) : null
    ])]));

    var spaces = s.spaces || [];
    var spaceList = el('div', {cls: 'nt-list'});
    spaces.forEach(function (sp) {
      var current = sp.id === s.space_id;
      spaceList.appendChild(el('button', {type: 'button', cls: 'nt-item' + (current ? ' is-current' : ''), disabled: busy,
        title: current ? 'Reload this workspace (chat list, Main and data)' : 'Switch to this workspace',
        onclick: function () { chooseSpace(sp.id); }}, [
        el('span', {cls: 'nt-avatar nt-avatar-space', text: initials(sp.name)}),
        el('span', {cls: 'nt-item-text'}, [el('b', {text: sp.name}), el('small', {text: current ? 'Current workspace - click to reload' : 'Click to switch'})]),
        el('span', {cls: 'nt-badge' + (current ? ' is-ok' : ''), text: current ? 'Current' : 'Switch'})
      ]));
    });
    if (!spaces.length) spaceList.appendChild(el('div', {cls: 'nt-empty', text: s.account_id ? 'No workspaces found for this account.' : 'Sign in first.'}));
    top.appendChild(card('Workspace', 'Chat list, Main chat and all data come from the chosen workspace.', [spaceList]));

    var jobs = (s.jobs || []).map(function (j) {
      return el('div', {cls: 'nt-job nt-job-' + j.state}, [el('b', {text: j.title}), el('span', {text: j.state === 'failed' ? (j.error || 'failed') : j.state})]);
    });
    top.appendChild(card('Main chat', s.main_thread_id ? 'Main is set up for this workspace.' : 'No Main chat in this workspace yet.', [
      el('div', {cls: 'nt-row'}, [
        s.main_thread_id ? el('button', {type: 'button', cls: 'nt-btn', text: 'Open Main', onclick: function () { openChat(s.main_thread_id); }}) : null,
        el('button', {type: 'button', cls: 'nt-btn nt-btn-quiet', disabled: busy || !s.account_id, text: 'Set up Main and Coworkers',
          onclick: function () { deskAction('account_setup', {account_id: s.account_id}, 'Starting setup...', 'Setup started'); }})
      ])
    ].concat(jobs.length ? [el('div', {cls: 'nt-jobs'}, jobs)] : [])));

    var standalone = s.ui_mode === 'standalone';
    top.appendChild(card('App window', standalone ? 'NotionTool runs in its own window (no Notion page).' :
      'The old Notion window with the overlay is set. It applies after a restart.', [
      el('div', {cls: 'nt-row'}, [
        el('button', {type: 'button', cls: 'nt-btn nt-btn-quiet', disabled: busy,
          text: standalone ? 'Use the old overlay window (fallback)' : 'Use the standalone window',
          onclick: function () {
            deskAction('app_settings_set', {values: {app_ui_mode: standalone ? 'overlay' : 'standalone'}}, 'Saving...',
              'Saved. Restart NotionTool to apply.');
          }})
      ])
    ]));
  }

  function mountAllSettings() {
    if (dom.allMounted || !dom.settingsAll) return;
    var host = dom.settingsAll, view = views.settings, api = parts.settingsTab;
    var mount = view && typeof view.mount === 'function' ? view.mount.bind(view) :
      (api && typeof api.mount === 'function' ? api.mount.bind(api) : null);
    host.textContent = '';
    if (!mount) { host.appendChild(el('div', {cls: 'nt-empty', text: parts.loaded ? 'All settings are not available.' : 'Loading settings...'})); return; }
    try {
      mount(host);
      dom.allMounted = true;
    } catch (e) {
      host.textContent = '';
      host.appendChild(el('div', {cls: 'nt-empty', text: 'Settings could not be shown: ' + e.message}));
    }
  }

  // ---------- desktop parts ----------
  function loadScript(src) {
    return new Promise(function (resolve) {
      var tag = document.createElement('script');
      tag.src = src;
      tag.onload = function () { resolve(true); };
      tag.onerror = function () { resolve(false); };
      document.head.appendChild(tag);
    });
  }

  function loadParts() {
    return loadScript('../desktop/settings.js').then(function () {
      try {
        if (typeof window.__ntDesk.settingsTab === 'function') parts.settingsTab = window.__ntDesk.settingsTab(ctx);
      } catch (e) { console.error('NotionTool settings', e); }
      return loadScript('../desktop/projects.js');
    }).then(function () {
      try {
        if (typeof window.__ntDesk.projects === 'function') parts.projects = window.__ntDesk.projects(ctx);
      } catch (e) { console.error('NotionTool projects', e); }
      queued.forEach(register);
      queued = [];
      parts.loaded = true;
      renderTabs();
      if (app.tab === 'settings') mountAllSettings();
    });
  }

  // ---------- keys ----------
  function onKey(e) {
    if (!e.ctrlKey || e.altKey || e.shiftKey || e.metaKey) return;
    var k = String(e.key || '').toLowerCase();
    if (k === 'k') { e.preventDefault(); showTab('chat'); dom.search.focus(); dom.search.select(); }
    else if (k === 'n') { e.preventDefault(); newChat(); }
    else if (k === ',') { e.preventDefault(); showTab('settings'); }
    else if (/^[1-9]$/.test(k)) {
      var t = tabList()[Number(k) - 1];
      if (t) { e.preventDefault(); showTab(t.id); }
    }
  }

  // ---------- start ----------
  function boot() {
    build();
    renderTabs();
    showTab('chat');
    renderSpace();
    renderStatus();
    document.addEventListener('keydown', onKey);
    if (!token) {
      renderMessages('Open NotionTool from its own window: the access token is missing.');
      return;
    }
    renderChats();
    renderMessages('Loading...');
    loadState().then(function () { return loadChats(false); }).then(function () { pickChat(); });
    loadParts();
    setInterval(function () { if (!app.switching) loadState(); }, STATE_MS);
    setInterval(function () { if (!app.switching && !document.hidden) loadChats(false); }, CHATS_MS);
  }

  window.__ntApp = {state: function () { return app; }, openChat: openChat, newChat: newChat, chooseSpace: chooseSpace};
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();
