# 04 - Candidates, area B (reliability, tests, store, packaging, integrations)

Role: search role for area B of docs/research/02-plan-critique.md (needs B1-B8). Date of checks: 2026-10-22.
Out of scope: the app's screens and look.

## 0. How this was checked (and what could not be checked)

- No web-search tool exists for this role (no web module). Every candidate was opened and checked through:
  - GitHub API with the owner's logged-in `gh` (licence, last push, last release, archived flag, stars) for 30 repositories;
  - PyPI JSON (licence, latest upload, wheel tags: win_amd64 or py3-none-any, Python requirement, core dependency count) for 23 packages;
  - the code of this worktree (grep/outline of patch_mode.py, store.py, updater.py, NotionTool.spec, engine.py, mcp_servers.py, tests/);
  - two real runs on the owner PC (section 4, proof for R3/R5).
- NOT done: blog posts / benchmarks from the wider web, awesome-mcp lists and the MCP registry were NOT browsed in detail (a registry query for "web search" returned nothing usable; `servers` repo README says it only houses 7 reference servers). Anything that only a web search would have found is therefore not in this list.
- The unauthenticated GitHub API was exhausted (0 of 60), so all GitHub data comes from the authenticated `gh`.
- The PC was heavily loaded by other coworker tasks during the runs: the unittest baseline took 727.5 s instead of the usual 64-75 s. Absolute times are therefore not comparable with the memory; only the ratio between the two runs is used, and it is marked as unproven where needed.

Baselines used (measured in this worktree or from the critique): store.py:43-44 sets only `journal_mode=WAL` and `synchronous=FULL`; no VACUUM, no auto_vacuum, no user_version anywhere in store.py; DB 325 MB with freelist 49,975 of 79,391 pages (63 %); NotionTool.spec:8-9 lists 9 hand-typed hiddenimports; tests/ has 0 hypothesis imports, 7 pytest imports, 59 time.sleep( calls; engine.py has 47 time.time/monotonic uses and only 12 injectable-clock spots; the repo has no `logging` use at all in notiontool/ (0 matches).

## 1. Accepted candidates (7 accepted candidates, max 12)

Order = priority of the need. "Dev-time" = never shipped inside the EXE.

### C1 - pytest + pytest-xdist (+ pytest-timeout) as an optional fast runner  [need B1]
- Links: https://github.com/pytest-dev/pytest-xdist , https://github.com/pytest-dev/pytest-timeout , https://github.com/pytest-dev/pytest
- Licence: MIT / MIT / MIT.
- Last activity: xdist last release 3.8.0 on 2025-07-01 (15 months, but commits on 2026-10-22 and an active tracker, so R1 passes through activity); timeout 2.4.0 on 2025-05-05, commits 2026-10-20; pytest 9.1.1 current.
- Windows proof: both wheels are py3-none-any (pure Python, Python >=3.9 / >=3.7); installed and run on this PC with Python 3.12.10 (proof below, section 4).
- Type: dev-time (not in requirements.txt, not in the EXE).
- Plugs into: `notiontool/patch_mode.py` - `verify()` (:371) and the command list (:362 task tests, :367 desktop suite), TEST_TIMEOUT_S (:37), and the failed-name parser `_FAILED_TEST` (:155) / `failed_tests` (:160). Tests need no change: pytest ran all 277 unittest-based files with no conftest.
- Replaces / adds: replaces only the runner `python -m unittest ...`; adds parallel workers (`-n 4`), a per-test timeout (`--timeout`) instead of one 900 s kill for the whole suite, and `--junitxml` for exact failed names instead of a regex on text.
- Expected gain: measured on this PC under heavy load: unittest sequential 727.5 s (3032 tests, 14 failures + 2 errors) vs pytest `-n 4` 360 s (3172 passed + 17 failed + 7 skipped), i.e. about 2x faster. The two runs were not simultaneous, so the 2x is an indication, not a proof. Measure: run both on a quiet PC, compare wall time and the set of failed test names.
- Risk: (1) failure sets differ (16 vs 17); the 17 include `test_limits.EightSlotTests` x5, `test_server_route_and_install_crlf` x2, `test_restart_argv...`, `test_telemetry...failed_attempt...`, `test_native...` - must be checked for parallel-only failures (shared temp files, ports, the single shared store) before it becomes the gate. (2) The gate stays `unittest` for the merge until the parity check passes; the parser in patch_mode.py must accept both outputs. (3) R12: the tests create their own stores in-process; xdist uses processes, so no shared connection - but tests that use fixed files/ports must be found.
- Effort: M.
- Why it beats unittest-parallel (the other candidate for this need): xdist + pytest was run end to end and measured; unittest-parallel could not even start in my one trial (section 3).

### C2 - time-machine  [need B1]
- Link: https://github.com/adamchainz/time-machine
- Licence: MIT. Last activity: release 3.5.1 on 2026-09-08, commits 2026-10-22, 1,016 stars.
- Windows proof: 8 win wheels (win_amd64 / win32 / arm64) on PyPI, Python >=3.10, 0 dependencies; installed and imported on this PC (section 4). C extension, so it is dev-time only.
- Plugs into: tests/ - `tests/test_limits.py` (EightSlotTests, failed in the pytest run), `tests/test_engine.py` fixtures, and the 107 places that patch `time.time/monotonic` or pass `clock=`. Production code is not changed: engine.py's 47 clock reads are patched from outside.
- Replaces / adds: replaces hand-made `patch('time.time')` helpers with one `time_machine.travel(..., tick=False)` context/decorator; works with plain unittest (decorator on TestCase methods), no pytest needed.
- Limit (honest): it does NOT skip `time.sleep()`; it only removes the need to really wait for a clock to advance. It does not cut the 59 sleep calls by itself.
- Expected gain: fewer timing failures under load. Measure: run `tests.test_limits` 20 times under parallel load before and after; target 0 flaky failures (baseline: failed under full load, memory 2026-10-16).
- Risk: low; dev-time; a frozen clock can hide real timing bugs, so use it only in tests that already mock the clock. Effort: S.
- Why not freezegun: last release 2025-08-09 (14 months), pure-Python patching is slower and misses `from time import time` imports that time-machine handles.

### C3 - Hypothesis (stateful testing, RuleBasedStateMachine)  [need B2]
- Link: https://github.com/HypothesisWorks/hypothesis
- Licence: MPL-2.0 (PyPI; GitHub shows NOASSERTION). Last activity: release 6.170.6 on 2026-10-22, 9,105 stars.
- Windows proof: 11 win wheels, Python >=3.11, one dependency (sortedcontainers); installed on this PC (section 4). Dev-time only; MPL-2.0 is file-level, not shipped.
- Plugs into: new test file `tests/test_engine_state_machine.py` driving `engine.Engine` (`_loop`/`tick`, `_dispatch`, `_complete`, `resolve_uncertain`, `retry_blocked`), `stale_tasks.py`, `turn_watch.py`, with the invariants taken from `file_leases.LIVE_TASK_STATES`.
- Replaces / adds: adds a test type that does not exist (0 hypothesis imports today): random sequences of events (answer arrives, restart, stale, retry, resume) with invariants such as "no task is lost", "a finished answer is never resent (I5)", "every state is in the declared table (C4)".
- Expected gain: finds ordering bugs of the kind fixed by hand (uncertain, orphaned, lost answer b59013e3, resend hazards). Measure: number of distinct invariant violations found in the first 5,000 generated runs; each real one becomes a fixed regression test. If it finds none after a fair try, drop it.
- Risk: needs a deterministic fake of the Notion calls and `derandomize=True` in the gate so the merge gate is not random; run time must stay below 30 s. Effort: M.

### C4 - Declared transition table (technique, no library)  [need B2]
- Prior art / links: https://github.com/fgmacedo/python-statemachine (MIT, release 3.2.2 on 2026-10-14, pure Python) and https://github.com/pytransitions/transitions (MIT) - only used as a model; NOT added as a dependency (see rejected list).
- Licence: none needed (own code, about 40 lines). Windows: n/a.
- Type: runtime (new module notiontool/task_states.py, shipped in the EXE)
- Plugs into: a new small module `notiontool/task_states.py` (enum + `ALLOWED: dict[state, set[state]]`), used by the engine's single place that writes the task state, and by C3 as the oracle. `file_leases.LIVE_TASK_STATES` becomes a view of it.
- Replaces / adds: replaces state strings checked in many places by one table; adds an assertion/telemetry event when an unlisted transition happens (log first, raise later).
- Expected gain: every wrong-state bug becomes a visible "illegal transition X->Y" event instead of a lost task. Measure: count of `illegal_transition` events per week. Baseline (B2 of 02-plan-critique.md, measured 2026-10-22): (a) state-fix commits in the last 30 days: at least 10 (git log since 2026-09-22 = 317 commits; 14 match the keyword grep uncertain|orphan|stale|stuck|lost answer|wrong state|resend|resume; 10 of them are engine/task-state fixes: 24a732c, 4042ce9, acb87a4, 3a18fbf, 9d5d83f, be20ce3, 1eab199, d79e3f3, 7ab6987, 89011e0; the other 4 are UI-only or mixed, so 10 is a lower bound because the grep is keyword-based). (b) Tasks in uncertain or orphaned states per run: host API GET /api/metrics holds 1,201 tasks in 195 runs; 7 tasks have an attempt that ended in state uncertain (0.04 per run), 46 tasks carry error_class REMOTE_UNKNOWN (0.24 per run), 19 tasks sit in state blocked (0.10 per run). No task-level uncertain or orphaned state exists today, so these three counts are the closest measurable proxies; with C4 they become one direct count.
- Risk: first run will show many legal-in-practice transitions; start in log-only mode. Effort: S-M.

### C5 - SQLite auto_vacuum=INCREMENTAL + a one-time VACUUM (technique)  [need B6]
- Links: https://www.sqlite.org/pragma.html#pragma_auto_vacuum , https://www.sqlite.org/lang_vacuum.html (SQLite is public domain; part of the Python standard library).
- Windows proof: standard `sqlite3` module of the Python 3.12.10 on this PC. Runtime, 0 MB added.
- Plugs into: `notiontool/store.py` `Store.__init__` (:43-44, add the pragma before the CREATE TABLE statements) plus a maintenance step in `backup.py` or the engine idle time that runs `PRAGMA incremental_vacuum(N)`.
- Replaces / adds: adds space reclaim (store.py never vacuums today).
- Expected gain: the DB is 325 MB, of which 49,975 of 79,391 pages (63 %, about 204 MB) are free. After one VACUUM the file should shrink to about 120 MB; backups (backup.py) and any copy of the data folder shrink with it. Measure: `PRAGMA freelist_count` and file size before/after; time of `Store` open.
- Risk: (1) VACUUM needs free disk about the DB size and locks the database for the whole run (not measured; probably several seconds to a minute). R12: store.py uses ONE shared connection with an RLock, so run it only at start-up before the engine threads start, or when idle, and never from a second connection. (2) Changing auto_vacuum on an existing DB only takes effect with a full VACUUM. (3) The bridge forbids coworkers from touching the data folder, so the owner or the Main must start it. Effort: S.

### C6 - `PRAGMA synchronous=NORMAL` in WAL mode (technique, owner decision)  [need B6]
- Link: https://www.sqlite.org/pragma.html#pragma_synchronous and https://www.sqlite.org/wal.html. Public domain, standard library.
- Plugs into: `notiontool/store.py:44` (`synchronous=FULL` today).
- Replaces / adds: replaces FULL with NORMAL. In WAL mode NORMAL is safe against an application crash and an OS crash that keeps the file system consistent; only the last commits can be lost after a power cut or a hard OS crash.
- Expected gain: fewer fsyncs per commit; project memory measured about 2.5 ms per event and 8.7 ms per put. Measure: a micro-benchmark of 1,000 `append_event` and `put` calls under FULL and NORMAL on this PC. Measured on 2026-10-22 on this PC (Windows, Python 3.12.10, throwaway store in a temp folder, real `Store.event` = the append_event path, and `Store.put`, 1,000 calls each, 3 rounds per mode in alternating order, median shown; PC loaded by other coworker tasks): FULL 4.915 s for 1,000 events (4.92 ms each) and 4.435 s for 1,000 puts (4.43 ms each); NORMAL 0.383 s for 1,000 events (0.38 ms each) and 0.236 s for 1,000 puts (0.24 ms each). Gain: 92 % for events and 95 % for puts, well above the 30 % bar, so C6 is kept. All 3 FULL runs: events 2.378 / 5.651 / 4.915 s; all 3 NORMAL runs: 0.316 / 0.462 / 0.383 s (ranges do not overlap). Note: this is a per-write saving of about 4.5 ms; whether it matters for the user depends on the write rate, which was not measured.
- Windows proof: standard `sqlite3` module of Python 3.12.10 on this PC; the benchmark above ran there; 0 MB added.
- Type: runtime (one pragma line in notiontool/store.py, shipped in the EXE).
- Risk: real durability trade-off. The owner must decide, because lost answers are already a pain point (I5). Mitigation: keep FULL for the `objects` table writes that hold task state and only use NORMAL for events, which means two connections - this conflicts with R12, so the simple all-NORMAL switch is the only variant offered here. Effort: S.

### C7 - PyInstaller `collect_submodules('notiontool')` in the spec  [need B4]
- Link: https://github.com/pyinstaller/pyinstaller (docs: PyInstaller.utils.hooks.collect_submodules). Licence: GPL-2.0-or-later with the bootloader exception (the output may be distributed under any licence; PyInstaller is already the project's build tool, so no licence change). Last release v6.22.3 on 2026-09-12; commits 2026-10-22.
- Windows proof: 2 win wheels on PyPI, already used in release.yml on windows-latest.
- Plugs into: `NotionTool.spec:8-9` (`hiddenimports=[...]` with 9 hand-typed names, 5 of them own modules (_build, autopilot, context_archive, dag, role_policy); four of them - autopilot, context_archive, dag, role_policy - were added after the EXE failed) and `.github/workflows/release.yml`.
- Replaces / adds: replaces the hand-typed list of own modules with `hiddenimports = collect_submodules('notiontool') + [...third-party...]`; adds a test that imports every module name in `notiontool/` (`pkgutil.walk_packages`) in the frozen EXE smoke step.
- Expected gain: removes the whole class of "ModuleNotFoundError only in the EXE". Measure: number of hand-typed own-module entries (own-module entries 5 -> 0; the 4 third-party entries (webview.platforms.edgechromium, PIL, PIL.Image, PIL.ImageGrab) stay) and a frozen-build smoke test that imports all modules (baseline: none).
- Risk: bundles modules that were left out on purpose (NOT WIRED ones), size grows by a few hundred KB; exclude test helpers by a filter. Effort: S.

## 2. Needs with no accepted candidate

- B3 Logging and tracing: nothing accepted. notiontool/ has no `logging` use (0 matches), the events table is the log, and coworkers cannot read the data folder (the bridge refuses it), so a log file in data/ would only help the owner. No library has a number behind it (R5).
- B5 Limits and retries: nothing accepted (see rejected: PyrateLimiter, stamina, tenacity).
- B7 Weekly MCP servers: nothing accepted (see rejected: reference servers, Serena). No server was found whose weekly calls would replace several nt_shell / nt_search_code calls.
- B8 Line endings: nothing external. The need is to wire the existing `eol_guard.py` (NOT WIRED) into `bridge.write_file` and `patch_mode`; no outside tool beats it (R11).

## 3. Rejected tempting items

| Item | Facts checked | Rejected because |
|---|---|---|
| unittest-parallel (MIT, 1.8.6, 2026-08-22, pure Python) | my trial `python -m unittest_parallel -j 4 -s tests -p test_*.py -t .` ended in 1.9 s with "Start directory is not importable" (not retried; my invocation or the missing tests package) | R10: C1 was measured end to end, this was not; keeps unittest output but unproven. Retry only if C1 fails the parity check |
| pytest-rerunfailures (MPL-2.0, 16.7, 2026-09-17) | active | R5/I14: re-running hides flaky failures instead of fixing them; breaks the "never trust done" rule and the known-failures baseline |
| freezegun (Apache-2.0, 1.5.5, 2025-08-09) | last push 2025-08-19 | R1 (14 months without release) and loses to time-machine (C2) |
| pytest-json-report (MIT, last release 2022-03-15, last push 2023-07-31) | not maintained | R1; pytest's built-in `--junitxml` does the same |
| python-statemachine (MIT, 3.2.2, 2026-10-14) / transitions (MIT, 0.9.3, 2025-07-02, last push 2025-09-11) | transitions: 13 months without a push | R10/R4: the engine state lives as strings in store `objects` documents and in many modules; a state-machine framework would own the state and force a rewrite of engine.py (2,294 lines). C4 gets the benefit with 40 lines. transitions also fails R1 |
| structlog (MIT OR Apache-2.0, 26.1.0, 2026-06-06) / concurrent-log-handler (Apache-2.0, 0.9.31, 2026-10-21, needs portalocker) / loguru (MIT, last release 2024-12-06) | all alive except loguru's release date | R5/R11: no logging code to replace, events already structured in SQLite, no number for the gain; coworkers cannot read data/ files; loguru also R1 |
| OpenTelemetry SDK | not opened in detail | R5/R7: no named gain, large dependency tree; not evaluated further |
| PyrateLimiter (MIT, 4.5.1, 2026-10-20) / stamina (MIT, 26.1.0) / tenacity (Apache-2.0, 9.2.1, 2026-10-07) | all alive, pure Python | R10/R11: network_budget.py (464 lines) has a persisted window, `CircuitOpen`, `RemotePaused`, `run_with_priority` and per-kind breakers that none of them offer; step_guard.py (703 lines) is a budget of AI steps, not a retry. Only 8 of 313 commits in 7 days touch this area, and the existing limit tests would have to pass unchanged |
| tufup (MIT, 0.10.0, 2025-10-04) | 7 dependencies, no push in 12 months | R1, R10 (a framework that owns the update flow of updater.py) |
| sigstore-python (Apache-2.0, 4.5.1, 2026-10-20) | 13 dependencies, needs the Sigstore trust root and network | R3 (not offline), R7 (size), R10 |
| Nuitka (AGPL-3.0) | very active | R2 (AGPL) and R7 (needs a C compiler on the build machine) |
| PyInstaller onedir instead of onefile | not measured | effort L; updater.py:284 downloads one ASSET file; no number for the antivirus gain (R5) |
| pyinstaller-hooks-contrib (Apache/GPL, 2026.9) | active | already used automatically by PyInstaller; no gain for own modules |
| editorconfig-checker (MIT, v4.0.2, 2026-09-19; PyPI 3.11.1 has no wheels) | Go binary from GitHub releases | R11: duplicates `eol_guard.py` and `test_repo_eol.py`; R3 on PyPI (sdist only) |
| ruff format (MIT, 0.17.2, 2026-10-22) | win wheels | R10: reformatting the whole repo would create merge conflicts and break the 20 CRLF files pinned in .gitattributes. `ruff check` is a machine check for area A (reviews), not B |
| yoyo-migrations / alembic / sqlite-utils (Apache-2.0, 4.2.1) | not needed | R10: store.py has 5 `CREATE TABLE IF NOT EXISTS` statements and no migration history; `PRAGMA user_version` (stdlib) is enough when a first real migration comes |
| modelcontextprotocol/servers: git, filesystem, time, memory, sequentialthinking, everything, fetch (v2.0.0, 2026-10-19; licence is moving from MIT to Apache-2.0, both fine) | node and uvx exist on the PC | R9/R11: git duplicates `nt_shell` git, filesystem duplicates nt_read_file/write/edit/list_dir, fetch duplicates `Invoke-WebRequest` through nt_shell; I could not name a weekly use with a call count; the other four are demos |
| Serena (oraios/serena, 2.1.0, 2026-10-19, 30,616 stars) | PyPI licence GPL-3.0-or-later, 33 dependencies, needs language servers | R2 (GPL as runtime), R7 (size), R11 (duplicates nt_search_code / nt_file_outline); belongs to area A2/A5 if anywhere |
| gitleaks (MIT, v8.30.1, 2026-03-21), detect-secrets (Apache-2.0, last release 2024-05-06) | gitleaks MIT v8.30.1 2026-03-21; detect-secrets Apache-2.0 last release 2024-05-06; the existing scan is SECRET_TEXT in notiontool/patch_mode.py lines 42-112 | area A (secret scan; patch_mode already has one); detect-secrets also R1 |
| pip-audit (Apache-2.0, 2.10.1, 2026-06-10) | 10 dependencies | R5: only 4 direct requirements, no number for the gain |
| zizmor (MIT, v1.31.1, 2026-10-15, commits 2026-10-22, 6,755 stars; 2 win wheels on PyPI, Python >=3.11, no Python dependencies) (was C8) | audits only .github/workflows/release.yml (builds the EXE and the sha256 file that updater.py:284-292 trusts) | R5 and the goal's no-forced-finds rule: low gain, one workflow, so a one-time check and not a weekly tool; no need for a permanent candidate. See section 5 for the one-time manual run |

## 4. Proof from the owner PC (Windows, Python 3.12.10)

1. `pip install --target %TEMP%\nt_res_b pytest pytest-xdist pytest-timeout time-machine hypothesis structlog concurrent-log-handler` succeeded without a compiler: pytest 9.1.1, pytest-xdist 3.8.0, pytest-timeout 2.4.0, time-machine 3.5.1, hypothesis 6.170.6, structlog 26.1.0, concurrent-log-handler 0.9.31 (+ portalocker, execnet, sortedcontainers, ...). Total about 19.3 MB on disk (dev-time).
2. `python -m pytest tests -n 4 -q -p no:cacheprovider --timeout=300` in this worktree: 17 failed, 3172 passed, 7 skipped, 908 subtests passed in 360.46 s.
3. `python -m unittest discover -s tests -p test_*.py` in this worktree: Ran 3032 tests in 720.07 s, FAILED (failures=14, errors=2, skipped=7).
4. Both runs happened while other coworker tasks were running, so the PC was loaded. Parity of the failed sets was not compared by name (the pytest tail only showed 11 names).

## 5. Notes for the next roles

- C1 must be validated first (name-by-name parity of failed tests, then the parser change in patch_mode.py); C2-C4 are independent small steps.
- C5 is the biggest measurable win in this area: about 200 MB free pages; the design question is only when VACUUM may run (single shared connection, R12).
- Gap: coworkers have no web search; this role had to use `gh` and the PyPI API. A candidate for area A/B7 would be a web-search/fetch tool, but no server was proven to be called weekly.
- zizmor (moved to the rejected list, was C8) can be run once by hand on .github/workflows/release.yml: `pipx run zizmor --offline .github/workflows` (or `pip install zizmor` into a temp folder). It is not a permanent tool; only act on findings that are real.
- C6 was benchmarked (section 1): the gain is large, but it trades durability for speed after a power cut, so the owner must decide before the pragma changes. C4, C5 and C6 are runtime changes in notiontool/ (task_states.py, store.py); C7 is a build-time change to NotionTool.spec and .github/workflows/release.yml; C1, C2 and C3 are dev-time.
