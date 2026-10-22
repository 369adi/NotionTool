# 05 - Tests: how to prove parity, plan-first mode and speed fixes

Role: Tester. Planning only; no code is changed in this round.
Sources read: notiontool/canary.py (suites roles, parallel, health), docs/BACKTEST.md,
tests/test_desktop_*.py, tests/test_operator_main.py, tests/test_main_switch_operator.py, tests/test_turn_watch*.py.

## 0. Ground rules for every check

- Three layers, always in this order: (1) mocked unit tests (pytest, no network), (2) DOM checks in the
  real window over CDP port 9333, (3) live canary with real coworkers. A layer only runs when the one
  before is green.
- Live checks never start or stop Notion play/playtest, never restart the harness, never create Notion
  agents, automations or chats outside /api/operator/delegate.
- All live files stay under work/canary/<suite>-<id>/ (canary.py `_guard`). A check that writes outside
  fails immediately.
- Every live check records its numbers into the report Markdown written by `render_report`.
- No duplicate sends (applies to all layers):
  - every outgoing message has a step id; the run is failed if the same step id, or the same text into
    the same thread within 60 s, appears twice in the event log;
  - a send that raised is never retried (turn_watch rule); only an explicit busy result may retry;
  - counted from events `turn_resume*`, `coworkers_done*`, `main_continue*`, `delegate*`.

## 1. Feature parity in the new window

Input: the parity list from 01-parity.md (one row per old feature). Each row gets one test id
`P-<nn>` and one of the check kinds below. A feature without a check counts as missing.

### 1a. Mocked (static + node DOM)

- Pattern of tests/test_desktop_live.py: static checks (prefixed classes, no innerHTML, no network,
  no removal of Notion nodes, LF/UTF-8) plus `node --check` and a jsdom-free node DOM run.
- New file tests/test_desktop_parity.py: for each P-row assert the module registers its part, the
  setting key exists in desktop_settings.py, server.py and settings.js (same as SERVER_BOOLS in
  tests/test_desktop_settings.py), and the sidebar/nav entry exists (test_desktop_shell_nav.py style).

### 1b. Live DOM over CDP 9333

- Attach to http://127.0.0.1:9333/json, pick the NotionTool window target (title/URL check, never a
  random tab). Read-only `Runtime.evaluate`; no clicks unless the row is about an action.
- Per P-row: a selector that must exist and be visible (`offsetParent !== null`, size > 0), the
  expected text (English), and for actions one click plus the expected DOM change within 3 s.
- Screenshot per row: `Page.captureScreenshot` of the window, 1280x720, saved next to the report as
  P-<nn>.png; plus one screen_capture of the verified window for the overview. No unchanged loops.
- Pass: every P-row present + visible + action works; zero console errors (`Runtime.exceptionThrown`)
  during the run; window still responsive (evaluate round trip < 200 ms).

## 2. Plan-first team mode (canary suite `plan`)

canary.py has only roles, parallel, health. Add `plan` to SUITES and TIMEOUTS (proposed limit 30 min),
with `plan_plan(rel)` and `check_plan(run, tasks, events)` next to `check_roles`.

### 2a. Mocked

- tests/test_operator_plan_mode.py with the FakeStore/FakeAdapter style of test_turn_watch_operator.py:
  - delegation in plan mode creates planner tasks only; no executor task before the plan is approved;
  - approval creates exactly the planned executor tasks (ids match plan items);
  - rejection/edit creates no tasks; a second approval click is a no-op;
  - the Main gets one 'all coworkers are finished' message per batch (turn_watch `check_all_done`).

### 2b. Live with real coworkers

Scenario: the Main delegates a small sandbox job (3 files + one test) in plan mode.

| Step | Expected | Measured from |
|---|---|---|
| delegate | run with plan stage, 1..n planner tasks | /api status, task.stage |
| plan ready | plan file under the sandbox, one item per executor task | sandbox files |
| approve | executor tasks = plan items, none earlier | task created times vs approval event |
| execute | tasks run in parallel where the plan allows (`peak_overlap` >= 2) | task started/finished |
| verify | sandbox test passes (`sandbox_test`) | report |
| finish | exactly one coworkers_done_sent to the Main | events |

Pass criteria: all rows true; no executor started before approval; no extra Main or agent created
(`_mains`, `_agents` before/after equal); zero duplicate sends (section 0); run finished within the limit.

## 3. Speed fixes (before/after from events)

- Baseline first: run suite `health` (about 30 s) and `parallel` on the current main build; keep the
  reports as before.
- Numbers per run (from the event log and `counters_of`):
  - time to first token after a send (send event -> first stream event), p50/p95;
  - stall count: turns with no stream event for > 60 s; resume messages sent;
  - sleeping-tab wakeups and their duration;
  - UI round trip over CDP (evaluate) p95 and long tasks > 200 ms;
  - total parallel wall time (`total_s`) and `peak_overlap`.
- After: same suites on the fix branch, same machine, same number of coworkers, two runs each.
- Pass: p95 first token and stall count each at least 30 percent lower or within noise if already low;
  no metric worse by more than 10 percent; no new errors; duplicate sends still zero.
- Mocked side: tests/test_desktop_no_lag.py style checks for throttling and timers stay green.

## 4. Order of the final test run

1. python -m pytest -q (all) in the task worktree.
2. Parity DOM check over CDP 9333 with screenshots.
3. canary health, then parallel, then plan.
4. Compare before/after numbers, write the summary table into the canary report.

Overall pass: every step green and the duplicate-send check is zero in all live runs.
