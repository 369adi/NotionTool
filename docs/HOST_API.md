# NotionTool Host API

UI / Harness / tools -> Host API (this PC) -> Notion internal AI API (`https://app.notion.com/api/v3/<method>` via the lean Edge context).
The public Notion Integrations API is NOT used.

## Binding and auth
- Default bind: `127.0.0.1`, port `host_api_port` (default 8790). CLI `--port` overrides. If the port is busy, the next free port is used and reported in `/api/health`.
- Network access only when `host_api_allow_network=true` AND `host_api_bind` is set (e.g. `0.0.0.0`). Bearer auth is then mandatory; use a firewall rule / VPN.
- Every request: `Authorization: Bearer <api-key>`. The built-in UI uses its session UI token instead. Missing/wrong key -> `401`.
- Key: shown in Settings; `POST /api/host/key` with `{"rotate":true}` rotates it. The key is stored with the protected session data and is never written to logs (log redaction covers bearer tokens, token_v2, cookies and login codes; numeric token counts stay readable).
- Origin checks apply only to non-bearer (browser UI) requests.

## Read routes (GET)
| Route | Purpose |
|---|---|
| `/api/health` | connection + auth check, bound host/port, Notion session state, schema version |
| `/api/state` | full UI snapshot (Mains, Coworkers, tasks, operations, host_api, limits) |
| `/api/chats` | chats with role (main/coworker), Main number, status, context generation, successor/superseded |
| `/api/chat?id=<chat>` | chat details; messages are paged (`before`, `limit`); `marker` changes only when content changes |
| `/api/run?id=<run>` | run status, tasks, dependencies, attempts, verification, results/report |
| `/api/request?id=<request_id>` | idempotency lookup: what happened to a client request (use after reconnect) |
| `/api/operations` / `/api/operation?id=<op>` | durable operations (create_main) with step log |
| `/api/stream?since=<seq>` | Server-Sent Events: `event: probe`, state/status/answer deltas; reconnect with the last seq |

## Write routes (POST, JSON)
| Route | Body | Notes |
|---|---|---|
| `/api/main/create` | `{request_id}` | creates exactly one Main through the existing chat creation; same `request_id` returns the same operation |
| `/api/send` | `{chat_id, text, request_id}` | starts a run (planner decides direct answer vs Coworkers); duplicate `request_id` -> same run |
| `/api/run/pause` | `{id, paused}` | controlled pause (`paused` defaults to true; no interruption of an unconfirmed remote action) |
| `/api/run/cancel` | `{id}` | cancel a run |
| `/api/run/retry` | `{id}` | resume a blocked or failed run; finished tasks are kept, blocked tasks are queued again (refused while a task still needs reconciliation) |
| `/api/chat/compress` | `{chat_id, reason?}` | Compress a Main in the same chat: the checkpoint runs with its next message; returns `{ok, chat_id, reason, same_chat:true}` |
| `/api/task/reconcile` | `{id}` | only for a task with `remote_unknown`: check the remote state again instead of resending |
| `/api/operation/resume` | `{id}` | resume an interrupted operation; never blindly resends unconfirmed remote work |
| `/api/host/key` | `{rotate:true}` | rotate API key |

All previously existing routes (account, settings, tools/MCP, files, recover) are unchanged. `POST /api/settings` accepts partial bodies, for example `{"coworker_fresh_context":"threshold"}` or `{"context_replace_mode":"soft"}`.

## Operation states
`reserved -> submitted -> (unconfirmed) -> done | failed`.
`unconfirmed` = the remote request may have been accepted (timeout). The host checks the real remote state (thread lookup / message marker) before any further action.

## Client rule after reconnect
1. `GET /api/request?id=...` or `/api/operation?id=...`
2. Only if the state says `not found`, send again with the SAME `request_id`.

## Run events used for recovery
- `model_fallback`: Notion reported another execution model than requested; the result is kept and the run continues.
- `context_rotated`: context summarized in the same Welcome chat (checkpoint); no new chat. `estimated_tokens` and `token_limit` are numbers.
- `compress_requested`: a Main was marked for Compress.
- `remote_turn_failed`: Notion ended the turn with an error step before any output (for example `temporarily-unavailable`); the task is retried after a cooldown.
- `context_scan_waiting`: native context scan deferred; normal right after a restart.

## Maintenance job
- Tailscale MCP and account connections are maintained about every 60 s by one job with the stable id `j-bridge-maintenance`; it records a run event only when its result changes, and old maintenance records are pruned at startup.
- The Funnel route is applied only when it is missing, points to another target, or Funnel is off for that port; a live identical route is left untouched. Routes of other apps are never changed.
