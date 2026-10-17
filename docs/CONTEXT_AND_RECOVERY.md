# Context and recovery

Scope: how NotionTool replaces a chat's context and how it recovers after a restart. Every statement names the code it was checked against (file:line, checked on 2026-10-16 at about 18:25; notiontool/engine.py rechecked after its 18:24:25 edit). `notiontool/engine.py` is being edited by another batch at the same time, so its line numbers can shift. Points that could not be checked say UNKNOWN.

## 1. One context replacement mechanism: same Welcome chat, generations
- There is one entry point: `ChatContext.rotate()` (notiontool/chat_context.py:208).
- Token estimate: UTF-8 bytes / 3, rounded up (notiontool/chat_context.py:12-14). `usage()` counts only the current generation, from `context_base_seq` / `context_base_at` on (notiontool/chat_context.py:45-65). Limits default to 60000 tokens and 80 turns (notiontool/chat_context.py:64-65, notiontool/store.py:192).
- Below both limits, with no pending rotation, no `every_task` cleanup due and no Compress request, `rotate()` only stores `context_usage` and returns (notiontool/chat_context.py:213-216).
- Preconditions: the native chat must be idle (notiontool/chat_context.py:230-231), the worker may have no other unresolved assignment (notiontool/chat_context.py:232-235), and a cancel stops the rotation (notiontool/chat_context.py:244-245, 252-253).
- A durable `rotations` record holds the handoff (notiontool/chat_context.py:246-249). The handoff is built by `handoff()` (notiontool/chat_context.py:166-206) and capped at `context_handoff_chars` (default 24000; notiontool/chat_context.py:203-205, notiontool/store.py:192).
- If the adapter supports it, one summary turn runs in the same chat (notiontool/chat_context.py:250-251, 91-164). The native history is archived first (notiontool/chat_context.py:100, 67-89). An ambiguous send is never repeated: it is read back instead (notiontool/chat_context.py:134-137, 145-148). The JSON fields and the 20000-character cap are checked; on failure the original chat is kept (notiontool/chat_context.py:149-158).
- Replacement stays in the same chat: the worker's `generation` goes up by 1, the handoff is stored, the context base moves to the current sequence, and a `thread_generations` record is written with `successor` = the same `thread_id` and `mode='same_thread'` (notiontool/chat_context.py:266-277). Event `context_rotated` (notiontool/chat_context.py:278-281).
- The handoff leads the next prompt once (notiontool/notion.py:799-804) and is then marked `handoff_delivered` (notiontool/notion.py:928-932).
- Account refresh skips thread ids found in `thread_generations` (notiontool/notion.py:309, 317-318). Same-chat generation ids have the form `<thread_id>:<n>` (notiontool/chat_context.py:273), so by reading they never hide the current chat. Not covered by a test here: UNKNOWN.

## 2. Soft vs full (`context_replace_mode`)
- `full` only when the setting is exactly `full`; anything else is `soft` (notiontool/chat_context.py:257). The default is `soft` (notiontool/store.py:194). The settings API accepts only `soft` or `full` (notiontool/server.py:635-638).
- Soft: the verified checkpoint leads the next normal message and supersedes the earlier history (notiontool/chat_context.py:254-256).
- Full: the worker gets `context_reset=True` (notiontool/chat_context.py:270), and the next send sets `isPartialTranscript=False` (notiontool/notion.py:829-830).
- Fallback: if Notion verifiably does not accept the reset, the worker returns to `soft` with `handoff_delivered=False`, the task is retried with its attempt refunded, and event `context_reset_fallback` is logged (notiontool/engine.py:687-706).
- `full` end to end in a live run: UNKNOWN (not run; live calls are not allowed in this batch).

## 3. Main: only a Compress hint plus button (`main_context_policy`)
- For the Main (`purpose == 'main'`), without a pending rotation or Compress request and unless `main_context_policy == 'auto'` (default `hint`), `rotate()` sends nothing into the operator chat. It sets `context_hint`, logs `main_compress_hint`, and raises `RateLimited(10, free=True, scope='thread')`, so the task moves to a free coworker (notiontool/chat_context.py:217-228).
- The Compress button calls `POST /api/chat/compress`. This sets `compress_requested` and a reason (default "Context at about X of Y tokens"), logs `compress_requested` and answers `same_chat=True` (notiontool/server.py:674-688). The next `rotate()` then passes the checks (notiontool/chat_context.py:213, 217) and clears the flag (notiontool/chat_context.py:272).
- `main_context_policy` has no store default (notiontool/store.py:189-194) and no settings setter turned up; the only match in notiontool/*.py is notiontool/chat_context.py:218. How to set `auto`: UNKNOWN.
- How the button is drawn in the web UI: UNKNOWN (notiontool/web/app.js not read; another task is editing it).

## 4. Coworker auto-cleanup and `cleanup_blocker`
- Trigger: `coworker_fresh_context` is `threshold` by default (notiontool/store.py:194), using the token and turn limits above. With `every_task`, cleanup runs before a new task once the worker has turns and a previous task (notiontool/chat_context.py:211-212). The settings API accepts only these two values (notiontool/server.py:631-634).
- `cleanup_blocker()` (notiontool/chat_context.py:285-296) looks at the worker's other tasks (not the current one, not `__context__`; notiontool/chat_context.py:288). It blocks when a task is running, cancelling, uncertain, checkpoint or preparing, or is `remote_unknown` (notiontool/chat_context.py:290-291); when a done task has no stored result (notiontool/chat_context.py:292-293); or when a done task has no `finished` time (notiontool/chat_context.py:294-295).
- When blocked: event `coworker_cleanup_deferred`, `cleanup_state = 'deferred: <reason>'`, and the worker keeps its context (notiontool/chat_context.py:236-243).
- A coworker handoff holds only the current run's messages, with no native history or previous checkpoint (notiontool/chat_context.py:169-173, 194-197).
- The scheduler does not give a worker with a pending rotation for another, unfinished task a new task (notiontool/engine.py:470-473, 517-519).
- Least-recently-used choice of the next coworker: UNKNOWN (not checked in this pass).

## 5. Recovery after restart
- The engine calls `_restore()` at start-up (notiontool/engine.py:72).
- Running or cancelling tasks still in `submission_phase == 'preparing'` go back to `retry` (or `cancelled` if their run is cancelled), with the attempt refunded (notiontool/engine.py:76-82). All other running or cancelling tasks become `uncertain` with the error "Recovering the previous API request" (notiontool/engine.py:83-84).
- Workers with an uncertain or `remote_unknown` task become `recovering` and keep their lease. Other busy or recovering workers become `ready` with an empty lease (notiontool/engine.py:85-93).
- `_reconcile()` handles uncertain or `remote_unknown` tasks once `reconcile_at` has passed. A missing worker makes the task `blocked` (notiontool/engine.py:790-805).
- An inference that may have left is never resent: the task stays `uncertain` with backoff `min(60, 5*2^n)` s and becomes `blocked` + `remote_unknown` after `task_timeout`+120 s (notiontool/engine.py:709-718, 729-736).
- A half-done rotation survives a restart as a `rotations` record (notiontool/chat_context.py:210, 246-249). Its summary turn resumes through `summary_task_id` without a second send (notiontool/chat_context.py:96-99, 110, 136-137).
- An account refresh returns workers in `missing` or `error` without a lease to `ready` (notiontool/notion.py:322-323).
- After `ACCOUNT_BLOCKED`, a manual resume requeues only the tasks the pause stopped (notiontool/engine.py:289-301).
- A full restart end to end in this pass: UNKNOWN (no restart allowed).

## 6. Planned modules: projects and context_budget
Status: planned. Both files are being written by parallel tasks; their contents were not read for this document, so their actual APIs are UNKNOWN.
- `notiontool/projects.py` (planned): project records with their own id (create, rename, open, archive, switch) and per-project stores for goals, rules, decisions, artifacts, task results, summaries and open items, via the existing store API. Switching saves intermediate results and records `project_id` per task.
- Today a run's project is its `project_id` or, until projects exist, its `folder_id` (notiontool/store.py:146-154). How this connects to the new project record: UNKNOWN.
- `notiontool/context_budget.py` (planned): `build_context(store, task, budget_tokens)` returns text plus `context_manifest {sources:[{kind,id,sha256,tokens}], total_tokens, budget}` and includes only artifacts whose `project_id` matches the task. It reuses the token estimate (notiontool/chat_context.py:12-14, 45-65). Not wired into notiontool/engine.py yet (planned).
- Shared planner rule (planned, UNKNOWN in code): artifacts live under the store kind `artifacts`, and every record has a `project_id` field.

## 7. Earlier notes (kept from the previous version, not rechecked: UNKNOWN)
- Observed live on the test account (2026-10-15): `isPartialTranscript:false` hid older history from the model (codeword ZEBRA-41 answered with "NONE"). In Welcome chats a full replacement was not kept reliably (older user messages re-committed, onboarding prompt inserted, new step dropped: "Record had no final space"), which is why `soft` is the default.
- Deleting on the server does not remove older messages from the chat; a replacement changes only what the model sees.
- Turn errors: status `running`, `missing`, `error` or `completed` is read from the server record; `error` gives `remote_turn_failed` and a retry after 20 s; a turn that produced output is never resent automatically; a failed checkpoint turn is reset before the next task.
- Run context: the last 18000 characters of chat history plus `Project memory:` (last 12000 characters of findings not already quoted); the reviewer does not get it.
- Live runs r9a60d0dfe6464127b1f1b2265719d102, r8eca0aedcb6549f893461e9e2c7f6c3c, r3cfe6eb5137a41fe848ef10365dfb545 and rf80dc076de9a43659cb9dd9453e65a2e; not yet observed live: the busy-chat wait and `context_replace_mode=full` end to end.
