/* NotionTool desktop overlay part: settings (docs/DESKTOP_APP.md, overlay part contract).
 *
 * window.__ntDesk.settings(ctx) -> { stop, open }
 * - Data comes from ctx.call('settings_schema', {}) (notiontool/desktop_settings.py settings_schema()).
 *   Server keys are saved with ctx.call('settings_set', {values}); app-local keys (key starts with
 *   'app_' or entry.app_local) with ctx.call('app_settings_set', {values}).
 * - Layout: app-style settings window. Left navigation with the sections General, Mains & Coworkers,
 *   Desktop app and Advanced (SECTIONS / SECTION_OF_KEY; unknown keys go to Advanced, unknown app-local
 *   keys to Desktop app), right content area with cards of rows. Booleans are toggle switches, choices
 *   with up to SEGMENT_MAX options are segmented controls, help text is shown inline under each label.
 *   Unsaved rows and sections are marked; Save (or Ctrl+S inside the panel) shows a toast in the panel.
 * - Inside Notion's settings dialog (a [role=dialog] whose sidebar has 'Settings' / 'Einstellungen' /
 *   'My account' / 'Mein Konto') a 'NotionTool' entry is added at the top of the sidebar list. It shows
 *   our panel over the dialog's content area; any other sidebar entry hides it again.
 * - Notion settings sections listed in hidden_notion get the class nt-desk-hidden (exact visible
 *   label, English and German). Account, security, billing, Notion AI and connections are never hidden.
 * - open() and Ctrl+Shift+, show the same panel as a centered modal with a blurred backdrop when no
 *   settings dialog is open. Esc, the close button or a click on the backdrop close it.
 * Notion nodes are never removed. DOM is built with ctx.el and textContent only.
 */
(function () {
  'use strict';
  window.__ntDesk = window.__ntDesk || {};

  var DEBOUNCE_MS = 250;
  var TOAST_MS = 2600;
  var SEGMENT_MAX = 4;
  var MAX_LABEL = 60;
  var ROW = '[role="button"],[role="tab"],[role="menuitem"],[role="treeitem"],[role="link"],a,button';
  // Sidebar entries that identify Notion's settings dialog (lower case, English and German).
  var ANCHORS = ['settings', 'einstellungen', 'my account', 'mein konto', 'my settings', 'meine einstellungen',
    'preferences', 'präferenzen'];
  // Never hidden, even when listed in hidden_notion.
  var NEVER_HIDE = ['account', 'my account', 'mein konto', 'konto', 'security', 'sicherheit',
    'security & data', 'sicherheit & daten', 'billing', 'abrechnung', 'plans', 'pläne', 'upgrade',
    'notion ai', 'ai', 'ki', 'connections', 'verbindungen', 'my connections', 'meine verbindungen',
    'settings', 'einstellungen', 'preferences', 'präferenzen'];
  var NEVER_HIDE_IDS = /account|security|billing|plan|notion_ai|^ai$|connection/;
  var SECTIONS = [
    { id: 'general', label: 'General', icon: '◎',
      desc: 'How the Main chat keeps going and which chats the app shows.' },
    { id: 'team', label: 'Mains & Coworkers', icon: '⇄',
      desc: 'How many coworkers run at once and when they get a fresh chat.' },
    { id: 'app', label: 'Desktop app', icon: '▣',
      desc: 'Hide Notion areas you do not need. Saved in this desktop app only.' },
    { id: 'advanced', label: 'Advanced', icon: '⚙',
      desc: 'Token and turn limits. Change these only if you know why.' }
  ];
  var SECTION_OF_KEY = {
    main_auto_continue: 'general', turn_watch: 'general', main_context_policy: 'general', app_chat_filter: 'general',
    max_workers: 'team', per_account: 'team', max_runs_per_chat: 'team', coworker_fresh_context: 'team',
    coworker_delete_old_chat: 'team', coworker_max_age_hours: 'team',
    context_token_limit: 'advanced', main_context_ask_tokens: 'advanced', main_context_ask_turns: 'advanced'
  };
  var SECTION_OF_GROUP = { 'coworkers': 'team', 'main chat': 'general', 'app': 'app' };
  var CLS = {
    hidden: 'nt-desk-hidden',
    nav: 'nt-desk-set-nav',
    navActive: 'nt-desk-set-active',
    panel: 'nt-desk-set-panel',
    modal: 'nt-desk-set-modal'
  };

  function norm(text) {
    return String(text || '').replace(/[\u2018\u2019]/g, "'").replace(/\s+/g, ' ').trim().toLowerCase();
  }

  function message(err) { return (err && err.message) || String(err || 'Unknown error'); }

  function list(value) { return Array.isArray(value) ? value : []; }

  function isAppLocal(entry) { return !!(entry && (entry.app_local || String(entry.key).indexOf('app_') === 0)); }

  function kindOf(entry) {
    var t = norm(entry && entry.type);
    if (t === 'bool' || t === 'boolean' || t === 'checkbox') return 'checkbox';
    if (t === 'int' || t === 'integer' || t === 'number' || t === 'float') return 'number';
    if (t === 'choice' || t === 'select' || list(entry && entry.options).length) return 'select';
    return 'text';
  }

  function sectionOf(entry) {
    var key = String(entry && entry.key);
    if (Object.prototype.hasOwnProperty.call(SECTION_OF_KEY, key)) return SECTION_OF_KEY[key];
    if (isAppLocal(entry)) return 'app';
    var group = norm(entry && entry.group);
    if (Object.prototype.hasOwnProperty.call(SECTION_OF_GROUP, group)) return SECTION_OF_GROUP[group];
    return 'advanced';
  }

  // 'every_task' -> 'Every task'. Only the label changes; the saved value stays the raw option.
  function pretty(option) {
    var text = String(option === null || option === undefined ? '' : option).replace(/_/g, ' ');
    return text.charAt(0).toUpperCase() + text.slice(1);
  }

  function fmt(n) {
    try { return Number(n).toLocaleString('en-US'); } catch (err) { return String(n); }
  }

  window.__ntDesk.settings = function (ctx) {
    ctx = ctx || {};
    var stopped = false;
    var timer = 0;
    var observer = null;
    var unsubscribe = null;
    var schema = null;
    var loading = null;
    var saving = false;
    var hiddenRows = [];
    var dlg = null;        // { dialog, list, nav } of the open Notion settings dialog
    var panel = null;      // our panel (in the dialog or in the modal)
    var modal = null;      // standalone modal
    var inputs = {};       // key -> { entry, node, kind, section, row }
    var active = SECTIONS[0].id;
    var pages = {};        // section id -> page node
    var secButtons = {};   // section id -> nav button
    var bodyNode = null;
    var statusNode = null;
    var saveBtn = null;
    var toastNode = null;
    var toastTimer = 0;
    // Studio v3: the full Settings tab view (second part of this file). Ctrl+Shift+, and open() use it.
    var tab = null;
    try { if (typeof window.__ntDesk.settingsTab === 'function') tab = window.__ntDesk.settingsTab(ctx); } catch (err) { tab = null; }

    // ---- helpers ---------------------------------------------------------------------------
    function el(tag, props, kids) {
      if (typeof ctx.el === 'function') return ctx.el(tag, props, kids);
      var node = document.createElement(tag);
      Object.keys(props || {}).forEach(function (key) {
        var value = props[key];
        if (value === undefined || value === null || value === false) return;
        if (key === 'text') node.textContent = String(value);
        else if (key === 'cls') node.className = value;
        else node.setAttribute(key, value === true ? '' : String(value));
      });
      (kids || []).forEach(function (kid) {
        if (kid) node.appendChild(typeof kid === 'string' ? document.createTextNode(kid) : kid);
      });
      return node;
    }

    function toast(text, kind) {
      try { if (typeof ctx.toast === 'function') ctx.toast(text, kind || 'info'); } catch (err) { /* ignore */ }
    }

    // Save feedback: a toast inside our panel; the harness toast when the panel is not shown.
    function showToast(text, kind) {
      kind = kind || 'info';
      if (!toastNode || !toastNode.isConnected) { toast(text, kind); return; }
      toastNode.textContent = String(text);
      toastNode.className = 'nt-desk-set-toast nt-desk-set-toast-show nt-desk-set-toast-' + kind;
      if (toastTimer) clearTimeout(toastTimer);
      var node = toastNode;
      toastTimer = setTimeout(function () {
        toastTimer = 0;
        node.classList.remove('nt-desk-set-toast-show');
      }, TOAST_MS);
    }

    function call(action, args) {
      try {
        if (typeof ctx.call !== 'function') return Promise.reject(new Error('The harness is not connected'));
        return Promise.resolve(ctx.call(action, args || {}));
      } catch (err) {
        return Promise.reject(err);
      }
    }

    function safe(fn) {
      return function () {
        try { return fn.apply(null, arguments); } catch (err) { return undefined; }
      };
    }

    function rowLabel(row) {
      return norm(row.getAttribute('aria-label') || row.innerText || row.textContent);
    }

    function ours(node) {
      return !!(node && node.closest && node.closest('.' + CLS.nav + ',.' + CLS.panel + ',.' + CLS.modal));
    }

    // Rows inside root whose exact trimmed visible label is in labels (lower case).
    function rowsByLabel(root, labels) {
      var hits = [];
      if (!root || !labels.length) return hits;
      var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
      var node;
      while ((node = walker.nextNode())) {
        var text = node.nodeValue;
        if (!text || text.length > MAX_LABEL) continue;
        var label = norm(text);
        if (labels.indexOf(label) < 0 || !node.parentElement || ours(node.parentElement)) continue;
        var row = node.parentElement.closest(ROW) || node.parentElement;
        if (!root.contains(row) || rowLabel(row) !== label) continue;
        if (hits.indexOf(row) < 0) hits.push(row);
      }
      return hits;
    }

    // ---- schema ----------------------------------------------------------------------------
    function loadSchema() {
      if (loading) return loading;
      loading = call('settings_schema', {}).then(function (result) {
        schema = result && typeof result === 'object' ? result : { harness: [], hidden_notion: [] };
        loading = null;
        schedule();
        return schema;
      }, function (err) {
        loading = null;
        throw err;
      });
      return loading;
    }

    function hideLabels() {
      var labels = [];
      list(schema && schema.hidden_notion).forEach(function (section) {
        if (!section || NEVER_HIDE_IDS.test(norm(section.id))) return;
        [section.label, section.label_de].forEach(function (label) {
          var l = norm(label);
          if (l && NEVER_HIDE.indexOf(l) < 0 && labels.indexOf(l) < 0) labels.push(l);
        });
      });
      return labels;
    }

    // ---- Notion settings dialog --------------------------------------------------------------
    function findSettingsDialog() {
      var dialogs = document.querySelectorAll('[role="dialog"]');
      for (var i = 0; i < dialogs.length; i++) {
        var dialog = dialogs[i];
        if (ours(dialog)) continue;
        var anchors = rowsByLabel(dialog, ANCHORS);
        if (!anchors.length) continue;
        // Climb from the entry to the element that is one child of the sidebar list.
        var item = anchors[0];
        while (item.parentElement && item.parentElement !== dialog && item.parentElement.children.length === 1) {
          item = item.parentElement;
        }
        var sidebarList = item.parentElement;
        if (!sidebarList || sidebarList === dialog) continue;
        return { dialog: dialog, list: sidebarList };
      }
      return null;
    }

    function ensureNav(found) {
      var nav = found.list.querySelector(':scope > .' + CLS.nav);
      if (!nav) {
        nav = el('div', { cls: CLS.nav, role: 'button', tabindex: '0', 'data-nt-desk': 'settings-nav',
          title: 'NotionTool harness settings' }, [
          el('span', { cls: 'nt-desk-set-nav-icon', 'aria-hidden': 'true', text: 'N' }),
          el('span', { cls: 'nt-desk-set-nav-label', text: 'NotionTool' })
        ]);
        found.list.insertBefore(nav, found.list.firstChild);
      }
      found.nav = nav;
      return found;
    }

    function applyHidden(dialog) {
      var want = dialog ? rowsByLabel(dialog, hideLabels()) : [];
      hiddenRows.forEach(function (row) {
        if (want.indexOf(row) < 0) row.classList.remove(CLS.hidden);
      });
      want.forEach(function (row) {
        row.classList.add(CLS.hidden);
        row.setAttribute('data-nt-desk', 'settings-hidden');
      });
      hiddenRows = want;
    }

    function placeInDialog() {
      if (!panel || !dlg || !panel.parentNode) return;
      var parent = panel.offsetParent || dlg.dialog;
      var pr = parent.getBoundingClientRect();
      var dr = dlg.dialog.getBoundingClientRect();
      var lr = dlg.list.getBoundingClientRect();
      var left = Math.max(lr.right, dr.left);
      panel.style.position = 'absolute';
      panel.style.left = (left - pr.left + parent.scrollLeft) + 'px';
      panel.style.top = (dr.top - pr.top + parent.scrollTop) + 'px';
      panel.style.width = Math.max(240, dr.right - left) + 'px';
      panel.style.height = Math.max(200, dr.height) + 'px';
    }

    // ---- panel -----------------------------------------------------------------------------
    function buildPanel(inModal) {
      var node = el('div', { cls: CLS.panel + (inModal ? ' nt-desk-set-in-modal' : ''), role: 'region',
        'aria-label': 'NotionTool settings', 'data-nt-desk': 'settings-panel' });
      renderPanel(node);
      return node;
    }

    function syncSegment(group, value) {
      var buttons = group.querySelectorAll('.nt-desk-set-seg-btn');
      for (var i = 0; i < buttons.length; i++) {
        buttons[i].setAttribute('aria-checked', buttons[i].getAttribute('data-value') === value ? 'true' : 'false');
      }
    }

    function controlFor(entry, section) {
      var kind = kindOf(entry);
      var value = entry.value === undefined ? entry['default'] : entry.value;
      var text = value === null || value === undefined ? '' : String(value);
      var id = 'nt-desk-set-' + String(entry.key).replace(/[^\w-]/g, '_');
      var labelFor = id;
      var node, wrap;
      if (kind === 'checkbox') {
        node = el('input', { cls: 'nt-desk-set-check', type: 'checkbox', role: 'switch', id: id });
        node.checked = !!value;
        wrap = el('label', { cls: 'nt-desk-set-switch' }, [node,
          el('span', { cls: 'nt-desk-set-track', 'aria-hidden': 'true' }, [el('span', { cls: 'nt-desk-set-thumb' })])]);
      } else if (kind === 'number') {
        node = el('input', { cls: 'nt-desk-set-input nt-desk-set-number', type: 'number', id: id, step: '1',
          inputmode: 'numeric', min: typeof entry.min === 'number' ? entry.min : null,
          max: typeof entry.max === 'number' ? entry.max : null });
        node.value = text;
        wrap = node;
      } else if (kind === 'select') {
        var options = list(entry.options).slice();
        if (text !== '' && options.map(String).indexOf(text) < 0) options = [value].concat(options);
        if (options.length && options.length <= SEGMENT_MAX) {
          // Segmented control; the hidden input holds the value that collect() reads.
          node = el('input', { type: 'hidden', id: id });
          node.value = text;
          labelFor = null;
          wrap = el('div', { cls: 'nt-desk-set-seg', role: 'radiogroup', 'aria-label': entry.label || entry.key });
          options.forEach(function (option) {
            var btn = el('button', { cls: 'nt-desk-set-seg-btn', type: 'button', role: 'radio',
              'data-value': String(option), 'aria-checked': String(option) === text ? 'true' : 'false',
              text: pretty(option) });
            btn.addEventListener('click', function (ev) {
              ev.preventDefault();
              ev.stopPropagation();
              node.value = String(option);
              syncSegment(wrap, node.value);
              updateDirty();
            });
            wrap.appendChild(btn);
          });
          wrap.appendChild(node);
        } else {
          node = el('select', { cls: 'nt-desk-set-select', id: id });
          options.forEach(function (option) {
            node.appendChild(el('option', { value: String(option), text: pretty(option) }));
          });
          node.value = text;
          wrap = node;
        }
      } else {
        node = el('input', { cls: 'nt-desk-set-input', type: 'text', id: id, spellcheck: 'false' });
        node.value = text;
        wrap = node;
      }
      node.addEventListener('input', updateDirty);
      node.addEventListener('change', updateDirty);
      inputs[entry.key] = { entry: entry, node: node, kind: kind, section: section, row: null };
      return { wrap: wrap, labelFor: labelFor };
    }

    function rowFor(entry, section) {
      var kind = kindOf(entry);
      var control = controlFor(entry, section);
      var label = el('label', { cls: 'nt-desk-set-label', 'for': control.labelFor, text: entry.label || entry.key });
      if (isAppLocal(entry)) {
        label.appendChild(el('span', { cls: 'nt-desk-set-badge', title: 'Saved in this desktop app only',
          text: 'This device' }));
      }
      var text = el('div', { cls: 'nt-desk-set-text' }, [label]);
      if (entry.help) text.appendChild(el('div', { cls: 'nt-desk-set-help', text: String(entry.help) }));
      if (kind === 'number' && typeof entry.min === 'number' && typeof entry.max === 'number') {
        text.appendChild(el('div', { cls: 'nt-desk-set-range', text: 'Allowed: ' + fmt(entry.min) + ' to ' + fmt(entry.max) }));
      }
      var row = el('div', { cls: 'nt-desk-set-row nt-desk-set-row-' + kind, 'data-key': String(entry.key) },
        [text, el('div', { cls: 'nt-desk-set-control' }, [control.wrap])]);
      inputs[entry.key].row = row;
      return row;
    }

    function sectionIndex(id) {
      for (var i = 0; i < SECTIONS.length; i++) if (SECTIONS[i].id === id) return i;
      return 0;
    }

    function selectSection(id) {
      if (!pages[id]) id = SECTIONS[0].id;
      active = id;
      SECTIONS.forEach(function (sec) {
        var on = sec.id === id;
        if (pages[sec.id]) pages[sec.id].classList.toggle('nt-desk-set-page-active', on);
        if (secButtons[sec.id]) {
          secButtons[sec.id].setAttribute('aria-selected', on ? 'true' : 'false');
          secButtons[sec.id].tabIndex = on ? 0 : -1;
        }
      });
      if (bodyNode) bodyNode.scrollTop = 0;
    }

    function renderPanel(node) {
      if (!node) return;
      node.textContent = '';
      inputs = {};
      pages = {};
      secButtons = {};
      var side = el('div', { cls: 'nt-desk-set-side' });
      side.appendChild(el('div', { cls: 'nt-desk-set-brand' }, [
        el('span', { cls: 'nt-desk-set-logo', 'aria-hidden': 'true', text: 'N' }),
        el('div', { cls: 'nt-desk-set-brand-text' }, [
          el('div', { cls: 'nt-desk-set-title', text: 'NotionTool' }),
          el('div', { cls: 'nt-desk-set-sub', text: 'Settings' })
        ])
      ]));
      var tabs = el('div', { cls: 'nt-desk-set-tabs', role: 'tablist', 'aria-orientation': 'vertical',
        'aria-label': 'Settings sections' });
      tabs.addEventListener('keydown', safe(function (ev) {
        if (ev.key !== 'ArrowDown' && ev.key !== 'ArrowUp') return;
        ev.preventDefault();
        ev.stopPropagation();
        var step = ev.key === 'ArrowDown' ? 1 : SECTIONS.length - 1;
        var next = SECTIONS[(sectionIndex(active) + step) % SECTIONS.length].id;
        selectSection(next);
        if (secButtons[next]) secButtons[next].focus();
      }));
      side.appendChild(tabs);
      side.appendChild(el('div', { cls: 'nt-desk-set-side-foot', text: 'Ctrl+Shift+, opens and closes settings' }));
      var main = el('div', { cls: 'nt-desk-set-main' });
      bodyNode = el('div', { cls: 'nt-desk-set-body' });
      main.appendChild(bodyNode);
      node.appendChild(side);
      node.appendChild(main);

      var buckets = {};
      SECTIONS.forEach(function (sec) { buckets[sec.id] = []; });
      if (schema) {
        list(schema.harness).forEach(function (entry) {
          if (!entry || !entry.key) return;
          buckets[sectionOf(entry)].push(entry);
        });
      }
      SECTIONS.forEach(function (sec) {
        var btn = el('button', { cls: 'nt-desk-set-sec', type: 'button', role: 'tab', 'data-section': sec.id,
          'aria-selected': 'false' }, [
          el('span', { cls: 'nt-desk-set-sec-icon', 'aria-hidden': 'true', text: sec.icon }),
          el('span', { cls: 'nt-desk-set-sec-label', text: sec.label }),
          el('span', { cls: 'nt-desk-set-sec-dot', 'aria-hidden': 'true' })
        ]);
        btn.addEventListener('click', function (ev) { ev.preventDefault(); ev.stopPropagation(); selectSection(sec.id); });
        tabs.appendChild(btn);
        secButtons[sec.id] = btn;
        var card = el('div', { cls: 'nt-desk-set-card' });
        if (!schema) {
          card.appendChild(el('div', { cls: 'nt-desk-set-empty', text: loading ? 'Loading settings…' : 'Settings are not loaded yet.' }));
        } else if (!buckets[sec.id].length) {
          card.appendChild(el('div', { cls: 'nt-desk-set-empty', text: 'No settings in this section.' }));
        }
        buckets[sec.id].forEach(function (entry) { card.appendChild(rowFor(entry, sec.id)); });
        var page = el('section', { cls: 'nt-desk-set-page', role: 'tabpanel', 'data-section': sec.id,
          'aria-label': sec.label }, [
          el('div', { cls: 'nt-desk-set-page-head' }, [
            el('h2', { cls: 'nt-desk-set-page-title', text: sec.label }),
            el('p', { cls: 'nt-desk-set-page-desc', text: sec.desc })
          ]),
          card
        ]);
        bodyNode.appendChild(page);
        pages[sec.id] = page;
      });

      statusNode = el('div', { cls: 'nt-desk-set-status' });
      saveBtn = el('button', { cls: 'nt-desk-set-save', type: 'button', text: saving ? 'Saving…' : 'Save changes' });
      if (saving || !schema) saveBtn.disabled = true;
      saveBtn.addEventListener('click', function (ev) { ev.preventDefault(); ev.stopPropagation(); doSave(); });
      main.appendChild(el('div', { cls: 'nt-desk-set-foot' }, [
        statusNode,
        el('span', { cls: 'nt-desk-set-kbd', text: 'Ctrl+S' }),
        saveBtn
      ]));
      toastNode = el('div', { cls: 'nt-desk-set-toast', role: 'status', 'aria-live': 'polite' });
      node.appendChild(toastNode);
      selectSection(active);
      updateDirty();
    }

    function refreshPanel() {
      if (panel) renderPanel(panel);
      if (dlg) placeInDialog();
    }

    function collect() {
      var server = {}, app = {}, errors = [], changed = 0, dirty = [], bad = [];
      Object.keys(inputs).forEach(function (key) {
        var item = inputs[key], entry = item.entry, value;
        if (item.kind === 'checkbox') value = !!item.node.checked;
        else if (item.kind === 'number') {
          value = Number(item.node.value);
          if (item.node.value === '' || !isFinite(value) || Math.floor(value) !== value) {
            errors.push((entry.label || key) + ' must be a whole number'); bad.push(key); return;
          }
          if ((typeof entry.min === 'number' && value < entry.min) || (typeof entry.max === 'number' && value > entry.max)) {
            errors.push((entry.label || key) + ' must be between ' + entry.min + ' and ' + entry.max); bad.push(key); return;
          }
        } else value = item.node.value;
        if (value === entry.value) return;
        changed++;
        dirty.push(key);
        if (isAppLocal(entry)) app[key] = value; else server[key] = value;
      });
      return { server: server, app: app, errors: errors, changed: changed, dirty: dirty, bad: bad };
    }

    // Marks unsaved and invalid rows and sections and updates the footer status.
    function updateDirty() {
      if (!panel && !statusNode) return;
      var data = collect();
      var flagged = {};
      Object.keys(inputs).forEach(function (key) {
        var item = inputs[key];
        var isDirty = data.dirty.indexOf(key) >= 0, isBad = data.bad.indexOf(key) >= 0;
        if (isDirty || isBad) flagged[item.section] = true;
        if (!item.row) return;
        item.row.classList.toggle('nt-desk-set-row-dirty', isDirty);
        item.row.classList.toggle('nt-desk-set-row-invalid', isBad);
      });
      SECTIONS.forEach(function (sec) {
        if (secButtons[sec.id]) secButtons[sec.id].classList.toggle('nt-desk-set-sec-dirty', !!flagged[sec.id]);
      });
      if (statusNode) {
        var text = 'All changes saved', kind = '';
        if (!schema) text = loading ? 'Loading…' : 'Not connected';
        else if (data.errors.length) { text = data.errors[0]; kind = ' nt-desk-set-status-error'; }
        else if (data.changed) {
          text = data.changed + (data.changed === 1 ? ' unsaved change' : ' unsaved changes');
          kind = ' nt-desk-set-status-dirty';
        }
        statusNode.textContent = text;
        statusNode.className = 'nt-desk-set-status' + kind;
      }
      if (saveBtn) saveBtn.classList.toggle('nt-desk-set-save-ready', data.changed > 0 && !data.errors.length);
    }

    function setSaving(on) {
      saving = on;
      if (saveBtn) {
        saveBtn.disabled = on;
        saveBtn.textContent = on ? 'Saving…' : 'Save changes';
      }
    }

    function doSave() {
      if (saving) return;
      var data = collect();
      if (data.errors.length) { updateDirty(); showToast(data.errors[0], 'error'); return; }
      if (!data.changed) { showToast('No changes to save', 'info'); return; }
      setSaving(true);
      var chain = Promise.resolve();
      if (Object.keys(data.server).length) chain = chain.then(function () { return call('settings_set', { values: data.server }); });
      if (Object.keys(data.app).length) chain = chain.then(function () { return call('app_settings_set', { values: data.app }); });
      chain.then(function () { return loadSchema(); }).then(function () {
        saving = false;
        refreshPanel();
        showToast('Settings saved', 'success');
      }, function (err) {
        // Keep the user's edits on screen so they can retry.
        setSaving(false);
        updateDirty();
        showToast(message(err), 'error');
      });
    }

    function showInDialog() {
      if (!dlg) return;
      closeModal();
      if (!panel || panel.parentNode !== dlg.dialog) {
        if (panel && panel.parentNode) panel.remove();
        panel = buildPanel(false);
        dlg.dialog.appendChild(panel);
      }
      dlg.nav.classList.add(CLS.navActive);
      placeInDialog();
      if (!schema) loadSchema().then(refreshPanel, function (err) { refreshPanel(); showToast(message(err), 'error'); });
    }

    function hideDialogPanel() {
      if (dlg && dlg.nav) dlg.nav.classList.remove(CLS.navActive);
      if (panel && !modal) { if (panel.parentNode) panel.remove(); panel = null; }
    }

    function openModal() {
      if (modal) return;
      hideDialogPanel();
      panel = buildPanel(true);
      var close = el('button', { cls: 'nt-desk-set-close', type: 'button', 'aria-label': 'Close settings',
        title: 'Close (Esc)', text: '×' });
      close.addEventListener('click', function (ev) { ev.preventDefault(); closeModal(); });
      var box = el('div', { cls: 'nt-desk-set-box', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'NotionTool settings' },
        [close, panel]);
      modal = el('div', { cls: CLS.modal, 'data-nt-desk': 'settings-modal' }, [box]);
      modal.addEventListener('click', function (ev) { if (ev.target === modal) closeModal(); });
      (document.body || document.documentElement).appendChild(modal);
      try { if (secButtons[active]) secButtons[active].focus(); } catch (err) { /* ignore */ }
      if (!schema) loadSchema().then(refreshPanel, function (err) { refreshPanel(); showToast(message(err), 'error'); });
    }

    function closeModal() {
      if (!modal) return;
      if (modal.parentNode) modal.remove();
      modal = null;
      panel = null;
      statusNode = null;
      saveBtn = null;
      toastNode = null;
    }

    function open() {
      if (stopped) return false;
      scan();
      if (dlg) showInDialog(); else if (!(tab && tab.open())) openModal();
      return true;
    }

    // ---- events ----------------------------------------------------------------------------
    var onClick = safe(function (ev) {
      if (stopped || !dlg || !ev.target || !ev.target.closest) return;
      if (ev.target.closest('.' + CLS.nav)) {
        ev.preventDefault();
        ev.stopPropagation();
        showInDialog();
        return;
      }
      if (ours(ev.target)) return;
      if (dlg.list.contains(ev.target)) hideDialogPanel();
    });

    var onKey = safe(function (ev) {
      if (stopped) return;
      if (ev.key === 'Escape' && modal) { ev.preventDefault(); ev.stopPropagation(); closeModal(); return; }
      if (panel && ev.ctrlKey && !ev.shiftKey && !ev.altKey && (ev.key === 's' || ev.key === 'S') &&
          ev.target && panel.contains(ev.target)) {
        ev.preventDefault();
        ev.stopPropagation();
        doSave();
        return;
      }
      if (dlg && dlg.nav && ev.target === dlg.nav && (ev.key === 'Enter' || ev.key === ' ')) {
        ev.preventDefault();
        showInDialog();
        return;
      }
      var comma = ev.code === 'Comma' || ev.key === ',' || ev.key === ';' || ev.key === '<';
      if (ev.ctrlKey && ev.shiftKey && !ev.altKey && comma) {
        ev.preventDefault();
        ev.stopPropagation();
        if (!modal && tab && tab.shortcut()) return;
        if (modal) closeModal(); else open();
      }
    });

    var onResize = safe(function () { if (dlg) placeInDialog(); });

    // ---- scan ------------------------------------------------------------------------------
    function scan() {
      timer = 0;
      if (stopped || !document.body) return;
      var found = findSettingsDialog();
      if (dlg && (!found || found.dialog !== dlg.dialog)) {
        if (panel && !modal) { if (panel.parentNode) panel.remove(); panel = null; }
        dlg = null;
      }
      if (!found) { applyHidden(null); return; }
      dlg = ensureNav(found);
      if (!schema && !loading) loadSchema().then(null, function () { /* retried when the panel opens */ });
      applyHidden(dlg.dialog);
      if (panel && panel.parentNode === dlg.dialog) placeInDialog();
    }
    scan = safe(scan);

    function schedule() {
      if (stopped || timer) return;
      timer = setTimeout(scan, DEBOUNCE_MS);
    }

    try {
      document.addEventListener('click', onClick, true);
      document.addEventListener('keydown', onKey, true);
      window.addEventListener('resize', onResize);
      if (typeof MutationObserver === 'function' && document.documentElement) {
        // childList only: our own class and style changes do not trigger it.
        observer = new MutationObserver(schedule);
        observer.observe(document.documentElement, { childList: true, subtree: true });
      }
      if (typeof ctx.onState === 'function') unsubscribe = ctx.onState(schedule);
    } catch (err) { /* never throw */ }
    scan();

    return {
      open: open,
      stop: safe(function () {
        stopped = true;
        if (timer) clearTimeout(timer);
        timer = 0;
        if (toastTimer) clearTimeout(toastTimer);
        toastTimer = 0;
        if (observer) observer.disconnect();
        observer = null;
        if (typeof unsubscribe === 'function') unsubscribe();
        unsubscribe = null;
        document.removeEventListener('click', onClick, true);
        document.removeEventListener('keydown', onKey, true);
        window.removeEventListener('resize', onResize);
        closeModal();
        if (tab) tab.stop();
        tab = null;
        if (panel && panel.parentNode) panel.remove();
        panel = null;
        if (dlg && dlg.nav && dlg.nav.parentNode) dlg.nav.remove();
        dlg = null;
        hiddenRows.forEach(function (row) { row.classList.remove(CLS.hidden); row.removeAttribute('data-nt-desk'); });
        hiddenRows = [];
      })
    };
  };
})();

/* Settings tab view (Studio v3, docs/DESKTOP_APP.md).
 *
 * window.__ntDesk.settingsTab(ctx) -> { open, shortcut, mount, stop }; used by window.__ntDesk.settings.
 * - Registers a 'Settings' tab with window.__ntShell (registerTab / register / addTab, lazy: retried on
 *   the 'nt-desk-shell-ready' event and every second while shell.js is not there yet). Without a shell the
 *   same view opens as a full-window page; Ctrl+Shift+, opens or closes it.
 * - Sections: Team, Models & effort, App look, Notifications. Each change is saved on its own:
 *   validate -> 'Saving...' -> ctx.call('settings_set' | 'app_settings_set', {values}) -> re-read
 *   ctx.call('settings_schema', {}) -> 'Applied' only when the stored value matches, else the error.
 * - Coworkers writes per_account (the team size key of notiontool/team_size.py: N = min(per_account,
 *   max_workers)); when max_workers is lower it is raised to the same number in the same call.
 * - Pure logic is exposed as window.__ntDesk.settingsLogic for tests (no DOM needed).
 */
(function () {
  'use strict';
  window.__ntDesk = window.__ntDesk || {};

  var TEAM_MAX = 40;
  var DEFAULT_COWORKERS = 10;
  var POLL_MS = 1000;
  var POLL_MAX = 300;
  var TAB_ID = 'settings';
  var TAB_SECTIONS = [
    { id: 'team', label: 'Team', help: 'How many helper chats work for you and when they get a fresh chat.',
      keys: ['per_account', 'coworker_max_age_hours', 'max_workers'] },
    { id: 'models', label: 'Models & effort', help: 'How hard the AI thinks. Automatic picks a level for each kind of task.',
      keys: ['app_effort_override'] },
    { id: 'look', label: 'App look', help: 'How the app looks on this computer.',
      keys: ['app_theme_intensity', 'app_compact', 'app_declutter'] },
    { id: 'notify', label: 'Notifications', help: 'When the app tells you that something happened.',
      keys: ['app_notify_desktop', 'app_notify_done', 'app_notify_needs_you', 'app_notify_sound'] }
  ];
  var TAB_TEXT = {
    per_account: ['Coworkers', 'Helper chats per Notion account. 1 to 40.'],
    coworker_max_age_hours: ['Replace chats after (hours)', 'A helper chat older than this gets a fresh chat. The old one is kept.'],
    max_workers: ['Max at once (all accounts)', 'Most helper chats working at the same time, over all accounts.'],
    app_effort_override: ['Effort', 'Automatic is best for most people. Pick a level to use it for every task.'],
    app_theme_intensity: ['Color strength', 'How strong the colors and glow are.'],
    app_compact: ['Compact mode', 'Smaller spacing so more fits on screen.'],
    app_declutter: ['Hide Notion extras', 'Hide tips, upgrade offers and other Notion parts the app does not use.'],
    app_notify_desktop: ['Desktop pop-ups', 'Show a pop-up from Windows.'],
    app_notify_done: ['When a task is done', 'Tell me when a task is finished.'],
    app_notify_needs_you: ['When the app needs you', 'Tell me when a chat waits for my answer.'],
    app_notify_sound: ['Play a sound', 'Play a short sound with each notice.']
  };
  var OPTION_TEXT = { auto: 'Automatic', low: 'Low', medium: 'Medium', high: 'High', xhigh: 'Very high',
    max: 'Maximum', soft: 'Soft', normal: 'Normal', strong: 'Strong' };
  var EFFORT_RULE = 'Automatic: quick jobs (routine, docs, tests) use light effort, code and research use high, ' +
    'reviews and the Main chat use the highest level the model supports.';

  // ---- pure logic ------------------------------------------------------------------------
  function entriesOf(schema) {
    var out = {};
    ((schema && schema.harness) || []).forEach(function (e) { if (e && e.key) out[e.key] = e; });
    return out;
  }
  function isApp(entry) { return !!(entry && (entry.app_local || String(entry.key).indexOf('app_') === 0)); }
  function errText(err) {
    if (!err) return 'Unknown error';
    if (typeof err === 'string') return err;
    return String(err.message || err.error || err.detail || err);
  }
  function optionsOf(entry, schema) {
    var opts = (entry && entry.options) || [];
    var cat = schema && schema.model_efforts;
    if (entry && entry.key === 'app_effort_override' && cat && cat.length) {
      opts = opts.filter(function (o) { return o === 'auto' || cat.indexOf(o) >= 0; });
    }
    return opts;
  }
  function validate(entry, raw, schema) {
    if (!entry) return { ok: false, error: 'This setting is not available.' };
    if (entry.type === 'int') {
      var text = String(raw == null ? '' : raw).trim();
      if (!/^-?[0-9]+$/.test(text)) return { ok: false, error: 'Enter a whole number.' };
      var n = parseInt(text, 10);
      if ((entry.min != null && n < entry.min) || (entry.max != null && n > entry.max)) {
        return { ok: false, error: 'Use a number from ' + entry.min + ' to ' + entry.max + '.' };
      }
      return { ok: true, value: n };
    }
    if (entry.type === 'bool') {
      return raw === true || raw === false ? { ok: true, value: raw } : { ok: false, error: 'Choose on or off.' };
    }
    if (entry.type === 'choice') {
      return optionsOf(entry, schema).indexOf(raw) >= 0 ? { ok: true, value: raw }
        : { ok: false, error: 'Pick one of the listed options.' };
    }
    return { ok: false, error: 'This setting cannot be changed here.' };
  }
  function planWrite(entry, value, schema) {
    var values = {};
    values[entry.key] = value;
    if (entry.key === 'per_account') {
      var cap = entriesOf(schema).max_workers;
      var capValue = cap ? Number(cap.value) : TEAM_MAX;
      if (!(capValue >= value)) values.max_workers = value;
    }
    return { action: isApp(entry) ? 'app_settings_set' : 'settings_set', values: values };
  }
  function teamSize(map) {
    var per = Number(map.per_account ? map.per_account.value : DEFAULT_COWORKERS);
    var cap = Number(map.max_workers ? map.max_workers.value : TEAM_MAX);
    return Math.max(1, Math.min(TEAM_MAX, per, cap));
  }
  function same(stored, wanted) { return typeof wanted === 'number' ? Number(stored) === wanted : stored === wanted; }
  function shown(value) {
    if (value === true) return 'On';
    if (value === false) return 'Off';
    if (value == null) return 'nothing';
    return OPTION_TEXT[value] || String(value);
  }
  function verify(plan, schema) {
    var map = entriesOf(schema);
    var keys = Object.keys(plan.values);
    for (var i = 0; i < keys.length; i++) {
      var stored = map[keys[i]] ? map[keys[i]].value : undefined;
      if (!same(stored, plan.values[keys[i]])) return { applied: false, key: keys[i], stored: stored };
    }
    if ('per_account' in plan.values && teamSize(map) !== plan.values.per_account) {
      return { applied: false, key: 'team size', stored: teamSize(map) };
    }
    return { applied: true };
  }
  function failed(result) { return !!(result && typeof result === 'object' && result.ok === false); }
  // call(action, args) -> Promise; report(kind, text) with kind 'saving' | 'applied' | 'error'.
  function saveField(call, schema, key, raw, report) {
    report = typeof report === 'function' ? report : function () {};
    var entry = entriesOf(schema)[key];
    var check = validate(entry, raw, schema);
    if (!check.ok) { report('error', check.error); return Promise.resolve({ ok: false, error: check.error, schema: schema }); }
    var plan = planWrite(entry, check.value, schema);
    report('saving', 'Saving...');
    return Promise.resolve().then(function () { return call(plan.action, { values: plan.values }); })
      .then(function (res) { if (failed(res)) throw res; return call('settings_schema', {}); })
      .then(function (fresh) {
        if (failed(fresh) || !fresh || !fresh.harness) throw (fresh || new Error('Could not read the settings again.'));
        var res = verify(plan, fresh);
        if (res.applied) { report('applied', 'Applied'); return { ok: true, plan: plan, schema: fresh }; }
        var msg = 'Not applied: the app kept ' + shown(res.stored) + '.';
        report('error', msg);
        return { ok: false, error: msg, plan: plan, schema: fresh };
      }, function (err) {
        var msg = errText(err);
        report('error', msg);
        return { ok: false, error: msg, plan: plan, schema: schema };
      });
  }

  window.__ntDesk.settingsLogic = { sections: TAB_SECTIONS, text: TAB_TEXT, entriesOf: entriesOf, validate: validate,
    planWrite: planWrite, verify: verify, teamSize: teamSize, saveField: saveField, optionsOf: optionsOf };

  // Backups and Details cards (notiontool/backup.py: backup_status, backup_now, usage_report).
  function backupText(res) {
    var last = res && res.last;
    if (!last) return 'Last backup: none yet';
    var when = last.time_text || res.last_text || new Date(Number(last.time) * 1000).toLocaleString();
    return 'Last backup: ' + when;
  }
  function duration(sec) {
    var s = Math.round(Number(sec) || 0);
    if (s < 60) return s + ' s';
    return Math.floor(s / 60) + ' min ' + (s % 60) + ' s';
  }
  function usageLines(report) {
    report = report || {};
    var lines = [];
    var req = report.requests || {};
    var limit = Number(req.limit_per_minute) || 0;
    lines.push('Requests in the last minute: ' + (Number(req.last_minute) || 0) +
      (limit ? ' of ' + limit + ' allowed' : ' (no limit set)'));
    lines.push(report.average_task_seconds == null ? 'Average task time: no finished tasks yet'
      : 'Average task time: ' + duration(report.average_task_seconds));
    var turns = report.turns || [];
    if (!turns.length) lines.push('Turns per model: none in the last ' + (report.days || 7) + ' days');
    turns.slice(0, 14).forEach(function (t) { lines.push('Turns on ' + t.day + ' with ' + t.model + ': ' + t.turns); });
    var team = report.coworkers || [];
    if (!team.length) lines.push('Tasks per coworker: none yet');
    team.slice(0, 20).forEach(function (c) { lines.push('Tasks by ' + c.name + ': ' + c.tasks + ' (' + c.done + ' done)'); });
    return lines;
  }
  window.__ntDesk.settingsLogic.backupText = backupText;
  // Work in parallel copies (patch_mode on the server; engine.patch_status).
  function patchText(st) {
    if (!st || typeof st !== 'object') return 'Status: not available yet';
    return 'Merges today: ' + (Number(st.merges_today) || 0) + ' - Conflicts waiting: ' + (Number(st.conflicts_waiting) || 0) +
      ' - Waiting to merge: ' + (Number(st.waiting_to_merge) || 0);
  }
  window.__ntDesk.settingsLogic.patchText = patchText;
  window.__ntDesk.settingsLogic.usageLines = usageLines;

  // Restart card (notiontool/restart_guard.py: app_restart, app_restart_status, app_health).
  var RESTART_KEY = 'nt-desk-restart-pending';
  function restartConfirmText(running) {
    running = running || [];
    if (!running.length) return 'Restart NotionTool now? No task is running.';
    var lines = ['Restart NotionTool now? These tasks are still running:'];
    running.slice(0, 15).forEach(function (t) { lines.push('- ' + (t.title || t.id) + ' (' + t.state + ')'); });
    if (running.length > 15) lines.push('- and ' + (running.length - 15) + ' more');
    lines.push('Their answers may need manual checking.');
    return lines.join('\n');
  }
  function restartText(res) {
    res = res || {};
    if (res.ok === false) return res.error || 'The restart could not be started.';
    if (res.state === 'waiting') {
      var n = (res.running || []).length;
      return 'Waiting for ' + n + (n === 1 ? ' task' : ' tasks') + ' to finish. New tasks wait. Then NotionTool restarts.';
    }
    if (res.state === 'restarting') return 'Restarting...';
    if (res.state === 'failed') return res.error || 'The restart failed.';
    return '';
  }
  // Polls until the restarted harness answers. onState('waiting'|'restarting'|'offline'|'online'|'failed'|'timeout').
  // opts.resumed: a restart was started before this overlay was injected again.
  function watchRestart(callFn, opts) {
    opts = opts || {};
    var wait = opts.wait || function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
    var interval = opts.interval || 2000, tries = opts.tries || 300;
    var onState = opts.onState || function () {};
    var seen = !!opts.resumed, down = false;
    function again(n) { return wait(interval).then(function () { return step(n + 1); }); }
    function step(n) {
      if (n >= tries) { onState('timeout'); return Promise.resolve({ ok: false, state: 'timeout' }); }
      return Promise.resolve().then(function () { return callFn('app_restart_status', {}); }).then(function (res) {
        if (!res || typeof res.state !== 'string') throw new Error('No answer');
        if (res.state === 'waiting' || res.state === 'restarting') { seen = true; onState(res.state, res); return again(n); }
        if (res.state === 'failed' && !down) { onState('failed', res); return { ok: false, state: 'failed', error: res.error }; }
        if (!seen && !down) return { ok: true, state: 'idle' };
        return Promise.resolve().then(function () { return callFn('app_health', {}); }).then(function (h) {
          if (!h || h.ok === false) throw new Error('Not healthy yet');
          onState('online', h);
          return { ok: true, state: 'online' };
        });
      }).then(null, function () {
        down = true;
        onState('offline');
        return again(n);
      });
    }
    return step(0);
  }
  window.__ntDesk.settingsLogic.restartConfirmText = restartConfirmText;
  window.__ntDesk.settingsLogic.restartText = restartText;
  window.__ntDesk.settingsLogic.watchRestart = watchRestart;

  // ---- view ------------------------------------------------------------------------------
  window.__ntDesk.settingsTab = function (ctx) {
    ctx = ctx || {};
    var call = function (action, args) {
      if (typeof ctx.call !== 'function') return Promise.reject(new Error('The app is not connected.'));
      return ctx.call(action, args);
    };
    var stopped = false;
    var schema = null;
    var loading = null;
    var views = [];
    var host = null;
    var registered = false;
    var pollTimer = 0;
    var polls = 0;

    function node(tag, cls, text) {
      var n = document.createElement(tag);
      if (cls) n.className = cls;
      if (text != null) n.textContent = text;
      n.setAttribute('data-nt-desk', 'settings-tab');
      return n;
    }

    function applyLook() {
      var root = document.documentElement;
      if (!root || !schema) return;
      var map = entriesOf(schema);
      root.setAttribute('data-nt-look', String(map.app_theme_intensity ? map.app_theme_intensity.value : 'normal'));
      root.classList.toggle('nt-desk-compact', !!(map.app_compact && map.app_compact.value === true));
    }

    function load() {
      if (loading) return loading;
      loading = Promise.resolve().then(function () { return call('settings_schema', {}); }).then(function (result) {
        loading = null;
        if (!result || !result.harness) throw (result || new Error('No settings were returned.'));
        schema = result;
        applyLook();
        views.forEach(fill);
        return schema;
      }, function (err) {
        loading = null;
        views.forEach(function (v) { banner(v, 'Could not load settings: ' + errText(err)); });
        throw err;
      });
      return loading;
    }

    function banner(view, text) {
      view.banner.textContent = text || '';
      view.banner.hidden = !text;
    }

    function setStatus(status, kind, text) {
      status.textContent = text || '';
      status.className = 'nt-desk-stab-status' + (kind ? ' nt-desk-stab-' + kind : '');
    }

    function syncControl(row) {
      var entry = entriesOf(schema)[row.key];
      if (!entry) return;
      if (entry.type === 'bool') {
        row.control.setAttribute('aria-checked', entry.value === true ? 'true' : 'false');
        row.control.textContent = entry.value === true ? 'On' : 'Off';
      } else {
        row.control.value = String(entry.value);
      }
    }

    function commit(view, row, raw) {
      row.control.disabled = true;
      saveField(call, schema, row.key, raw, function (kind, text) { setStatus(row.status, kind, text); })
        .then(function (res) {
          row.control.disabled = false;
          if (res.schema && res.schema !== schema) {
            schema = res.schema;
            applyLook();
            views.forEach(function (v) { Object.keys(v.rows).forEach(function (k) { if (v.rows[k] !== row || res.ok) syncControl(v.rows[k]); }); });
          }
        });
    }

    function control(view, entry, row) {
      var c;
      if (entry.type === 'bool') {
        c = node('button', 'nt-desk-stab-switch');
        c.type = 'button';
        c.setAttribute('role', 'switch');
        c.addEventListener('click', function (ev) {
          ev.preventDefault();
          commit(view, row, c.getAttribute('aria-checked') !== 'true');
        });
      } else if (entry.type === 'choice') {
        c = node('select', 'nt-desk-stab-select');
        optionsOf(entry, schema).forEach(function (o) {
          var opt = document.createElement('option');
          opt.value = o;
          opt.textContent = OPTION_TEXT[o] || o;
          c.appendChild(opt);
        });
        c.addEventListener('change', function () { commit(view, row, c.value); });
      } else {
        c = node('input', 'nt-desk-stab-number');
        c.type = 'number';
        if (entry.min != null) c.min = String(entry.min);
        if (entry.max != null) c.max = String(entry.max);
        c.step = '1';
        c.addEventListener('change', function () { commit(view, row, c.value); });
      }
      return c;
    }

    function rowFor(view, entry) {
      var text = TAB_TEXT[entry.key] || [entry.label || entry.key, entry.help || ''];
      var row = { key: entry.key };
      var wrap = node('div', 'nt-desk-stab-row');
      var info = node('div', 'nt-desk-stab-info');
      var label = node('label', 'nt-desk-stab-label', text[0]);
      info.appendChild(label);
      info.appendChild(node('div', 'nt-desk-stab-help', text[1]));
      row.status = node('div', 'nt-desk-stab-status');
      row.status.setAttribute('role', 'status');
      row.status.setAttribute('aria-live', 'polite');
      info.appendChild(row.status);
      row.control = control(view, entry, row);
      row.control.setAttribute('aria-label', text[0]);
      wrap.appendChild(info);
      wrap.appendChild(row.control);
      view.rows[entry.key] = row;
      syncControl(row);
      return wrap;
    }

    function fill(view) {
      view.nav.textContent = '';
      view.body.textContent = '';
      view.rows = {};
      banner(view, '');
      if (!schema) { view.body.appendChild(node('p', 'nt-desk-stab-empty', 'Loading settings...')); return; }
      var map = entriesOf(schema);
      TAB_SECTIONS.forEach(function (sec) {
        var card = node('section', 'nt-desk-stab-card');
        card.setAttribute('data-nt-section', sec.id);
        card.appendChild(node('h3', 'nt-desk-stab-h', sec.label));
        card.appendChild(node('p', 'nt-desk-stab-sub', sec.help));
        if (sec.id === 'models') card.appendChild(node('p', 'nt-desk-stab-rule', EFFORT_RULE));
        var count = 0;
        sec.keys.forEach(function (k) { if (map[k]) { card.appendChild(rowFor(view, map[k])); count += 1; } });
        if (!count) card.appendChild(node('p', 'nt-desk-stab-empty', 'Not available yet.'));
        view.body.appendChild(card);
        var btn = node('button', 'nt-desk-stab-navbtn', sec.label);
        btn.type = 'button';
        btn.addEventListener('click', function (ev) {
          ev.preventDefault();
          try { card.scrollIntoView({ block: 'start', behavior: 'smooth' }); } catch (err) { /* ignore */ }
        });
        view.nav.appendChild(btn);
      });
      extraCards(view);
    }

    function card(view, id, title, help) {
      var c = node('section', 'nt-desk-stab-card');
      c.setAttribute('data-nt-section', id);
      c.appendChild(node('h3', 'nt-desk-stab-h', title));
      c.appendChild(node('p', 'nt-desk-stab-sub', help));
      view.body.appendChild(c);
      var btn = node('button', 'nt-desk-stab-navbtn', title);
      btn.type = 'button';
      btn.addEventListener('click', function (ev) {
        ev.preventDefault();
        try { c.scrollIntoView({ block: 'start', behavior: 'smooth' }); } catch (err) { /* ignore */ }
      });
      view.nav.appendChild(btn);
      return c;
    }

    function patchCard(view) {
      var p = card(view, 'parallel', 'Parallel copies',
        'Each coworker edits its own copy of the project. NotionTool tests each change and adds it to the project by itself. ' +
        'Only a real clash between two changes goes to one coworker to fix. When off, coworkers share one folder and files are locked instead.');
      var label = node('label', 'nt-desk-stab-help');
      var box = document.createElement('input');
      box.type = 'checkbox';
      box.checked = true;
      label.appendChild(box);
      label.appendChild(document.createTextNode(' Work in parallel copies (recommended)'));
      var line = node('p', 'nt-desk-stab-help', 'Status: loading...');
      var status = node('div', 'nt-desk-stab-status');
      status.setAttribute('role', 'status');
      box.addEventListener('change', function () {
        var want = !!box.checked;
        setStatus(status, 'saving', 'Saving...');
        Promise.resolve().then(function () { return call('settings_set', { values: { patch_mode: want } }); }).then(function (res) {
          if (failed(res)) throw res;
          setStatus(status, 'applied', want ? 'On. New tasks use their own copy.' : 'Off. Files are locked again.');
        }, function (err) {
          box.checked = !want;
          setStatus(status, 'error', 'Not saved: ' + errText(err));
        });
      });
      p.appendChild(label);
      p.appendChild(line);
      p.appendChild(status);
      Promise.resolve().then(function () { return call('settings_get', {}); }).then(function (res) {
        var v = res && (res.values || res.settings || res);
        if (v && Object.prototype.hasOwnProperty.call(v, 'patch_mode')) box.checked = String(v.patch_mode).toLowerCase() !== 'false' && v.patch_mode !== false;
      }, function () { /* keep the default: on */ });
      Promise.resolve().then(function () { return call('patch_status', {}); }).then(function (res) {
        if (failed(res)) throw res;
        line.textContent = patchText(res);
      }, function () { line.textContent = patchText(null); });
    }

    function extraCards(view) {
      patchCard(view);
      var b = card(view, 'backups', 'Backups', 'A copy of the app data is made before every restart and once a day. The last 10 are kept.');
      var last = node('p', 'nt-desk-stab-help', 'Last backup: loading...');
      var status = node('div', 'nt-desk-stab-status');
      status.setAttribute('role', 'status');
      var now = node('button', 'nt-desk-stab-navbtn', 'Back up now');
      now.type = 'button';
      now.addEventListener('click', function (ev) {
        ev.preventDefault();
        now.disabled = true;
        setStatus(status, 'saving', 'Backing up...');
        Promise.resolve().then(function () { return call('backup_now', {}); }).then(function (res) {
          if (failed(res)) throw res;
          now.disabled = false;
          setStatus(status, 'applied', 'Backup made.');
          last.textContent = backupText({ last: res });
        }, function (err) {
          now.disabled = false;
          setStatus(status, 'error', 'Backup failed: ' + errText(err));
        });
      });
      b.appendChild(last);
      b.appendChild(now);
      b.appendChild(status);
      Promise.resolve().then(function () { return call('backup_status', {}); }).then(function (res) {
        if (failed(res)) throw res;
        last.textContent = backupText(res);
      }, function (err) { last.textContent = 'Last backup: not available (' + errText(err) + ')'; });

      var d = card(view, 'details', 'Details', 'How much the app is used. Read only.');
      var lines = node('ul', 'nt-desk-stab-usage');
      d.appendChild(lines);
      Promise.resolve().then(function () { return call('usage_report', {}); }).then(function (res) {
        if (failed(res)) throw res;
        usageLines(res).forEach(function (t) { lines.appendChild(node('li', 'nt-desk-stab-help', t)); });
      }, function (err) { lines.appendChild(node('li', 'nt-desk-stab-help', 'Not available: ' + errText(err))); });
      restartCard(view);
    }

    // ---- Restart NotionTool ----
    var watching = false;
    function remember(on) {
      try {
        if (on) window.sessionStorage.setItem(RESTART_KEY, String(Date.now()));
        else window.sessionStorage.removeItem(RESTART_KEY);
      } catch (err) { /* no storage: the notice only lives in this overlay */ }
    }
    function pending() {
      try {
        var t = Number(window.sessionStorage.getItem(RESTART_KEY)) || 0;
        return t > 0 && Date.now() - t < 15 * 60 * 1000;
      } catch (err) { return false; }
    }
    // Small notice that stays visible outside the Settings tab while the harness restarts.
    function notice(text) {
      if (typeof document === 'undefined' || !document.body) return;
      var el = document.querySelector && document.querySelector('.nt-desk-restart-note');
      if (!text) { if (el) el.remove(); return; }
      if (!el) {
        el = node('div', 'nt-desk-restart-note');
        el.setAttribute('role', 'status');
        el.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:2147483000;padding:8px 14px;' +
          'border-radius:8px;background:#1f2937;color:#fff;font:13px/1.4 system-ui,sans-serif;box-shadow:0 2px 8px rgba(0,0,0,.3)';
        document.body.appendChild(el);
      }
      el.textContent = text;
    }
    function watch(resumed, show) {
      if (watching) return;
      watching = true;
      show = show || function () {};
      watchRestart(call, { resumed: resumed, onState: function (state, res) {
        if (state === 'waiting') { notice(restartText(res)); show('saving', restartText(res)); }
        if (state === 'restarting' || state === 'offline') { notice('Restarting...'); show('saving', 'Restarting...'); }
        if (state === 'online') {
          remember(false);
          notice('Back online');
          show('applied', 'Back online');
          setTimeout(function () { notice(''); }, 5000);
        }
        if (state === 'failed' || state === 'timeout') {
          remember(false);
          var msg = state === 'failed' ? restartText(res) : 'NotionTool did not come back. Check work/restart.out.';
          notice(msg);
          show('error', msg);
          setTimeout(function () { notice(''); }, 10000);
        }
      } }).then(function () { watching = false; }, function () { watching = false; });
    }
    function restartCard(view) {
      var r = card(view, 'restart', 'Restart NotionTool',
        'When idle waits until no task is running, preparing or uncertain; new tasks wait meanwhile. Now restarts right away. A backup is made first.');
      var mode = node('select', 'nt-desk-stab-select');
      [['idle', 'When idle'], ['now', 'Now']].forEach(function (o) {
        var opt = node('option', null, o[1]);
        opt.value = o[0];
        mode.appendChild(opt);
      });
      mode.value = 'idle';
      var go = node('button', 'nt-desk-stab-navbtn', 'Restart NotionTool');
      go.type = 'button';
      var cancel = node('button', 'nt-desk-stab-navbtn', 'Cancel restart');
      cancel.type = 'button';
      cancel.hidden = true;
      var ask = node('div', 'nt-desk-stab-confirm');
      ask.hidden = true;
      var status = node('div', 'nt-desk-stab-status');
      status.setAttribute('role', 'status');
      function show(kind, text) { setStatus(status, kind, text); }
      function start(m) {
        go.disabled = true;
        ask.hidden = true;
        show('saving', m === 'now' ? 'Restarting...' : 'Asking NotionTool to restart when idle...');
        Promise.resolve().then(function () { return call('app_restart', { mode: m }); }).then(function (res) {
          if (!res || res.ok === false) throw res;
          remember(true);
          cancel.hidden = res.state !== 'waiting';
          show('saving', restartText(res));
          notice(restartText(res));
          watch(false, function (kind, text) { if (kind !== 'saving') { go.disabled = false; cancel.hidden = true; } show(kind, text); });
        }, function (err) {
          go.disabled = false;
          show('error', (err && err.error) || ('Restart failed: ' + errText(err)));
        });
      }
      go.addEventListener('click', function (ev) {
        ev.preventDefault();
        if (mode.value !== 'now') return start('idle');
        go.disabled = true;
        Promise.resolve().then(function () { return call('app_restart_status', {}); }).then(function (res) {
          return (res && res.running) || [];
        }, function () { return []; }).then(function (running) {
          go.disabled = false;
          ask.textContent = '';
          restartConfirmText(running).split('\n').forEach(function (t) { ask.appendChild(node('p', 'nt-desk-stab-help', t)); });
          var yes = node('button', 'nt-desk-stab-navbtn', 'Restart now');
          yes.type = 'button';
          var no = node('button', 'nt-desk-stab-navbtn', 'Keep running');
          no.type = 'button';
          yes.addEventListener('click', function (e) { e.preventDefault(); start('now'); });
          no.addEventListener('click', function (e) { e.preventDefault(); ask.hidden = true; });
          ask.appendChild(yes);
          ask.appendChild(no);
          ask.hidden = false;
        });
      });
      cancel.addEventListener('click', function (ev) {
        ev.preventDefault();
        Promise.resolve().then(function () { return call('app_restart', { mode: 'cancel' }); }).then(function (res) {
          if (!res || res.ok === false) throw res;
          remember(false);
          notice('');
          cancel.hidden = true;
          go.disabled = false;
          show('applied', 'Restart cancelled. Tasks can start again.');
        }, function (err) { show('error', (err && err.error) || errText(err)); });
      });
      r.appendChild(mode);
      r.appendChild(go);
      r.appendChild(cancel);
      r.appendChild(ask);
      r.appendChild(status);
      if (pending()) { go.disabled = true; show('saving', 'Restarting...'); }
    }
    // Overlay injected again after a restart (no page reload): finish the notice with 'Back online'.
    if (pending()) watch(true);

    // Builds the view into container; returns a handle { root, el, stop }.
    function mount(container) {
      var root = node('div', 'nt-desk-stab');
      var head = node('div', 'nt-desk-stab-head');
      head.appendChild(node('h2', 'nt-desk-stab-title', 'Settings'));
      head.appendChild(node('p', 'nt-desk-stab-sub', 'Changes save right away.'));
      var view = { root: root, banner: node('div', 'nt-desk-stab-banner'), nav: node('nav', 'nt-desk-stab-nav'),
        body: node('div', 'nt-desk-stab-body'), rows: {} };
      view.banner.setAttribute('role', 'alert');
      var main = node('div', 'nt-desk-stab-main');
      main.appendChild(view.nav);
      main.appendChild(view.body);
      root.appendChild(head);
      root.appendChild(view.banner);
      root.appendChild(main);
      if (container && container.appendChild) container.appendChild(root);
      views.push(view);
      fill(view);
      load().then(null, function () { /* shown in the banner */ });
      return { root: root, el: root, stop: function () {
        views = views.filter(function (v) { return v !== view; });
        if (root.parentNode) root.remove();
      } };
    }

    // ---- shell tab, lazy ---------------------------------------------------------------------
    var descriptor = { id: TAB_ID, label: 'Settings', title: 'Settings', icon: '\u2699', order: 90,
      shortcut: 'Ctrl+Shift+,', mount: mount, render: mount };

    function tryRegister() {
      if (stopped || registered) return registered;
      var shell = window.__ntShell;
      var fn = shell && (shell.registerTab || shell.register || shell.addTab);
      if (typeof fn !== 'function') return false;
      try { fn.call(shell, descriptor); registered = true; } catch (err) { return false; }
      if (pollTimer) clearTimeout(pollTimer);
      pollTimer = 0;
      if (host) closeHost();
      return true;
    }

    function openShellTab() {
      var shell = window.__ntShell;
      var fn = registered && shell && (shell.openTab || shell.open || shell.show);
      if (typeof fn !== 'function') return false;
      try { fn.call(shell, TAB_ID); return true; } catch (err) { return false; }
    }

    var onHostKey = function (ev) {
      if (host && ev.key === 'Escape') { ev.preventDefault(); ev.stopPropagation(); closeHost(); }
    };

    function openHost() {
      if (host) return;
      var wrap = node('div', 'nt-desk-stab-host');
      wrap.setAttribute('role', 'dialog');
      wrap.setAttribute('aria-label', 'Settings');
      var close = node('button', 'nt-desk-stab-close', 'Close');
      close.type = 'button';
      close.title = 'Close (Esc)';
      close.addEventListener('click', function (ev) { ev.preventDefault(); closeHost(); });
      wrap.appendChild(close);
      (document.body || document.documentElement).appendChild(wrap);
      host = { node: wrap, view: mount(wrap) };
      document.addEventListener('keydown', onHostKey, true);
    }

    function closeHost() {
      if (!host) return;
      host.view.stop();
      if (host.node.parentNode) host.node.remove();
      host = null;
      document.removeEventListener('keydown', onHostKey, true);
    }

    function open() {
      if (stopped) return false;
      tryRegister();
      if (openShellTab()) return true;
      openHost();
      return true;
    }

    // Ctrl+Shift+,: the shell tab, else toggles the full-window view.
    function shortcut() {
      if (stopped) return false;
      tryRegister();
      if (openShellTab()) return true;
      if (host) closeHost(); else openHost();
      return true;
    }

    var onReady = function () { tryRegister(); };
    if (!tryRegister()) {
      window.addEventListener('nt-desk-shell-ready', onReady);
      var poll = function () {
        pollTimer = 0;
        polls += 1;
        if (!tryRegister() && !stopped && polls < POLL_MAX) pollTimer = setTimeout(poll, POLL_MS);
      };
      pollTimer = setTimeout(poll, POLL_MS);
    }
    load().then(null, function () { /* retried when the view opens */ });

    return {
      open: open,
      shortcut: shortcut,
      mount: mount,
      stop: function () {
        stopped = true;
        if (pollTimer) clearTimeout(pollTimer);
        pollTimer = 0;
        window.removeEventListener('nt-desk-shell-ready', onReady);
        closeHost();
        views.slice().forEach(function (v) { if (v.root.parentNode) v.root.remove(); });
        views = [];
        var shell = window.__ntShell;
        var un = registered && shell && (shell.unregisterTab || shell.unregister || shell.removeTab);
        if (typeof un === 'function') { try { un.call(shell, TAB_ID); } catch (err) { /* ignore */ } }
        registered = false;
        var root = document.documentElement;
        if (root) { root.removeAttribute('data-nt-look'); root.classList.remove('nt-desk-compact'); }
      }
    };
  };
})();
