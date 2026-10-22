# 01 - Research plan: open-source candidates for NotionTool

Scope: engine, delegation, prompts, MCP bridge, merging, store, tests, packaging, integrations.
Out of scope: the app's screens and their look (desktop_shell, web/desktop, notion_shell overlay).

Sources read for this plan (worktree t8beb21c, 2026-10-22):
- project-memory/NotionTool.md (the copy in the main folder; the worktree has no project-memory/ folder, see note at the end)
- outlines of notiontool/engine.py (2,200+ lines, 140 KB), operator_main.py (433), team_graph.py (179),
  plain_protocol.py (346), prompt_once.py (79), bridge.py (65 KB), server.py (1,766), patch_mode.py (720), store.py (219)
- tests/: 277 test files, no pytest.ini / conftest.py / pyproject.toml, 59 time.sleep( calls and 11 threading.Thread uses in tests
- requirements.txt (pywebview, pyinstaller, websocket-client, pillow), NotionTool.spec, .github/workflows/release.yml

Hard frame for every candidate: 450 AI steps per 60 min (step_guard.py), 180 Notion requests per minute
(network_budget.py), Windows 10/11 desktop, Python 3.12, one-file PyInstaller EXE, private repo, no Defender exceptions (I15).

---

## 1. The 12 biggest needs and weaknesses today

Each need: what is wrong, the evidence, the file(s) it concerns, and what a good find must achieve.

### N1 - Merging coworker branches fails too often (merging)
- Files: notiontool/patch_mode.py (PatchMode, conflict_hunks, _start_merges in engine.py:1100-1280, remerge)
- Evidence: on 2026-10-22, 15 finished task branches could not be merged (real conflicts, red tests, main busy) and had to be
  rebuilt by hand onto main; lost answer b59013e3 stayed unmerged for a day; a branch test file was truncated (SyntaxError).
  Plain line-based `git merge` plus a coworker prompt "fix only the conflicts below".
- Goal: syntax-aware (AST) merging of Python, fewer textual conflicts, automatic rebase of short-lived branches before merge,
  and a conflict brief that shows intent, not just hunks.

### N2 - Splitting work is planned but not wired (splitting work)
- Files: notiontool/parallel_plan.py ("Not wired into the engine yet"), team_graph.py (expand/_build/_analysis),
  operator_main.py (delegate, _request_route), dag.py, task_router.py / effort_policy.py / work_queue.py (P4, not wired)
- Evidence: project memory lists P4 modules behind dynamic_roles as still open; split decisions use fixed est_s guesses
  (~60 s fixed cost per coworker) and file lists the Main types by hand.
- Goal: a real dependency/file-overlap analysis of the code (import graph, call graph) to decide which parts are
  independent, and a proven scheduler for a small DAG with resource limits.

### N3 - Review quality depends on one chat reading a diff (reviews)
- Files: engine.py (_choose_worker reviewer paths, review_note, _complete), operator_roles.py, verifier.py, review economy
  (tests/test_review_economy.py), bridge.py (reviewer read-only scope, read_only_test_modules)
- Evidence: rule I14/I42 "never trust done"; reviewer is read-only and could not run tests (memory 2026-10-16 17:45);
  reports often disagree (e.g. different lists of pre-existing failures in two review rounds).
- Goal: machine checks before the AI review (lint, type check, changed-lines coverage, mutation spot checks), so the
  reviewer judges intent and the tool judges mechanics.

### N4 - Tokens and steps per task are too high (tokens)
- Files: context_pack.py, prompt_once.py, plain_protocol.py (prompt/_prompt_body), chat_context.py, token_budget.py
- Evidence: 6,712 coworker steps / 28 chats; 44 % of tool calls were read/search, 7 % polling, ~11 % per-turn overhead;
  every tool call costs 2-3 Notion steps. Context pack is a 40k-char budget filled by markers and whole small files.
- Goal: a repo map / symbol index that ships only the relevant functions (ranked), token counting before send,
  and prompt compression that keeps code intact.

### N5 - Code search for coworkers is slow and outside the app (tokens, speed)
- Files: devtools.py (nt_search_code, nt_file_outline), bridge.py (OPERATOR_TOOLS), work/cbm.py (external
  codebase-memory-mcp.exe v0.11.0)
- Evidence: cbm index goes stale and needs manual reindex; trace_path misses dynamic calls; work/backups is indexed too;
  nt_search_code is plain regex.
- Goal: an in-process, incremental code index (symbols, references, call sites) that the bridge serves as one tool call.

### N6 - The task engine is one 140 KB file with hand-made state handling (reliability, quality)
- Files: notiontool/engine.py (Engine: _loop, tick, _dispatch, _complete 1825-2150, resolve_uncertain, retry_blocked),
  turn_watch.py, stale_tasks.py
- Evidence: many fixes are about wrong states (uncertain, orphaned, busy, held, awaiting_main, lost answers, resend
  hazards I5); states are strings checked in many places.
- Goal: an explicit state machine with allowed transitions (checked in tests), durable timers and idempotent steps.

### N7 - SQLite store does a commit per event (speed, reliability)
- Files: notiontool/store.py (one connection, check_same_thread=False, RLock, WAL, synchronous=FULL), telemetry.py
- Evidence: ~2.5 ms per event and ~8.7 ms per put (memory Phase 2); thread_context was 53.7 MB with 33 MB freelist;
  36 % of events were context_scan_waiting; rule R7 read-only access only.
- Goal: batched writes, synchronous=NORMAL where safe, a migration tool for schema changes, vacuum/retention.

### N8 - Rate and step limits are home-made (reliability, limits)
- Files: network_budget.py (180/min sliding window, breakers), step_guard.py (450 AI steps/60 min, 48 h budgets),
  notion.py (RateLimited, TransientInference)
- Evidence: the window was only in memory and started empty after restart (later fixed); several breaker kinds
  (background, account, thread scope) grew by patches.
- Goal: a tested, persistent rate limiter and retry/backoff library with jitter and per-key budgets.

### N9 - Flaky and slow tests, no pytest setup (flaky tests)
- Files: tests/ (277 files), no conftest.py / pytest.ini; tests/test_engine.py fixtures reused by other tests
- Evidence: 59 time.sleep( calls and 11 threads in tests; test_limits failed under full load only (timing); the full suite
  took 64-75 s and has long carried 19-25 known failures; CRLF/LF checks fail often (R8 byte patches).
- Goal: parallel runs, flaky detection/quarantine, frozen time instead of sleep, a known-failures baseline that is
  compared automatically.

### N10 - Line endings and formatting break merges and tests (quality, merging)
- Files: .gitattributes, notion.py / server.py / chat_context.py / mains.py / devtools.py / mcp_servers.py (CRLF),
  test_repo_eol.py, test_eol_guard.py
- Evidence: R8 forces byte patches for CRLF files; server.py was LF by mistake and had to be converted back.
- Goal: one enforced line-ending and formatting rule (pre-commit style check run by patch_mode before merge).

### N11 - Packaging and updates are fragile (packaging)
- Files: NotionTool.spec, .github/workflows/release.yml, notiontool/updater.py, work/restart-harness.ps1
- Evidence: ~35 MB one-file EXE; _MEI temp folder reuse broke PIL (_PYI_* env inherited); hiddenimports added by hand;
  restart without -Rebuild starts the old EXE (I27); updates are pulled from a GitHub release checked only by sha256 next
  to the file; antivirus false alarms are a known risk (I15).
- Goal: faster, more stable builds (one-folder or alternative freezer), signed or verified updates, automatic
  hidden-import detection, a reproducible build check.

### N12 - Integrations: missing weekly tools for coworkers and observability (integrations)
- Files: bridge.py (tool groups, local_client, local MCP servers via mcp_servers.py / local_mcp.py), telemetry.py,
  server.py (/api/metrics)
- Evidence: coworkers have file, shell, search and screen tools only; telemetry is own JSON events without traces;
  stalls (turn_watch, stall_watch) are detected by own heuristics.
- Goal: only MCP servers or libraries a coworker or the Main would call every week (e.g. git, Python static analysis,
  test runner with structured results), plus standard tracing of a run across tasks.

---

## 2. Search areas and concrete search terms per need

Search on GitHub (stars, last commit, open issues), PyPI (release dates, wheels for win_amd64), and the wider web
(benchmarks, blog posts from real users). Use the terms as given, then follow the "alternatives" sections of the hits.

| Need | Search area | Concrete search terms |
|---|---|---|
| N1 Merging | structured / AST merge, git merge drivers, rebase automation | "mergiraf", "structural merge python git driver", "weave semantic merge", "git merge driver AST python", "spork merge", "git rerere automation", "automatic rebase short lived branches merge queue", "llm merge conflict resolution benchmark" |
| N2 Splitting | import/call graph analysis, DAG schedulers | "python import graph library", "grimp", "import-linter", "pydeps", "python call graph static analysis", "graphlib TopologicalSorter resource constraints", "lightweight DAG scheduler python in-process", "task parallelism file conflict detection" |
| N3 Reviews | static checks, changed-lines coverage, mutation testing, AI review tools | "ruff", "pyright", "mypy incremental", "diff-cover", "coverage.py dynamic contexts", "mutmut", "cosmic-ray", "semgrep python rules", "open source ai code review agent", "pr-agent" |
| N4 Tokens | repo maps, token counting, prompt compression | "aider repo map tree-sitter", "repomix", "tree-sitter python bindings windows wheel", "tiktoken", "anthropic token count offline", "LLMLingua", "code context retrieval ranking pagerank symbols" |
| N5 Code search | in-process code index, LSP, ripgrep | "jedi references", "rope python refactoring library", "multilspy", "pygls", "ripgrep python wrapper", "serena mcp", "code index mcp server python", "sqlite fts5 code search" |
| N6 Engine states | state machines, durable workflows | "python-statemachine", "transitions library python", "durable execution python sqlite", "DBOS python", "temporal python alternatives lightweight", "idempotent task queue sqlite", "huey sqlite" |
| N7 Store | SQLite tuning, migrations, batching | "sqlite wal synchronous normal durability", "sqlite-utils", "yoyo-migrations", "alembic sqlite", "apsw", "litestream windows", "sqlite batch insert event log python" |
| N8 Limits | rate limiting, retries, circuit breakers | "pyrate-limiter sqlite bucket", "limits python sliding window", "tenacity", "stamina retries", "aiobreaker", "pybreaker", "token bucket persistent python" |
| N9 Tests | parallel/flaky/time control | "pytest-xdist", "pytest-rerunfailures", "pytest-randomly", "time-machine", "freezegun", "pytest-timeout", "pytest-split", "flaky test detection open source", "pytest json report baseline compare" |
| N10 Line endings / format | formatter and hooks | "ruff format line-ending", "pre-commit hooks mixed-line-ending", "editorconfig-checker", "gitattributes eol best practice windows" |
| N11 Packaging | freezers, signing, updates | "pyinstaller onedir vs onefile antivirus", "nuitka windows standalone", "briefcase windows msi", "pyapp", "tufup", "python-tuf update framework", "sigstore python sign artifacts", "pyinstaller-hooks-contrib", "windows code signing open source" |
| N12 Integrations | weekly MCP servers, tracing | "modelcontextprotocol servers git", "mcp server pytest", "mcp server python static analysis", "opentelemetry python sqlite exporter", "structlog", "logfire self-hosted alternative", "mcp python sdk FastMCP" |

Role hint for the next roles: each search role takes 2-3 needs, keeps a log of searched terms and rejected hits (with the
rule number from section 3 that killed them), and never adds an item without a named NotionTool file.

---

## 3. Strict acceptance rules for a candidate

A candidate enters the final list only if it passes ALL rules A1-A10. Record the evidence (link, date, number) per rule.

- A1 Active: a release or a commit on the default branch within the last 6 months, and maintainers answer issues
  (at least one maintainer reply in the last 3 months). Archived or "looking for maintainer" projects are rejected.
- A2 Licence fits: MIT, BSD, Apache-2.0, ISC, MPL-2.0 or PSF. LGPL only for separately loaded libraries and only with a
  note. GPL/AGPL/SSPL, "non-commercial" and unclear licences are rejected (the EXE bundles everything).
- A3 Works on Windows: wheels or a binary for win_amd64 and Python 3.12, or pure Python; it runs without WSL, Docker,
  admin rights or a Defender exception (I15). Proof: Windows CI in the project or a documented Windows install.
- A4 Clear integration point: names the exact NotionTool file and function it plugs into (e.g. patch_mode.PatchMode,
  engine.Engine._complete, store.Store, network_budget.NetworkBudget.acquire), and says what it replaces or adds.
- A5 Real gain: a measurable claim that beats what exists (fewer merge conflicts, fewer steps or tokens per task,
  faster suite, fewer failed states, smaller or faster build), with a way to measure it in NotionTool (benchmark, test,
  telemetry event). "Nice to have" without a number is rejected.
- A6 Fits the limits: it must not add Notion requests or AI steps per task (450 AI steps/60 min, 180 requests/min);
  anything that calls a hosted AI or cloud service is rejected unless it is optional and off by default.
- A7 Packaging-safe: it can be bundled into the PyInstaller EXE (or called as a separate local binary) without breaking
  the build; size added under 20 MB unless the gain justifies it in writing; no native compiler needed on the user's PC.
- A8 Local and private: no telemetry by default, no network calls, no upload of code; the repo stays private (R10).
- A9 MCP servers: only if a coworker or the Main would really call it every week; name the concrete weekly use case
  and the current tool it replaces. One-off or demo servers are rejected.
- A10 Smallest change: prefer a library that slots into an existing module over a framework that forces a rewrite
  ("never rewrite from scratch", project memory Goal). If two candidates solve the same need, keep only the better one
  and say why the other lost.

Each accepted item is written as: need id, name + link, licence, last release date, Windows proof, NotionTool file and
function, replaces/adds, expected gain + how to measure, risk, rough effort (S/M/L).
