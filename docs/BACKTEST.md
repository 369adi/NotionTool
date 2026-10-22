# Live backtest (canary)

Rule: a harness feature that affects coworkers, runs or the UI is only done after a LIVE backtest with
real open coworkers. Unit tests alone are not enough.

The backtest is `notiontool/canary.py`. Only the Main starts it, on purpose, usually right after a restart
once the coworkers are open and ready. It never restarts anything, never reloads Notion and never sends a
chat itself: it queues small requests through the normal operator delegate path, and the coworkers do them.

## How to start

```
python -m notiontool.canary --suite roles|parallel|health [--timeout SECONDS] --port PORT --key HOST_API_KEY
```

- `--port` / `--key` (or `NOTIONTOOL_PORT` / `NOTIONTOOL_API_KEY`): the harness API port and host API key
  (Settings > Host API, or `POST /api/host/key`).
- `--project`: project folder; default is the folder of the operator chat.
- Inside the harness process: `from notiontool.canary import run_suite; run_suite(engine, 'roles')`.
- Exit code 0 = all checks passed, 1 = a check failed, 2 = missing port/key.

Output: JSON on stdout (also returned by `run_suite`) and a short report
`work/canary/report-<time>-<suite>.md` with one line per check (passed/FAILED and numbers).

## When the Main must run which suite

| Change touches | Run |
|---|---|
| Roles, review, fix rounds, planner/executor/reviewer choice, verifier, done rules | `roles` |
| Team size, rotation, scheduling, allocation, file leases, delegate path, Main creation, agent guard | `parallel` |
| Desktop app, state push, heartbeat, overlay/shell, turn watcher, stall watch | `health` |
| After every harness restart, before saying a feature is done | `health`, then the suites of the change |
| Large or unclear changes | all three: `health`, `parallel`, `roles` |

A feature only counts as done when its suites pass. If a suite fails, report the failed lines as they are.

## What each suite checks

### roles (default limit 30 min)
One small request in a sandbox folder: plan (Designing), write `clamp()` with a test (Building), check it
(Reviewing). The builder is told to leave acceptance point 3 open on purpose.
- Stages in order: Designing -> Building -> Reviewing, by first start time.
- Reviewer is another coworker: no worker both built and reviewed.
- Exactly one fix round: the first review says REVIEW: FAIL, one fix round follows.
- Final result approved: last review says REVIEW: PASS, the request did not fail, and our own hidden
  test of the delivered code passes (clamp values and ValueError), plus the coworker's own tests.

Note: operator runs (operator_main.delegate) do not yet add a review/fix round by themselves
(engine.py, operator branch). Until they do, this suite reports the missing fix round as FAILED.

### parallel (default limit 20 min)
N tiny independent tasks, N = enabled coworkers of the operator's account, at most 10. Each writes one
marker line into its own sandbox file.
- All tasks done (with RESULT: DONE), Ran at the same time (peak vs. min(N, request limit)), Total time,
  No file conflicts (missing/mixed/shared files), No second Main, No Notion agent created.

### health (about 30 s)
No request is sent. Reads the desktop app status twice, 30 s apart.
- State pushes and heartbeats arrive (desktop_state_cache counters grow; at least half the expected
  heartbeats at one per 3 s).
- No offline condition (no push timeouts or failed pushes).
- No false hang (no turn watcher hang report for a coworker without an open task).

## Safety rules

- Sandbox only: all files live in `work/canary/<suite>-<time>-<id>/`; paths outside are refused. The folder
  is removed after the checks; only the report stays.
- Never resend: on timeout the canary cancels its own request once and reports "nothing was resent".
- Errors become a failed check; nothing is retried.
- Unit tests of the checking logic: `python -m unittest tests.test_canary` (fake harness only).
