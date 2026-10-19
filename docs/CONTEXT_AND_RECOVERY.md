# Context and recovery

Scope: how NotionTool replaces a chat's context, how unconfirmed tasks are resolved, and how runs are accepted. Checked against the code on 2026-10-19 01:05. Functions are named first; file:line references are as read at that time and can drift when the files change. `notiontool/engine.py` is being edited by another task, so re-check section 8 after that edit. Points not checked say UNKNOWN.

## 1. Context usage and the rotation entry point
- `estimate_tokens()` in `notiontool/chat_context.py`: UTF-8 bytes / 3, rounded up. `ChatContext.usage()` counts only the current generation (from `context_base_seq` / `context_base_at` on). Limits: `context_token_limit` (default 1200000) and `context_turn_limit` (default 600); the settings API (`action()` in `server.py`) clamps them.
- `ChatContext.rotate()` is the one entry point. It only stores `context_usage` and returns when there is no pending `rotations` record, no `every_task` cleanup due (`coworker_fresh_context`), no `compress_requested` flag, and both limits are below the threshold.
- Before replacing anything, `rotate()` requires an idle native chat and no other unresolved assignment on the worker. For coworkers, `cleanup_blocker()` defers the cleanup (event `coworker_cleanup_deferred`, `cleanup_state = 'deferred: ...'`) while an earlier task is not confirmed, has no stored result, or has no `finished` time.
- A durable `rotations` record holds the handoff text built by `ChatContext.handoff()`.

## 2. `handoff()`
- Builds a continuation checkpoint: goal, project directory, project memory, the last 10 user messages, all tasks of the run (state, files, acceptance, result excerpt, error), and a pointer to the context tool for full originals. Capped at `context_handoff_chars` (default 24000).
- For a coworker (`purpose == 'worker'`) only the current run's messages are included, with no native history and no previous checkpoint. Mains also get recent native history and their previous checkpoint.
- Coworkers get no model-written summary turn (event `coworker_checkpoint_skipped`). Only non-coworkers with an adapter that supports it run `semantic_checkpoint()` (one summary turn in the old chat, never resent on an ambiguous send, JSON fields and a 20000-character cap checked; on failure the original chat is kept).

## 3. Coworkers: new 'Welcome to Notion' chat (`fresh_welcome`, `retire_old`)
- When a coworker's context is over a limit, `every_task` cleanup is due, or a fresh context was requested, and `coworker_rotate_mode` is not `same_thread` (default `new_welcome`), `rotate()` calls `fresh_welcome()`.
- `fresh_welcome()` creates a new Welcome chat with `adapter.create_welcome()` (title `WELCOME_TITLE`, i.e. 'Welcome to Notion'; never a 'New Chat'), moves the worker there (new `thread_id`, `generation` + 1, handoff stored, `handoff_delivered=False`, `context_mode='soft'`), adds the old id to `retired_threads`, writes a `thread_generations` record with `mode='new_welcome'`, calls `retire_old()` and logs `context_rotated`.
- If the new chat cannot be created or is not listed, nothing moves: event `coworker_new_chat_failed` and the same-chat path below is used this time.
- `retire_old()` (chat_context.py:327-345) reads `coworker_delete_old_chat` (code default true; chat_context.py:330).
  - true **and** the adapter has a `delete_thread` endpoint: the old chat is deleted (event `coworker_old_chat_deleted`).
  - true without that endpoint, or the delete fails: the old chat is renamed 'Cowork (retired)' and kept (event `coworker_old_chat_kept`, note `no verified delete endpoint yet` or `delete failed: ...`).
  - false: no delete is tried. The old chat is renamed 'Cowork (retired)' (chat_context.py:340), stays out of the pool (its id is in `retired_threads`), and the event `coworker_old_chat_kept` says `<old chat>: kept by the setting coworker_delete_old_chat; excluded from the pool` (chat_context.py:331, :343-344).
  - A failed rename only adds `; rename failed: ...` to the note; the chat stays out of the pool either way.

## 4. Same chat (`same_thread` coworkers and Mains)
- `rotate()` keeps the chat: `generation` + 1, handoff stored, context base moved to the current sequence, `thread_generations` record with `successor` = same `thread_id` and `mode='same_thread'`, event `context_rotated`.
- `context_replace_mode`: `full` only when set exactly to `full` (the next send asks Notion to replace the context; a verified rejection falls back to `soft`); otherwise `soft` (default), where the checkpoint leads the next normal message.
- Handoff delivery (Phase 5b): for a coworker with `handoff_mode` `file` (default), `NotionAdapter.run` writes the checkpoint to `<data>/handoffs/<file_key(worker id)>.json` and the chat reads it and acks with a check word (notion.py:888-911; `handoff_files.write_handoff()`, `first_message()`, `verify_ack()`). Mains and any failure get the checkpoint inline (event `handoff_file_failed` ... `checkpoint is sent inline`, notion.py:912-915).
- File name: `handoff_files.file_key()` (handoff_files.py:48-59). Real worker ids look like `<account>:<thread>` (69 characters, with `:`), which `safe_worker_id()` rejects (`_SAFE_ID`, handoff_files.py:24: only letters, digits, `_`, `-`, `.`, at most 64 characters). A safe id is used as it is; any other id becomes its safe characters (others replaced by `_`, last 51 kept) + `_` + the first 12 hex characters of its sha256, so the name is at most 64 characters and two ids never share a file. notion.py uses `file_key` at all four handoff call sites: `handoff_path` (notion.py:895), `write_handoff` (:904), `mark_acked` (:1087), `mark_first_task_done` (:1088). Before this fix the handoff file always failed for real workers and the checkpoint went inline.

## 5. Main: hint only (`main_context_policy`)
- `main_context_policy` is `hint` (code default; no store default) or `auto`; the settings API accepts only these two values.
- With `hint`, a Main over its limit gets no checkpoint turn in the operator chat: `rotate()` sets `context_hint`, logs `main_compress_hint` and raises `RateLimited(..., free=True, scope='thread')` so the task moves to a free coworker. With `auto`, the Main follows the same-chat path.

## 6. `POST /api/chat/compress`
- With `worker_id` (in `action()` in `server.py`): only an enabled coworker in state `ready` is accepted. It sets `compress_requested` and a reason (default "Fresh context requested at about N tokens"), logs `compress_requested` and answers `same_chat` = (`coworker_rotate_mode == 'same_thread'`). Nothing is sent to Notion; the next `rotate()` for that coworker performs the switch.
- With `chat_id` (a Main): with policy `hint`, the request is cleared instead (Notion compacts the operator chat itself), a stale `rotations` record is dropped and event `main_compress_native` is logged; nothing is sent. With `auto`, `compress_requested` is set.

## 7. Unconfirmed tasks: `engine.resolve_uncertain()` via `POST /api/task/reconcile`
- With a `resolution` in the body, `action()` calls `Engine.resolve_uncertain(id, resolution, note)`. Allowed values: `accept` or `abandon`. The task must be `uncertain` or `remote_unknown`, Notion must have confirmed the turn finished (`remote_done_at`), and no reconciliation check may be in flight.
- `accept`: the task becomes `done` with a summary ending in `RESULT: DONE`. `abandon`: the task becomes `failed`. Either way the worker lease is released, `main_resolution` / `resolved_at` are stored, event `task_resolved_by_main` is logged, and **nothing is sent again**.
- Without a `resolution`, the same route only re-queues a check (`state='uncertain'`, `reconcile_at=0`); nothing is resent.

## 8. Three-stage acceptance (machine check -> Main -> done)
- Stage 1, machine check: `Engine._finish()` (engine.py:385-394) calls `_main_decisions()` (engine.py:363-383), which runs `_machine_check()` (engine.py:331-361) on every `done` task. The check is local and fast (no Notion, no network, no test runs): every file declared in the task's `files` must exist inside the project root; a path outside the project folder or a missing file fails. It runs as one `static` criterion through `done_definition.build_done_definition` and `verifier.run_verifier`; tasks without declared files pass. A failure returns `Machine check failed: ...` and is logged as event `machine_check_failed` (engine.py:388-389).
- `_main_decisions()` also flags tasks of an operator run whose answer has no `RESULT: DONE` line, and tasks with `requires_review`. Reasons are stored in `run['needs_main_reasons']`, machine failures in `run['machine_check_failed']`.
- Stage 2, Main: if any task is flagged, the run goes to `awaiting_main` with `needs_main` (event `awaiting_main`, engine.py:390-392). Nothing is retried or resent. If nothing is flagged, the run goes straight to `done` (engine.py:394).
- Stage 3, done: `POST /api/run/confirm {id}` (server.py:251, `confirm_run_request()` server.py:855, then `Engine.confirm_run()` engine.py:396-409) moves only `awaiting_main` to `done` and sets `confirmed_at` (events `run_confirmed`, `stage_changed`). Answers 404 for an unknown run, 409 if the run is not awaiting Main, 501 if the engine has no `confirm_run`.
- engine.py is being changed by another task (clearer `awaiting_main` summary); re-check the event text after that change.

## 9. Recovery after restart (not re-checked in this pass)
- `Engine._restore()` at start-up: tasks still `preparing` go back to `retry` with the attempt refunded; other running tasks become `uncertain`. `_reconcile()` then checks them; an inference that may have left is never resent. A half-done rotation survives as a `rotations` record. `resume_remote()` requeues only tasks stopped by `ACCOUNT_BLOCKED`. A full restart end to end: UNKNOWN.

## 10. Automatic turn resume uses the task's model (`turn_watch.py`)
- `TurnWatch.send()` (turn_watch.py:170-185) finds the worker's active task and, when `resume_takes_task()` (turn_watch.py:187-196) sees that the adapter's `continue_turn` accepts `task=` (or `**kwargs`), calls `continue_turn(worker, RESUME_TEXT, step, task=task)`. `NotionAdapter.continue_turn` then sends with the task's own model (task `model`, otherwise the account or template model). Adapters without `task=` get the old call without the task. A busy chat logs `turn_resume_skipped`, an error `turn_resume_failed`; nothing else is resent.

## 11. Stuck compression tasks in `/api/state`
- `/api/state` (snapshot, server.py:552) has the read-only field `stale_context_tasks` from `Application.stale_context()` (server.py:559-565): at most 20 items from `stale_tasks.stale_context_tasks(store)`, and `[]` on any error (it never raises). Each item: `id`, `state`, `title`, `worker_id`, `age_seconds`, `worker_state`. Listed are tasks of run `__context__` in state `checkpoint` older than 6 hours (time from `sent_at`, `started` or `created`) whose worker is missing, or ready without a lease that matches the task. Nothing is changed or cleaned up.

## 12. Settings used here (`POST /api/settings`)
- `coworker_rotate_mode`: `new_welcome` (code default) or `same_thread`; other values return 400 (server.py:1014-1017).
- `main_context_policy`: `hint` (code default) or `auto`; other values return 400 (server.py:1018-1021).
- `coworker_delete_old_chat`: boolean; the strings `true`, `1`, `on`, `yes` (any case) mean true, everything else false (server.py:1022-1024). Code default true (chat_context.py:330).
