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

Status: partial (fix task t26df8bba281a4993b17ba15526016c5a, review t52d2ec4a244e45d6b1fdd05b2b6d8f01). R1 now holds the full A4 table from `db --raw-per-cell 3`. R3 is the B1 output of the offline run in work/bench/context_switch_offline.jsonl. R6 marks its assumptions.
Live measurement on hg/d84a: 0 runs (see R5). The 5 x 2 x 3 raw table (R3) comes from the MOCKED offline client. tests/test_bench_context_switch.py could not be added: the write was refused because the file is not in this task's file list.

## R1. Existing data (notiontool.db, mode=ro)

- Snapshot: `python bench/context_switch.py db --raw-per-cell 3` (mode=ro), run 2026-10-21 23:58 UTC. 997 tasks, 942 Coworker tasks with a send time, 2026-10-15 18:08 to 2026-10-21 23:56 UTC (2ed5=557, 971f=308 read only, d84a=59, 06e0=17, cbde=1); new_welcome generations 51, same_thread 42; context_rotated 90; remote_turn_failed 5.
- The earlier snapshot (2026-10-21 23:17 UTC, 936 Coworker tasks, 06e0=11, new_welcome 50) is the basis of R2 and R4. The differences are the 6 new 06e0 tasks of this fix run.
- No Coworker chat got above about 150k estimated tokens. The 500k/1M/2M levels have no history.

| Level | Variant | n | tokens at send | TTFO s med [IQR] | turn s med [IQR] | task total s | turns with error | retried | done |
|---|---|---|---|---|---|---|---|---|---|
| 50k | long | 743 | 27k | 52.6 [22.8-126.8] n=581 | 81.1 [34.9-191.0] n=641 | 120.0 [55.7-293.4] | 0.15 | 0.03 | 0.96 |
| 50k | first | 57 | 1k | 75.5 [26.6-145.7] n=37 | 110.9 [37.3-263.1] n=44 | 122.5 [42.3-353.1] | 0.14 | 0.02 | 0.93 |
| 50k | fresh | 90 | 11k | 109.8 [54.8-155.1] n=63 | 132.8 [78.5-226.4] n=72 | 188.6 [99.6-305.6] | 0.30 | 0.01 | 0.94 |
| 200k | long | 51 | 141k | 40.2 [11.5-62.5] n=46 | 58.3 [41.8-83.3] n=49 | 78.3 [63.8-184.4] | 0.45 | 0.04 | 0.92 |
| 200k | first | 0 | - | - | - | - | - | - | - |
| 200k | fresh | 1 | 9k | 286.3 n=1 | 303.8 n=1 | 336.2 | 1.00 | 0.00 | 0.00 |
| 500k / 1M / 2M | all | 0 | - | - | - | - | - | - | - |

Fits (long + first, 23:58 snapshot): turn time -46.2 s/100k (90% bootstrap -67.6 to -26.9, n=734); TTFO -25.0 s/100k (90% -44.0 to -2.9, n=664); error share +0.158/100k (90% +0.100 to +0.220, n=851). The 23:17 snapshot gave -44.3 s/100k, -22.7 s/100k and +0.158/100k.
The negative time slope is confounding: the larger chats are short check turns, mostly run r9ef733ebc6cb484d898bfc549c5e02ea. error_class also appears on attempts that ended done, so the error share is an upper bound. Up to about 150k there is no measurable time penalty.

Raw rows with full IDs: the A4 table from `db --raw-per-cell 3` (23:58 snapshot), pasted unchanged. These are the newest 3 per cell, so the rows of this fix run (06e0, run rc2ae3bbd7f81428cad3793c21b319631) now take the 50k long/first places. "-" means no first_output/turn end recorded (for example, the turn was still running at snapshot time).

| Level | Variant | task | run | acct | tokens at send | TTFO s | turn s | errors | retries | state |
|---|---|---|---|---|---|---|---|---|---|---|
| 50k | long | t0f7685bebbff4f3ab7cc41103abc0df2 | rc2ae3bbd7f81428cad3793c21b319631 | 06e0 | 5k | - | - | 1 | 0 | uncertain |
| 50k | long | t52d2ec4a244e45d6b1fdd05b2b6d8f01 | rc2ae3bbd7f81428cad3793c21b319631 | 06e0 | 3k | 79.8 | - | 0 | 0 | done |
| 50k | long | t02795384a87e407b9c9519bba4547820 | rc2ae3bbd7f81428cad3793c21b319631 | 06e0 | 8k | 55.7 | 65.3 | 0 | 0 | done |
| 50k | first | t31ea5fe888764110ae044fdeff1c29b0 | rc2ae3bbd7f81428cad3793c21b319631 | 06e0 | 2k | 470.6 | 498.3 | 0 | 0 | done |
| 50k | first | t558a50aed1a94142872d6c595aee9978 | rc2ae3bbd7f81428cad3793c21b319631 | 06e0 | 4k | 144.7 | 170.4 | 0 | 0 | done |
| 50k | first | t40f59511158f47b9948eae8248516a97 | rc2ae3bbd7f81428cad3793c21b319631 | 06e0 | 2k | 456.0 | 470.6 | 0 | 0 | done |
| 50k | fresh | t72ba918fe53b48649e2117402a67d8b3 | r9b62efb2e4c9404a8eb9795174d8fa1f | 2ed5 | 2k | 55.4 | 91.6 | 0 | 0 | done |
| 50k | fresh | tad0120303479465183c0b50d63d3d1c5 | ree80b0a296f642f3acf3cdc6116896c1 | 2ed5 | 3k | 138.5 | 456.5 | 0 | 0 | done |
| 50k | fresh | t26df8bba281a4993b17ba15526016c5a | rc2ae3bbd7f81428cad3793c21b319631 | 06e0 | 2k | - | - | 1 | 0 | uncertain |
| 200k | long | t5abad4cb9fec4468937873488e344b93 | r9ef733ebc6cb484d898bfc549c5e02ea | 2ed5 | 116k | - | - | 0 | 0 | done |
| 200k | long | tb1724adfc2204b03800f8725d8ea3eae | r9ef733ebc6cb484d898bfc549c5e02ea | 2ed5 | 120k | 35.6 | 181.3 | 0 | 0 | done |
| 200k | long | t0e34f872ef7f49fab58cb28100ce426e | r9ef733ebc6cb484d898bfc549c5e02ea | 2ed5 | 123k | 81.3 | 83.3 | 0 | 0 | done |
| 200k | fresh | t05591fd83f4e4436a1dbe8931872fb54 | r47d173a4364b45d89521c80dfed11b25 | 971f | 9k | 286.3 | 303.8 | 1 | 0 | failed |

Rows from the earlier snapshot that A4 no longer lists (full IDs): te8a8591b4c9043a3928306a2ce00c5f4 (run re0b7e6b8f8d34b678abbfcdf4dce4546), tbe0eb744a93c43b5815a0edb89398d1a and t8e7fd252ab284cb79a3b83df3ce2a72b (run rc2ae3bbd7f81428cad3793c21b319631), t7c2784a5ecc046708bbe396d05be8990 (run ree80b0a296f642f3acf3cdc6116896c1). To list them, run `db --raw-per-cell 6`.

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

C_switch = 13.2 + 28.4 = 41.6 s, with R = 61.7 s (23:58 UTC snapshot, see the note below). Older value (23:17 UTC snapshot, table above): C_switch = 13.0 + 49.0 = 62 s (band about 10-165 s). The old chat at a switch has a median of 27k tokens, the handoff 12k and the checkpoint 3k.

Snapshot note: these values come from the 23:17 UTC snapshot. The 23:58 UTC re-run (A2/A3) gives an extra pre-send of 13.2 [9.1-17.9] s (n=51) and handoff to end of turn 139.4 s. The first turn without handoff is now 110.9 s instead of 90.3 s, because of the 6 new 06e0 tasks. So C_switch = 13.2 + 28.4 = 41.6 s and R = 61.7 s. A handful of new tasks moves C_switch by about a third, so it is not a stable value.

## R3. Raw table 5 levels x 2 variants x 3 reps (MOCKED offline, work/bench/context_switch_offline.jsonl)

Task cs_recall_sum; checklist Q1 JSON shape, Q2 count=12, Q3 sum=454, Q4 codeword AMBER-4417, Q5 file unchanged.

Generated with `python bench/context_switch.py bench --client bench.context_switch:offline_client --account offline --reps 3 --out work/bench/context_switch_offline.jsonl --report` (exit 0, 30 rows written). The table below is the B1 section of that run, pasted unchanged, so run_ref, context tokens, turn and switch match the JSONL file. All rows are mocked: the times are local script times in seconds.

The JSONL file is not in git: `work/` is git-ignored (`git check-ignore -v work/bench/context_switch_offline.jsonl` -> `.gitignore:13:work/`). To regenerate it, run the same command again from the project root: `python bench/context_switch.py bench --client bench.context_switch:offline_client --account offline --reps 3 --out work/bench/context_switch_offline.jsonl --report` (options from `bench/context_switch.py` lines 844-849; without `--out` the script writes to its default `work/bench/context_switch.jsonl`, line 57). The run times are mocked, so new run times differ slightly from the table.

| Level | Variant | rep | run_ref | context tokens | TTFO s | turn s | switch s | Q1 | Q2 | Q3 | Q4 | Q5 | quality | accepted | mocked |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 50k | long | 0 | offline-B0-1 | 50045 | 0.0060 | 0.0060 | - | yes | yes | yes | yes | yes | 1.00 | yes | yes |
| 50k | fresh | 0 | offline-final-2 | 74 | 0.0050 | 0.0050 | 0.0009 | yes | yes | yes | yes | yes | 1.00 | yes | yes |
| 50k | long | 1 | offline-B0-3 | 50045 | 0.0091 | 0.0091 | - | yes | yes | yes | yes | yes | 1.00 | yes | yes |
| 50k | fresh | 1 | offline-final-4 | 74 | 0.0056 | 0.0056 | 0.0014 | yes | yes | yes | yes | yes | 1.00 | yes | yes |
| 50k | long | 2 | offline-B0-5 | 50045 | 0.0053 | 0.0053 | - | yes | yes | yes | yes | yes | 1.00 | yes | yes |
| 50k | fresh | 2 | offline-final-6 | 74 | 0.0047 | 0.0047 | 0.0008 | yes | yes | yes | yes | yes | 1.00 | yes | yes |
| 200k | long | 0 | offline-B0-7 | 200047 | 0.0050 | 0.0050 | - | yes | yes | yes | yes | yes | 1.00 | yes | yes |
| 200k | fresh | 0 | offline-final-8 | 74 | 0.0050 | 0.0050 | 0.0041 | yes | yes | yes | yes | yes | 1.00 | yes | yes |
| 200k | long | 1 | offline-B0-9 | 200047 | 0.0090 | 0.0090 | - | yes | yes | yes | yes | yes | 1.00 | yes | yes |
| 200k | fresh | 1 | offline-final-10 | 74 | 0.0073 | 0.0073 | 0.0038 | yes | yes | yes | yes | yes | 1.00 | yes | yes |
| 200k | long | 2 | offline-B0-11 | 200047 | 0.0056 | 0.0056 | - | yes | yes | yes | yes | yes | 1.00 | yes | yes |
| 200k | fresh | 2 | offline-final-12 | 74 | 0.0053 | 0.0053 | 0.0041 | yes | yes | yes | yes | yes | 1.00 | yes | yes |
| 500k | long | 0 | offline-B0-13 | 500053 | 0.0057 | 0.0057 | - | yes | yes | yes | yes | yes | 1.00 | yes | yes |
| 500k | fresh | 0 | offline-final-14 | 74 | 0.0058 | 0.0058 | 0.0095 | yes | yes | yes | yes | yes | 1.00 | yes | yes |
| 500k | long | 1 | offline-B0-15 | 500053 | 0.0113 | 0.0113 | - | yes | yes | yes | yes | yes | 1.00 | yes | yes |
| 500k | fresh | 1 | offline-final-16 | 74 | 0.0070 | 0.0070 | 0.0114 | yes | yes | yes | yes | yes | 1.00 | yes | yes |
| 500k | long | 2 | offline-B0-17 | 500053 | 0.0055 | 0.0055 | - | yes | yes | yes | yes | yes | 1.00 | yes | yes |
| 500k | fresh | 2 | offline-final-18 | 74 | 0.0096 | 0.0096 | 0.0132 | yes | yes | yes | yes | yes | 1.00 | yes | yes |
| 1000k | long | 0 | offline-B0-19 | 1000050 | 0.1151 | 0.1151 | - | yes | yes | yes | yes | yes | 1.00 | yes | yes |
| 1000k | fresh | 0 | offline-final-20 | 74 | 0.0062 | 0.0062 | 0.0208 | yes | yes | yes | yes | yes | 1.00 | yes | yes |
| 1000k | long | 1 | offline-B0-21 | 1000050 | 0.0067 | 0.0067 | - | yes | yes | yes | yes | yes | 1.00 | yes | yes |
| 1000k | fresh | 1 | offline-final-22 | 74 | 0.0065 | 0.0065 | 0.0220 | yes | yes | yes | yes | yes | 1.00 | yes | yes |
| 1000k | long | 2 | offline-B0-23 | 1000050 | 0.0062 | 0.0062 | - | yes | yes | yes | yes | yes | 1.00 | yes | yes |
| 1000k | fresh | 2 | offline-final-24 | 74 | 0.0070 | 0.0070 | 0.0222 | yes | yes | yes | yes | yes | 1.00 | yes | yes |
| 2000k | long | 0 | offline-B0-25 | 2000045 | 0.0058 | 0.0058 | - | yes | yes | yes | yes | yes | 1.00 | yes | yes |
| 2000k | fresh | 0 | offline-final-26 | 74 | 0.0066 | 0.0066 | 0.0457 | yes | yes | yes | yes | yes | 1.00 | yes | yes |
| 2000k | long | 1 | offline-B0-27 | 2000045 | 0.0059 | 0.0059 | - | yes | yes | yes | yes | yes | 1.00 | yes | yes |
| 2000k | fresh | 1 | offline-final-28 | 74 | 0.0056 | 0.0056 | 0.0428 | yes | yes | yes | yes | yes | 1.00 | yes | yes |
| 2000k | long | 2 | offline-B0-29 | 2000045 | 0.0054 | 0.0054 | - | yes | yes | yes | yes | yes | 1.00 | yes | yes |
| 2000k | fresh | 2 | offline-final-30 | 74 | 0.0064 | 0.0064 | 0.0450 | yes | yes | yes | yes | yes | 1.00 | yes | yes |

The B summary of the same run gives no break-even (P(L) slope -0.000049 s per 100k). This is expected for a mock and says nothing about the model.

The control client (no codeword in the handoff) fails Q4 in 5/5 fresh runs, so the checklist catches missing context.

## R4. Break-even

T* = T0 + C_switch / (N_remaining x (b + e x R)), with C=41.6 s (13.2 + 28.4), T0=12k, R=61.7 s, e=+0.1581/100k and b=-46.2 s/100k (23:58 UTC snapshot, R1/R2).

- Time only: b <= 0, so no break-even.
- Errors only (e x R = 0.1581 x 61.7 = 9.75 s/100k): T* = 12k + 427k/N (41.6 s / 9.75 s per 100k = 426.7k). That gives N=1 439k, N=3 154k, N=5 97k, N=10 55k. Halving e doubles the term.
- Combined point estimate: b + e x R = -46.2 + 9.75 = -36.4 s/100k, so no break-even.
- Older comparison (23:17 UTC snapshot: C=62 s, R=60.4 s, b=-44 s/100k, e x R = 9.5 s/100k): T* = 12k + 652k/N, i.e. older values N=1 665k, N=3 230k, N=5 143k, N=10 77k, N=20 45k; combined -34.8 s/100k, also no break-even. The older C band (about 10-165 s) scaled that term by 0.16 to 2.7.
- Every T* above about 150k is extrapolated beyond the measured range.

## R5. Live measurement not possible

Re-checked on 2026-10-22 (GET /api/state in the fix run): hg/d84a (d84a51c708be46b78965f35089704b46) is still not configured in the running NotionTool. The only configured account is 06e039884abc4fae80777591123c5b6c (Slot 2). d84a appears only in old runs (2026-10-15/16). The owner prerequisite "set up account hg/d84a" is therefore not met, so live_client was not built against it and no live raw table exists. No numbers in this document are live values. There is also no coworker tool to drive another Coworker chat to a set context level without delegating. 0 live runs, 0 DB entries. The script refuses non-mocked runs off d84a and refuses 971f.

## R6. Recommendation

1. Do not switch Coworkers below about 150k estimated tokens for speed reasons. The switch costs about 42 s (C_switch = 41.6 s; older value 62 s) plus more errors in the first turn, and there is no measured time penalty in that range. Until live data exists, the only measured result is: no slowdown up to about 150k tokens.
2. Above that, switch only between tasks when tokens_at_send >= T0 + C_switch / (N_remaining x (b + e x R)). Use rolling DB values: C from thread_generations, b and e from bootstrap fits, N_remaining from queued tasks. If b + e x R <= 0 or its 90% interval includes 0, switch only at the hard limits.
3. Starting values until live data exists: C=41.6 s, T0=12k, e x R=9.75 s/100k (e=0.1581/100k, R=61.7 s), **b=0 (assumption, not a measurement)**. b=0 is set because the measured turn-time slope is negative (-44 to -46 s/100k). That slope is skewed by the task mix: the large chats are mostly short check turns of run r9ef733ebc6cb484d898bfc549c5e02ea. With the measured b, the combined estimate b + e x R is negative, so it gives no break-even. Under b=0 the thresholds are T* = 12k + 427k/N = 439k (1 task left), 154k (3) and 97k (5), but never below 150k (so N=5 uses 150k). **439k and 154k are estimates beyond the measured range (at most about 150k tokens in the DB) and must be replaced once live data exists.** Older comparison (C=62 s, e x R=9.5 s/100k): 665k / 230k / 143k. Keep the hard cap at 2M, the age limit at 22 h and the turn limit at 600.
4. Influencing factors: N_remaining, C_switch (Welcome 13-20 s, handoff +28 s; older value +49 s), the error slope, the handoff size, and the account/model.

## R7. Limits

- The token estimate is local; tool traffic is missing, so real chats are larger.
- The data is observational and confounded, with no data above 150k. The error share is an upper bound.
- The events table is pruned (16 presend_timing rows). create_welcome comes from the /api/state log, not from the DB.
- R3 is mocked: its times are meaningless for the model and only prove the pipeline and checklist.

## Defaults 2026-10-22

Production defaults of the switch rule. Sources are lines of `docs/context_sweet_spot.md`:

| Value | Default | Source |
|---|---|---|
| C (switch cost) | 76 s | line 69: S = 141 - 64.9 = 76 s (76.4 s), measured |
| b (turn-time slope) | 3514 ms per task per 100k | line 86: fit A, 0.251 s per step per 100k; line 75: median of 14 steps per task, so 0.251 x 14 = 3.514 s |
| e (error slope) | 0 | not used in production; the measured +0.1581/100k (R1) is only an upper bound |
| floor (minimum tokens) | 400k | line 90: up to 400k the extra time per task stays at or below +3.5 s, far below 76 s |
| T0 (context after a switch) | 12k | handoff median 12k (R2) |
| R (seconds lost per failed turn) | 60 s | rounded from the measured R = 61.7 s (R2); not used while e = 0 |

Lines 87-88: fit B gives T* of about 265,235 tokens instead, and the fit swings 8x depending on how single huge tool results are counted, so it is not robust.

Switch points: T* = 12,000 + 76 s / (N x 3.514 s per 100k), at least the 400k floor and at most the 1,200,000-token hard limit:

| Tasks ahead N | formula | switch point |
|---|---|---|
| 1 | 2,174,778 | 1,200,000 (capped at the hard limit) |
| 3 | 732,926 | 732,926 |
| 5 | 444,556 | 444,556 |

These defaults are deliberately more conservative than the measured C_switch = 41.6 s: the higher C = 76 s, e = 0 and the 400k floor move every switch point later, so a Coworker only switches when the expected saving clearly outweighs the cost.
