# p1748 / ROUTER — automatic DIRECT vs TEAM routing for operator runs

Status: plan only, no code changed. Owner task t44a2ddb1990f4a4eaf7d65064dd8791d (fix rounds: tedc302a8d64e40abb12fb15948e1d509, t8c0207272d8047faaf240795a3bdacae).

## 0. Current state (what was read)

- `notiontool/operator_main.py:delegate` validates the operator plan, creates a run with
  `allocation=dict(mode='operator', advisers=0, ...)` and imports the tasks straight as executors.
- `notiontool/operator_roles.py` exists in the main project folder (it is not in the older task copy that was read first).
  There `operator_main.py:delegate` calls `operator_roles.py:choose_quality(goal, tasks, quality)` -> `fast|standard|high`,
  a keyword/regex heuristic that adds at most one designer/architect stage plus review/fix; `operator_roles.py:step`
  is called in `engine.py:tick`, next to the operator run handling. **The build starts from the main branch state.**
- The real role team only exists for non-operator runs: `engine.py:_approach` uses `allocation.py:allocation` ->
  `advisers` -> `architect / researcher / quality_planner` proposer tasks, then a planner.
- Reusable pure pieces, not wired for operator runs:
  - `role_split.py:split_roles` — spreads roles over free coworkers, builder never also a checker.
  - `role_split.py:role_sections_prompt`, `role_split.py:parse_role_sections`, `role_split.py:missing_roles` — `## As <role>:` sections.
  - `role_policy.py:eligible` — reviewer must not review its own executor part; read-only roles.
  - `work_queue.py:plan_assignments` — scheduling; `allocation.py:parallel_width` — wave width.
  - `delegation.py:validate_delegation` — limits/claims for nested plans.
  - `engine.py:_import_plan`, `engine.py:add_task`, `engine.py:cancel` — task creation and per-run cancel.

Root cause: the decision "how much team does this request need" is made by regex in one place (`choose_quality`) and the
team machinery is reachable only through `engine.py:_approach`, which operator runs skip.

## 1. Data model

New pure module `notiontool/router.py` (no I/O, no network; stdlib + project imports). One function:

```python
def route(goal, tasks, history, settings, free_coworkers, override=None, now=None) -> dict
```

Returned and stored as `run['route']` (JSON, persisted in the run record):

```json
{
  "version": 1,
  "mode": "direct" | "team",
  "source": "auto" | "main_override" | "forced_rule" | "middle_range" | "capacity_fallback",
  "score": 0-100,
  "threshold": {"team_at": 55, "direct_below": 35, "min_fail_gain": 0.18},
  "signals": [
    {"id": "ui", "value": 0.0-1.0, "weight": 15, "points": 15.0, "evidence": "files web/desktop/app.css; word 'redesign'"}
  ],
  "cost": {"direct_turns": 4, "team_turns": 13, "direct_minutes": 12, "team_minutes": 21},
  "gain": {"p_fail_direct": 0.40, "p_fail_team": 0.20, "fail_gain": 0.20,
           "expected_direct_turns": 9.9, "expected_team_turns": 15.5},
  "capacity": {"free": 4, "proposers": 4, "separate_lead": false, "waiting": false, "waiting_since": null},
  "reason": "TEAM: score 50 in middle range, failure gain 0.20 >= 0.18, team 13 turns <= 20",
  "roles": ["designer", "tester", "critic", "prompt_writer"],
  "max_fix_rounds": 2,
  "decided_at": 1792425051.9,
  "build_hold_until": 1792425081.9
}
```

Team progress is stored as `run['team']` (only for mode `team`):

```json
{
  "stage": "propose" | "score" | "merge" | "approve" | "build" | "review" | "fix" | "done",
  "proposals": {"<task_id>": {"role": "designer", "worker_id": "w..", "critique": "...", "plan": "..."}},
  "scores": {"<proposal_task_id>": {"by": {"<scorer_task_id>": 7}, "mean": 6.8}},
  "winner": "<proposal_task_id>",
  "lead_task": "<task_id>",
  "merged_plan_task": "<task_id>",
  "fix_round": 0
}
```

Every created task gets `stage` (`propose|score|merge|build|review|fix`), `route_mode` and a unique `step_id`
(`<stage>-<role or task>-<attempt>`), so the Live view and the backtest can group tasks without parsing prompts.
`history` input = list of finished runs `{id, goal, files, state, fixes, route, created}` read by the caller from the
run store (last 30 days, max 200).

## 2. Scoring formula

Eight signals, each `value` in 0..1, `points = value * weight`, `score = round(sum(points))`, weights sum to 100.

| id | weight | value |
| --- | --- | --- |
| ambiguity | 15 | 0.4 if no task has `acceptance`; +0.3 if goal has vague words (better, nicer, improve, clean up, schöner, besser, verbessern, aufräumen); +0.3 if goal has >1 `?` or no file and >60 words. Cap 1. |
| ui | 15 | 1.0 if any file under `notiontool/web/` or ending `.css/.html/.jsx/.tsx`, or words ui, ux, design, layout, redesign, look, oberfläche; 0.5 if only `.js`; else 0. "redesign" counts only here. |
| breadth | 15 | `min(1, (modules - 1) / 3)`. Module = the file stem for `notiontool/*.py` (`notiontool/engine.py` -> `engine`); the first subfolder for files under `notiontool/web/` (`notiontool/web/desktop/app.js` -> `web/desktop`); files under `tests/` and `docs/` are ignored; any other path counts by its first folder. |
| risk | 15 | max over tasks: task `risk` high = 1, medium = 0.5. A file in `core_files` (default engine.py, store.py, server.py, protocol.py, operator_main.py, bridge.py) sets 0.5, unless the task itself says risk high (then 1). |
| new_arch | 10 | 1.0 only for the words architecture, new module, migration, protocol, schema (architektur, neues modul, migration, protokoll, schema); +0.5 if a declared file does not exist yet. Cap 1. No other words count. |
| dissatisfaction | 10 | 1.0 for still, again, not working, broken, ugly, wrong, regression, noch immer, wieder, geht nicht, kaputt, hässlich, falsch; 0.5 for one weaker hint (why, warum). |
| past_failure | 10 | Similar run = Jaccard(file set) >= 0.4 or Jaccard(goal words minus stop words) >= 0.35. value = min(1, 0.5 * failed_similar + 0.25 * similar_with_fixes>=2). |
| task_count | 10 | `min(1, (len(tasks) - 1) / 4)`. |

All regexes are case-insensitive, English and German, and live as constants in `router.py` (not reused from
`allocation.py`, whose `NEEDS_PLAN` would match almost every coding request).

### Cost, failure cost and expected gain

Turns are coworker turns (one Notion AI answer). `n` = number of operator tasks, `R` = number of proposer roles.

- `direct_turns = 2 * n` (1 build + 1 review per task).
- `team_turns = R proposers + R scoring turns + 1 lead merge (0 when there is no separate lead) + 2 * n`.
- Failure probability (defaults, calibrated by BACKTEST.md): `p_fail_direct = 0.10 + 0.60 * score / 100`,
  `p_fail_team = p_fail_direct * 0.5`.
- Cost of one failed run, including the Main's rework and reading: `fail_cost_turns = a * n + b` with
  `a = router_fail_cost_a` (default 3) and `b = router_fail_cost_b` (default 4): fix + re-review per task + the Main
  reading the failure, rewriting the request and confirming.
- A second failure adds another review per task: `second_fail_cost = fail_cost_turns + n`.
- `expected_X_turns = X_turns + p_fail_X * fail_cost_turns + p_fail_X^2 * second_fail_cost` (shown in the UI and logged).
- `fail_gain = p_fail_direct - p_fail_team` (expected failures avoided by TEAM).
- Minutes = turns * `minutes_per_turn` (default 3) / parallel width (`allocation.py:parallel_width`, capped by free coworkers).

The middle range decides on expected failures, not on turns, because one failed DIRECT run costs the Main and the user
far more than coworker turns:

```
TEAM  if fail_gain >= min_fail_gain (0.18) and team_turns <= max_team_turns (20)
DIRECT otherwise
```

With the default formulas `fail_gain = 0.5 * p_fail_direct`, so the middle range picks TEAM from score 44 upward when the
turn cap holds.

### Worked example (n = 2 tasks, roles designer, tester, critic, prompt_writer: R = 4, 4 free coworkers)

`direct_turns = 4`, `team_turns = 4 + 4 + 1 + 4 = 13`, `fail_cost_turns = 3 * 2 + 4 = 10`, `second_fail_cost = 12`.

| score | p_fail_direct | p_fail_team | fail_gain | expected direct | expected team | result |
| --- | --- | --- | --- | --- | --- | --- |
| 50 | 0.40 | 0.20 | 0.20 >= 0.18 | 4 + 4.0 + 1.92 = 9.9 | 13 + 2.0 + 0.48 = 15.5 | **TEAM** (13 <= 20) |
| 40 | 0.34 | 0.17 | 0.17 < 0.18 | 4 + 3.4 + 1.39 = 8.8 | 13 + 1.7 + 0.35 = 15.0 | **DIRECT** |

Reason strings: `TEAM: score 50 in middle range, failure gain 0.20 >= 0.18, team 13 turns <= 20` and
`DIRECT: score 40 in middle range, failure gain 0.17 < 0.18`.

## 3. Thresholds and decision order

Step A picks the mode; step B (capacity) runs after **every** TEAM decision, whatever picked it.

A. Mode, first rule that applies:

1. **Main override** (section 6) -> `source=main_override`.
2. **Forced TEAM** (`forced_rule`): `new_arch >= 1` and `breadth >= 0.67`; or `dissatisfaction >= 1` and `past_failure > 0`.
3. **Forced DIRECT** (`forced_rule`): 1 task, <= 1 file, `risk <= 0.5`, `ui == 0`, `new_arch == 0`, acceptance present.
4. **Score**: `score >= team_at (55)` -> TEAM; `score < direct_below (35)` -> DIRECT.
5. **Middle range 35-54** (`middle_range`): the expected-failure rule from section 2.

B. Capacity check (free enabled coworkers of the operator's account), after every TEAM decision:

- **3 or more free**: TEAM, roles trimmed to fit (`R <= free`, at most `team_max_roles`). Trim order: prompt_writer,
  tester, then the lower-signal one of designer/architect; critic is never trimmed. The lead is a free non-proposer if
  one is left, else the coworker with the best-scored proposal.
- **Exactly 2 free**: TEAM with 2 proposers — critic and the top-signal role (ui -> designer, new_arch/breadth/risk ->
  architect, else tester) — and no separate lead: the coworker with the higher-scored proposal merges.
- **Fewer than 2 free**: DIRECT, `source=capacity_fallback`, reason names the missing count — except for a Main
  override: **the override sets the mode; capacity may delay the run but never changes the mode**. The run waits with
  `capacity.waiting=true` and logs `route_waiting`.
- **Waiting time limit**: after `router_wait_timeout_min` (default 15) minutes still waiting for 2 free coworkers, the
  run goes to `awaiting_main` with the reason `Team waits for 2 free coworkers since 15 min` and a
  `route_waiting_timeout` event is logged. The mode stays TEAM; the Main decides (wait more, switch to DIRECT, cancel).

Role selection before trimming: always `critic`, `tester`, `prompt_writer`; `designer` if ui > 0; `architect` if
new_arch > 0 or breadth > 0 or risk >= 1; no filler roles.

`reason` is one line: mode, score vs threshold, the top 3-4 signals with points, the deciding rule, and the capacity
result when it changed roles or delayed the run.

## 4. Stage graphs

Stage names are the same everywhere (data model, events, Live view, Plan-view stepper, PLAN.md):
**Propose, Score, Merge, (Approve), Build, Review, Fix**. Approve exists only when `team_require_approval` is on
(default off: the goal says "then build"; the Lead fixes this default in PLAN.md).

### DIRECT

```
(hold: build_hold_s = 30 s after the route is decided, so the Main can still switch to TEAM)
Build (operator's tasks, executors, parallel by DAG)
   -> Review (one reviewer per build task, different coworker: role_policy.py:eligible)
        pass -> machine check (engine.py:_machine_check) -> done / awaiting_main
        fail -> Fix (same builder, review text attached) -> Review   [max_fix_rounds, default 2]
        still failing after max rounds -> awaiting_main with reason
```

The operator's tasks are used as-is. DIRECT always has an independent review; `fast` no longer means "no review".

### TEAM

| stage | what the request asked for | tasks |
| --- | --- | --- |
| Propose | critique + plans/prompts | R parallel read-only tasks, one per role |
| Score | cross-scoring of the proposals | R scoring turns |
| Merge | lead merges into one plan | 1 lead task (or the best proposer when only 2 coworkers) |
| Approve | optional Main approval | only with `team_require_approval` |
| Build / Review / Fix | build -> independent review -> fix | as in DIRECT |

```
Propose  (R parallel tasks, one per role; role_split.py:split_roles over free coworkers;
          prompt = goal + operator tasks + role_sections_prompt(['critic', role]).
          Every propose task writes two sections:
            '## As critic:'  critique of the request and of the Main's tasks (gaps, risks, wrong assumptions)
            '## As <role>:'  its plan or prompt for its own role (designer, architect, tester, prompt_writer);
          the critic-role task writes '## As critic:' plus '## As critic plan:' (the fixes it proposes).
          So several coworkers critique, not only the critic. For BACKTEST.md 2.2 each propose task counts as
          one critique and one plan.)
   -> Score   (each proposer scores every other proposal 0-10 on fit, risk, testability, cost;
               one turn per scorer; never its own; parsed from '## As scorer:' lines)
   -> Merge   (lead per section 3.B; output = plan JSON in the planner contract; checked by
               protocol.py:validate_plan and delegation.py:validate_delegation; no file overlap between parallel tasks;
               the critiques are passed to the lead and listed in the plan as resolved or rejected)
   -> [Approve]  (only with team_require_approval: awaiting_main with the merged plan; POST /api/run/confirm continues)
   -> Build   (engine.py:_import_plan with prefix 'm', executors)
   -> Review  (independent reviewers; the tester proposal's checks become acceptance)
   -> Fix     (max_fix_rounds, default 2) -> machine check -> done / awaiting_main
```

Failure handling:

- **Retries never resend.** A retry is a new follow-up task with its own `step_id` (attempt + 1), created only after
  the first turn has finished (answered, failed or timed out). The same turn is never sent again.
- Proposer missing a section (`role_split.py:missing_roles`) -> one follow-up task asking only for the missing section,
  then dropped (min 2 proposals, else fall back to DIRECT with the operator's own tasks and log `route_fallback`).
- Invalid merged plan -> one follow-up task with the validation error, then fall back to the operator's tasks plus the
  winning proposal as context.
- Score ties: the role for the top signal wins (ui -> designer, new_arch -> architect).

## 5. Settings (store settings, shown in desktop_settings.py)

| key | default | allowed | meaning |
| --- | --- | --- | --- |
| `router_enabled` | true | bool | false = old behaviour |
| `router_team_at` | 55 | int 30-90 | score for TEAM |
| `router_direct_below` | 35 | int 0..team_at | below = DIRECT |
| `router_min_fail_gain` | 0.18 | 0.05-0.50 | middle range: failures TEAM must avoid |
| `router_max_team_turns` | 20 | int 5-60 | middle range: turn cap for TEAM |
| `router_fail_cost_a` | 3 | int 1-10 | turns per task of one failure |
| `router_fail_cost_b` | 4 | int 0-20 | fixed turns of one failure (Main rework and reading) |
| `router_weights` | table above | int 0-40 each | dict id -> int; normalised to sum 100 |
| `router_core_files` | list above | list of paths | files that count as medium risk |
| `team_max_roles` | 5 | int 2-5 | proposer cap |
| `team_require_approval` | false | bool | adds the Approve stage between Merge and Build |
| `max_fix_rounds` | 2 | int 0-4 | both modes |
| `router_build_hold_s` | 30 | int 0-120 | DIRECT build tasks wait this long after the route decision |
| `router_wait_timeout_min` | 15 | int 5-120 | waiting for coworkers before awaiting_main |
| `router_minutes_per_turn` | 3 | int 1-15 | cost estimate |
| `router_history_days` | 30 | int 1-180 | past-failure window |
| `router_shadow` | false | bool | compute and log the route but run old behaviour (canary for BACKTEST.md) |

The capacity limits (3 / 2 / fewer than 2) are fixed rules, not settings. Bounds go into the `/api/settings` clamp table
in `server.py` and in `desktop_settings.py`.

## 6. Main override

`POST /api/operator/delegate` body gets optional fields:

- `route`: `"auto"` (default) | `"direct"` | `"team"`.
- `roles`: optional subset of designer, architect, tester, critic, prompt_writer (TEAM only; still trimmed by capacity).
- `max_fix_rounds`: optional int 0-4.
- Legacy `quality`: `fast` -> `direct`, `standard` -> `auto`, `high` -> `team`.

Rules:

- The override sets the mode; capacity may delay the run but never changes the mode. The score is still computed and
  stored for the backtest; `reason` starts with `Main override:`.
- A delayed TEAM run waits at most `router_wait_timeout_min` (15) minutes for 2 free coworkers; then it goes to
  `awaiting_main` with a reason and a `route_waiting_timeout` event.
- New `POST /api/run/route` `{run_id, route}` switches a running run:
  - **DIRECT -> TEAM** only while no build task has been sent. DIRECT build tasks wait `router_build_hold_s` (30 s)
    after the route is decided so this switch is possible. The held build tasks are dropped unsent and the TEAM pass
    starts from Propose.
  - **TEAM -> DIRECT** only before Build: open proposer and scorer tasks are cancelled with the existing per-run cancel
    (`engine.py:cancel` on those tasks' run step) and nothing is resent; the operator's original tasks are imported.
  - Otherwise 400 `Route can only change before the build starts`.
- A repeated `request_id` returns the stored route unchanged.

## 7. Hook points (file:function)

`server.py` keeps CRLF line endings and is changed with targeted edits only (no full rewrite).

| file:function | change |
| --- | --- |
| `notiontool/router.py:route` (new) | pure decision, sections 2-3 |
| `notiontool/router.py:similar_runs` (new) | Jaccard history match for past_failure |
| `notiontool/router.py:modules` (new) | module count rule of the breadth signal |
| `notiontool/router.py:capacity_plan` (new) | section 3.B: roles, lead, waiting, timeout |
| `notiontool/team_pass.py:start` / `notiontool/team_pass.py:step` (new) | TEAM stage machine (section 4); uses role_split, role_policy, protocol, delegation |
| `notiontool/team_pass.py:switch` (new) | route switch rules of section 6 |
| `notiontool/operator_main.py:delegate` | after `validate_plan`, before the run dict: call `router.py:route`, store `run['route']`, set `allocation=dict(mode=route['mode'], ...)`; DIRECT -> `engine.py:_import_plan` with build hold + review tasks; TEAM -> `team_pass.py:start`. Replaces the `operator_roles.py:choose_quality` call (main folder). New keyword args `route`, `roles`, `max_fix_rounds`, `quality`. |
| `notiontool/operator_main.py:_summary` | add route mode, score and reason to the response |
| `notiontool/operator_roles.py:choose_quality` (main folder) | thin wrapper over `router.py:route` mapped back to quality for old tests |
| `notiontool/operator_roles.py:step` (main folder) | hands TEAM runs to `team_pass.py:step` |
| `notiontool/engine.py:tick` | for operator runs with `route.mode == 'team'`: call `team_pass.py:step` when all tasks of a stage are finished; release held DIRECT build tasks after `build_hold_until`; check the waiting timeout — in engine.py:tick, next to the operator run handling |
| `notiontool/engine.py:cancel` | reused for TEAM -> DIRECT (cancel open proposer/scorer tasks) |
| `notiontool/engine.py:_finish` | reached only after review/fix; acceptance unchanged |
| `notiontool/engine.py:_approach` | unchanged for non-operator runs (later may call the router too) |
| `notiontool/server.py:Application.action` | the `/api/operator/delegate` branch calls `operator_main.py:delegate`; pass `route`, `roles`, `max_fix_rounds`, `quality` from the body; add the `/api/run/route` branch calling `team_pass.py:switch` |
| `notiontool/server.py:Application.snapshot` | handler of `GET /api/state` (called from the request handler's `handle_request`); runs are copied without `context`, so `route` and `team` appear without code change — only add `route_waiting` run ids next to `awaiting_main` |
| `notiontool/server.py` request handler `handle_request`, `GET /api/run` branch | returns the run (without `context`) and `Application.safe_task` tasks; `route`/`team` come along; `safe_task` must keep `stage`, `route_mode`, `step_id` |
| `notiontool/progress_feed.py:ProgressFeed.public` (with `ProgressFeed.payload`) | builds the per-task progress state (`state.progress`, `GET /api/progress`); add `stage` and `step_id` to each payload so Live rows group by stage. There is no separate run-state builder in progress_feed.py; the run state comes from `Application.snapshot`. |
| `notiontool/desktop_settings.py` | settings rows from section 5 |
| `tests/test_router.py` (new) | each signal (core file = 0.5, module counting, redesign only ui), thresholds, middle-range example (50 -> TEAM, 40 -> DIRECT), overrides, capacity 3/2/1, waiting timeout |
| `tests/test_team_pass.py` (new) | stage transitions with a fake engine, critic sections in every propose task, 2-proposer pass without lead, retries as new follow-up tasks, route switch rules, optional Approve |

Events (`store.event`): `route_decided` (mode, score, reason), `route_overridden`, `route_switched`, `route_fallback`,
`route_waiting`, `route_waiting_timeout`, `team_stage` (from -> to), `team_scores` (winner, means). BACKTEST.md reads
`route_decided` plus the run outcome to calibrate weights, `p_fail` and `min_fail_gain`.

## 8. Live view (desktop app Live and Plan tabs; tokens from the redesign spec)

- Main card: route chip with the word `Direct` (text-2 on raised) or `Team` (accent text on accent-soft), plus
  `score 50 / 55` in mono 12/18. Hover or click shows the full `reason`.
- Plan tab, "Why this route": the 8 signals (name, 4 px value bar, points, evidence), thresholds, the failure gain, and
  the cost line `Direct ~10 turns / ~12 min · Team ~16 turns / ~21 min` (expected turns).
- Stepper with the same stage names as section 4: TEAM `Propose -> Score -> Merge -> Build -> Review -> Fix (n/2)`,
  with `Approve` between Merge and Build only when `team_require_approval` is on; DIRECT `Build -> Review -> Fix (n/2)`.
  Current stage accent dot, done stages success with check icon, failed danger with x icon (never color only).
  A 2-proposer pass shows `Merge (by proposer)`. During the DIRECT build hold a 30 s countdown shows `Build starts in 24 s`.
- Proposals list: one row per role: role, coworker, mean score (e.g. 7.4), winner marked `Chosen`; click opens the
  critique and the plan section.
- Override control (Main only): segmented `Auto · Direct · Team`, enabled only while switching is allowed; when
  disabled the tooltip shows the 400 reason.
- Needs you: `route_fallback`, `route_waiting` ("Team waits for 2 free coworkers") and `route_waiting_timeout`
  appear as warning rows with one action (`Open`); Approve (if on) shows `Confirm`.
- Shadow mode: muted chip `Shadow: would be Team (50)` next to the real route.

## 9. Open points for the Lead (PLAN.md)

- `operator_roles.py` exists in the main folder; the build starts from main, not from an older task copy.
- PLAN.md decides the approval default in one place: no Main approval between Merge and Build; Approve only behind
  `team_require_approval`.
- Default weights, `p_fail` and `min_fail_gain` are guesses until BACKTEST.md runs; ship with `router_shadow` first, then enable.
- A full 5-role pass costs about 3x the turns of DIRECT; the middle range caps it with `router_max_team_turns`.
