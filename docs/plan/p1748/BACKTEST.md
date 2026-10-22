# p1748 - Backtest plan for the router (DIRECT vs TEAM)

Planning document only. Nothing here is built yet. It proves the router from `ROUTER.md` in three
steps: offline replay, live canary, quality comparison. A step only counts as passed when all its
numbers below are met. Names marked (R) must match what `ROUTER.md` finally defines; if they differ,
the builder maps them, the thresholds stay.

Sources read (read-only): `docs/BACKTEST.md`, `notiontool/canary.py`, `tests/test_desktop_push.py`,
`notiontool/desktop_state_cache.py`, `notiontool/store.py`, `notiontool/operator_roles.py`,
`data/notiontool.db` (opened as `file:data/notiontool.db?mode=ro`).

Token table used below: the merged token table from the redesign lead,
`docs/plan/p1748/REDESIGN-FINAL.md` (named in `PLAN.md`). Only if that file does not exist, use the
colour/token table in `docs/plan/p1748/REDESIGN.md`. The report always says which file was used.

## 0. What the history shows (baseline, 19 Oct 2026)

- 115 runs, 583 tasks. Task roles: executor 384, reviewer 61, planner 58, integrator 36,
  context_compactor 41, architect 3. There is no critic, scorer or lead role yet.
- Only 61 reviewer tasks for 115 runs: about half of all runs had no independent review.
- Big UI and design requests were often cancelled or redone (Studio v2, Studio v3 first try, desktop
  overlay polish, live progress first try). Clear fixes mostly finished in one run with 0-1 fix rounds.

This history explains why the router is wanted. It is NOT a router input (see 1.2).

## 1. Offline replay (labelled set)

### 1.1 Labels (from the goal text only)

- **DIRECT**: one clear, checkable change or answer; one build task plus one independent review is
  enough. Includes probes ("reply with exactly ...") and narrow follow-ups with a fixed scope.
- **TEAM**: the goal text asks for design or taste work (redesign, "looks terrible", "make it
  better"), names an unknown root cause or an incident, spans several subsystems or separate features,
  or asks for new architecture or a new process.
- **BORDER**: reasonable people disagree. Reported, not counted in accuracy.

The earlier rule "failed or cancelled when done by one builder" is removed: the router only sees the
goal text, so every label and reason is set from the goal text alone. Rows 20, 21, 29, 32 and 33 were
re-checked against that rule; their labels stay TEAM, with goal-text reasons.

Labels are frozen in this file and in the holdout file. The router author must not change them; a
disagreement is filed as a comment, not an edit.

### 1.2 Replay input and extraction query (exact, read-only)

The replay passes exactly one input per run to the router: the full `goal` text. No run state, fix
count, earlier attempts or task history are given.

```sql
-- python: sqlite3.connect('file:data/notiontool.db?mode=ro', uri=True)
SELECT id,
       json_extract(doc,'$.goal')     AS goal,
       json_extract(doc,'$.state')    AS state,
       json_extract(doc,'$.fixes')    AS fixes,
       json_extract(doc,'$.operator') AS operator,
       json_extract(doc,'$.created')  AS created
FROM objects WHERE kind='runs'
ORDER BY json_extract(doc,'$.created');
```

`state`, `fixes`, `operator` and `created` are read only for the report, never passed to the router.
The id in the tables is the first 12 characters of the run id; the replay matches on prefix.

### 1.3 Labelled set: 24 dev rows (shown) + 10 holdout rows (hidden) + 4 BORDER rows

Dev rows (the router author may read these):

| # | Run | Goal (short) | Label | Reason (goal text only) |
|---|---|---|---|---|
| 1 | r7ed3819360a2 | Fix the model-routing bug from MODEL_ROUTING.md | DIRECT | Bug already analysed, one code path |
| 2 | raeaad0287f5d | Bug: POST /api/account/team created a second Main | DIRECT | Named route and function, clear repro |
| 3 | reca1ab0c67d7 | tests.test_run_context fails (failures=3) | DIRECT | Failing test is the acceptance check |
| 4 | r8d887343034e | Allow screen capture for all coworkers in every role | DIRECT | One policy switch, decision already made |
| 5 | r1fda890edd9c | Name coworker chats 'Cowork 1', 'Cowork 2' | DIRECT | Small naming change, clear output |
| 6 | r3b90c3b11a5b | Two buttons in the account settings | DIRECT | Small, fully specified UI addition |
| 7 | r8b4686d03e9c | Write ONE missing part (Part B) | DIRECT | Scope fixed by the request |
| 8 | r7faf2e531e00 | Fix overlay banner 'no state update for 20 seconds' | DIRECT | One symptom, one code area |
| 9 | rba26b5cafa92 | Redo of op-fix-16 retiredchats after an inference error | DIRECT | Same task again, plan already given |
| 10 | rc7ecaeb2bf0b | Stop shell commands flagged by Windows Defender | DIRECT | Cause named, one guard |
| 11 | r3eaefeb82e33 | Tell the Main to continue when coworkers finish | DIRECT | One trigger, clear behaviour |
| 12 | r6d5523afeb12 | Read-only check of a suspected wrong key in worker_gc.py | DIRECT | One file, yes/no answer |
| 19 | r633c349f291c | Redesign the complete desktop UI | TEAM | Asks for a full redesign (taste, many screens) |
| 20 | rc79bcce01b1e | Studio v2: a product that no longer looks like Notion | TEAM | Open design direction across the whole app |
| 21 | rfdbd9d9c8267 | Studio v3: complete redesign of the desktop app | TEAM | Asks for a complete redesign |
| 22 | ra1eee1a1a63c | UI looks terrible and tabs do not work | TEAM | Taste complaint plus a functional bug |
| 23 | r0213ededa6aa | 'Make the UI better' should run through several roles | TEAM | Asks for a new process |
| 24 | r614963c314b1 | Live backtest rule for every harness feature | TEAM | New subsystem with several suites |
| 25 | r78db4800076b | Planning round: full feature parity old vs new | TEAM | Planning across many areas |
| 26 | rc60b56070d6d | Git, GitHub and patch mode instead of file locks | TEAM | Architecture change, many touch points |
| 27 | r9add8f26fc77 | Extras: proof per task, restart button, inbox, ... | TEAM | Six separate features |
| 28 | r8e6e70da41a8 | Scaffold the desktop app (Notion client + CDP overlay) | TEAM | New architecture |
| 29 | r34afe144a4f3 | Live progress of every coworker | TEAM | New data flow from engine to UI for every coworker |
| 30 | r2babc3559f13 | Incident: Notion chat created a Custom Agent | TEAM | Incident, root cause unknown, safety impact |

Holdout rows: **10 rows** (6 DIRECT, 4 TEAM; 3 of them `"probe": true`) in
`tests/data/route_labels_holdout.json`, written by the operator, not by the router author. Each row
has `run`, `goal`, `label`, `reason` and `probe`. The router author must not open that file; only
the replay test and the reviewer read it. sha256 of the file: **pending - the operator creates the
file and writes its sha256 here** (this task was not allowed to write it). The replay test refuses to
run while this value is missing or differs from the file's sha256.

BORDER rows (reported, not counted):

| # | Run | Goal (short) | Why border |
|---|---|---|---|
| 35 | ra9d4132c7182 | Full test suite has 29 failures | Clear check, but many possible causes |
| 36 | red5974a45ca5 | Review claw-code, list ideas for the harness | Research; one reader may be enough |
| 37 | r41fe1c6c8b78 | Replace the 'other chats are editing' text with a live check | Clear spec, but touches dispatch |
| 38 | r725c183409ef | Round 3 of the desktop overlay | Design follow-up with a fixed list |

Held out completely (never labelled, never scored, because they are the current requests):
r8b49dc6fefd6 (this router planning) and rb4e21a8a7f4f (classic features in the new app).

### 1.4 Metrics and pass/fail

| Metric | Pass |
|---|---|
| Extraction check (replay unit test) | the query returns >= 115 runs; each of the 38 run prefixes (24 dev + 10 holdout + 4 BORDER) matches exactly one run; every returned row has a non-empty goal |
| Holdout file integrity | sha256 of `tests/data/route_labels_holdout.json` equals the value in 1.3; the file has 10 rows, exactly 3 with `"probe": true` |
| Accuracy on the 34 labelled rows (dev + holdout) | >= 0.88 (at least 30 of 34) |
| Holdout accuracy (10 rows) | >= 0.80 (at least 8 of 10) |
| TEAM recall (all 16 TEAM rows) | >= 0.90 (at most 1 TEAM row sent DIRECT) |
| Redesign rows 19-22 sent DIRECT | 0 |
| DIRECT recall (all 18 DIRECT rows) | >= 0.85 (at most 2 DIRECT rows sent TEAM) |
| Probe rows in the holdout sent DIRECT (probe rows = the rows marked `"probe": true` in the holdout file) | 3 of 3 |
| Same goal twice gives the same route | 100 % |
| Every decision has a non-empty reason naming at least one signal (R: `route_reason`) | 100 % |
| Decision time per goal | < 50 ms if rule-based; < 60 s if a model is asked |

Output: `work/canary/report-<time>-replay.md` with the confusion matrix, every miss with goal and
reason (holdout misses show only the run prefix and the routes, not the label reason), and the
BORDER rows with the route chosen.

## 2. Live backtest with real open coworkers (canary suite `route`)

Follows `docs/BACKTEST.md`: the Main starts it after a restart, sandbox only
(`work/canary/route-<time>-<id>/`), never resend, a failure becomes a failed check, nothing is retried.

### 2.1 Steps

1. Run `python -m unittest tests.test_desktop_push` and check the real exit code (`$LASTEXITCODE` in
   PowerShell must be 0; text on the error stream does not count). Then run the `health` suite; it must
   pass. If either fails, stop; `route` does not start.
2. Prepare the sandbox: `calc.py` with a known bug (`add()` returns `a - b`) plus `test_calc.py` that
   fails, and `card.html` + `card.css`, a cluttered status card (gradient, glow, 11 px grey text,
   colour-only status, mixed 6/10/14 px gaps).
3. Request D (clear fix, auto route): "Fix: add() in calc.py returns a-b; test_calc fails. Make the
   test pass."
4. Request T (UI redesign, auto route), after D is done: "Redesign card.html: it looks cluttered and
   the status is hard to read. Make it clean, readable and accessible."
5. Request FD (forced DIRECT): the same redesign text as T on a fresh copy of the card, sent with the
   route override (R) `route=direct`.
6. Request FT (forced TEAM): the same fix text as D on a fresh copy of `calc.py`, sent with the route
   override (R) `route=team`.
7. Requests run one after another (never in parallel), each within its limit; then read run, tasks
   and events and check 2.2.
8. Run `python -m unittest tests.test_desktop_push` again; `$LASTEXITCODE` must be 0.
9. Write `work/canary/report-<time>-route.md`; remove the sandbox, keep the report.

Limits: D 15 min, T 45 min, FD 15 min, FT 45 min; total about 2 h.

### 2.2 Checks

Fix rounds: the harness allows up to 2 fix rounds (setting `max_fix_rounds`, default 2 in
`store.py`; operator path `operator_roles.MAX_FIX_ROUNDS = 2`). The checks use that limit.

Request D (auto route, clear fix):

| Check | Pass |
|---|---|
| Route | run route (R) = direct with a reason; exactly one `route_decided` (R) event |
| Tasks | exactly 1 executor task (plus fix tasks) and at least 1 reviewer task; 0 critic, plan, scorer or lead tasks |
| Independent review | reviewer worker differs from the executor worker |
| Fix rounds | at most 2 |
| Result | last review says REVIEW: PASS; our hidden test passes (`add(2,3)==5`, `add(-1,1)==0`) |
| Time | done within 15 min |

Request T (auto route, UI redesign):

| Check | Pass |
|---|---|
| Route | run route (R) = team with a reason; exactly one `route_decided` (R) event |
| Critique | at least 2 critic tasks by 2 different workers, each with a non-empty result |
| Plans / prompts | at least 2 plan tasks by 2 different workers |
| Scoring | every plan scored by at least 1 other worker; no worker scores its own plan; scores are numbers |
| Lead merge | exactly 1 lead task; started after all scoring tasks finished; result names the chosen plan(s) |
| Build | at least 1 executor task started after the lead task finished |
| Review and fix | reviewer differs from the builder; at most 2 fix rounds; last review PASS |
| Stage order | critique -> plans -> scoring -> lead -> build -> review, by first start time |
| Result | hidden checks on `card.css`: only hex values from the merged token table in `docs/plan/p1748/REDESIGN-FINAL.md` (fallback `REDESIGN.md` only if that file does not exist), all px spacing on the 4 px grid, no font size under 12 px, status has a text label, a focus style exists |
| Time | done within 45 min |

Forced routes (the quality suite in section 3 depends on this):

| Check | Pass |
|---|---|
| FD: design request forced to DIRECT | route = direct, the reason says it was forced; 0 critic, plan, scorer or lead tasks; 1 executor + reviewer as in D |
| FT: clear fix forced to TEAM | route = team, the reason says it was forced; every team stage of T ran (critique, plans, scoring, lead, build, review) in that order; hidden calc test passes |

All four runs (no hangs):

| Check | Pass |
|---|---|
| Run state | done (not blocked, failed or cancelled) |
| Every task finished | every task done; each result ends with a RESULT line |
| No stall | no running task without an event for more than 10 min; no stall-watch report |
| No false hang | no turn-watcher hang report (same check as `health`) |
| No resend | 0 resends; at most 1 cancel per run, and only on timeout |
| State push | from `StatePusher.stats.snapshot()['counters']` (`notiontool/desktop_state_cache.py`, read by `canary.counters_of`): `pushes` grows during each run, `heartbeats` grows by at least floor(elapsed_seconds / 6) during each run, `timeouts` and `failures` grow by 0. These are the counters tested in `test_send_only_on_change_and_heartbeat`, `test_slow_page_never_blocks_and_stale_pushes_drop` and `test_failed_push_is_counted_and_retried` |
| Push tests | `tests.test_desktop_push` passes before (step 1) and after (step 8) the suite, checked by exit code |
| Safety | 0 Notion agents created; no second Main |

The suite passes only if every line above passes.

## 3. Quality comparison (DIRECT vs TEAM on the same small UI task, suite `quality`)

### 3.1 Setup

- When it runs: only before the default route policy is switched (for example before UI requests
  default to TEAM), and again before any later policy switch. It is not part of the normal
  after-restart checks. It needs a passed `route` suite, including the forced-route lines, in the
  same harness version.
- Token source: the merged token table the redesign lead produces in
  `docs/plan/p1748/REDESIGN-FINAL.md` (named in `PLAN.md`; its dark colours come from the visual
  spec). Only if that file does not exist, use the colour/token table in `docs/plan/p1748/REDESIGN.md`.
  The suite copies the table into the sandbox and records the file name and its sha256 in the report.
  If neither table exists, the suite stops with a failed check.
- Three small UI tasks, each with a written spec using only those tokens: Q1 status badge (5 states),
  Q2 empty-state block, Q3 button set (primary, secondary, ghost, danger; all states).
- Each task runs twice in fresh sandbox copies: once forced DIRECT, once forced TEAM (override (R)
  `route=direct|team`). The order is random per task.
- Independent reviewer: a coworker that took part in neither run of that task. It gets both outputs
  as A and B (random labels), the spec and the rubric, and does not know which route made which.
- A second blind reviewer scores the same pair. If the two totals differ by more than 15 points, a
  third reviewer scores it and the two closest totals count.
- Expected cost: about 4 h in total (6 runs: about 15 min DIRECT and 45 min TEAM per task) plus 6-9
  reviewer turns (2 per task, up to 1 tie-breaker per task).

### 3.2 Rubric (100 points)

| Criterion | Points | How |
|---|---|---|
| Spec match | 25 | Layout, sizes and tokens as written |
| States complete | 20 | default, hover, focus, active, disabled (+ empty/loading/error where specified) |
| Accessibility | 20 | text contrast >= 4.5:1, visible focus ring, status not colour-only |
| Code quality | 15 | no duplicated rules, tokens used as variables, tests pass |
| Visual judgement | 20 | Clean, aligned, consistent |

Objective gate before scoring (both outputs): only hex values from the merged token table in
`docs/plan/p1748/REDESIGN-FINAL.md` (fallback `REDESIGN.md` only if that file does not exist), 4 px
grid, >= 12 px text. An output that fails the gate gets at most 50 points.

### 3.3 Pass/fail

| Metric | Pass |
|---|---|
| TEAM wins | TEAM scores higher in at least 2 of 3 tasks |
| Mean gain | mean (TEAM - DIRECT) >= 10 points |
| Floor | DIRECT scores >= 60 on every task |
| Cost | TEAM wall time <= 4x DIRECT per task |
| Reviewer agreement | final two reviewer totals within 15 points on every task |
| Budget | whole suite done within 5 h and at most 9 reviewer turns |

If TEAM wins by less than 10 points on average, this is reported as a finding: small UI tasks should
go DIRECT, and the router rule changes, not the threshold.

Output: `work/canary/report-<time>-quality.md` with both totals per task, per-criterion scores,
reviewer ids, wall times and the token file name and sha256.

## 4. Overall verdict

The router is DONE only when replay (1.4), live `route` (2.2) and, before a policy switch, `quality`
(3.3) all pass in the same harness version. Any failed line is reported as it is, with numbers.

## 5. Changes needed in other files (not done here)

- `tests/data/route_labels_holdout.json` (operator): create it with the 10 holdout rows (`run`,
  `goal`, `label`, `reason`, `probe`; exactly 3 with `probe: true`), then write its sha256 into 1.3.
- `notiontool/canary.py`: new suites `route` and `quality` (plans like `roles_plan`, checks like
  `check_roles`, reusing `stage_of`, `peak_overlap`, `counters_of`, `check_health`), CLI choices
  `--suite route|quality`, and the forced-route requests FD and FT.
- `tests/test_canary.py`: fake-harness tests for `check_route` and `check_quality` (pass and fail cases
  for every line above).
- New `notiontool/route_replay.py` plus `tests/data/route_labels_dev.json` (dev and BORDER rows from
  1.3) and a unit test that runs the extraction check, the sha256 and probe checks and the replay
  offline.
- `docs/BACKTEST.md`: add rows "Router, route rules, team stages -> `route`" and "Before switching the
  default route policy -> `quality`".
- `docs/plan/p1748/ROUTER.md`: must define the run fields `route` and `route_reason`, a
  `route_decided` event, a forced-route override for tests whose reason says "forced", and the task
  roles for critic, plan, scorer and lead (score stored as a number), or say which existing names map
  to them.
- `docs/plan/p1748/REDESIGN-FINAL.md` (redesign lead, named in `PLAN.md`): one merged colour/token
  table with hex values; the hidden CSS checks and the quality suite read it.

## 6. Before the merge

1. Run `git status --porcelain -uall` in the task copy. It must list `docs/plan/p1748/BACKTEST.md`.
   If it does not, save the file again in the task copy through the file tool.
2. Confirm the file is only in the task copy: `Test-Path <main>\docs\plan\p1748\BACKTEST.md` must be
   False and `git -C <main> status --porcelain -uall` must not list it (the file tool has saved it into
   the main folder twice).
