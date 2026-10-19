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
| `/api/state` | full UI snapshot (Mains, Coworkers, tasks, operations, host_api, limits); read-only report fields `main_context_usage`, `roles`, `stale_context_tasks` and `awaiting_main` (see below) |
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
| `/api/run/retry` | `{id, allow_resend?}` | resume a blocked or failed run; finished tasks are kept, blocked tasks are queued again (refused while a task still needs reconciliation). 409 when the retry would resend work that already reached Notion; send again with `allow_resend:true` to resend anyway |
| `/api/run/confirm` | `{id}` | the Main confirms a run in `awaiting_main`; only `awaiting_main -> done` (200/404/409/501, see below) |
| `/api/chat/compress` | `{worker_id, reason?}` or `{chat_id, reason?}` | nothing is sent to Notion. `worker_id`: only an enabled coworker in state `ready`; sets `compress_requested`, returns `{ok, worker_id, reason, same_chat}` (`same_chat` true only with `coworker_rotate_mode=same_thread`). `chat_id` (a Main): with `main_context_policy=hint` the request is cleared because Notion compacts the operator chat itself (returns `{ok, chat_id, reason, same_chat:true, native:true, stale_rotation_removed}`); with `auto` it sets `compress_requested` (server.py:1066-1112) |
| `/api/task/reconcile` | `{id, resolution?, note?}` | without `resolution`: only for a task with `remote_unknown` or state `uncertain`, check the remote state again instead of resending. With `resolution` (`accept` or `abandon`): the Main decides the task (`Engine.resolve_uncertain`); nothing is resent |
| `/api/operator/register` | `{worker_id, project_path?}` | register the operator's own Notion AI chat as the operator Main (see below) |
| `/api/operator/delegate` | `{chat_id, goal, tasks, request_id?, summary?}` | the operator Main delegates a list of tasks to Coworkers (see below) |
| `/api/operation/resume` | `{id}` | resume an interrupted operation; never blindly resends unconfirmed remote work |
| `/api/host/key` | `{rotate:true}` | rotate API key |

Other routes that server.py also routes, not detailed here: `/api/account/open|provision|refresh|remove|select|setup|team`, `/api/bridge/connect|tunnel`, `/api/mcp/add|refresh`, `/api/chat/release`, `/api/chat/update`, `/api/main/rotate`, `/api/workers/enable`, `/api/folder`, `/api/project/pick`, `/api/report`, `/api/coordination`, `/api/settings`. There are no separate files or recover routes. `POST /api/settings` accepts partial bodies, for example `{"coworker_fresh_context":"threshold"}` or `{"context_replace_mode":"soft"}`.

## Run and worker control routes (detail)
Source: `notiontool/server.py` as read on 2026-10-19 01:05 (read only). Functions are named; line numbers are as read then and can drift: GET `/api/state` :213, POST `/api/run/confirm` :251 -> `confirm_run_request` :855, POST `/api/run/retry` :253 -> `retry_run_request` :845, `/api/task/reconcile` :958-969, `/api/settings` :989, `/api/chat/compress` :1066-1112. Every POST body must be a JSON object (`read_body`). `/api/run/confirm` and `/api/run/retry` are routed first in the request handler and return the status chosen by `confirm_run_request` / `retry_run_request`; all other POST routes go through `Application.action()` and return 200 with the route result. In the handler, `ValueError`, `KeyError`, `TypeError` and `OSError` become 400 with `{error: <message>}`; any other exception becomes 500 `Operation failed; see the activity log`. A missing required field such as `id` raises `KeyError`, so it also returns 400.

| Route | Body | Response | Error cases and behavior | Source |
|---|---|---|---|---|
| `/api/run/confirm` | `{id}` | 200 `{ok:true, id, state, result}`; `state` is the saved run state (`done`), `result` is the engine result object `{id, state:'done', confirmed_at}` or null | 501 `Run confirmation is not available: the engine has no confirm_run yet` when the engine has no `confirm_run`; 404 `Run not found` (missing or empty `id`, or unknown run); 409 `Run is not awaiting Main confirmation (state: <state>)`. On success `Engine.confirm_run` sets `state=done`, `confirmed_at`, `updated` and records `run_confirmed` and `stage_changed` events. Runs reach `awaiting_main` in `Engine._finish` (engine.py:385-394) when `Engine._main_decisions` finds done tasks that failed the machine check (`Engine._machine_check`, engine.py:331-361: every declared file must exist inside the project folder), failed the operator acceptance check (no `RESULT: DONE` line) or have `requires_review`. Nothing is resent; runs without such tasks go straight to `done` | `confirm_run_request`, `Engine.confirm_run` |
| `/api/run/retry` | `{id, allow_resend?}`; `allow_resend` is read with `bool()`, default false | 200 `{ok:true}` | 409 `{error:'Retry would resend work that already reached Notion: <task> (<reason>); ...'}` when `Engine._resend_hazard` finds a blocked/failed task whose attempt was submitted (reasons: the failed attempt used tools or claimed files; the submitted turn has no confirmed outcome; the submitted turn used tools or claimed files) and `allow_resend` is false. Only this refusal becomes 409 (`RESEND_REFUSAL` prefix); other errors keep 400: `Only a blocked or failed run can be resumed`, `Wait for reconciliation before retrying` while any task is `uncertain` or `remote_unknown`. The web UI (`retryRun` in web/app.js) asks "Resend anyway?" on 409 and then sends `allow_resend:true` | `retry_run_request`, `action()`, `Engine.retry_blocked` |
| `/api/run/pause` | `{id, paused?}`; `paused` defaults to true | `{ok:true}` | 400 `Run not found`. `paused:true` only pauses an active run; `paused:false` only resumes a paused run | `action()` |
| `/api/coordination/resume` | `{}` (no field is read) | `{resumed, requeued_tasks, resumed_runs}` | No route-specific error. Manual resume after `ACCOUNT_BLOCKED` | `action()`, `Engine.resume_remote` |
| `/api/task/reconcile` | `{id, resolution?, note?}` | `{ok:true}`; with `resolution` `{ok:true, ...}` plus the result of `Engine.resolve_uncertain` | With `resolution` (server.py:959-962) the route calls `Engine.resolve_uncertain(id, resolution, note)` (`accept` -> `done`, `abandon` -> `failed`; only after Notion confirmed the turn finished); its errors are 400 and nothing is resent. Without `resolution`: 400 `This task does not need manual reconciliation` when the task does not exist or has neither `remote_unknown` nor state `uncertain`. On success the task becomes `uncertain` with `remote_unknown=false`, `reconcile_at=0`, `reconcile_checks=0` and a new `started` time, so the remote state is checked again; nothing is resent | `action()` |
| `/api/operator/register` | `{worker_id, project_path?}` | `{ok:true, chat_id, worker_id, created}` | 400 with the message from `operator_main.register_operator`: `Notion chat not found`; `This Notion chat is a Main; release its chat first`; `This Notion chat was released to its user`; `This Notion chat still holds a harness task; reconcile it first`; `This account already has an operator Main: <label>`. The operator's own Notion AI chat becomes the Main that delegates; the harness never writes into it | `action()`, `operator_main.register_operator` |
| `/api/operator/delegate` | `{chat_id, goal, tasks, request_id?, summary?}`; `tasks` is a non-empty list of task objects (each may carry an optional plain-language `summary`) | run summary `{ok:true, run_id, state, chat_id, tasks, ...}` | 400 with the message from `operator_main.delegate`: `tasks must be a non-empty list of task objects`; `Register this chat as operator Main first`; `Describe the goal of the delegated work`; `request_id already belongs to another chat`; `The operator chat lost its Notion thread; register it again`; `No enabled Coworker in this account` | `action()`, `operator_main.delegate` |
| `/api/worker` | `{id, enabled}`; `enabled` is read with `bool()` | `{ok:true}` | 400 `Worker not found`; 400 `Main threads are reserved for their chat` | `action()` |

## `/api/state` report fields (read only)
- `stale_context_tasks`: at most 20 stuck native-compression tasks (run `__context__`, state `checkpoint`, older than 6 hours, worker missing or ready without a matching lease) from `stale_tasks.stale_context_tasks`; `[]` on any error, never raises, changes nothing (`Application.stale_context`, server.py:559-565; field at server.py:552). Item fields: `id`, `state`, `title`, `worker_id`, `age_seconds`, `worker_state`.
- `awaiting_main`: ids of runs in state `awaiting_main` (server.py:553). `main_context_usage` and `roles` are report fields too (server.py:550-551).

## Settings for context handling (`POST /api/settings`)
Partial bodies are accepted. Invalid values return 400.
- `coworker_rotate_mode`: `new_welcome` (code default) or `same_thread` (server.py:1014-1017).
- `main_context_policy`: `hint` (code default) or `auto` (server.py:1018-1021).
- `coworker_delete_old_chat`: boolean; `true`, `1`, `on`, `yes` (any case) mean true, everything else false (server.py:1022-1024); code default true. False keeps a retired coworker chat renamed 'Cowork (retired)' instead of deleting it.

## Operation states
`reserved -> submitted -> (unconfirmed) -> done | failed`.
`unconfirmed` = the remote request may have been accepted (timeout). The host checks the real remote state (thread lookup / message marker) before any further action.

## Client rule after reconnect
1. `GET /api/request?id=...` or `/api/operation?id=...`
2. Only if the state says `not found`, send again with the SAME `request_id`.

## Run events used for recovery
- `model_fallback`: Notion reported another execution model than requested; the result is kept and the run continues.
- `context_rotated`: the context was replaced: for coworkers by default in a new 'Welcome to Notion' chat (`coworker_rotate_mode=new_welcome`), with `same_thread` and for Mains in the same chat. `estimated_tokens` and `token_limit` are numbers.
- `coworker_old_chat_deleted` / `coworker_old_chat_kept`: what happened to the old coworker chat after a new Welcome chat; with `coworker_delete_old_chat=false` the message says `kept by the setting coworker_delete_old_chat`.
- `handoff_file_failed`: the checkpoint file could not be written; the checkpoint is sent inline.
- `machine_check_failed`, `awaiting_main`, `run_confirmed`: three-stage acceptance (see `/api/run/confirm`).
- `compress_requested`: a Main was marked for Compress.
- `remote_turn_failed`: Notion ended the turn with an error step before any output (for example `temporarily-unavailable`); the task is retried after a cooldown.
- `context_scan_waiting`: native context scan deferred; normal right after a restart.

## Maintenance job
- Tailscale MCP and account connections are maintained about every 60 s by one job with the stable id `j-bridge-maintenance`; it records a run event only when its result changes, and old maintenance records are pruned at startup.
- The Funnel route is applied only when it is missing, points to another target, or Funnel is off for that port; a live identical route is left untouched. Routes of other apps are never changed.
