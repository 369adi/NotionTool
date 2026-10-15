# Context and recovery in Welcome chats

Applies to the single active account. NotionTool uses only Welcome chats (1 Main + 5 Coworkers) and never creates a replacement chat for context cleanup.

## Four context levels
1. Server chat record (`thread`, `thread_message`): every committed step stays on Notion's side. NotionTool deletes nothing there and uses no delete call.
2. Turn transcript (`runInferenceTranscript`): `isPartialTranscript:true` = the server appends the new step to the stored history; `isPartialTranscript:false` = the sent transcript (config + checkpoint + new step) replaces the history the model sees in this turn.
3. Model view: what the model sees in a turn = level 2 plus server-side additions (Welcome configuration; in Welcome chats also an onboarding prompt).
4. Local worker state: estimated tokens and turns since the context base (`context_base_seq`, `context_base_at`), generation, checkpoint summary, handoff and `handoff_delivered`.

## Observed live (test account)
- `isPartialTranscript:false` hides older history from the model: a codeword stored earlier in the same chat (ZEBRA-41) was answered with "NONE" after the replacement.
- In Welcome chats a full replacement send was not kept reliably: the server re-committed older user messages, inserted the onboarding prompt and dropped the new step ("Record had no final space"). Therefore `context_replace_mode=soft` is the default, and `full` falls back to `soft` when the server does not accept it.
- Deleting on the server: older messages stay visible in the chat. A replacement only changes what the model sees, not the stored record.

## Cleanup in the same chat
- Checkpoint: a summary turn in the same Welcome chat (goal, done work, decisions, open steps, critical facts). The context base moves to the checkpoint; the next prompt carries the handoff once.
- Mains: yellow Compress button with a reason (for example "Context at about X of Y tokens"); the checkpoint runs with the next message; same chat id.
- Coworkers: `coworker_fresh_context=threshold` (default; at `context_token_limit` 60000 or `context_turn_limit` 80) or `every_task` (before every new task). The least recently used free Coworker gets the next task. Cleanup waits for confirmed handoffs and for running or unconfirmed work (`coworker_cleanup_deferred`).

## Turn errors and retries
- The reported execution model is compared with the requested model. A different model is a non-blocking `model_fallback` (result kept, hint shown).
- Turn status is read from the server record: `running`, `missing`, `error` (Notion wrote an error step, for example `temporarily-unavailable`, and the turn produced no output) or `completed`. Notion writes the error step a few seconds after the stream ends.
- `error` -> event `remote_turn_failed`; the task is retried after a 20 s cooldown and a parked run resumes. A turn that produced any output is never resent automatically.
- A failed checkpoint turn is reset (`turn_error`, not sent), so a new checkpoint is written before the next task on that worker.
- Busy chat: while Notion is still finishing the previous turn, nothing is sent; waiting does not use an attempt.
- `POST /api/run/retry {id}` resumes a blocked or failed run; finished tasks are kept, blocked tasks can move to another Coworker.

## Run context and project memory
- Every run gets a run context: the last 18000 characters of the chat history, followed by `Project memory:` with the findings that are not already quoted in that history (the last 12000 characters of them).
- A memory entry that repeats an earlier answer word for word, or its first 6000 characters (what the memory stores), is left out as a whole entry; findings that only quote part of an answer stay. Without remaining findings there is no `Project memory:` label, and a fresh chat gets an empty context.
- The run context reaches the direct, adviser, planner, integrator and executor (Coworker) prompts; the reviewer does not get it.

## Verification state (2026-10-15)
- Unit tests: 192 passed (23:55, after the project cleanup).
- Live: run r9a60d0dfe6464127b1f1b2265719d102 (6 parallel tasks, resumed with retry, done; a live integrator turn error was retried automatically) and run r8eca0aedcb6549f893461e9e2c7f6c3c (a failed checkpoint turn in Coworker 1 was detected and reset; the task finished on another Coworker). Same-chat cleanups on Coworkers 2, 4 and 5. No new chats were created.
- Live: run r3cfe6eb5137a41fe848ef10365dfb545 (3 Coworker parts): Coworker 1 wrote its new checkpoint once in the same Welcome chat before its task (`context_rotated`); a reviewer turn error (`temporarily-unavailable`) was resent automatically on another Coworker and passed.
- Live: run rf80dc076de9a43659cb9dd9453e65a2e (DEDUPE-OK): run context 2679 characters, each of the five earlier answers quoted once, no `Project memory:` label.
- Not yet observed live: the busy-chat wait, `context_replace_mode=full` end to end.
