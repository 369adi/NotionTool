# 02 - Critique of 01-plan.md and the final search brief

Input: docs/research/01-plan.md (sha256 da83aa0e..., 164 lines), checked against the code in this worktree
(branch task/7c910287e4824e6f87baab57c46ec703, HEAD 2b698c9, 2026-10-22).
This file replaces section 2 (search terms) and section 3 (rules) of 01-plan.md for all search roles.

---

## 1. Claims checked against the code

| 01-plan claim | Check (command / file) | Verdict |
|---|---|---|
| engine.py is 140 KB, 2,200+ lines | engine.py = 140,846 bytes, 2,294 lines | correct |
| bridge.py 65 KB | 64,906 bytes | correct |
| store.py: WAL, synchronous=FULL, commit per write | store.py:43-44 PRAGMA WAL / FULL; commit() at :53, :106, :112, :143, :169 | correct |
| tests: 277 files, no pytest.ini/conftest/pyproject, 59 time.sleep( | git ls-files tests = 278; no conftest/pytest.ini/pyproject/setup.cfg/tox.ini/.pre-commit; 59 time.sleep( | correct (278) |
| "tests/ with pytest" (user brief) and pytest plugins in N9 | 776 unittest matches vs 7 pytest imports; patch_mode.py:160 parses **unittest** output for failed test names | **wrong frame**: the merge gate is unittest. Any pytest plugin needs patch_mode.verify + the failed-name parser changed, or it brings no gain |
| N2: parallel_plan "not wired into the engine" | docstring says so, but server.py:1120-1134 serves /api/operator/plan with parallel_plan.delegation_decision, and agent_guard.PLAN_RULE makes the Main call it | **half wrong**: it is wired into the Main's delegation path, only not into engine.py |
| N2: split decisions use hand-typed file lists | file_leases.py exists: short write leases, optimistic check_write against the read hash, plan_order turns shared files into dependencies | **missed**: the overlap logic exists; search must target *what feeds it* (import graph), not a new scheduler |
| N8: window only in memory | network_budget.py:147/249 load and save 'network_window' in the store; step_guard.py persists 'rolling' | already fixed; a library swap would only move tested code |
| N10: needs a formatter / pre-commit | eol_guard.py exists (normalize keeps dominant EOL + BOM) but is "NOT WIRED" into bridge.write_file; .gitattributes already pins 20 CRLF files | **mostly a wiring job**, not a research need |
| N1: plain `git merge` | patch_mode.py already has a merge queue (ThreadPoolExecutor max_workers=1), a cross-process lock file (notiontool-merge.lock), a `git merge-tree --write-tree --name-only` probe, main-is-red detection and a secret scan | partly outdated: queue and probe exist; the gap is the **merge driver** and **conflict brief** |
| N3: secret scan not mentioned | patch_mode.py:42-112: own regex SECRET_TEXT list + ALLOW_MARKER | missed candidate area (gitleaks / detect-secrets) |
| N4: token counting | chat_context.estimate_tokens and context_archive.estimate_tokens are two separate heuristics; context_budget.py and run_context.py are "Not wired" | duplicated estimator is a real finding; see N-A2 |
| Python 3.12, Windows | release.yml: windows-latest, python-version '3.12' | correct |
| PyInstaller spec | hiddenimports typed by hand (notiontool.autopilot, .dag, .role_policy, ...), upx=False, console=False, one release workflow | correct |

Many pure modules are marked "NOT WIRED" (context_budget, run_context, delegation, model_fit, perfection,
snapshots, snapshot_diff, task_cache, eol_guard). Consequence for research: a candidate that duplicates one of
these is rejected unless it clearly beats it (new rule R11).

---

## 2. Problems found in 01-plan.md

### 2.1 Missing needs
1. **Worktree lifecycle** (asked for in area A, absent from the plan). patch_mode.py:267-272, 423-441, 619-622 does
   prune / add / remove --force by hand. Windows-specific risks: locked files on remove, long paths, stale worktrees
   and branches, disk use per copy. No need covers this.
2. **Result / handoff contract.** Coworker results arrive as JSON inside JSON inside a string, sometimes in code
   fences (see the handoff file of this run: `"summary": "```json {...}```"`). engine.py:1861 stores task['result']
   as json.dumps; plain_protocol.py parses replies. No need covers robust parsing and validation of results.
3. **Logging** (asked for in area B). The plan only has "tracing" inside N12. Today: own store events
   (telemetry.py task_events / chain_gaps / rollup) and ad-hoc JSONL writers (ai_disable_detector.py:50). No
   structured, rotating log for post-mortems outside the DB.
4. **Secret scanning in the merge gate** (see table) - belongs to reviews/merging.
5. **Duplicate token estimators** (chat_context vs context_archive) - belongs to tokens.

### 2.2 Wrong priorities
- Steps/tokens (N4, N5) hit the hardest limit (450 AI steps/h; 44 % of tool calls are read/search). They must be #2,
  right after merging, not #4/#5.
- Flaky/slow tests (N9) are a merge blocker (the gate runs the tests; 19-25 known failures make red/green
  ambiguous). Move up next to merging.
- Store (N7): 2.5 ms per event is noise next to AI turns of seconds to minutes. The real issue is size/freelist
  and schema migration. Move down.
- Limits (N8): persistent and tested already. Move down; search only for a drop-in with per-key budgets if it is
  smaller than network_budget.py.
- Line endings (N10): wire eol_guard first; research only a formatter check. Move to the bottom.
- N4 and N5 are one need ("ship less, find faster"); split roles would search the same tools twice. Merged.

### 2.3 Vague or misleading search terms
- "anthropic token count offline": misleading. The official count is an API call (violates A6/A8); tiktoken is an
  OpenAI tokenizer. Replace with a calibration task: compare chat_context.estimate_tokens against real Notion step
  sizes from telemetry; search only for "offline claude tokenizer approximation" and accept nothing that calls out.
- "LLMLingua": needs a local model + torch (hundreds of MB, A7). Drop unless a no-model variant exists.
- "litestream windows": Litestream has no official Windows support. Drop; backup.py already uses the sqlite3
  backup API.
- "DBOS python", "temporal python alternatives": frameworks that own the process (A10). Keep only as a
  "reject with reason" check, search instead for small transition-table libraries.
- "pr-agent", "open source ai code review agent": call hosted AIs (A6). Drop; reviews stay with Notion coworkers.
- "multilspy", "pygls": language-server stacks, heavy for a bridge tool. Replace with "jedi", "griffe",
  stdlib ast + sqlite fts5.
- "task parallelism file conflict detection", "code context retrieval ranking pagerank symbols": no target
  project. Replace with named projects (grimp, aider repomap) and the exact question to answer.
- "automatic rebase short lived branches merge queue": the queue exists. Replace with "git merge driver
  registration .gitattributes per file type" and "git rerere in worktrees".
- "mcp server pytest", "mcp server python static analysis": too broad. Ask for servers that return structured
  test results / diagnostics and compare with nt_shell + nt_search_code, which coworkers already have.

### 2.4 Rules too strict
- A2 rejects GPL outright. Correct for anything bundled in the EXE, too strict for **separate CLI binaries run as a
  subprocess and not shipped in the EXE** (dev-time merge drivers, linters). Mergiraf (reportedly GPL-3.0, verify)
  would be lost. New R2 below.
- A1 "maintainer reply within 3 months": kills small, finished, stable libraries. Use releases within 12 months
  OR an active issue tracker, and no "unmaintained" notice.
- A3 "Windows CI in the project": too strict for pure-Python packages. Pure Python + a passing local run on the
  owner's Windows PC counts as proof.
- A7 treats every candidate as shipped. Split runtime (in the EXE) from dev-time (merge gate, tests, build), which
  never reaches the user's PC.

### 2.5 Rules too weak
- A5 asks for "a number" but names no baseline. Each need now has a fixed baseline metric (section 3).
- No rule against duplicating existing NotionTool modules (the NOT WIRED set). Added R11.
- No rule for the threading model: store.py uses one connection with check_same_thread=False + RLock; a candidate
  that opens its own connections or event loop must say how it fits. Added R12.
- No rule that dev-time tools must run offline and without admin inside a worktree. Merged into R3.

---

## 3. Final search brief

Each item: priority, files, baseline to beat, search terms, what a good find does. Search roles keep a log of
terms used and rejected hits with the rule number (R1-R12) that rejected them.

### Area A - orchestration, splitting, reviews, merging, worktrees, tokens and context

**A1 Merging (was N1) - priority 1**
- Files: patch_mode.py (_merge, merge-tree probe, conflict_hunks, remerge), engine.py _start_merges (~1100-1280),
  .gitattributes (merge driver registration).
- Baseline: share of finished branches that need a manual rebuild (15 on 2026-10-22); conflicts per merge.
- Terms: "mergiraf" (licence + Windows binary), "git merge driver python AST", "weave semantic merge git",
  "git merge-tree --write-tree conflict info parsing", "git rerere worktree shared", "diff3 zealous conflict
  style", "git merge-file --diff-algorithm histogram".
- Good find: registers per *.py via .gitattributes, runs as a subprocess from patch_mode, cuts textual conflicts on
  measured replays of the 2026-10-22 branches.

**A2 Steps and tokens per task: retrieval instead of reading (was N4 + N5) - priority 2**
- Files: context_pack.py (40k-char pack), chat_context.estimate_tokens + context_archive.estimate_tokens (merge into
  one), devtools.py nt_search_code / nt_file_outline, bridge.py OPERATOR_TOOLS, context_budget.py (NOT WIRED).
- Baseline: read/search share of tool calls (44 %), steps per task (6,712 / 28 chats), pack size.
- Terms: "aider repomap" (how it ranks symbols, licence Apache-2.0), "grep-ast", "tree-sitter-language-pack
  win_amd64 wheel", "griffe python api extraction", "jedi get_references", "sqlite fts5 trigram code search",
  "ripgrep json output python", "offline claude tokenizer approximation".
- Good find: one tool call returns ranked relevant functions/call sites (replaces 3-6 read/search calls), runs
  in-process or as rg, index updates incrementally per worktree.

**A3 Reviews: machine checks before the AI reviewer (was N3) - priority 3**
- Files: patch_mode.verify / _merge (gate), verifier.py, engine.py review paths (_choose_worker, review_note),
  patch_mode.SECRET_TEXT.
- Baseline: review rounds per task, disagreeing review reports, defects found after merge.
- Terms: "ruff check --output-format json", "pyright --outputjson", "diff-cover", "coverage.py dynamic contexts",
  "mutmut windows", "gitleaks", "detect-secrets baseline", "semgrep community python rules licence".
- Good find: dev-time only, runs in the worktree in seconds on changed lines, gives the reviewer a short
  machine report so the AI judges intent only.

**A4 Splitting by real dependencies (was N2) - priority 4**
- Files: parallel_plan.py (delegation_decision, served at server.py:1120-1134), file_leases.py (plan_order,
  check_write), team_graph.py, operator_main.py delegate, dag.py.
- Baseline: tasks that later conflict on the same files; est_s error of the plan.
- Terms: "grimp import graph", "import-linter contracts", "pydeps", "python stdlib ast call graph",
  "graphlib TopologicalSorter" (stdlib, compare before adding anything).
- Good find: feeds file_leases.plan_order with import/call edges so parts that touch dependent modules are
  ordered; no new scheduler.

**A5 Worktree lifecycle (new) - priority 5**
- Files: patch_mode.py worktree add/remove/prune (:267-272, :423-441, :619-622), worker_gc.py, bridge.py task copy
  lookup (:298).
- Baseline: stale worktrees/branches after a day, failed removals on Windows, disk per copy.
- Terms: "git worktree remove locked files windows", "git worktree lock prune expire", "core.longpaths worktree",
  "git sparse-checkout worktree", "git clone --shared vs worktree", "pygit2 worktree" (only if it removes a
  failure class).
- Good find: a documented technique or small helper that makes cleanup reliable on Windows; most likely a
  technique, not a library.

**A6 Result and handoff contract (new) - priority 6**
- Files: plain_protocol.py (reply parsing), engine.py _complete (~1825-2150, task['result'] at :1861),
  result_files.py, handoff_files.py.
- Baseline: results that need repair (nested JSON in strings, code fences, missing RESULT line).
- Terms: "json-repair python", "msgspec strict decode", "pydantic v2 TypeAdapter json", "llm structured output
  parsing robust python".
- Good find: one validated result schema replaces ad-hoc parsing; stdlib-first, pure Python.

### Area B - reliability, tests, packaging, logging, integrations and MCP

**B1 Fast, trustworthy test gate (was N9) - priority 1**
- Files: patch_mode.verify and the unittest failed-name parser (:160), tests/test_engine.py fixtures, 278 test
  files, TEST_TIMEOUT_S = 900.
- Baseline: suite 64-75 s; 19-25 known failures; 59 time.sleep( calls.
- Terms: "pytest run unittest TestCase compatibility", "pytest-xdist windows", "pytest-timeout windows
  thread method", "time-machine", "pytest-json-report", "junitxml parse python", "known failures baseline
  compare pytest", "unittest-parallel".
- Good find: parallel run + machine-readable results that patch_mode reads instead of parsing text, and a
  known-failures file compared automatically.

**B2 Engine states (was N6) - priority 2**
- Files: engine.py (_loop, tick, _dispatch, _complete, resolve_uncertain, retry_blocked), turn_watch.py,
  stale_tasks.py, file_leases.LIVE_TASK_STATES.
- Baseline: number of state-fix commits; tasks in uncertain/orphaned states per run.
- Terms: "python-statemachine", "transitions python", "state transition table test property based",
  "hypothesis stateful testing".
- Good find: a declared transition table checked in tests, adopted state by state (R10); hypothesis stateful
  tests are likely the bigger win than a runtime library.

**B3 Logging and tracing (split from N12) - priority 3**
- Files: telemetry.py (task_events, chain_gaps, rollup), store events, ai_disable_detector.py JSONL, server.py
  /api/metrics.
- Baseline: time to explain a stalled task from logs; DB growth from events.
- Terms: "structlog json rotating file", "logging.handlers RotatingFileHandler windows locked file",
  "concurrent-log-handler", "opentelemetry python sdk file exporter offline".
- Good find: one structured rotating log next to the DB with task/run ids; no collector, no network.

**B4 Packaging and updates (was N11) - priority 4**
- Files: NotionTool.spec (hand-typed hiddenimports), .github/workflows/release.yml, updater.py,
  work/restart-harness.ps1.
- Baseline: EXE ~35 MB, build time, antivirus reports, _MEI reuse breakage.
- Terms: "pyinstaller onedir antivirus false positive", "pyinstaller --collect-submodules", "nuitka standalone
  windows pywebview", "tufup", "sigstore python verify", "minisign windows".
- Good find: signed/verified update file checked by updater.py, automatic submodule collection instead of the
  hand list.

**B5 Limits and retries (was N8) - priority 5**
- Files: network_budget.py (acquire, saved window), step_guard.py, notion.py (RateLimited, TransientInference).
- Terms: "pyrate-limiter sqlite bucket", "stamina retries", "tenacity wait_random_exponential".
- Good find only if smaller than the existing code and passes the existing limit tests unchanged.

**B6 Store (was N7) - priority 6**
- Files: store.py, backup.py, desktop_state_cache.py.
- Terms: "sqlite incremental_vacuum auto_vacuum", "sqlite synchronous normal wal durability",
  "PRAGMA user_version migrations python", "yoyo-migrations sqlite".
- Good find: a retention + migration technique; batching only with a measured win.

**B7 Weekly MCP servers and integrations (rest of N12) - priority 7**
- Files: bridge.py tool groups, mcp_servers.py, local_mcp.py, mcp_policy.py.
- Terms: "modelcontextprotocol servers git", "mcp python sdk FastMCP stdio", "serena mcp" (compare with A2).
- Good find: a server a coworker would call every week that replaces several nt_shell/nt_search_code calls;
  name the weekly task and the calls it removes.

**B8 Line endings and format (was N10) - priority 8**
- Files: eol_guard.py (wire first into bridge.write_file), .gitattributes, test_repo_eol.py, test_eol_guard.py.
- Terms: "ruff format line-ending keep", "editorconfig-checker".
- Good find: a check run in the merge gate; no mass reformat.

---

## 4. Final rules (replace A1-A10)

A candidate enters the list only if it passes all rules. Record evidence (link, date, number) per rule.

- R1 Alive: a release in the last 12 months or an active issue tracker, and no archived/unmaintained notice.
- R2 Licence: runtime (bundled in the EXE): MIT, BSD, Apache-2.0, ISC, MPL-2.0, PSF; LGPL only for separately
  loaded libraries with a note. Dev-time CLI run as a subprocess and never shipped: any OSI licence including GPL,
  with a note. Non-commercial, SSPL and unclear licences are rejected.
- R3 Windows: win_amd64 wheel or binary for Python 3.12, or pure Python; no WSL, Docker, admin rights or Defender
  exception (I15); runs offline. Proof: Windows CI or a passing local run on the owner's PC.
- R4 Integration point: exact NotionTool file and function, and what it replaces or adds.
- R5 Gain against the baseline named in section 3, with a way to measure it (benchmark, test, telemetry event).
- R6 Limits: adds no Notion requests or AI steps (450/60 min, 180/min); no hosted AI or cloud call unless optional
  and off by default.
- R7 Packaging: runtime items are PyInstaller-safe and add under 20 MB unless justified; dev-time items are marked
  dev-time and stay out of the EXE.
- R8 Private: no telemetry by default, no code upload; repo stays private (R10 of project memory).
- R9 MCP servers: a named weekly use by a coworker or the Main and the calls it replaces.
- R10 Smallest change: slots into an existing module; no framework that owns the process. Of two candidates for
  one need keep the better and say why the other lost.
- R11 No duplication: check notiontool/ first (including NOT WIRED modules such as file_leases, eol_guard,
  context_budget, parallel_plan); a candidate that duplicates one must clearly beat it.
- R12 Threading: say how it fits store.py's single shared connection with RLock and the engine threads; no own
  event loop or extra DB connections without a reason.

Output per accepted item: need id, name + link, licence, last release, Windows proof, runtime or dev-time,
NotionTool file + function, replaces/adds, gain vs baseline + how to measure, risk, effort (S/M/L).
