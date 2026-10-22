# Classic window vs. new desktop app - feature parity

> This file replaces docs/plan/p1642/01-parity.md (a stub that only said "Draft - being filled in"). When merging, delete 01-parity.md, or move this content into it, so only one parity table remains.

Scope: classic window = notiontool/web/index.html + notiontool/web/app.js (calls /api/* through api(path) and action(path, body)). Old Notion overlay = notiontool/web/notion_shell/overlay.js (already has an MCP tab). New app = notiontool/web/desktop/*.js, reached only through desktop_shell.DESK_ACTIONS = notion_shell.ACTIONS (state, runs, run_pause, run_cancel, run_retry, task_reconcile, settings_get, settings_set, mcp_list, mcp_refresh, run_confirm) + new_welcome_chat, settings_schema, app_settings_get, app_settings_set, main_switch_start.

Method: read-only code reading (app.js functions and endpoints, index.html views and buttons, server.py routes, notion_shell.py actions, overlay.js, keyword search in every desktop file). No live screenshot. Status: present = same data and actions; partial = shown but some actions or fields missing; missing = not in the new app; n/a = replaced by Notion itself.

## Feature table

| # | Classic feature (app.js / index.html) | Status | Evidence in new app | Where it should go |
| --- | --- | --- | --- | --- |
| F1 | Live state + stream (GET /api/state, /api/stream, host-down banner) | present | state action in every file; extras.js lost-connection banner and status dot | none |
| F2 | Runs list with Pause/Resume (runControls) | present | live.js run_pause, extras.js palette | none |
| F3 | Run step Confirm / Cancel / Retry, reconcile uncertain task | present | live.js run_confirm, run_cancel, run_retry, task_reconcile | none |
| F4 | Attention list (renderAttention, mainReasons) | partial | notion_shell builds attention; live.js/declutter.js show items, no Main reasons text | notiontool/web/desktop/live.js |
| F5 | Task report viewer (GET /api/report?id=) | missing | no report call in desktop/*.js | notiontool/web/desktop/live.js + action report in notiontool/desktop_shell.py |
| F6 | Operation status (GET /api/operation?id=) | partial | desktop_shell.py polls operations only for new_welcome_chat | notiontool/web/desktop/live.js |
| F7 | Worker dashboard (renderWorkerDashboard, workerHints, ages) | partial | overlay.js, progress.js, studio.js show coworkers and chat age; no per-worker hints | notiontool/web/desktop/overlay.js |
| F8 | Coworker panel: Add coworker chat (POST /api/account/add-worker) | present | new_welcome_chat via POST /api/main/create (overlay.js, extras.js) | none |
| F9 | Fresh context for coworkers (coworker_fresh_context) | present | settings.js team group; live.js/notes.js fresh-context text | none |
| F10 | Context rotation / switch to fresh Main | present | extras.js switch banner + main_switch_start | none |
| F11 | Compress chat (POST /api/chat/compress) | missing | no compress in desktop/*.js | notiontool/web/desktop/extras.js + action chat_compress in notiontool/desktop_shell.py |
| F12 | Accounts view: list accounts and their jobs (renderAccounts, accountJobs; data = state.accounts) | missing | new-app state (notion_shell.build_state) has no accounts list | new notiontool/web/desktop/accounts.js |
| F13 | Sign in / Add account / Remove account / select, open, refresh, setup, team, provision | missing | none of these in DESK_ACTIONS | notiontool/web/desktop/accounts.js + actions in notiontool/desktop_shell.py |
| F14 | Team capacity (max_workers, per_account) | partial | settings.js team group edits keys; no capacity read-out | notiontool/web/desktop/settings.js |
| F15 | MCP servers list (renderMcp, app.js:411): state.mcp_servers = mcp_servers.public() = local_mcp.public() built-ins + store mcp_servers; fields used: name, url, toggle, label, enabled, hint, builtin, installed, id | partial | notiontool/web/notion_shell/overlay.js loadMcp and renderMcp (lines 603 to 624) already show the server list from mcp_list. mcp_list returns mcp_servers.public() minus tools, so every classic field is there (built-in: id, name, url, builtin, installed, enabled, toggle, label, hint, read_auto, write_auto; custom: id, name, url, read_auto, write_auto). The new desktop app (web/desktop) has no MCP tab yet | new notiontool/web/desktop/mcp.js (port overlay.js renderer) + tab in notiontool/web/desktop/shell.js |
| F16 | MCP built-in on/off switch: classic POST /api/settings {[toggle]: checked} | missing | settings_set forwards to POST /api/settings and allows every page_settings_keys() key, which includes local_mcp.SETTINGS (rea_enabled, roblox_enabled, blender_enabled, codebase_memory_enabled); none is in PAGE_EXCLUDED_SETTINGS, so all four can be saved. overlay.js renderMcp has no switch; settings.js only saves settings_schema entries, so the MCP tab must call settings_set itself | notiontool/web/desktop/mcp.js |
| F17 | Add MCP server: POST /api/mcp/add {name, url (form mcp_url), token (form mcp_bearer_token), write_auto} | missing | no mcp_add action in DESK_ACTIONS; no form in overlay.js | notiontool/web/desktop/mcp.js + mcp_add in notiontool/desktop_shell.py |
| F18 | Refresh tools: POST /api/mcp/refresh {} returns servers [{name, enabled, ok, count, error}] | partial | overlay.js renderMcp (lines 603 to 624) has a Refresh button calling mcp_refresh, then reloads mcp_list; it drops the per-server result and reads tool_count, which the server never sends. extras.js palette also calls mcp_refresh | notiontool/web/desktop/mcp.js |
| F19 | MCP connected to N accounts: classic counts state.accounts where mcp_connections[server.id].connected is true | missing | data missing: new-app state and mcp_list have no accounts and no mcp_connections (0 matches in notion_shell.py and desktop_shell.py) | notiontool/notion_shell.py (add count) + notiontool/web/desktop/mcp.js |
| F20 | Remote MCP access, Tailscale MCP, Copy URL, Copy/Rotate token, host key (POST /api/host/key) | missing (by design) | notion_shell.py keeps secrets and public_mcp_url out of the overlay | keep in classic window; mcp.js shows a caption only |
| F21 | Settings (renderSettings, Save settings) | partial | settings.js settings_schema + settings_set/app_settings_set; some keys missing from the schema (for example auto_continue_on_done) | notiontool/web/desktop/settings.js + notiontool/desktop_settings.py |
| F22 | Project folder picker (POST /api/project/pick) | missing | no project/pick call in desktop/*.js | notiontool/web/desktop/settings.js + action project_pick in notiontool/desktop_shell.py |
| F23 | Chat folders tree (tree, folderRows, newFolder) | missing | no folder UI | notiontool/web/desktop/overlay.js |
| F24 | Chat view and send (GET /api/chat?id=, POST /api/send, GET /api/request?id=) | n/a | the new app runs inside the Notion chat | none |
| F25 | New Main chat (POST /api/main/create) | present | new_welcome_chat in desktop_shell.py | none |
| F26 | Activity / recent progress | present | progress.js activity feed | none |
| F27 | PC tools and Harness card (index.html) | not verified | no matching UI found by keyword search; may be in the settings_schema harness group | check notiontool/web/desktop/settings.js |
| F28 | Toasts, busy buttons, modal dialogs | present | settings.js toast, live.js confirm dialogs | none |

## Next steps per missing or partial feature

1. F15-F19 MCP servers tab (task tb2cfee1d, Fix 2: MCP servers tab in the new app). Do not write it from scratch: port or reuse loadMcp and renderMcp from notiontool/web/notion_shell/overlay.js (lines 603 to 624) in a new notiontool/web/desktop/mcp.js, and add the tab in shell.js. It keeps the same data as the classic window: the server list from mcp_list (= mcp_servers.public() minus tools) with the fields toggle, label, enabled, hint, builtin, installed, url (plus id and name); mcp_list already returns all of them, so no field is missing. What the overlay renderer lacks and mcp.js must add: (a) the built-in on/off toggles via settings_set {values: {[toggle]: checked}}; rea_enabled, roblox_enabled, blender_enabled and codebase_memory_enabled are all in page_settings_keys(), so no key needs adding; (b) the Add server form: new action mcp_add in DESK_ACTIONS -> POST /api/mcp/add {name, url, token, write_auto}, never echo the token; (c) the per-server refresh result: show servers[].name, ok, count, error from mcp_refresh (POST /api/mcp/refresh {}) instead of the never-sent tool_count; (d) the connected-accounts count: count accounts where mcp_connections[server.id].connected is true, exactly like app.js:411. That data is missing in the new app, so notion_shell.mcp_list (or build_state) must add per custom server connected_accounts and needs_review counts computed from store accounts, without tokens or other account secrets.
2. F12-F13 Accounts. Classic endpoints: list = GET /api/state (field accounts, plus harness_account_id; there is no separate list endpoint); sign in and add account = POST /api/account/browser-login {} (both buttons, app.js:313); remove = POST /api/account/remove {account_id} (app.js:314); also POST /api/account/select, /api/account/open, /api/account/refresh, /api/account/setup, /api/account/team, /api/account/provision, /api/account/add-worker, /api/account/reset-coworkers {account_id}. Add matching desktop actions (accounts, account_login, account_remove, account_select and so on) to DESK_ACTIONS with handlers that call these routes, expose a sanitized accounts list (id, email, state, error, no secrets), and add accounts.js as an Accounts tab or Settings section.
3. F22 Project folder picker: add action project_pick -> POST /api/project/pick and a Project folder row in the settings view.
4. F5 Task report: add action report -> GET /api/report?id= and a View report button on finished tasks in live.js.
5. F11 Compress chat: add action chat_compress -> POST /api/chat/compress and a palette entry in extras.js.
6. F4, F6, F7, F14: show Main reasons text, operation status (GET /api/operation?id=), per-worker hints and team capacity from fields already in state.
7. F21: add the classic keys missing from the desktop_settings.py schema (for example auto_continue_on_done) and map them in settings.js.
8. F23 Chat folders: decide whether folders are still needed inside Notion; if yes, add a folder filter to overlay.js.
9. F27: open the classic PC tools and Harness card and confirm each key is in settings_schema; add missing ones.
10. F20: keep remote access (host key, public MCP URL, Tailscale) in the classic window on purpose.

Files other chats are editing right now (settings.js, shell.js, desktop_shell.py, notion_shell.py, tests/test_desktop_shell.py) are only named here as targets; this doc does not change them.
