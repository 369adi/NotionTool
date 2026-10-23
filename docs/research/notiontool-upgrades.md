# NotionTool upgrades - final ranked list

Sources: kept items K1-K12 and the four conditional extras of `05-candidates-critique.md`, and the gap fills G1-G8 of
`06-gap-fill.md`. Rules R1-R12 and needs A1-A6 / B1-B8 come from `02-plan-critique.md`. Screens and their look are out
of scope. Code references were checked in this worktree (HEAD 61ca7d4). Nothing here was installed or run, so every
gain is a target with a way to measure it.

## How the ranking works

- Gain 1-5 for NotionTool: 5 = removes a priority-1/2 cost from 02 (A1 hand rebuilds, A2 read/search steps);
  3 = removes a known defect or a measured waste; 1 = nice to have or still unproven.
- Effort: S = 1 (under a day, one lane), M = 2 (one to three days, or needs a replay or baseline first).
- Score = gain / effort, highest first. Within one score, risk sorts low, then medium, then high; equal score and
  equal risk keep the source order (05 before 06). One exception: rank 19 (diff-cover) has a score of 2.0 but is held
  behind rank 11 (pytest), so it is listed with the 1.0 items, after the low-risk ones and before the other
  medium-risk ones, because it is already scheduled (batch 4) while those are still conditional.
- Items marked "Cond" are built only after the named test passes. "Dev" items never go into the EXE.
- Merged on purpose: K4 + G1 (the same function), K8 + G4 (both change the pytest command and the failed-test
  parser).

## 1. Ranked table

| Rank | Name | Link | Need | Gain | Effort | Score | Risk | Licence |
|---|---|---|---|---|---|---|---|---|
| 1 | git rerere + zdiff3 + histogram (K2) | https://git-scm.com/docs/git-rerere | A1 | 4 | S | 4.0 | low | git GPL-2.0 (tool, not bundled) |
| 2 | Wire the existing eol_guard into the bridge write path (G8) | own: notiontool/eol_guard.py | B8 | 3 | S | 3.0 | low | own code |
| 3 | PyInstaller collect_submodules + import-all smoke test (K12) | https://github.com/pyinstaller/pyinstaller | B4 | 3 | S | 3.0 | low | GPL-2.0+ with bootloader exception (build tool) |
| 4 | ruff check, fixed rule set, in the gate (K5, Dev) | https://github.com/astral-sh/ruff | A3 | 3 | S | 3.0 | low | MIT |
| 5 | Worktree hardening (K6, Dev technique) | https://git-scm.com/docs/git-worktree | A5 | 3 | S | 3.0 | medium | git GPL-2.0 (tool) |
| 6 | SQLite auto_vacuum=INCREMENTAL + one VACUUM (K11) | https://www.sqlite.org/pragma.html#pragma_auto_vacuum | B6 | 3 | S | 3.0 | medium | SQLite public domain |
| 7 | Mergiraf as merge driver (K1, Dev, Cond: replay) | https://codeberg.org/mergiraf/mergiraf | A1 | 5 | M | 2.5 | medium | GPL-3.0 (subprocess only, R2 ok) |
| 8 | Code index: ast + SQLite FTS5 trigram + ranked repo map (K3) | https://www.sqlite.org/fts5.html | A2 | 5 | M | 2.5 | medium | PSF / public domain, own code |
| 9 | One estimate_tokens, then calibration (K4 + G1) | own: notiontool/chat_context.py | A2 | 2 | S | 2.0 | low | own code |
| 10 | est_s versus real seconds per plan part (G3) | own: notiontool/parallel_plan.py | A4 | 2 | S | 2.0 | low | own code |
| 11 | pytest + xdist + timeout gate with junit known-failures check (K8 + G4, Dev, Cond: parity) | https://github.com/pytest-dev/pytest-xdist | B1 | 4 | M | 2.0 | medium | MIT (pytest, xdist, timeout) |
| 12 | duckduckgo-mcp-server for research roles, off by default (G7a, Cond: trial) | https://github.com/nickclyde/duckduckgo-mcp-server | B7 | 2 | S | 2.0 | medium (R8: queries leave the PC) | MIT |
| 13 | Declared task-state transition table (K9) | own: new notiontool/task_states.py | B2 | 3 | M | 1.5 | medium | own code |
| 14 | msgspec schema for plan, review and integrator results (K7) | https://github.com/msgspec/msgspec | A6 | 3 | M | 1.5 | medium | BSD-3-Clause |
| 15 | Rotating JSON-lines run log + coworker read tool (G5) | https://docs.python.org/3/library/logging.handlers.html | B3 | 3 | M | 1.5 | medium | PSF (stdlib) |
| 16 | Hypothesis stateful engine test (K10, Dev, Cond: Notion fake) | https://github.com/HypothesisWorks/hypothesis | B2 | 2 | M | 1.0 | low | MPL-2.0 (tests only) |
| 17 | gitleaks as second secret scan (extra, Dev, Cond: seeded test) | https://github.com/gitleaks/gitleaks | A3 | 1 | S | 1.0 | low | MIT |
| 18 | time-machine in clock tests that fail under load (extra, Dev) | https://github.com/adamchainz/time-machine | B1 | 1 | S | 1.0 | low | MIT |
| 19 | diff-cover + coverage.py on changed lines (extra, Dev, Cond: after rank 11) | https://github.com/Bachmann1234/diff_cover | A3 | 2 | S | 2.0, held | medium (gate time) | Apache-2.0 |
| 20 | mypy type check, new findings only (G2, Dev, Cond: trial) | https://github.com/python/mypy | A3 | 2 | M | 1.0 | medium | MIT |
| 21 | json-repair as second decode stage (extra, Cond: after rank 14) | https://github.com/mangiucugna/json_repair | A6 | 1 | S | 1.0 | medium (silent wrong repair) | MIT |
| 22 | Ed25519 signature on updates (G6, Cond: owner decides key custody) | https://github.com/pyca/cryptography | B4 | 2 | M | 1.0 | high (key handling) | Apache-2.0 OR BSD-3-Clause |

Rank 19 has a score of 2.0 but is held behind rank 11: on today's 64-727 s gate, coverage would add time before
the gate is fast. Not on the list: B5 (closed on purpose, `network_budget.py` and `step_guard.py` stay) and every
item rejected in 05 section 4 and 06 "Rejected in this round".

## 2. Build plans

Each plan has the files, the steps, how to test it and what may break. Line numbers are from HEAD 61ca7d4.

### Rank 1 - git rerere + zdiff3 + histogram (K2)

- Files: `notiontool/patch_mode.py` (repo setup next to the worktree code, `conflict_hunks` at 121);
  `tests/test_patch_mode.py`.
- Steps: (1) on start-up set, in the project repo only, `rerere.enabled=true`, `rerere.autoupdate=true`,
  `merge.conflictStyle=zdiff3` and `diff.algorithm=histogram` (all four are unset today; set them only when unset, so
  an owner choice wins). (2) Teach `conflict_hunks` the `|||||||` base section, so the conflict brief shows ours, base
  and theirs. (3) When `_merge` (533) remerges a branch, note in the result when rerere resolved a hunk.
- Test: a unit test with a zdiff3 conflict text for `conflict_hunks`; a test repo with the same conflict solved twice,
  where the second remerge is clean; then `python -m unittest tests.test_patch_mode`.
- May break: a conflict parser that assumes two sections; rerere can replay a wrong old resolution, so the gate
  still runs after every rerere merge (it does today: `verify` 371 after `_merge`).

### Rank 2 - Wire eol_guard into the bridge write path (G8)

- Files: `notiontool/bridge.py` (write check near line 825); `tests/test_eol_guard.py`. The matching call in
  `patch_mode.py` is done in the patch_mode lane (batch 2) to keep the lanes apart.
- Steps: (1) before writing an existing file, read its bytes and call `eol_guard.normalize(text, existing)`
  (`normalize` at eol_guard.py line 68 keeps the dominant ending and the BOM state); (2) for new files, read the eol
  attribute (`git check-attr eol -- <path>` or a small `.gitattributes` reader) and pass it as `default=` to
  `eol_guard.normalize(text, None, default=...)` (`'lf'` or `'crlf'`; `'lf'` when the attribute is unset).
  `eol_guard.detect(data)` (line 44) only classifies the bytes of an existing file, so it is not used for new files;
  (3) report a changed ending in the write result.
- Test: writing LF text into a CRLF file keeps CRLF; a new `.md` gets the ending its eol attribute asks for;
  `python -m unittest tests.test_eol_guard tests.test_repo_eol`.
- May break: binary or mixed-ending files; skip anything where `eol_guard.detect` on the existing bytes returns
  `mixed`, and leave it as given; `git check-attr` adds one subprocess per new file.

### Rank 3 - PyInstaller collect_submodules + import-all smoke test (K12)

- Files: `NotionTool.spec` (hiddenimports at line 8); `.github/workflows/release.yml`; new `tests/test_import_all.py`.
- Steps: (1) replace the hand-typed notiontool names with `collect_submodules('notiontool')` and keep the
  third-party ones (webview, PIL); (2) add a hidden `--self-test-imports` start flag that imports every notiontool
  module and exits 0; (3) run it on the built EXE in release.yml before upload.
- Test: the new unit test imports every module of the package; a local build, then `NotionTool.exe
  --self-test-imports` returns 0.
- May break: modules that run code at import time (they will now be bundled and imported); the EXE grows by the
  modules that were missing before, a few hundred KB.

### Rank 4 - ruff check in the gate (K5, Dev)

- Files: `notiontool/patch_mode.py` (`test_commands` 344); `requirements-dev.txt` (new, all dev pins at once,
  see batch 1); `tests/test_patch_mode.py`.
- Steps: (1) add `('lint', [python, '-m', 'ruff', 'check', '--select', 'F821,F811,F401,E9', '--output-format',
  'json', <changed .py files>])` before the unit tests; (2) fail only on findings that are not already on main, using
  the same baseline idea as `main_baseline` (402); (3) skip with a note when ruff is not installed.
- Test: a task that adds an undefined name goes red; a task that touches a file with an old F401 stays green.
- May break: F401 in `__init__`-style re-exports; keep the rule list fixed and small.

### Rank 5 - Worktree hardening (K6)

- Files: `notiontool/patch_mode.py` (267-272, 423-425, `_remove` 618-622); `notiontool/worker_gc.py` (new
  reconcile function next to `plan_cleanup` 55); `tests/test_patch_mode.py`.
- Steps: (1) set `core.longpaths=true` in the project repo; (2) `git worktree lock --reason <task id>` while a task
  is live, unlock before remove; (3) in `_remove`, retry `worktree remove --force --force` with a short back-off when
  Windows still holds a file; (4) a reconcile step that reads `git worktree list --porcelain` and removes worktrees
  whose task is finished and older than N days (64 are registered today).
- Test: a locked worktree survives `worktree prune`; the reconcile step on a test repo with 3 stale and 1 live
  worktree removes exactly 3; count the registered worktrees before and after one week.
- May break: removing a worktree that a review still reads (bridge.py `worktree` 297 uses the reviewed task's
  copy). The reconcile step must skip any worktree that a live or reviewed task names.

### Rank 6 - auto_vacuum + one VACUUM (K11)

- Files: `notiontool/store.py` (`Store.__init__` 43-44); new `tests/test_store_vacuum.py`.
- Steps: (1) at start-up, before the engine threads start, read `PRAGMA auto_vacuum`; if it is 0, set
  `auto_vacuum=INCREMENTAL` and run one `VACUUM` (needed for the change to take effect); (2) in an idle step run
  `PRAGMA incremental_vacuum(500)`; (3) log the freelist count before and after.
- Test: a test DB with many deleted rows shrinks and keeps all rows; the real DB (about 325 MB, 63 % free pages)
  should end near 125 MB. The first VACUUM on the real DB is started by the owner or the Main: coworkers cannot
  touch data/.
- May break: the one-time VACUUM needs about the DB size in free disk space and blocks start-up for seconds; skip
  it with a note when free space is too low. It runs on the single shared connection (R12).

### Rank 7 - Mergiraf as merge driver (K1, Dev, Cond: replay)

- Files: `.gitattributes` (`*.py merge=mergiraf`, plus .js/.json/.md only if the replay shows a gain); repo git
  config `merge.mergiraf.driver`; `notiontool/patch_mode.py` (`_merge` 533, probe 546, merge 552); new
  `tools/merge_replay.py`; the patch_mode half of rank 2 (eol call) goes in the same lane.
- Steps: (1) replay first: rebuild the 15 branches that were rebuilt by hand on 2026-10-22 with and without the
  driver; (2) only if at least a third of the conflicting files merge clean and pass py_compile and the tests, add the
  attribute and the driver line (only when `mergiraf` is found on PATH); (3) find out whether `merge-tree --write-tree`
  (546) uses the driver; if not, the probe must not report conflicts that the real merge (552) would solve.
- Test: the replay report (clean files, gate result per branch); a unit test where the probe and the merge agree.
- May break: GPL-3.0 means subprocess only, never bundled or imported; a structural merge can produce code that
  parses but is wrong, so the gate must run after every driver merge.

### Rank 8 - Code index with ranked repo map (K3)

- Files: new `notiontool/code_index.py`; `notiontool/devtools.py` (`nt_search_code` 206, `nt_file_outline` 224);
  `notiontool/bridge.py` (`OPERATOR_TOOLS` 132/174, one new read-only tool); `notiontool/context_pack.py`
  (`BUDGET_CHARS` 40000); new `tests/test_code_index.py`.
- Steps: (1) per worktree, parse every .py with `ast` into symbols, imports and call names; store them in an FTS5
  trigram table in a per-worktree DB file (not in the main store, R12); (2) refresh only files whose mtime or size
  changed; (3) new tool `nt_find_symbol` (definition, callers, importers in one call); (4) a ranked repo map
  (PageRank over import and call edges, about 60 lines) that `context_pack` uses to fill its 40,000 chars with the
  most connected symbols of the task's files.
- Test: on this repo, `nt_find_symbol estimate_tokens` returns the two definitions and their callers in one call;
  measure read/search calls per task over 10 tasks before and after (baseline: 44 % of tool calls).
- May break: FTS5 inside the frozen EXE (check once with the built EXE); stale index after a git checkout, so the
  refresh check must run before each query.

### Rank 9 - One estimate_tokens, then calibration (K4 + G1)

- Files: step 1: `notiontool/chat_context.py` (13), `notiontool/context_archive.py` (33), `bench/context_switch.py`
  (80); callers stay unchanged (`context_budget.py`, `run_context.py`, `tools/context_latency_report.py`).
  Steps 2-3: `notiontool/chat_context.py`, plus `notiontool/context_budget.py` if the calibration factor is applied
  there, plus the default of the new setting in `notiontool/store.py` `settings()` (189-203).
- Steps: (1) keep the definition in `chat_context.py`, import it in the other two (check for a circular import
  first); (2) check whether Notion step records carry a real token count; (3) only if they do, fit the divisor per
  content kind (code, prose, JSON) on one week of data. Step 1 is batch 1 lane T; steps 2-3 are batch 3 lane T2.
- Test: `python -m unittest tests.test_context_archive tests.test_context_budget tests.test_context_switch_policy
  tests.test_harness_context tests.test_run_context_v2` stay green after step 1 and after step 3; for step 3, the
  median error over at least 200 turns must drop.
- May break: an import cycle between `chat_context` and `context_archive`; a new divisor changes budget decisions,
  so ship it behind a setting first.

### Rank 10 - est_s versus real seconds (G3)

- Files: `notiontool/server.py` (1120-1134), `notiontool/store.py` (one new record kind in the objects table, not
  `event()` at 123, which keeps only 2000 rows), `notiontool/parallel_plan.py` (stats near 187,
  `delegation_decision` 232); `tests/test_parallel_plan.py`.
- Steps: (1) record plan id, part id, role and est_s when `/api/operator/plan` answers; (2) store real seconds when
  the task ends; (3) a report with the median and 90th percentile of real/est per role; (4) feed the factor to
  `delegation_decision` only after it shrinks the error.
- Test: unit tests for the report on fixed records; after 20 plans, compare the error with and without the factor.
- May break: object-table growth (cap or prune old records); a wrong factor flips delegation decisions, so keep it
  read-only until measured.

### Rank 11 - pytest gate with junit known-failures check (K8 + G4, Dev, Cond: parity)

- Files: `notiontool/patch_mode.py` (`test_commands` 344, `_run_tests` 385, `_FAILED_TEST` 155, `failing_tests`
  159, TEST_TIMEOUT_S 37, `main_baseline` 402); `notiontool/auto_accept.py` (regex copy, `MAX_FAILED_TESTS` 43);
  `notiontool/fix_loop.py` (35, 166); `tests/test_patch_mode.py`.
- Steps: (1) parity run on a quiet PC: unittest and `pytest -n 4 --timeout 300` on the same commit, compare the failed
  names; go on only if the sets match after name mapping; (2) switch the command to pytest with
  `--junitxml=<file>`; (3) one function reads failing ids from the XML (stdlib `xml.etree`, about 25 lines) and
  replaces the regex in patch_mode, auto_accept and fix_loop; (4) rule: green when the task's failing set is a subset
  of main's failing set; (5) keep unittest as the fallback when pytest is missing.
- Test: the parity report; a test that is red on main and on the task does not block; replay the 15 hand-rebuilt
  branches and count the rebuilds the subset rule avoids; gate time before and after on a quiet PC (64-75 s today).
- May break: tests that share files or ports run in parallel for the first time; tests that rely on run order;
  timing tests under load. Mark those `xdist_group` or serial.

### Rank 12 - duckduckgo-mcp-server (G7a, Cond: trial)

- Files: `notiontool/local_mcp.py` only. No stored default exists for `blender_enabled` (`store.py` `settings()`
  189-203 does not list it): `notiontool/local_mcp.py:95` `enabled()` falls back to False
  (`settings.get(server['setting'], False)`). The new entry goes in `SERVERS` next to the blender entry at
  `notiontool/local_mcp.py:62` with `setting='web_enabled'`, so it is off by default, and `SETTINGS` (line 74)
  picks up the toggle.
- Steps: (1) read the server README first (tool names, handling of local and internal addresses); (2) add the `web`
  entry in `SERVERS` next to the blender entry (local_mcp.py:62), with `setting='web_enabled'` and the version
  pinned in the uvx command (`uvx_path` 16); (3) give it only to research roles.
- Test: the 10-query trial from 06 (8 of 10 usable searches and page texts); steps per research task with and
  without it; `python -m unittest tests.test_rea` (it asserts the list of toggles, so update that list too).
- May break: R8, because queries leave the PC (search text only, never source code); DuckDuckGo blocking scraping;
  an upstream change, which the pin covers; `tests/test_rea.py` line 56, which lists the toggles exactly. If research
  chains run less than weekly, R9 fails and it stays off.

### Rank 13 - Declared transition table (K9)

- Files: new `notiontool/task_states.py`; `notiontool/engine.py` (state writes at 637, 640, 1838, 1860, 2226;
  `LIVE_TASK_STATES` at 140); `notiontool/bridge.py` (`LIVE_TASK_STATES` at 183); `notiontool/file_leases.py`
  (`LIVE_TASK_STATES` at 20); new `tests/test_task_states.py`.
- Steps: (1) declare the states and allowed transitions; (2) one `set_state(task, new)` helper used by all five
  writes; (3) log-only first: an illegal move writes an `illegal_transition` event and still goes through; (4) merge
  the three live-state lists on purpose: file_leases has 5 states (running, cancelling, uncertain, checkpoint,
  preparing), engine and bridge have 2 (running, uncertain), so decide per caller which set it needs and name both
  sets in task_states.py.
- Test: unit tests for the table; a week in log-only mode with zero or explained `illegal_transition` events; then
  switch to refuse.
- May break: copying the 5-state set into engine or bridge changes which tasks count as live (leases, cleanup).

### Rank 14 - msgspec result schema (K7)

- Files: new `notiontool/result_schema.py`; `notiontool/engine.py` (json.loads at 786, 1737, 2241, 2272, 2283);
  `notiontool/operator_roles.py` (255, 437); `notiontool/plain_protocol.py`; `requirements.txt`; `NotionTool.spec`
  only if the build misses msgspec.
- Steps: (1) Structs for plan, review and integrator results with today's fields; (2) one `decode(kind, text)` that
  strips code fences and surrounding text, then decodes strictly; (3) on error, send the field name and reason back
  to the coworker once; (4) replace the seven `json.loads` sites.
- Test: decode tests on real stored results (copied by the owner into a test fixture, never read from data/ by a
  coworker); count how many fail before and after.
- May break: results with extra or missing fields that work today; start with `forbid_unknown_fields=False` and
  optional fields, then tighten.

### Rank 15 - Rotating JSON-lines run log (G5)

- Files: new `notiontool/runlog.py`; writers in `notiontool/notion_shell.py` (171-176),
  `notiontool/protocol_forward.py` (169), `notiontool/bridge.py` (1092 plus one read-only log tool),
  `notiontool/updater.py` (182); new `tests/test_runlog.py`.
- Steps: (1) a JSON-lines formatter with `store.redact()` on every record; (2) one `RotatingFileHandler` per process
  (`runlog-main.jsonl`, `runlog-gateway.jsonl`, 5 MB x 5), because two processes renaming one file fail on Windows;
  (3) route the existing writers through it and keep the old files until the new log is trusted; (4) a read-only
  bridge tool that returns the last N redacted lines for a task id, because coworkers cannot read data/.
- Test: rotation at the cap; redaction of a planted fake secret; two processes writing at the same time; time from
  'failed' to 'cause found' over the next 10 failed tasks.
- May break: secrets in log lines (redact before writing); disk use (the cap covers it).

### Rank 16 - Hypothesis stateful engine test (K10, Dev, Cond: Notion fake)

- Files: new `tests/test_engine_state_machine.py`; a deterministic Notion fake in `tests/`.
- Steps: (1) a RuleBasedStateMachine over engine `_dispatch`, `_complete`, `resolve_uncertain`, `retry_blocked`;
  (2) the rank 13 table as the oracle; (3) `derandomize=True`, under 30 s.
- Test: the run itself; drop it if it finds nothing after a fair try.
- May break: nothing at runtime (tests only); flaky if the fake is not deterministic.

### Rank 17 - gitleaks second opinion (extra)

- Files: `notiontool/patch_mode.py` (`secret_scan` 459, `SECRET_TEXT` 52).
- Steps: a one-time test with 10 planted fake secrets and the repo history; add `gitleaks detect --no-git --source
  <changed files>` only if it finds what SECRET_TEXT misses or avoids its false positives.
- Test: the seeded test report.
- May break: false positives that block merges; run it as a warning first.

### Rank 18 - time-machine (extra)

- Files: `tests/test_limits.py` (EightSlotTests) and only tests that fail on the clock under load.
- Steps: replace manual clock patching with `time_machine.travel(..., tick=False)` in those tests.
- Test: those tests pass 20 runs in a row on a loaded PC.
- May break: nothing at runtime. It does not remove the 59 `time.sleep` calls.

### Rank 19 - diff-cover on changed lines (extra, after rank 11)

- Files: `notiontool/patch_mode.py` (`test_commands`).
- Steps: run coverage only on the changed modules' tests, then `diff-cover coverage.xml --compare-branch <base>`,
  and report the number without failing at first.
- Test: the extra gate seconds on 10 tasks; keep it only if the report is cheap and the reviewer uses it.
- May break: gate time and coverage files left in worktrees.

### Rank 20 - mypy, new findings only (G2, Dev, Cond: trial)

- Files: `notiontool/patch_mode.py` (`test_commands` 344, a baseline like `main_baseline` 402 and `_baseline`
  203).
- Steps: (1) trial on main with `--check-untyped-defs`: count findings and time a warm run on 5 files;
  (2) if under 15 s warm, add `('types', [python, '-m', 'mypy', ...])` after ruff; (3) fail only on findings that
  are new against main, matched by file and message, not by line.
- Test: the three measures from 06; over 20 past merges, bugs it would have caught that ruff and the tests missed. If
  that count is 0, drop it.
- May break: a flood of old findings (the baseline covers it); cache folders in worktrees (keep them git-ignored).

### Rank 21 - json-repair (extra, after rank 14)

- Files: `notiontool/plain_protocol.py` or `result_schema.py`; `requirements.txt`.
- Steps: count the results that fail rank 14 for reasons other than fences or surrounding text; only if trailing
  commas or missing braces remain, add json-repair as a second stage that marks `repaired: true`.
- Test: decode tests on the failing samples.
- May break: a repair that guesses wrong silently; the marker and the schema check after it cover this.

### Rank 22 - Ed25519 update signature (G6, Cond: owner decision)

- Files: `notiontool/updater.py` (284-302); `.github/workflows/release.yml` (31-36); `requirements.txt`;
  `NotionTool.spec` if needed.
- Steps: (0) the owner decides where the private key lives; if it is in GitHub secrets, stop, because there is no
  gain; (1) sign the EXE on the owner's PC after the build and upload a `.sig`; (2) verify with the public key
  embedded as a constant; (3) refuse an update whose signature fails; (4) write a key-rotation note.
- Test: a release with a swapped EXE and hash is refused; a frozen-EXE test that cryptography loads.
- May break: lost key means no updates (keep a backup key); a semi-manual release flow.

## 3. Build order in parallel batches

Rule: inside one batch, no two lanes touch the same file. `patch_mode.py` is changed by eight items, so it gets one
lane (P) that runs its items one after the other. `requirements-dev.txt` is created once in batch 1 with every
dev pin (ruff, pytest, pytest-xdist, pytest-timeout, mypy, hypothesis, time-machine, coverage, diff-cover), so no later
lane touches it.

### Batch 1 - small and safe (all S)

| Lane | Items | Files |
|---|---|---|
| P | rank 1, then 4, then 5 | patch_mode.py, worker_gc.py, tests/test_patch_mode.py, requirements-dev.txt (new) |
| B | rank 2 | bridge.py, tests/test_eol_guard.py |
| S | rank 6 | store.py, tests/test_store_vacuum.py (new) |
| X | rank 3 | NotionTool.spec, .github/workflows/release.yml, tests/test_import_all.py (new) |
| T | rank 9 step 1 (estimate_tokens in one place) | chat_context.py, context_archive.py, bench/context_switch.py |

### Batch 2 - the two big wins, plus measurements

| Lane | Items | Files |
|---|---|---|
| P | rank 7 (replay first) and the patch_mode half of rank 2 | .gitattributes, patch_mode.py, tools/merge_replay.py (new), tests/test_patch_mode.py |
| C | rank 8 | code_index.py (new), devtools.py, bridge.py, context_pack.py, tests/test_code_index.py (new) |
| G | rank 10 | server.py, store.py, parallel_plan.py, tests/test_parallel_plan.py |
| M | rank 12 (trial, off by default) | local_mcp.py (no stored default exists: local_mcp.py:95 enabled() falls back to False; new entry in SERVERS next to the blender entry at local_mcp.py:62 with setting='web_enabled'), tests/test_rea.py |

### Batch 3 - gate, engine states, token calibration

| Lane | Items | Files |
|---|---|---|
| P | rank 11 (parity first) | patch_mode.py, auto_accept.py, fix_loop.py, tests/test_patch_mode.py |
| E | rank 13 | task_states.py (new), engine.py, bridge.py, file_leases.py, tests/test_task_states.py (new) |
| T2 | estimate_tokens steps 2-3 (rank 9; Cond: only if a real token count exists) | chat_context.py, context_budget.py (only if the factor goes there), store.py (setting default in settings() 189-203), tests/test_context_budget.py |

No other lane in batch 3 touches chat_context.py, context_budget.py or store.py. Rank 15 also touches bridge.py, so
it waits for batch 4.

### Batch 4 - results, logs, checks

| Lane | Items | Files |
|---|---|---|
| P | rank 20 trial, then rank 19 | patch_mode.py |
| R | rank 14 | result_schema.py (new), engine.py, operator_roles.py, plain_protocol.py, requirements.txt, NotionTool.spec |
| L | rank 15 | runlog.py (new), notion_shell.py, protocol_forward.py, bridge.py, updater.py, tests/test_runlog.py (new) |
| H | rank 16 (needs rank 13 from batch 3) | tests/test_engine_state_machine.py (new), tests/notion_fake.py (new) |
| K | rank 18 | tests/test_limits.py |

### Batch 5 - conditional extras

| Lane | Items | Files |
|---|---|---|
| P | rank 17 (seeded test first) | patch_mode.py |
| R | rank 21 (only if rank 14 leaves real repair cases) | plain_protocol.py or result_schema.py, requirements.txt |

### Batch 6 - only after the owner decides

| Lane | Items | Files |
|---|---|---|
| U | rank 22 | updater.py, .github/workflows/release.yml, requirements.txt, NotionTool.spec |

After each batch: run the full gate on main, merge lane by lane, and re-measure the baseline the next batch depends on
(gate time for batch 3, read/search share for rank 8, conflict count for rank 7).

## 4. Still open (needs a local run, not a lookup)

1. Rank 7: the 15-branch replay and whether `merge-tree --write-tree` uses the driver.
2. Rank 8: FTS5 trigram inside the frozen EXE (works in Python 3.12.10 / SQLite 3.49.1 on this PC).
3. Rank 9: whether Notion step records carry a real token count. Batch 3 lane T2 (estimate_tokens steps 2-3) is
   conditional on that: it runs only if a real token count exists, and whether the factor goes into chat_context.py
   or context_budget.py is decided then.
4. Rank 11: pytest/unittest parity by failed test name on a quiet PC.
5. Rank 12: the server README and the 10-query trial.
6. Rank 15: which bridge tools coworkers may call, to place the log read tool.
7. Rank 17: the seeded-secret test.
8. Rank 20: mypy time and finding count on main.
9. Rank 22: the owner's key-custody decision.
