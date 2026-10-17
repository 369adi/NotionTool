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
| `/api/metrics?run_id=<run>` | per-task metrics from the event chain: total, queue/ready wait, time to first output, remote, after stream, verify messages read, errors, fallbacks, attempt log (see docs/TELEMETRY.md) |

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

## Run and worker control routes (detail)
Source: `notiontool/server.py` (CRLF, read only). Every POST body must be a JSON object of 1 byte to 2 MiB (server.py:191-198). A route result is returned with 200 (server.py:175-176). `ValueError`, `KeyError`, `TypeError` and `OSError` become 400 with `{error: <message>}` (server.py:178-179); any other exception becomes 500 `Operation failed; see the activity log` (server.py:180-182). A missing required field such as `id` raises `KeyError`, so it also returns 400, with the quoted field name as the message (server.py:178-179).

| Route | Body | Response | Error cases and behavior | Source |
|---|---|---|---|---|
| `/api/run/pause` | `{id, paused?}`; `paused` defaults to true | `{ok:true}` | 400 `Run not found` (engine.py:247-248). `paused:true` only pauses an active run and keeps its state for resume; `paused:false` only resumes a paused run; any other state is stored unchanged and still returns `{ok:true}` (engine.py:249-254) | server.py:586-588 |
| `/api/run/retry` | `{id}` | `{ok:true}` | 400 `Only a blocked or failed run can be resumed` (engine.py:296-297); 400 `Wait for reconciliation before retrying` while any task is `uncertain` or `remote_unknown` (engine.py:298-299). On success it also lifts the coordination pause and requeues blocked and failed tasks with `attempts=0`; finished tasks are kept (engine.py:300-310) | server.py:591-593 |
| `/api/coordination/resume` | `{}` (no field is read, but the body must still be a JSON object) | `{resumed, requeued_tasks, resumed_runs}` | No route-specific error. Manual resume after `ACCOUNT_BLOCKED`: lifts the pause, requeues only blocked tasks with `paused_by` and without `remote_unknown`, and resumes a blocked run only if none of its tasks is failed, blocked or `remote_unknown` (engine.py:269-291) | server.py:589-590 |
| `/api/task/reconcile` | `{id}` | `{ok:true}` | 400 `This task does not need manual reconciliation` when the task does not exist or has no `remote_unknown` (server.py:596-597). On success the task becomes `uncertain` with `remote_unknown=false`, `reconcile_at=0` and a new `started` time, so the remote state is checked again instead of resending (server.py:598-600; the later check itself is NOT VERIFIED here) | server.py:594-600 |
| `/api/worker` | `{id, enabled}`; `enabled` is read with `bool()`, a missing value disables the worker | `{ok:true}` | 400 `Worker not found` (server.py:603-604); 400 `Main threads are reserved for their chat` (server.py:605-606) | server.py:601-609 |

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
