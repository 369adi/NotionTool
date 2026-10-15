# Harness: Multi-Main, Coworkers, same-chat context

## Start
- EXE: `NotionTool.exe` (UI) or `NotionTool.exe --serve [--port 8790]` (host only).
- Source: `python main.py [--serve] [--port N]`.
- Settings (UI > Settings or settings store): `host_api_port` (8790), `host_api_allow_network` (false), `host_api_bind` (127.0.0.1), `coworker_fresh_context` (`threshold` | `every_task`), `context_replace_mode` (`soft` | `full`, default `soft`), `context_token_limit` (60000), `context_turn_limit` (80), existing parallel/Coworker limits.
- First start runs migration v1 -> v2: backup of the store, durable counters, Main numbers for existing chats. No account/session/chat/result data is removed.

## Existed before (kept)
Edge lean context + robots.txt tab on app.notion.com, direct `/api/v3` calls with streaming, transcript-based completion check, recover without blind resend, chat creation (Welcome/agent thread), worker leases, file locks, dependency gating, targeted retry, context rotation/checkpoints, message paging, MCP/local tools.

## Added / changed
- Multiple Mains ("Main 1, Main 2, ..."), durable concurrency-safe counter; `Create Main` = one durable operation per request_id via existing chat creation.
- Planner: focused/simple questions -> Main answers directly (no Coworker); larger work -> waves within capacity.
- Idempotent send (`request_id`).
- Only Welcome chats are used (1 Main + 5 Coworkers on the active account). Context cleanup never creates a new chat: a checkpoint summary is written in the same Welcome chat and leads the next turns. Details: [CONTEXT_AND_RECOVERY.md](CONTEXT_AND_RECOVERY.md).
- Compress for Mains: yellow `Compress` button with a reason; `POST /api/chat/compress`; the checkpoint runs with the next message in the same chat and the chat id stays the same.
- Coworker cleanup: automatic in the same Welcome chat, at the context limits (`threshold`) or before every new task (`every_task`). Gate: only after a stored + confirmed handoff and no running/unconfirmed/recovery dependency (`coworker_cleanup_deferred` event otherwise).
- Continue in New Chat was removed (UI, route, operation, tests).
- Model fallback: if Notion reports a different execution model than requested, the result is kept, a `model_fallback` event is recorded, the account card shows a hint and the run continues.
- Turn errors: a Notion turn that ended with an error step before any output (for example `temporarily-unavailable`) is retried automatically after a cooldown; a turn that produced output is never resent automatically.
- Host API with bearer auth, localhost default, SSE stream, operations, request lookup.
- UI: Create Main, Compress, Coworker labels, Operations, Known limits (confirmed/observed/configured), host-unreachable banner, reconcile-before-resend, refresh without full re-render, settings for `context_replace_mode` and `coworker_fresh_context`, fallback model status, task-row labels and worker hints.
- Log redaction for bearer tokens, token_v2, cookies and login codes; numeric token counts (`tokens`, `token_limit`, `token_count`, `token_budget`) stay readable in event logs.
- Coworker scope: each executor answers only its own task (live run r3cfe6eb5137a41fe848ef10365dfb545: PART-OK 1-3, each from its own Coworker).
- Run context: chat history plus the project-memory findings that are not already quoted in it (no duplicated answers, no empty label). Details: [CONTEXT_AND_RECOVERY.md](CONTEXT_AND_RECOVERY.md).
- Maintenance: Tailscale MCP and account connections are kept by one job with a stable id (`j-bridge-maintenance`) that records an event only when its result changes; the Funnel route is applied only when it is missing or changed.

## Known limitations
- Notion concurrency per account is not confirmed; local limits are configurable, observed rate limits are temporary. Parallel turns on one account can end with `temporarily-unavailable`; they are retried.
- `soft` cleanup is logical: older messages stay in the server-side chat and the checkpoint leads the next turns. `full` (`isPartialTranscript:false`) hides older history from the model, but Welcome chats did not keep the new step reliably, so `soft` is the default and `full` falls back to `soft`.
- The handoff is delivered once with the first prompt after a cleanup.
- Simple-question detection is heuristic.
- Network binding is opt-in; use firewall/VPN.
- Model display names do not prove model availability. Requested Opus 5.5 is not proven to run on the test account (observed execution model: almond-croissant-low).
