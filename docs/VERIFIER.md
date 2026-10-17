# Verifier and perfection mode

Status: design and integration plan for Phase 6, batch 1. Nothing in this document is wired into the engine yet. Every hook below is NOT WIRED. Points that could not be checked are marked UNKNOWN.

## 0. Sources and recheck data

- notiontool/engine.py was read (read only) at 2026-10-16 19:57:46 (Europe/Berlin).
  - Size: 57701 bytes, 926 lines, mtime 2026-10-16 18:37:08.
  - sha256: 79CA1BED455F4476453813008BC5F9547FCD9C55C1E2ECEC541A818F62CA0D02
  - All engine.py:N references in this file refer to exactly this version. If the hash differs, recheck every reference before using it.
- notiontool/capture_verify.py was read (read only).
- docs/ARCHITECTURE_AUDIT.md was read (read only). Its line 43 rates the verifier cascade as "partial (one reviewer task)". Its engine.py line numbers (for example engine.py:389, :476, :633) come from an older engine.py version and no longer match the version above.
- Module status at read time: notiontool/done_definition.py, notiontool/verifier.py, notiontool/fix_loop.py and notiontool/perfection.py did NOT exist yet. They are being written in parallel (T1 to T4). The APIs in sections 1 to 5 are taken from the Phase 6 request spec, not from code. Whether the final modules match exactly is UNKNOWN.

## 1. Done definition (notiontool/done_definition.py, T1)

Planned API (from the spec, module not present at read time):

- build_done_definition(task_id, planner_output) returns a list of criteria.
- validate_criterion(c) returns (ok, problems).

Criterion shape:

| Field | Meaning |
| --- | --- |
| id | Stable criterion id, unique within the task |
| text | Human readable statement of what must be true |
| method | One of the 6 methods below |
| target | Files and/or commands the check looks at |
| required | True if failing this criterion fails the task |

The 6 methods:

1. tests: run unit tests or commands named in target.
2. static: static checks on files in target (for example line endings, BOM, syntax, forbidden edits).
3. diff: compare the change against the files the task was allowed to touch.
4. requirements: match the result against the written acceptance text.
5. screenshot: compare before and after captures (see capture_verify below).
6. model_review: a read-only model review in a fresh context.

Normalisation: malformed planner output (missing fields, unknown method, wrong types) is normalised or dropped with a problem message. If no usable criterion is left, a single fallback criterion with method requirements is created from the task acceptance text, so every task always has at least one checkable criterion.

Today the engine has only a free-text acceptance field per task: engine.py:128 (add_task parameter acceptance) and engine.py:862 (_import_plan copies task['acceptance'] from the plan). The structured criteria list is NOT WIRED.

## 2. Verifier cascade (notiontool/verifier.py, T2)

Planned API (from the spec, module not present at read time):

- run_verifier(task_id, criteria, checks, risk, capture_verify=None, model_review=None) returns a verdict.
- All checks are injected callables, so tests can use mocks. No network calls.

Cascade order, cheapest first:

1. tests
2. static checks
3. diff / requirements match
4. screenshot, using an injected capture_verify callable
5. model review, only when risk is high; read only, fresh context, injected callable

Stop rule: the cascade stops at the first failing objective check (steps 1 to 4). Later and more expensive steps are not run.

Objective-over-model rule: a failing objective check can never be overruled by a passing model review. Model review can add defects, but it cannot turn an objective failure into a pass.

Screenshot step: notiontool/capture_verify.py provides verify(before, after) (capture_verify.py:70). It returns a dict with changed, distance, diff_ratio and summary. Thresholds: PIXEL_TOLERANCE = 16 (capture_verify.py:9), CHANGED_DISTANCE = 6 (capture_verify.py:11), CHANGED_RATIO = 0.01 (capture_verify.py:12). The verifier receives it as the capture_verify argument; how a criterion maps to a before/after image pair is UNKNOWN until T2 is done.

## 3. Verdict format

```
{
  "passed": bool,
  "failed_criteria": [criterion_id, ...],
  "defects": [{"criterion_id": str, "task_id": str, "detail": str}],
  "checks_run": [...],
  "cost": ...
}
```

- passed is true only if no required criterion failed.
- defects always name the task_id, so the fix loop can route each defect to the task that caused it.
- The exact types of checks_run entries and cost are UNKNOWN until T2 is done.

Today the reviewer contract is different: a reviewer reply needs a boolean passed and a string summary (engine.py:638-639), and when passed is false its fixes must be a valid plan (engine.py:640-643). The verdict format above is NOT WIRED.

## 4. Fix loop (notiontool/fix_loop.py, T3)

Planned API (from the spec, module not present at read time):

- new_state(limits) returns the initial loop state.
- decide(state, verdict, now=None) returns (action, reason, new_state). It is a pure state machine.

Actions: fix, done, stop, escalate. Every action comes with a reason string.

- done: the verdict passed.
- fix: send the concrete defect list only to the affected tasks (by task_id from defects). Correct parts are never re-run.
- stop: a stop rule was hit.
- escalate: hand the decision to the user.

Stop rules:

1. Maximum number of iterations reached.
2. Time budget used up.
3. Request budget used up.
4. 2 rounds in a row without improvement. Fewer failed criteria than the previous round counts as improvement.
5. Otherwise, when no rule allows another fix round, escalate to the user.

Whether a hit stop rule ends in stop or escalate in every case is defined by T3; the exact mapping is UNKNOWN here.

Today the engine has a single counter: in state reviewing, a failed review imports review['fixes'] as new executor tasks with the prefix "Fix: " while run['fixes'] < settings['max_fix_rounds'] (engine.py:910-912). Otherwise the run is blocked with "Verification still failed after the allowed repair rounds" (engine.py:913-914). There is no time budget, request budget or no-improvement rule there. The fix loop is NOT WIRED.

## 5. Levels and destructive change guard (notiontool/perfection.py, T4)

Planned API (from the spec, module not present at read time):

- LEVELS, get_level(name)
- escalate_level(current, failed, reason)
- guard_change(change) returns (allowed, reason)
- MASS_EDIT_THRESHOLD

The three levels: standard, high, perfection. Each level maps to:

- verifier depth (how far down the cascade checks run),
- max_fix_rounds,
- model_review policy,
- request budget and time budget.

The concrete numbers per level are UNKNOWN until T4 is done.

Escalation: a level is raised only after a failure, and only with a reason string. It is never raised without a failure.

Destructive change guard: these changes are denied unless there is a backup or a dry run, plus an explicit approval flag:

- deletions,
- overwrites of files that are not generated,
- mass edits (more files than MASS_EDIT_THRESHOLD; the value is UNKNOWN).

The guard returns allow or deny with a reason. NOT WIRED: nothing in engine.py calls a guard today. The closest existing protection is the file lease and claim logic (engine.py:371 claim_file, engine.py:391 claim_absolute), which blocks edits to files held by others, not destructive edits as such.

## 6. Integration plan for engine.py

All hooks in this section are NOT WIRED. This batch does not edit engine.py. References are for the engine.py version in section 0.

### 6.1 Where executor results come in

- engine.py:405 tick() collects finished futures and calls self._complete(future, info) at engine.py:410, then self._advance(run) at engine.py:425.
- engine.py:614 _complete(): an old lease is ignored (engine.py:616-617). The raw reply is read at engine.py:622 and parsed at engine.py:629 (plain_protocol.normalize or json_object).
- engine.py:644-645: an executor result must have status in done/blocked/failed and a string summary.
- engine.py:646-649: task state, result (JSON), requires_review and error are stored.

Hook V1 (NOT WIRED): after engine.py:647, when task['role'] == 'executor' and task['state'] == 'done', build the done definition for the task (build_done_definition) and store it on the task. Running the cascade here per task is possible, but it must not block the tick thread with long test runs; whether that is acceptable is UNKNOWN.

Hook V2 (NOT WIRED): at engine.py:862 in _import_plan, pass the planner criteria (if the plan contains them) together with acceptance, so build_done_definition gets the planner output and not only the acceptance text.

### 6.2 Where the reviewer task is created

- engine.py:897-903 in _advance: when every executor task of the run is finished (engine.py:869) and none failed (engine.py:871-884), the run moves from executing or fixing to reviewing and engine.py:903 creates the reviewer task with add_task(run, 'reviewer', ...).
- Solo runs skip the reviewer: engine.py:898-900 create an integrator task that checks its own work.
- The reviewer gets only executor results as context (engine.py:555-557), including each task's acceptance text (engine.py:590).
- A reviewer must not be the executor of the same run (engine.py:499-500); there is a reviewer_fallback event at engine.py:526.
- Every task, including the reviewer, is created through add_task (engine.py:128-137), which stores model_reason via with_reason (engine.py:134).

Hook V3 (NOT WIRED): in _advance at engine.py:897-903, run the objective cascade (run_verifier with steps 1 to 4) before creating the reviewer task. If it fails, skip the reviewer and go straight to the fix loop. If it passes, create the reviewer task only when the level or risk requires model review; otherwise go to integrating. This keeps objective results ahead of model opinion.

Hook V4 (NOT WIRED): at engine.py:904-914 (state reviewing), replace the run['fixes'] < max_fix_rounds check at engine.py:910 with fix_loop.decide(state, verdict). fix imports defects only for the affected tasks (instead of the whole review['fixes'] plan at engine.py:911); done moves to integrating (engine.py:907-909); stop and escalate block the run with the reason, as engine.py:914 does today.

Hook V5 (NOT WIRED): the level (perfection.get_level) should be chosen when the run is created in submit (engine.py:151) and stored on the run; max_fix_rounds would then come from the level instead of self.store.settings() (engine.py:910). Escalation (escalate_level) happens only after a failed verdict.

Hook V6 (NOT WIRED): guard_change should run before a write is allowed, next to the lease checks (engine.py:371 claim_file, engine.py:385 check_lease, engine.py:391 claim_absolute). Whether a change description (delete/overwrite/mass edit) is available at that point is UNKNOWN; the bridge write path was not read for this document.

### 6.3 Open points

- UNKNOWN: whether the final T1 to T4 APIs match the names in this document.
- UNKNOWN: where test and command checks should run (worker or host) and how their cost is counted.
- UNKNOWN: how the run stores verdicts and fix loop state (new fields on runs/tasks or a new store table); store.py was not read for this document.
- UNKNOWN: events for verify_* telemetry. ARCHITECTURE_AUDIT.md:61 lists them as missing.
- engine.py is edited by other batches. Recheck the sha256 in section 0 before wiring; line numbers may shift.