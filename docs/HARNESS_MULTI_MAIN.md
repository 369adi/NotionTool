# Harness: Multi-Main, Coworkers, same-chat context

## Start
- EXE: `NotionTool.exe` (UI) or `NotionTool.exe --serve [--port 8790]` (host only).
- Source: `python main.py [--serve] [--port N]`.
- Settings (UI > Settings or settings store): `host_api_port` (8790), `host_api_allow_network` (false), `host_api_bind` (127.0.0.1), `coworker_fresh_context` (`threshold` | `every_task`), `context_replace_mode` (`soft` | `full`, default `soft`), `context_token_limit` (1200000), `context_turn_limit` (600), `max_runs_per_chat` (code default 3 in `Engine._runs_per_chat()` in notiontool/engine.py, clamped to 1-6, no store default; the live setting in this install is 5), existing parallel/Coworker limits.
- First start runs migration v1 -> v2: backup of the store, durable counters, Main numbers for existing chats. No account/session/chat/result data is removed.

## Existed before (kept)
Edge lean context + robots.txt tab on app.notion.com, direct `/api/v3` calls with streaming, transcript-based completion check, recover without blind resend, chat creation (Welcome/agent thread), worker leases, file locks, dependency gating, targeted retry, context rotation/checkpoints, message paging, MCP/local tools.

## Added / changed
- Multiple Mains ("Main 1, Main 2, ..."), durable concurrency-safe counter; `Create Main` = one durable operation per request_id via existing chat creation.
- Planner: focused/simple questions -> Main answers directly (no Coworker); larger work -> waves within capacity.
- Idempotent send (`request_id`).
- Only Welcome chats are used (1 Main + 5 Coworkers on the active account). Main context cleanup never creates a new chat: the checkpoint is written in the same Welcome chat and leads the next turns. A Coworker by default (`coworker_rotate_mode` = `new_welcome`) continues in a new Welcome chat and the old one is retired; with `same_thread` it stays in the same chat. Details: [CONTEXT_AND_RECOVERY.md](CONTEXT_AND_RECOVERY.md).
- Compress for Mains: yellow `Compress` button with a reason; `POST /api/chat/compress`; the checkpoint runs with the next message in the same chat and the chat id stays the same.
- Coworker cleanup: automatic (new Welcome chat by default, see above), at the context limits (`threshold`) or before every new task (`every_task`). Gate: only after a stored + confirmed handoff and no running/unconfirmed/recovery dependency (`coworker_cleanup_deferred` event otherwise).
- Continue in New Chat was removed (UI, route, operation, tests).
- Model fallback: if Notion reports a different execution model than requested, the result is kept, a `model_fallback` event is recorded, the account card shows a hint and the run continues.
- Turn errors: a Notion turn that ended with an error step before any output (for example `temporarily-unavailable`) is retried automatically after a cooldown; a turn that produced output is never resent automatically.
- Host API with bearer auth, localhost default, SSE stream, operations, request lookup.
- UI: Create Main, Compress, Coworker labels, Operations, Known limits (confirmed/observed/configured), host-unreachable banner, reconcile-before-resend, refresh without full re-render, settings for `context_replace_mode` and `coworker_fresh_context`, fallback model status, task-row labels and worker hints.
- Log redaction for bearer tokens, token_v2, cookies and login codes; numeric token counts (`tokens`, `token_limit`, `token_count`, `token_budget`) stay readable in event logs.
- Coworker scope: each executor answers only its own task (live run r3cfe6eb5137a41fe848ef10365dfb545: PART-OK 1-3, each from its own Coworker).
- Run context: `run.context` is always empty; old reports and chat history are never injected into prompts. Context lives on the operator's PC as handoff files (`store.directory / 'handoffs' / <worker>.json`, i.e. `data/handoffs/` next to `notiontool.db`; this is the intended location, not `work/handoffs`) and in project memory. Details: [CONTEXT_AND_RECOVERY.md](CONTEXT_AND_RECOVERY.md).
- Maintenance: Tailscale MCP and account connections are kept by one job with a stable id (`j-bridge-maintenance`) that records an event only when its result changes; the Funnel route is applied only when it is missing or changed.

## Known limitations
- Notion concurrency per account is not confirmed; local limits are configurable, observed rate limits are temporary. Parallel turns on one account can end with `temporarily-unavailable`; they are retried.
- `soft` cleanup is logical: older messages stay in the server-side chat and the checkpoint leads the next turns. `full` (`isPartialTranscript:false`) hides older history from the model, but Welcome chats did not keep the new step reliably, so `soft` is the default and `full` falls back to `soft`.
- Coworker handoff (`handoff_mode='file'`, the default): the checkpoint goes into the handoff file, and the first prompt in the chat tells the coworker to read it with the NotionTool `read_file` tool, reply with an 8-hex check word alone on one line, then do the current assignment. If the reply contains the check word, the file is deleted after that first task; if not, event `handoff_ack_missing` is logged and the file is deleted after 24 h. Mains, other modes or a failed file write get the checkpoint inline in front of the prompt. Either way it is delivered once (`handoff_delivered`). Details: [CONTEXT_AND_RECOVERY.md](CONTEXT_AND_RECOVERY.md).
- Simple-question detection is heuristic.
- Network binding is opt-in; use firewall/VPN.
- Model display names do not prove model availability. Requested Opus 5.5 is not proven to run on the test account (observed execution model: almond-croissant-low).
