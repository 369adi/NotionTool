# Token calibration (Rank 9, steps 2-3)

Question: does Notion (stream or stored records) give a real token count per turn, so that the
`estimate_tokens` divisor can be fitted per content kind (code, prose, JSON)?

**Answer: no. 0 of 1,140 stored turns carry a real token count. Step 3 was not done and no code was changed.**

## Method

- Data: a copy of `data/notiontool.db` (db, `-wal` and `-shm` copied together; the live file was never opened),
  taken 2026-10-23 18:16 (Europe/Berlin). The copy was opened read-only.
- Scanned every object kind (31 kinds) and the last 2,000 event rows for any key whose name contains `token` or
  `usage`, up to depth 6, up to 400 newest documents per kind.
- Read the stream and record parsing in the code (`notiontool/notion.py`, `notiontool/vendor/transport.py`,
  `notiontool/edge_transport.py`, `notiontool/fake_notion.py`) and searched all of `notiontool/` and `*.md` for
  `usage`, `inputTokens`, `outputTokens`, `input_tokens`, `output_tokens`, `prompt_tokens`, `completion_tokens`,
  `totalTokens`, `tokenCount`, `tokens_used`, `cacheRead`, `contextWindow`.

## What the stored turns contain

Turns in the DB: 1,234 `tasks` records, of which 1,140 have both `submitted_prompt` and `raw_response` (text only).

Thread entities (`thread_context`, 12 records, 2,218 entities) have only three types and these keys:

| Entity type | Count | Keys |
| --- | --- | --- |
| `tool` | 2,035 | `id`, `created_at`, `source`, `sequence`, `kind`, `tool_use_id`, `name`, `presentation_kind`, `text`, `result`, `icon_name`, `payload` (`type`, `server_name`, `tool_name`, `input`), `started_at`, `interaction_status` |
| `assistant_message` | 99 | `id`, `created_at`, `source`, `sequence`, `kind`, `content` |
| `user_message` | 84 | `id`, `created_at`, `source`, `sequence`, `kind`, `text`, `client_message_id`, `is_current_user`, `user_message_kind`, `files` |

Task records carry timing and state keys (`sent_at`, `first_output_at`, `remote_done_at`, `attempt_log`, ...) but no
token or usage key. Events `remote_done` and `stream_turn_done` carry only `outcome`, `lines`, `seconds`, `idle_s`.

## Every `token` / `usage` field that exists, and where it comes from

None of these is a count reported by Notion:

| Field | Where | Source |
| --- | --- | --- |
| `estimated_tokens`, `native_tokens` | `thread_context`, `workers.context_usage`, events `context_switch` / `context_rotated` | `estimate_tokens` itself (bytes / divisor) |
| `usage.windows.*.prompt_tokens`, `context_tokens`, `estimated_tokens` | `ai_disable_reports` | `prompt_chars // 3` plus the estimate |
| `tokens`, `input`, `output` | `token_budget`, `token_ledger`, `token_rate` | `token_budget.charge()` fed with `tokens_of(text)`, again an estimate |
| `token`, `handoff_token`, `assignment_token` | handoff and assignment records | ack and auth tokens, unrelated to LLM tokens |
| `context_token_limit`, `switch_*_tokens`, `main_context_ask_tokens` | `settings` | configuration values |

Stream parsing (`notion.stream_text` and the `runInferenceTranscript` NDJSON reader) reads text and tool steps only;
no usage, token or credit field is read or stored anywhere.

## Consequences

- Real-token pairs available: 0 turns (needed: at least 200). Step 3 (fit a divisor per kind, setting
  `token_calibration`) is skipped by the rule in the plan. Median error before/after cannot be reported because
  there is no ground truth; fitting against another estimate would only measure itself.
- No change to `chat_context.py`, `context_budget.py` or `store.py`. The helper functions from step 1
  (`content_kind`, `fit_divisors`, `calibrated_tokens`, `calibration_error`) stay unused until real counts exist.

## What could change this

1. If the Notion stream carries a usage object in some step type, it is not in anything stored today: the stored
   entities are filtered to the three types above. A one-off capture of full raw NDJSON lines of one turn
   (`nt_notion_stream`, owner connection) would show it. That sends a real message, so it was not done in this task.
2. If such a field exists, store it next to `raw_response` on the task (for example `real_tokens_in`,
   `real_tokens_out`) and collect 200 turns, then run `fit_divisors` on one week of pairs and ship it behind
   `token_calibration` (default off) as planned.
