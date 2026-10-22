# NotionTool App - desktop base (Notion 7.39.0)

Goal: turn the installed Notion desktop app into the NotionTool AI-harness app,
used only as a base. The user runs it instead of the normal Notion client.
App text English (I8). No invented endpoints (I2). No secrets in the page; 127.0.0.1 + Bearer only (I7).

## Recon (LIVE GEPRUEFT 2026-10-19)
- Install: MSIX com.notion.app.desktop.notion 7.39.0.0 at
  C:\Program Files\WindowsApps\com.notion.app.desktop.notion_7.39.0.0_x64__87smfmv0eebam\Notion\Notion.exe
  (read-only, code-signed). The running client the user chats in is PID 32860 - never kill or patch it.
- Electron 44.4.4, Chrome 152, V8 15.2. Renderer is webpack in resources/app.asar
  (.webpack/main/index.js ~4.4MB, .webpack/renderer/tabs/index.js + preload.js).
- Fuses: RunAsNode off, EnableNodeCliInspectArguments off,
  EnableEmbeddedAsarIntegrityValidation OFF (not enforced), OnlyLoadAppFromAsar on,
  GrantFileProtocolExtraPrivileges on.
- A SEPARATE instance launches cleanly with
  --user-data-dir=<our profile> --remote-debugging-port=9333 and exposes CDP
  (verified: /json/version returns Chrome/152 + Notion/7.39.0; page targets Notion + login).
  The original install and the normal profile (%APPDATA%\Notion) stay untouched.

## Approach decision (D1): non-invasive CDP overlay on a dedicated instance
We do NOT patch app.asar (MSIX is signed/read-only; patches break on auto-update
and risk the account). Instead we launch our own Notion instance with a dedicated
profile + remote-debugging port and inject our overlay (JS/CSS) over the real UI
through one persistent CDP session. Everything the user clicks still runs on the
real Notion client and the real Notion API. Reuses the Phase 8b notion_shell CDP machinery.

## Risks (honest)
- Notion ToS forbid reverse engineering / modifying the client. Personal use on the
  user's own PC only; never distribute. Account risk exists.
- Auto-update overwrites the install; our overlay is external so it survives, but
  Notion DOM selectors can change on a new build -> maintenance cost.
- Never kill or patch the running app (PID 32860); this Main chat lives in it.

## Architecture (profile LOCALAPPDATA\NotionTool-App\profile)
- notiontool/desktop_app.py - launcher + lifecycle. Starts Notion.exe with our
  profile and debug port, finds the main page target, opens a persistent CdpSession
  (reuse notion_shell.CdpSession), injects the desktop overlay on every document,
  serves live state through the ntHarness binding. spawn/connect/http injected for tests.
- notiontool/web/desktop/ overlay assets, each self-contained (load order = PART_FILES):
  - theme.(js|css): design system. Defines the --nt-* tokens on :root and restyles the
    Notion surfaces (background, glass sidebar, top bar, dialogs, menus, composer, inputs,
    buttons, scrollbars). theme.js only adds/keeps the class nt-theme on <html>; every
    restyle rule is scoped to html.nt-theme:not(.nt-desk-off).
  - overlay.(js|css): sidebar - show only Main chats, hide cowork chats; bottom-left
    New chat creates a Welcome chat (new_welcome_chat), never a normal New Chat.
    Branded 'NotionTool' header with gradient mark, 'Mains' label over the hidden
    'Notion AI' heading, Main rows as cards (active accent bar, 'Operator' pill),
    'New agent' tile and date group headings hidden (never inside a Main row).
  - chrome.(js|css): remove Home, Meetings, What's new (hot & fresh), Templates,
    Marketplace; keep the top-left account switcher and real Settings. Tab title prefix
    'NotionTool ? ', slim top bar, floating composer card, message width 760 px,
    upsell/quota banners hidden (app_hide.upsell = false shows them).
  - live.(js|css): Live dashboard (glass panel from the right) - stat tiles, runs with
    progress, task per coworker, worker context usage, rotations/switches,
    attention/awaiting_main, event timeline, rate budget.
  - settings.(js|css): NotionTool settings as a modal settings window (left navigation:
    General, Mains & Coworkers, Desktop app, Advanced) and as an entry in Notion's own
    settings dialog.
  - extras.(js|css): bottom-left action bar (Live button, attention badge, status dot),
    Main switch banner, disconnected banner, toasts, Ctrl+K command palette and the
    Ctrl+Alt+Shift+N emergency stop.
- notiontool/desktop_settings.py - harness settings panel schema + which Notion
  settings to hide; served to the overlay.
- Loader + server wiring + injected_source assembly: done by Main after the parts land.

## Overlay part contract
Each JS part is self-contained and registers on a shared namespace:
  window.__ntDesk = window.__ntDesk || {};
  window.__ntDesk.<part> = function (ctx) { ... };   // part: theme | chrome | sidebar | live | settings | extras
ctx = { call(action,args)->Promise, onState(fn)->unsub, getState()->state|null,
        el(tag,props,kids), toast(text,kind) } - same helpers as notion_shell/overlay.js.
No part imports another. CSS uses the nt-desk- prefix. All app text in English.
notiontool/desktop_shell.py: PART_FILES = ('theme', 'chrome', 'overlay', 'live', 'settings',
'extras') is the file order in build_source(); MOUNT_ORDER = ('theme', 'chrome', 'sidebar',
'live', 'settings', 'extras') is the mount order. The theme is always first.
Class names shared by two parts (nt-desk-btn, nt-desk-dot) must be styled under the part's
own root (.nt-desk-live ..., .nt-desk-banner ..., .nt-desk-bar ...), never globally.

## Design system (theme part)
Tokens are defined once in theme.css on :root. Parts use var(--nt-<token>, <fallback>) so each
part still renders alone; the fallback must match the token value.

| Token | Value | Use |
|---|---|---|
| --nt-bg | #0b0d12 | app background |
| --nt-surface | #12151d | panels, sidebar, cards |
| --nt-raised | #181c26 | raised cards, inputs, menus |
| --nt-border / --nt-border-strong | rgba(255,255,255,.07) / .12 | hairlines |
| --nt-text / --nt-muted | #e8eaf0 / #8a90a2 | text, secondary text |
| --nt-accent-from / --nt-accent-to | #7c5cff / #22d3ee | gradient stops |
| --nt-accent | #7c5cff | single accent colour |
| --nt-accent-gradient | linear-gradient(135deg, #7c5cff 0%, #22d3ee 100%) | brand mark, primary buttons, active bars |
| --nt-accent-soft | rgba(124,92,255,.16) | selected and hover washes |
| --nt-success / --nt-warning / --nt-danger | #22c55e / #f59e0b / #ef4444 | status colours |
| --nt-radius / --nt-radius-sm | 12px / 8px | corners |
| --nt-font | Inter, Segoe UI Variable, Segoe UI, system-ui | UI font |
| --nt-shadow, --nt-shadow-glow, --nt-ring | | elevation, hover glow, focus ring |
| --nt-glass, --nt-hover, --nt-active, --nt-transition | | glass panels, states, 150ms ease |

Aliases (same values, used by single parts): --nt-accent-1 / --nt-accent-start = accent-from,
--nt-accent-2 / --nt-accent-end = accent-to, --nt-fg = text, --nt-line = border.
Part-local variables (--nt-desk-* in live/extras, --nt-set-* in settings) are scoped to the
part's own root element and derive from the tokens above.
The theme is dark only. The kill switch class nt-desk-off on <html> turns every theme and
overlay rule off.

## Shortcuts

| Keys | Action | Part |
|---|---|---|
| Ctrl+K | Command palette (not while text is selected in an editable field: Notion's link shortcut stays) | extras |
| Ctrl+Shift+L | Open/close the Live dashboard (also location.hash '#nt-live') | live |
| Ctrl+Shift+, | Open/close NotionTool settings; Ctrl+S saves inside the panel | settings |
| Ctrl+Alt+Shift+N | Emergency stop: NotionTool layer off/on (localStorage ntDeskOff) | extras |
| Esc | Close the palette, Live or settings | extras, live, settings |

## Phase delegation
- op-fix-19: scaffold (new files, tested locally): deskapp, desksidebar, deskchrome, desklive, desksettings.
- op-fix-20+: Main wires loader + server routes + injected_source; live settings panel; then live smoke test.
- UI redesign: theme part added first; sidebar, chrome, live, settings and extras restyled on
  the --nt-* tokens; Ctrl+K command palette and new toasts. Not yet checked in a live window.

## Round 3 (2026-10-19)

- Toasts and banners: one stack at the bottom right (right 16px, bottom var(--nt-desk-stack-bottom, 132px); toasts sit above the banners via --nt-desk-banner-h). top is always auto, so nothing ever covers the top bar. z-index 2147482000 (command palette 2147483100).
- Toast timing: TOAST_MS = TOAST_ERROR_MS = 6000 (6 s auto-dismiss), hover pauses the timer, at most TOAST_MAX = 5 visible (oldest dismissed first).
- Toast dedupe: key = code, else kind + text; a toast whose key is still visible is not shown again. Status toasts fire once per status change (makeStatusDedupe).
- Composer watermark: small tag on the top-left border of the composer (chrome.css, z-index 1, pointer-events none).
- Live pill: docks above the sidebar footer; only live.js renders it. The old extras bar pill came from a stale build.
- Re-attach: build id guard; part handles in window.__ntDeskMounted (window.__ntDeskParts is the same object); teardown stops each handle once, marks removed nodes data-nt-desk-dead ([data-nt-desk-dead] { display: none !important }), never reloads the page. Every part returns {stop} and marks its own nodes data-nt-desk-own="1".
- Sidebar: chat rows are matched by title/href of the live rows; tiles detected and collapsible.
- Theme: overrides Notion's --c-* / --ca-* variables on body / .notion-dark-theme plus a --nt-bg-glow background; all rules under html.nt-theme:not(.nt-desk-off). The user bubble selector depends on Notion markup (no stable class).
- Reasoning effort: notion.py EFFORT_FIELD = 'reasoningEffort', written to body.transcript[0].value next to model / modelFromUser; value from effort_policy.choose_reasoning_effort, clamped to the model's supportedReasoningEfforts; resume passes the task effort. Set EFFORT_FIELD = None to stop sending it.

## Studio v3 (2026-10-19): tabs, shortcuts, settings keys

This section replaces the Shortcuts table above where they differ.

### Tabs (shell.js, window.__ntShell)

| Tab | Part | What it shows | Fallback without the shell |
|---|---|---|---|
| Chat | Notion itself | Notion's AI chat (our view layer is hidden) | - |
| Live | live.js (+ progress.js cards) | Needs you, Working now, Team, History, Details | slide-in panel, #nt-live, Ctrl+Shift+L |
| Notes | notes.js | Plans and notes linked to requests, reader, draft box (never sends) | panel, #nt-notes, Ctrl+Shift+O |
| Settings | settings.js | Team, Models & effort, App look, Notifications | full-window page, Ctrl+Shift+, |

Shell API today: register(tab, {mount(box, ctx) -> cleanup fn, unmount(box), badge(state)}), open(tab),
window.__ntShellQueue.push([tab, view]) before the shell exists. live.js and settings.js register with ONE
descriptor object ({id, mount, ...}) and look for registerTab/openTab/unregisterTab and the
nt-desk-shell-ready event; the shell does not offer these yet (open point, see below).

### Shortcuts

| Keys | Action | Part |
|---|---|---|
| Ctrl+1 / 2 / 3 / 4 | Chat / Live / Notes / Settings tab | shell |
| Ctrl+Shift+L | Live (tab, else panel; also #nt-live) | live |
| Ctrl+Shift+O | Notes (tab, else panel; also #nt-notes) | notes |
| Ctrl+Shift+, | Settings (tab, else page) | settings |
| Ctrl+K | Command palette (kept; not while text is selected in an editable field) | extras |
| Ctrl+Alt+Shift+N | Emergency stop: NotionTool layer off/on (localStorage ntDeskOff) | extras |
| Esc | Close palette, panel or settings page | extras, live, notes, settings |

### Settings keys

| Label | Key | Saved via | Range / values |
|---|---|---|---|
| Coworkers | per_account | settings_set -> POST /api/settings | 1..40, default 10 |
| Replace chats after (hours) | coworker_max_age_hours | settings_set -> POST /api/settings | 1..72, default 23 (team_rotation.py) |
| Max at once (all accounts) | max_workers | settings_set -> POST /api/settings | 1..40 |
| Effort | app_effort_override | app_settings_set | auto, low, medium, high, xhigh, max |
| Color strength | app_theme_intensity | app_settings_set | soft, normal, strong |
| Compact mode | app_compact | app_settings_set | on/off |
| Hide Notion extras | app_declutter | app_settings_set | on/off |
| Desktop pop-ups / When a task is done / When the app needs you / Play a sound | app_notify_desktop, app_notify_done, app_notify_needs_you, app_notify_sound | app_settings_set | on/off |

Not yet effective: app_effort_override (notion.py does not read it), app_notify_* (studio.js does not read them).

### Notes routes

HTTP (server.py hook, GET only): /api/notes, /api/notes/page, /api/notes/draft, /api/notes/status -> notes_routes.http_route.
The tab itself uses overlay actions notes_list, notes_page, notes_draft; these need desktop_shell.py wiring (open point).
