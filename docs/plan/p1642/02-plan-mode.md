# 02 - Plan-first team mode (engine plan)

Planning only. No code was changed. Base: the current `notiontool/operator_main.py` and
`notiontool/operator_roles.py` in the main folder. Note that `operator_roles.py` is untracked there
and is not in this worktree yet. It also uses `engine.add_task`, `engine._import_plan`,
`validate_plan`, `parallel_width` and `team_size`.

## 1. Quality bug: why 'fast' still got a design stage

What happens today in `delegate(engine, chat_id, goal, tasks, request_id, now, summary, quality)`:

1. **The top-level `quality` is dropped by the HTTP route.**
   - `server.py` (`/api/operator/delegate`, line ~1038 in main, ~1008 in this worktree) calls
     `delegate(self.engine, chat_id, goal, tasks, request_id, summary=...)` and never passes
     `body.get('quality')`. `canary.py:244` has the same omission.
   - So `quality=''` always reaches `choose_quality`. A UI-sounding goal (`UI_WORDS`: ui, design,
     layout, css, sidebar, buttons, colors, ...) or any `web/` / `.css` file then forces
     `'high'`, and `'high'` adds the design task.
   - This is the observed bug: "fast" was requested, `UI/design work` was chosen.
2. **Per-task quality is collapsed into one run quality.**
   - `next(t['quality'] for t in tasks if t.get('quality'))` takes the *first* task's value for
     the whole run and then strips `quality` from every task.
   - A per-task `fast` next to a per-task `high` is lost. The design stage then hangs in front
     of *all* tasks, because every created executor gets `deps += [design_id]`.
3. **Review/fix runs regardless of quality.**
   - `operator_roles.step()` never checks the quality, so `fast` still gets a review and up to
     2 fix rounds. The module docstring says the stages are "standard and high".
   - Not the reported symptom, but the same class of bug.
4. **Unknown keys.**
   - If an MCP/nt_api client schema does not list `quality`, it is silently dropped before HTTP.
   - Check the nt_api tool schema and the `delegate` docs in the operator prompt
     (plain_protocol / operator instructions).

Fix:

- **F1** `server.py` route: pass `quality=str(body.get('quality') or '')`. Also pass the new
  `mode` (section 4). Do the same in `canary.py`.
- **F2** `operator_main.delegate`: resolve quality **per task**:
  `task_q = t.get('quality') or top_level or auto(goal, [t])`. Store it as `task['quality']`
  instead of stripping it. Keep `run['quality']` = the highest task quality, for display only.
- **F3** The design stage is added only for tasks whose quality is `high`. Only those tasks get
  the design dependency. An explicit `fast` never gets a design or review stage.
- **F4** `operator_roles.step()`: skip review when `task.quality == 'fast'`. `standard` gets
  review + fix, `high` gets design + review + fix. Return `review_state='skipped'` so the UI
  can show it.
- **F5** Validate the values: an unknown quality raises `ValueError` (already in
  `choose_quality`). Return `quality` and `quality_reason` per task in `_summary`.

## 2. Plan-first flow

```
Main --delegate(mode='plan')--> PLANNING (parallel, read-only coworkers)
   architect | designer | backend | tester (+ optional extra specialists, up to the free slots)
        -> CRITIC (1 coworker, read-only, gets all role reports)
        -> LEAD (1 coworker, read-only, writes the final plan JSON)
        -> AWAITING_APPROVAL (Main sees the plan; approves, edits or rejects)
        -> BUILD tasks created from plan JSON (existing executor path)
        -> existing review / fix stages per task quality (operator_roles.step)
        -> all done -> turn_watch 'All coworkers are finished' -> Main integrates
```

- Planning roles are read-only: no files, no locks (same as today's design stage, `read_only=True`).
- The critic depends on all role tasks. The lead depends on the critic and all role tasks. Their
  results are injected through the existing deps context.
- Each role prompt includes the goal, the project file tree summary, and a role-specific ask:
  - architect: modules, interfaces, data flow;
  - designer: visual spec, only when UI work exists;
  - backend: the concrete code changes per file;
  - tester: the tests and live checks that prove the result.
  - All roles must list the files they would touch.
- Critic prompt: find conflicts, file overlaps, missing tests and risky assumptions across the
  reports. Output a numbered issue list.
- Lead prompt: produce **only** a fenced JSON object (schema in section 3) that resolves every
  critic issue or lists it under `open_questions`.

Approval:

- Default: the Main must approve with `/api/operator/plan/approve`.
- Setting `plan_auto_approve` (default off): approve automatically when the plan validates and
  `open_questions` is empty.
- The Main may send an edited plan JSON in the approve call. It is validated the same way.
- Reject sets `run.state='cancelled'`, or `replan` with a note (max 1 replan, then it stays
  awaiting).

## 3. Data model

Run (new fields):

- `mode`: `'direct'` (today) | `'plan'`.
- `phase`: `'planning' | 'critic' | 'lead' | 'awaiting_approval' | 'building' | 'done'`. The
  existing `state` stays `executing` during planning; add `awaiting_main` reuse. The
  acceptance gate already has awaiting_main, so do not invent a new top-level state.
- `plan`: `{version, tasks, open_questions, lead_task, critic_task, approved_by, approved_at}`.
- `plan_roles`: the list of role names used.
- `replans`: an int.

Task (new or used fields):

- `stage_role`: `'plan_role' | 'critic' | 'lead' | 'design'` (design exists today).
- `plan_role`: `'architect' | 'designer' | 'backend' | 'tester' | ...`.
- `quality` per task (F2). `read_only=True` for all planning tasks.

Lead plan JSON (validated by `validate_plan` plus new checks):

```json
{"tasks": [{"id": "t1", "title": "...", "role": "backend", "prompt": "...",
            "acceptance": "python -m pytest -q tests/test_x.py passes",
            "files": ["notiontool/x.py", "tests/test_x.py"], "depends_on": [],
            "quality": "standard", "summary": "one plain sentence"}],
 "open_questions": []}
```

New checks:

- No file appears in two tasks that can run at the same time. Overlap is allowed only along a
  `depends_on` chain.
- `depends_on` references existing ids, and the graph has no cycle.
- Every task has `files`, `prompt` and `acceptance`. There are at most `MAX_DELEGATED_TASKS`
  tasks.
- `role` is one of the known specialties (mapped to `specialty`).
- On failure, re-ask the lead once with the validation error. After that, go to
  awaiting_approval with the errors shown.

## 4. API change: /api/operator/delegate

The request body adds:

- `quality`: optional `'fast' | 'standard' | 'high'`. Today it is ignored; with F1 it is passed
  through.
- `mode`: optional `'direct'` (default, today's behaviour) | `'plan'` | `'auto'`.
- `roles`: optional list that overrides the planning roles. Default:
  `['architect','designer','backend','tester']`, with `designer` dropped when the task is not UI.
- `tasks`: required for `direct`. For `plan` it is optional (a rough task list as hints).
  `goal` is required.

The response adds `mode`, `phase`, `quality` per task, and `plan` once the lead is done.

New endpoints:

- `GET /api/operator/plan?run_id=`: the plan JSON, critic issues and validation errors.
- `POST /api/operator/plan/approve {run_id, plan?}`: create the build tasks (through the
  existing `_import_plan`), set `phase='building'`, then run the existing stages.
- `POST /api/operator/plan/reject {run_id, note?, replan?}`.

All of them are idempotent by `request_id` / `run_id`, like today's duplicate check.

## 5. When to skip planning (mode='auto')

Go **direct** (no planning) when any of these is true:

- an explicit `mode='direct'` or `quality='fast'`;
- 1 task with at most 1 file;
- only docs/plan files;
- the goal is a review or a question;
- the run is a fix round;
- fewer than 3 free coworkers (planning would starve the build).

Go **plan** when any of these is true:

- explicit `mode='plan'`;
- more than 3 tasks or more than 5 files;
- files in more than 2 top-level areas (e.g. `notiontool/` + `web/` + `tests/`);
- quality `high` with code files;
- the Main sent no task list.

## 6. Using all 10 coworkers

- Planning uses 4 role tasks plus the critic plus the lead. Roles run in parallel, so the peak
  is 4.
- With 10 free coworkers, add up to 2 more specialists chosen from the goal (e.g. `security`,
  `performance`, `docs`). Never add more than `free - 2`.
- Build: the lead is told the free coworker count N (`capacity` from `team_size` /
  `max_workers`) and asked to split the work into up to N independent tasks without file
  overlaps.
  - `run['worker_budget'] = min(capacity, parallel_width(tasks))` stays.
  - Add a warning in `_warnings` when `parallel_width < capacity/2` and more than 3 tasks exist
    ("plan is mostly serial").
- Reviews use other coworkers (`exclude_workers` already does this). With 10 coworkers, reviews
  of early tasks overlap the build of later tasks.
- Planning tasks are read-only, so they hold no locks and never block builds of other runs.

## 7. Code touch points

- `notiontool/operator_main.py`: the `delegate(..., quality, mode, roles)` split into
  `_delegate_direct` (today plus F2/F3) and `_delegate_plan` (creates the role tasks).
  `approve_plan`, `reject_plan`, `plan_view`, and the `_summary` fields.
- `notiontool/operator_roles.py`: `PLAN_ROLES`, `role_prompt(role, goal, hints)`,
  `critic_prompt`, `lead_prompt(n_free)`, `parse_lead_plan(text)`, `check_overlaps(tasks)`,
  `choose_mode(goal, tasks, quality, free)`. In `step()`: advance planning -> critic -> lead ->
  awaiting_approval, plus the F4 review skip for `fast`. `stage_of` / `run_stage` add
  `Planning`, `Critiquing`, `Writing plan`, `Awaiting approval`.
- `notiontool/server.py`: F1 plus the three new endpoints. Keep CRLF line endings.
- `notiontool/canary.py`: pass quality.
- `notiontool/desktop_settings.py` + `web/desktop/settings.js` + `tests/test_desktop_settings.py`:
  `plan_auto_approve`, `plan_mode_default` (`auto`).
- UI rendering of the plan and approval is in 04-ui.md (other chat).
- `engine.py` is touched only if `step()` needs a hook after the lead task finishes. Today
  `operator_roles.step` is already called per run tick.

## 8. Tests

New `tests/test_operator_plan_mode.py` and extensions of `tests/test_operator_roles.py`:

1. `test_route_passes_top_level_quality`: POST `/api/operator/delegate` with `quality='fast'` and
   a UI goal creates no design task and no review task (regression for the bug).
2. `test_per_task_quality_kept`: tasks `fast` + `high`. Only the `high` task depends on a design
   task, and the `fast` task starts at once.
3. `test_fast_skips_review`: a done fast executor gets `review_state='skipped'` and no reviewer.
4. `test_plan_mode_creates_parallel_roles`: `mode='plan'` creates 4 read-only role tasks without
   files and without locks. The designer is dropped for non-UI goals.
5. `test_critic_waits_for_roles` / `test_lead_waits_for_critic`: dependencies and the order of
   the stage words.
6. `test_lead_json_validated`: overlap, cycle, missing acceptance and unknown dependency each
   cause one re-ask, then awaiting_approval with errors.
7. `test_approve_creates_build_tasks_then_review`: approve creates executors with files and
   deps. The existing review/fix flow runs, and the `request_id` duplicate is idempotent.
8. `test_edited_plan_on_approve` and `test_reject_and_replan_once`.
9. `test_auto_mode_skip_rules`: a table of goal/tasks/quality/free -> direct|plan.
10. `test_ten_coworkers_budget`: with 10 free coworkers the roles peak at 6. The lead prompt
    states N=10, `worker_budget = min(10, parallel_width)`, and the serial-plan warning fires.
11. `test_plan_auto_approve_setting`: auto-approve only when the plan is valid and
    `open_questions` is empty.
12. `tests/test_repo_eol.py` stays green: new files LF, `server.py` stays CRLF.

The acceptance for the build round: these tests plus the existing `tests/test_operator_roles.py`,
`tests/test_turn_watch_all_done.py` and `tests/test_desktop_settings.py` pass.

## 9. Risks

- **Planning cost:** 6 extra Notion turns per run. Skip rules and auto mode keep small tasks
  direct.
- **Malformed lead JSON:** handled by one re-ask and then Main approval with the errors shown.
- **Stale plan:** files changed by other runs between planning and approval. Re-check file claims
  in `_import_plan` at approve time (it already checks paths).
- **Approval wait:** a run in awaiting_approval must not count as busy for turn_watch's
  "all coworkers finished" message. The Main should instead get a "plan ready" message using the
  same send path.
