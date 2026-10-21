# Context-switch bench (Coworkers)

Question: when should a Coworker move to a new Welcome chat with a handoff instead of continuing its long chat?
Script: `bench/context_switch.py` (stdlib only, builds on `bench/suite.py`, `bench/offline_client.py`, `bench/run_bench.py`, `bench/metrics.py`).

## Commands

```
python bench/context_switch.py db [--db PATH] [--raw-per-cell 6] [--switch-limit 40]
python bench/context_switch.py bench --client bench.context_switch:offline_client --account offline --reps 3 --out work/bench/context_switch.jsonl --report
python bench/context_switch.py analyze work/bench/context_switch.jsonl
python bench/context_switch.py selftest
```

`db` opens `data/notiontool.db` only with the sqlite URI `mode=ro` and never writes.

## A. Evaluation of existing data (`db`)

Per Coworker task (tasks that are not Main and not `use_main`):

- tokens before sending: sum of `estimate_tokens(submitted_prompt + "\n" + raw_response)` of the earlier tasks on the same `thread_id`, reset at a `same_thread` generation (same rule as `chat_context.estimate_tokens`, ceil(utf-8 bytes / 3)); tokens at send = that + the prompt
- TTFO = `first_output_at - sent_at`; turn = `remote_done_at - sent_at`; pre-send = `attempt_log[-1].sent - started`; task total = `finished - attempt_log[0].started`
- turn errors = attempts with an `error_class` (or `remote_error`/failed without a log); retries = `attempts - 1`
- variants: `long` (earlier turns in the chat), `first` (first turn, no handoff), `fresh` (first turn after a switch, level = size of the old chat)

Per switch (`thread_generations` with mode `new_welcome`): extra pre-send time of the first task in the new chat over a normal turn (rotation + `create_welcome`), handoff prompt to first output and to end of turn, old chat size, handoff and checkpoint size. Also `presend_timing` events (only the newest rows survive pruning) and Main handoff prompt to ack.

## B. Fixed bench task `cs_recall_sum`

Files: `numbers.txt` with 12 fixed integers. The prompt asks for `answer.json` with `count`, `sum` and the project `codeword` that was given earlier in the chat (`PRIMER`, codeword `AMBER-4417`). The codeword is only in the earlier context or in the handoff, so a missing handoff fact is visible.

Checklist (quality = passed / 5):

| ID | Check |
|---|---|
| Q1 | answer.json is one JSON object with exactly count, sum, codeword |
| Q2 | count = 12 |
| Q3 | sum = 454 |
| Q4 | codeword = AMBER-4417 (context kept; false = context missing) |
| Q5 | numbers.txt unchanged, no other files |

Levels 50k, 200k, 500k, 1M, 2M tokens; per level and repetition one `long` run (`send_b0`, metrics variant `B0`) and one `fresh` run directly after a switch (`send_harness`, variant `final`). Every row passes `metrics.validate_row` and adds `level_tokens`, `cs_variant`, `context_tokens`, `switch_s`, `ttfo_s`, `turn_s`, `errors`, `retries`, `checklist`, `quality`, `context_missing`, `run_ref`, `mocked`.

Client interface: optional `prepare(level_tokens, variant)` -> `{context_tokens, switch_s, run_ref}`, then `send_b0` / `send_harness` and `wait_done` -> `{accepted, checklist, ttfo_s, turn_s, errors, retries, run_ref}`.

- `offline_client` is MOCKED: no model, no Notion. Long = primer + neutral filler at the level; fresh = simulated handoff. Times are local script times and say nothing about the model.
- `offline_control_client` drops the codeword from the handoff: every fresh run must fail Q4 (checked by `selftest`).
- Live: `run_cs_bench` refuses 971f (`run_bench.check_account`) and refuses every non-mocked client unless the account starts with `d84a`. No live client is built in (`live_client` raises): a live client must drive a Coworker younger than 23 h on d84a through NotionTool, fill its chat to the level, record each run as a task in `data/notiontool.db` and return `run_ref` = task id.

## C. Curves and break-even

- per level and variant: median and IQR of TTFO and turn time, error share, quality, context-missing share
- P(L) = median turn long - median turn fresh; C_switch = median switch time (+ handoff overhead in A)
- switch when P(T) * N_remaining > C_switch, so `T* = T0 + C_switch / (N_remaining * (b + e * R))` with b = turn-time slope (s/token), e = error-share slope (1/token), R = seconds lost per failed turn, T0 = context after a switch
- slopes come with a 90% bootstrap interval; a non-positive slope means a switch never pays off on time

## Limits

- token estimate is local (prompt + reply); tool traffic inside a turn is not counted, so real chat size is larger
- the events table is pruned, so per-switch detail is reconstructed from tasks and `thread_generations`
- historical data is observational: task type and account change together with chat size (confounding)

# Results 2026-10-22 (task tfae6c949f85c46d69d66c6215f00e7bb, run rc2ae3bbd7f81428cad3793c21b319631)

Status: partial. The result file in data/results could not be written: nt_write_file refuses the NotionTool data folder. This section is the full result.
Live measurement on hg/d84a: 0 runs (see R5). The 5 x 2 x 3 raw table (R3) comes from the MOCKED offline client.

## R1. Existing data (notiontool.db, mode=ro)

- 936 Coworker tasks with a send time, 2026-10-15 18:08 to 2026-10-21 23:17 UTC (2ed5=557, 971f=308 read only, d84a=59, 06e0=11, cbde=1); new_welcome generations 50, same_thread 42; context_rotated 90.
- No Coworker chat got above about 150k estimated tokens. The 500k/1M/2M levels have no history.

| Level | Variant | n | tokens at send | TTFO s med [IQR] | turn s med [IQR] | task total s | turns with error | retried | done |
|---|---|---|---|---|---|---|---|---|---|
| 50k | long | 740 | 27k | 52.4 [22.8-126.9] | 81.1 [34.9-191.0] | 120.6 | 0.15 | 0.03 | 0.96 |
| 50k | first | 55 | 1k | 48.5 [25.8-113.1] | 90.3 [37.1-223.9] | 111.8 | 0.15 | 0.02 | 0.89 |
| 50k | fresh | 89 | 11k | 109.8 [54.8-155.1] | 132.8 [78.5-226.4] | 188.6 | 0.29 | 0.01 | 0.96 |
| 200k | long | 51 | 141k | 40.2 [11.5-62.5] | 58.3 [41.8-83.3] | 78.3 | 0.45 | 0.04 | 0.92 |
| 200k | fresh | 1 | 9k | 286.3 | 303.8 | 336.2 | 1.00 | 0 | 0 |

Fits (long + first): turn time -44.3 s/100k (90% -65.8 to -24.0, n=730); TTFO -22.7 s/100k (90% -42.1 to -2.1); error share +0.158/100k (90% +0.103 to +0.221, n=846).
The negative time slope is confounding: the larger chats are short check turns, mostly run r9ef733ebc6cb484d898bfc549c5e02ea. error_class also appears on attempts that ended done, so the error share is an upper bound. Up to about 150k there is no measurable time penalty.

Raw rows with IDs, newest 3 per cell (task / run / acct / tokens / TTFO / turn / errors / state):

- 50k long: te8a8591b4c9043a3928306a2ce00c5f4 / re0b7e6b8f8d34b678abbfcdf4dce4546 / 06e0 / 1k / 123.4 / 126.8 / 5 / done; tbe0eb744a93c43b5815a0edb89398d1a / rc2ae3bb... / 06e0 / 1k / 35.5 / 42.8 / 5 / done; t8e7fd252ab284cb79a3b83df3ce2a72b / rc2ae3bb... / 06e0 / 5k / 189.8 / 206.2 / 6 / done
- 50k fresh: t7c2784a5ecc046708bbe396d05be8990 / ree80b0a296f642f3acf3cdc6116896c1 / 2ed5 / 199.5 / 461.7; t72ba918fe53b48649e2117402a67d8b3 / r9b62efb2e4c9404a8eb9795174d8fa1f / 55.4 / 91.6; tad0120303479465183c0b50d63d3d1c5 / ree80b0a... / 138.5 / 456.5
- 200k long: t5abad4cb9fec4468937873488e344b93, tb1724adfc2204b03800f8725d8ea3eae (35.6 / 181.3), t0e34f872ef7f49fab58cb28100ce426e (81.3 / 83.3), all run r9ef733ebc6cb484d898bfc549c5e02ea, 116k-123k
- 200k fresh: t05591fd83f4e4436a1dbe8931872fb54 / r47d173a4364b45d89521c80dfed11b25 / 971f / 286.3 / 303.8 / failed

## R2. Switch costs

| Component | median [IQR] | n |
|---|---|---|
| pre-send normal turn | 6.3 s | - |
| pre-send first task in the new chat (context_rotated + create_welcome) | 19.3 [15.3-23.9] s | 50 |
| extra per switch | 13.0 [9.0-17.6] s | 50 |
| create_welcome Main (/api/state operations log) | 19.5 [16.0-25.3] s, 14.1-42.7 | 12 |
| handoff prompt to first output | 117.5 [56.8-163.5] s (without handoff 48.5) | 41 |
| handoff prompt to end of turn | 139.4 [88.0-237.8] s (without handoff 90.3) | 47 |
| presend_timing.rotate | 1.73 [1.60-2.42] s | 16 |
| Main handoff to ack | 26.4 [23.0-29.2] s | 7 |

C_switch = 13.0 + 49.0 = 62 s (band about 10-165 s). The old chat at a switch has a median of 27k tokens, the handoff 12k and the checkpoint 3k.

## R3. Raw table 5 levels x 2 variants x 3 reps (MOCKED offline, work/bench/context_switch_offline.jsonl)

Task cs_recall_sum; checklist Q1 JSON shape, Q2 count=12, Q3 sum=454, Q4 codeword AMBER-4417, Q5 file unchanged.

| Level | rep | long run_ref | long ctx | long turn s | Q | fresh run_ref | switch s | fresh turn s | Q |
|---|---|---|---|---|---|---|---|---|---|
| 50k | 0 | offline-B0-1 | 50045 | 0.0066 | 5/5 | offline-final-2 | 0.0016 | 0.0087 | 5/5 |
| 50k | 1 | offline-B0-3 | 50045 | 0.0058 | 5/5 | offline-final-4 | 0.0009 | 0.0053 | 5/5 |
| 50k | 2 | offline-B0-5 | 50045 | 0.0058 | 5/5 | offline-final-6 | 0.0010 | 0.0053 | 5/5 |
| 200k | 0 | offline-B0-7 | 200047 | 0.0065 | 5/5 | offline-final-8 | 0.0038 | 0.0063 | 5/5 |
| 200k | 1 | offline-B0-9 | 200047 | 0.0059 | 5/5 | offline-final-10 | 0.0042 | 0.0063 | 5/5 |
| 200k | 2 | offline-B0-11 | 200047 | 0.0057 | 5/5 | offline-final-12 | 0.0034 | 0.0059 | 5/5 |
| 500k | 0 | offline-B0-13 | 500053 | 0.0072 | 5/5 | offline-final-14 | 0.0114 | 0.0061 | 5/5 |
| 500k | 1 | offline-B0-15 | 500053 | 0.0081 | 5/5 | offline-final-16 | 0.0121 | 0.0064 | 5/5 |
| 500k | 2 | offline-B0-17 | 500053 | 0.0059 | 5/5 | offline-final-18 | 0.0122 | 0.2037 | 5/5 |
| 1M | 0 | offline-B0-19 | 1000050 | 0.0051 | 5/5 | offline-final-20 | 0.0224 | 0.0059 | 5/5 |
| 1M | 1 | offline-B0-21 | 1000050 | 0.0057 | 5/5 | offline-final-22 | 0.0205 | 0.0058 | 5/5 |
| 1M | 2 | offline-B0-23 | 1000050 | 0.0053 | 5/5 | offline-final-24 | 0.0203 | 0.0067 | 5/5 |
| 2M | 0 | offline-B0-25 | 2000045 | 0.2274 | 5/5 | offline-final-26 | 0.0447 | 0.0060 | 5/5 |
| 2M | 1 | offline-B0-27 | 2000045 | 0.0063 | 5/5 | offline-final-28 | 0.0581 | 0.0083 | 5/5 |
| 2M | 2 | offline-B0-29 | 2000045 | 0.0058 | 5/5 | offline-final-30 | 0.0432 | 0.0060 | 5/5 |

The control client (no codeword in the handoff) fails Q4 in 5/5 fresh runs, so the checklist catches missing context.

## R4. Break-even

T* = T0 + C_switch / (N_remaining x (b + e x R)), with C=62 s, T0=12k, R=60.4 s, b=-44 s/100k, e=+0.158/100k.

- Time only: b <= 0, so no break-even.
- Errors only (e x R = 9.5 s/100k): T* = 12k + 652k/N. That gives N=1 665k, N=3 230k, N=5 143k, N=10 77k, N=20 45k. Halving e doubles the term; the C band scales it by 0.16 to 2.7.
- Combined point estimate: -34.8 s/100k, so no break-even.

## R5. Live measurement not possible

hg/d84a (d84a51c708be46b78965f35089704b46) is not configured in the running NotionTool. Only cbdec17c and 06e03988 are configured, and the last d84a runs are from 2026-10-15. There is also no coworker tool to drive another Coworker chat to a set context level without delegating. 0 live runs, 0 DB entries. The script refuses non-mocked runs off d84a and refuses 971f.

## R6. Recommendation

1. Do not switch Coworkers below about 150k estimated tokens for speed reasons. The switch costs about 62 s plus more errors in the first turn, and there is no measured time penalty in that range.
2. Above that, switch only between tasks when tokens_at_send >= T0 + C_switch / (N_remaining x (b + e x R)). Use rolling DB values: C from thread_generations, b and e from bootstrap fits, N_remaining from queued tasks. If b + e x R <= 0 or its 90% interval includes 0, switch only at the hard limits.
3. Starting values until live data exists: C=62 s, T0=12k, e x R=9.5 s/100k, b=0. That gives thresholds of 665k (1 task left), 230k (3), 143k (5), but never below 150k. Keep the hard cap at 2M, the age limit at 22 h and the turn limit at 600.
4. Influencing factors: N_remaining, C_switch (Welcome 13-20 s, handoff +49 s), the error slope, the handoff size, and the account/model.

## R7. Limits

- The token estimate is local; tool traffic is missing, so real chats are larger.
- The data is observational and confounded, with no data above 150k. The error share is an upper bound.
- The events table is pruned (16 presend_timing rows). create_welcome comes from the /api/state log, not from the DB.
- R3 is mocked: its times are meaningless for the model and only prove the pipeline and checklist.
