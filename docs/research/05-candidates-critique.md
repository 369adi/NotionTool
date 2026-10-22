# 05 - Critique of the candidates (03-candidates-a.md and 04-candidates-b.md)

Date of checks: 2026-10-22, worktree t8e0c095d (HEAD 34622e6). Rules R1-R12 and needs A1-A6 / B1-B8 are from
docs/research/02-plan-critique.md. Inputs: 03-candidates-a.md (12 candidates), 04-candidates-b.md (8 candidates +
rejected list).

## 0. How this was checked

- No web-search tool or browser exists for this role. "Opened" means: the signed-in GitHub API (`gh api`, account
  logged in, 4,884 of 5,000 calls left after the run), the PyPI JSON API, the Codeberg API, one wheel downloaded to
  %TEMP% and listed, and local commands in this worktree. Blogs, benchmarks and the MCP registry were not read.
- Every GitHub repo, every Windows release asset and every PyPI page that 03 section 5 marked UNVERIFIED was opened
  in this run (section 1). The 403 gaps of Researcher A are closed.
- Every integration point was checked with `git grep` in this worktree (section 2). Local: git 2.54.0.windows.1,
  Python 3.12.10, SQLite 3.49.1, `CREATE VIRTUAL TABLE ... USING fts5(x, tokenize=trigram)` works.
- Still needs a local run, not a lookup: the Mergiraf replay and whether `git merge-tree --write-tree` uses a merge
  driver; the gitleaks seeded-secret test; FTS5 inside the frozen EXE; pytest/unittest parity of failed names. All
  gains below are targets, not measurements.

## 1. Link, activity and licence checks (this run)

| Item | Repo (GitHub unless noted) | Licence | Last push / release | Windows evidence |
|---|---|---|---|---|
| Mergiraf | codeberg.org/mergiraf/mergiraf | LICENSE.txt opened: GNU GPL v3 (29 June 2007) | release v0.20.0 2026-09-26 (from 03, not re-opened) | mergiraf_x86_64-pc-windows-gnu.zip (from 03, not re-opened) |
| git | git/git | GPL-2.0 (GitHub shows NOASSERTION) | push 2026-10-22 | git 2.54.0.windows.1 on this PC |
| aider | Aider-AI/aider | Apache-2.0 | push 2026-05-22 | idea only, not installed |
| ruff | astral-sh/ruff | MIT | 0.17.2 on 2026-10-22 | ruff-x86_64-pc-windows-msvc.zip 9.8 MB; PyPI py3-none-win_amd64 9.81 MB |
| diff-cover | Bachmann1234/diff_cover | Apache-2.0 | push 2026-10-01; PyPI 10.6.0 2026-09-22 | pure Python, 4 deps |
| coverage.py | coveragepy/coveragepy (moved from nedbat) | Apache-2.0 | PyPI 7.17.0 2026-10-22 | cp312 win_amd64 0.22 MB, Python >=3.11 |
| gitleaks | gitleaks/gitleaks | MIT | v8.30.1 on 2026-03-21 (7 months, R1 ok) | gitleaks_8.30.1_windows_x64.zip 8 MB |
| grimp | python-grimp/grimp (moved from seddonym) | BSD-2-Clause | push 2026-10-15; PyPI 3.17 2026-09-04 | cp312 win_amd64 1.91 MB |
| msgspec | msgspec/msgspec (moved from jcrist) | BSD-3-Clause | push 2026-10-22; PyPI 0.22.0 2026-09-29 | cp312 win_amd64 0.18 MB, 0 deps |
| json-repair | mangiucugna/json_repair | MIT | push 2026-10-22; PyPI 0.64.1 2026-10-19 | pure Python, 0 deps |
| tree-sitter | tree-sitter/py-tree-sitter | MIT | push 2026-08-30; PyPI 0.26.0 2026-06-30 | cp312 win_amd64 0.12 MB |
| tree-sitter-language-pack | xberg-io/tree-sitter-language-pack (moved from Goldziher) | MIT | push 2026-10-22; PyPI 1.22.1 2026-10-12 | one wheel cp310-abi3-win_amd64 2.15 MB, opened: contains _native.pyd 5.5 MB and a `DownloadManager`, `download`, `download_all`, `cache_dir` API |
| ast-grep | ast-grep/ast-grep | MIT | 0.51.0 on 2026-10-15 | app-x86_64-pc-windows-msvc.zip 15.5 MB; PyPI ast-grep-py cp312 win_amd64 5.17 MB; ast-grep-cli 16.68 MB |
| ripgrep | BurntSushi/ripgrep | Unlicense | 15.2.0 on 2026-07-15 | ripgrep-15.2.0-x86_64-pc-windows-msvc.zip 1.7 MB (03 said 5 MB: wrong) |
| difftastic | Wilfred/difftastic | MIT | 0.71.0 on 2026-09-18 | difft-0.71.0-x86_64-pc-windows-msvc.zip 10 MB |
| mutmut | boxed/mutmut | BSD-3-Clause | 3.8.0 on 2026-09-12, push 2026-10-19 | no binary; py3-none-any wheel, 7 deps |
| semgrep | semgrep/semgrep | LGPL-2.1 (PyPI LGPL-2.1-or-later) | 1.182.1 on 2026-10-22 | win_amd64 wheel 56.44 MB, 28 deps |
| grep-ast | Aider-AI/grep-ast | Apache-2.0 | last push and release 0.9.0 on 2025-05-08 (17 months, R1 fails) | pure Python |
| pytest-xdist | pytest-dev/pytest-xdist | MIT | push 2026-10-22; last release 3.8.0 2025-07-01 (R1 by activity) | pure Python |
| pytest-timeout | pytest-dev/pytest-timeout | MIT | push 2026-10-20; PyPI 2.4.0 2025-05-05 (R1 by activity) | pure Python |
| time-machine | adamchainz/time-machine | MIT | push 2026-10-22; PyPI 3.5.1 2026-09-08 | cp312 win_amd64 0.03 MB |
| Hypothesis | HypothesisWorks/hypothesis | MPL-2.0 on PyPI (GitHub NOASSERTION) | PyPI 6.170.7 on 2026-10-22 | cp312 win_amd64 0.66 MB, Python >=3.11 |
| PyInstaller | pyinstaller/pyinstaller | GPL-2.0-or-later with bootloader exception | PyPI 6.22.3 2026-09-12, push 2026-10-22 | already the build tool |
| zizmor | zizmorcore/zizmor | MIT | v1.31.1 on 2026-10-15 | zizmor-x86_64-pc-windows-msvc.zip 8 MB; PyPI win_amd64 8.61 MB |

No item is archived. Corrections to the researchers: ripgrep's Windows asset is 1.7 MB, not 5 MB; four repos moved to
new owners (links in 03/04 still redirect); the tree-sitter-language-pack wheel has a download manager, so the offline
risk named in 03 is real.

## 2. Integration points checked in the code

| Claim | Found | Verdict |
|---|---|---|
| patch_mode `_merge` 533, probe `merge-tree --write-tree --name-only` 546, `git merge` 552, `conflict_hunks` 121 | yes, same lines | correct |
| .gitattributes has no `merge=` entry | 45 lines, no `merge=` | correct |
| git rerere / conflictStyle / diff.algorithm / core.longpaths unset | all four empty | correct |
| patch_mode `verify` 371, `_FAILED_TEST` 155 (unittest format `FAIL: name (module)`), TEST_TIMEOUT_S 900 at 37 | yes | correct |
| patch_mode `SECRET_TEXT` 42-112, `secret_scan` | SECRET_TEXT at 52, `secret_scan` at 459 | correct |
| worktree add/prune 267-272, 423-425, `_remove` 618-622 | yes | correct; 64 worktrees registered |
| worker_gc.py reconciles worktrees | worker_gc.py has `plan_cleanup(workers, tasks, chats, threads_alive)`, no worktree code | 03 #9(d) is a new function there, not an extension |
| sparse-checkout to leave out work/ and data/ (03 #9e) | `.gitignore` has `/data/` (line 6) and `work/` (line 13) | no gain: ignored folders are never checked out |
| work/cbm.py to be replaced (03 #3) | git-ignored (`.gitignore:13 work/`), not in the worktree | it is the owner's local tool, not repo code; the replaced part in the repo is devtools.py `nt_search_code` (206) / `nt_file_outline` (224) |
| context_pack.py 40k-char pack | `BUDGET_CHARS = 40000` (line 20) | correct |
| bridge.py `OPERATOR_TOOLS` | lines 132 and 174 | correct |
| code_index.py, result_schema.py, task_states.py | do not exist | correct, all three are proposed new modules |
| engine.py json.loads at 786, 1737, 2241, 2272, 2283; operator_roles.py 255, 437 | all seven found | correct |
| file_leases `plan_order` 211, `LIVE_TASK_STATES` 20 | yes | correct |
| parallel_plan `delegation_decision` 232, served at server.py:1120 | yes | correct |
| estimate_tokens twice (chat_context:13, context_archive:33) | yes | correct |
| store.py:43-44 WAL + synchronous=FULL, no user_version | yes; 0 files with user_version | correct |
| NotionTool.spec:8 hand-typed hiddenimports | yes | correct |
| 0 `logging` imports in notiontool/, 0 hypothesis files in tests/ | 0 and 0 | correct |
| requirements.txt | pywebview, pyinstaller, websocket-client, pillow (4 lines) | correct |

## 3. Kept (final list, 12 items)

Order = value for NotionTool. "Dev" = dev-time, never in the EXE. "Cond" = keep only after the named test passes.

| # | Need | Item | Plugs into | Why kept (short) |
|---|---|---|---|---|
| K1 | A1 | Mergiraf as merge driver (Dev, Cond: replay) | .gitattributes `*.py merge=mergiraf`; repo git config driver line; patch_mode `_merge` 546/552 | Only candidate that attacks the priority-1 baseline (15 rebuilt branches). GPL-3.0 confirmed, allowed under R2 as subprocess only. Condition: replay the 15 branches; accept if at least a third of conflicting files merge clean and pass py_compile; also settle whether the 546 probe uses the driver. |
| K2 | A1 | git rerere + merge.conflictStyle=zdiff3 + diff.algorithm=histogram (technique) | repo git config; patch_mode `conflict_hunks` 121 must accept the `\|\|\|\|\|\|\|` base section | Three settings, all unset today, no new package. Beats the plain line merge: a solved conflict is reused on remerge, and the conflict brief shows the base text. |
| K3 | A2 | Own code index: stdlib ast + SQLite FTS5 trigram, merged with the ranked repo map (old 03 #3 + #4) | new notiontool/code_index.py; devtools.py `nt_search_code` 206 / `nt_file_outline` 224; new tool in bridge.py `OPERATOR_TOOLS`; context_pack.py `BUDGET_CHARS` 40000 | Priority-2 need (44 % of tool calls are read/search). FTS5 trigram works on this PC. 03 #4 is not a separate item: it is the ranking step of the same module (about 60 lines PageRank), so the two are merged. Must keep its own in-memory or per-worktree DB file (R12). |
| K4 | A2 | One `estimate_tokens` (housekeeping, not an outside find) | chat_context.py:13 and context_archive.py:33 | Two copies of one formula; merge and calibrate against telemetry step sizes. Kept as a must-do note, it is S. |
| K5 | A3 | ruff check, fixed rule set, JSON output (Dev) | patch_mode `verify` 371 before the unittest command; result next to `scan` | Seconds per run, MIT, Windows asset and wheel confirmed. Adds F821/F811/F401 that py_compile does not catch. |
| K6 | A5 | Worktree hardening: core.longpaths, `worktree lock` for live tasks, retry `remove --force --force`, porcelain reconcile (Dev technique) | patch_mode 267-272, 423-425, `_remove` 618-622; new reconcile function in worker_gc.py | 64 registered worktrees and longpaths unset prove the baseline. Item (e) sparse-checkout is dropped: work/ and data/ are already ignored. |
| K7 | A6 | msgspec Struct schema for plan, review and integrator results (runtime, 0.18 MB) | new notiontool/result_schema.py; the seven json.loads sites in engine.py and operator_roles.py; plain_protocol.py | Replaces seven ad-hoc reads with one strict decode that names the bad field, so the reason can go back to the coworker. 0 deps, BSD-3, win wheel. |
| K8 | B1 | pytest + pytest-xdist + pytest-timeout as runner (Dev, Cond: parity) | patch_mode `verify` 371, `_FAILED_TEST` 155 (read `--junitxml` instead), TEST_TIMEOUT_S 37 | Only measured gain in both files (360 s vs 727 s under load). Condition: name-by-name parity of failed tests on a quiet PC; the gate stays unittest until then. |
| K9 | B2 | Declared transition table (own technique, log-only first) | new notiontool/task_states.py; the single place that writes task state in engine.py; file_leases `LIVE_TASK_STATES` 20 becomes a view of it | About 40 lines, makes every wrong-state bug visible as an `illegal_transition` event. Oracle for K10. |
| K10 | B2 | Hypothesis RuleBasedStateMachine (Dev, Cond: deterministic Notion fake) | new tests/test_engine_state_machine.py on engine `_dispatch`, `_complete`, `resolve_uncertain`, `retry_blocked` | 0 stateful tests today; targets the lost/resent-answer class. `derandomize=True` and under 30 s in the gate; drop if nothing is found after a fair try. Do after K9. |
| K11 | B6 | auto_vacuum=INCREMENTAL + one VACUUM at start-up (technique) | store.py `Store.__init__` 43-44; idle step `PRAGMA incremental_vacuum(N)` | Biggest measurable store win: 63 % free pages (about 200 MB). Run before the engine threads start, on the one shared connection (R12); owner or Main starts it, coworkers cannot touch data/. |
| K12 | B4 | PyInstaller `collect_submodules('notiontool')` + import-all smoke test | NotionTool.spec:8 hiddenimports; .github/workflows/release.yml smoke step | Removes the "ModuleNotFoundError only in the EXE" class; no new tool, PyInstaller is already used. |

Conditional extras (not counted in the 12, run once, keep only if the test says so):
- gitleaks (A3, Dev): seeded 10 fake secrets plus repo history against patch_mode `secret_scan` 459; keep only if it finds
  what SECRET_TEXT misses or avoids its false positives. Windows zip 8 MB confirmed.
- json-repair (A6): second stage after K7 fails, always marked `repaired: true`. First count how many stored results
  fail K7 only because of fences or surrounding text; a 15-line fence and outer-brace cut in plain_protocol.py covers
  those. Add json-repair only if trailing commas or missing braces remain (R10).
- diff-cover + coverage.py (A3, Dev): only after K8 makes the gate fast; measure the extra seconds on the changed
  modules' tests first.
- time-machine (B1, Dev): only in tests that already patch the clock and fail under load (tests/test_limits.py
  EightSlotTests); it does not remove the 59 time.sleep calls.

## 4. Rejected (with reason)

| Item | From | Rule | Reason |
|---|---|---|---|
| Ranked repo map as its own item | 03 #4 | duplicate | merged into K3, same module and data |
| grimp | 03 #8 | R10/R11 | the ast pass of K3 gives the same import edges in about 50 lines; file_leases.plan_order already orders tasks. Revisit only if K3's relative-import handling is wrong in a replay |
| tree-sitter + tree-sitter-language-pack | 03 #12 | R5/R3/R6 | no need: NotionTool code is Python and screens are out of scope; the pack's wheel has `download`/`download_all`/`cache_dir`, so grammars may be fetched at first use (offline risk) |
| ast-grep (py 0.51.0, CLI 0.51.0) | 03 section 5 | R11/R10 | MIT, active, Windows wheel 5.17 MB confirmed, plug-in point would be beside code_index.py; but for Python it duplicates K3 (search) and K5 (lint rules), and adds 5-17 MB for no named weekly use |
| git worktree sparse-checkout | 03 #9e | R5 | work/ and data/ are git-ignored, nothing to leave out |
| synchronous=NORMAL | 04 C6 | R5 | gain not measured, store time is noise next to AI turns (02 section 2.2), and it risks the last commits on power loss while lost answers (I5) are a known pain |
| zizmor | 04 C8 | R5/R9 | MIT, Windows zip confirmed, but one workflow in a private repo: a one-time audit, not something we integrate. Run it once by hand and stop |
| gitleaks in 04 rejected list | 04 | duplicate | same item as the conditional extra above (area A) |
| ripgrep | 03 section 4 | R5 | already installed, 1.7 MB, but does not cut tool calls; FTS5 in K3 covers search |
| difftastic | 03 section 4 | R5 | a diff viewer, not a merge tool |
| mutmut | 03 section 4 | R5 | BSD-3, pure Python, active; needs many full test runs on a 64-727 s suite; the earlier "Windows unverified" reason is replaced by cost |
| semgrep | 03 section 4 | R2/R7 | LGPL-2.1-or-later, 56.44 MB wheel, 28 deps |
| grep-ast | 03 section 4 | R1 | last push and release 2025-05-08 |
| jedi, griffe, pyright, LLMLingua/tiktoken, aider-chat package, tree-sitter-languages, detect-secrets, import-linter, pydeps, graphlib, pygit2, pydantic | 03 section 4 | as in 03 | reasons in 03 hold; not re-opened beyond section 1 |
| unittest-parallel, pytest-rerunfailures, freezegun, pytest-json-report, python-statemachine, transitions, structlog, concurrent-log-handler, loguru, OpenTelemetry, PyrateLimiter, stamina, tenacity, tufup, sigstore-python, Nuitka, onedir, pyinstaller-hooks-contrib, editorconfig-checker, ruff format, yoyo/alembic/sqlite-utils, reference MCP servers, Serena, pip-audit | 04 section 3 | as in 04 | reasons in 04 hold |

## 5. Gap list (needs with no good candidate) and search hints

| Need | Gap | Search hints |
|---|---|---|
| A2 | token estimate is a guess (bytes/3) with no calibration | not a library: compare `estimate_tokens` with real step sizes from telemetry; search "claude tokenizer approximation offline", accept nothing that calls out |
| A3 | no type check that runs offline without Node | "mypy --output json windows", "basedpyright pypi bundled node", "pyrefly windows wheel"; must run in seconds on changed files |
| A4 | est_s error of the plan has no tool | technique: log est_s vs real seconds per part in parallel_plan stats; search "task duration estimation from history python" |
| B1 | no automatic known-failures comparison | "junitxml diff baseline known failures", "pytest --junitxml compare two runs"; likely 30 lines of own code reading K8's XML |
| B3 | no structured rotating log outside the DB that coworkers can read | "logging.handlers RotatingFileHandler windows PermissionError rename", "json lines log rotate windows python stdlib"; plus a read path for coworkers (the bridge hides data/) |
| B4 | update file is trusted by sha256 only, no signature | "minisign windows binary verify", "pure python ed25519 verify", "signify windows"; must be offline and stdlib-light |
| B7 | no weekly MCP server found; both researchers lacked web search and used gh through nt_shell | "github-mcp-server windows binary" (would replace gh calls in nt_shell), "self-hosted web search mcp server searxng", "fetch mcp server markdown python"; name the weekly role and the calls removed (R9) |
| B8 | not a research gap | wire the existing eol_guard.py into bridge.write_file and patch_mode |
| B5 | closed on purpose | network_budget.py and step_guard.py beat every candidate (04) |

## 6. Order of work

1. K2 (S) and the K1 replay (M).
2. K6 and K5 (S each), K11 (S, owner starts it), K12 (S).
3. K3 with K4 (M).
4. K8 parity check (M), then the diff-cover extra.
5. K7 (M), then the json-repair check.
6. K9, then K10.
7. gitleaks one-time test; time-machine only where a clock test fails.
