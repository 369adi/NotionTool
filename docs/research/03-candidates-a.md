# 03 - Candidates for area A (merging, steps/tokens, machine checks, splitting, worktrees, result contract)

Date of checks: 2026-10-22. Worktree: tf5d20df5 (branch task/25ec28c8). Rules R1-R12 and the area A needs A1-A6 are from docs/research/02-plan-critique.md.

## 0. How this was checked (read first)

- There is no web-search tool and no browser in this chat. "Opened" means: I fetched the project page data over HTTPS with PowerShell (GitHub REST API, Codeberg API, PyPI JSON API, raw Cargo.toml) and read last-push date, archived flag, stars, licence field, release date, and wheel / binary file names. I did not read blog posts or benchmarks.
- The GitHub API started to answer 403 (rate limit) after about 25 calls. These things could NOT be opened and are marked UNVERIFIED below: GitHub release assets of gitleaks, ripgrep, ruff, difftastic; the GitHub pages of mutmut, semgrep and grep-ast; the LICENSE file text of mergiraf (the licence below comes from its Cargo.toml: GPL-3.0-only).
- No candidate was run on this PC except where it says "run locally". Nothing was installed. Gain numbers are TARGETS to measure, not results.
- Local facts were read in this worktree: patch_mode.py lines 121, 533-622; chat_context.py:13; context_archive.py:33; file_leases.py (plan_order at 211); .gitattributes (first 40 lines); requirements.txt; engine.py / operator_roles.py json.loads sites. Local tools: git 2.54.0.windows.1, Python 3.12.10, SQLite 3.49.1, ripgrep 15.2.0 (installed via winget).

## 1. Short list (12), in order of expected value

| # | Need | Candidate | Verdict | Effort |
|---|---|---|---|---|
| 1 | A1 | Mergiraf (structure-aware git merge driver) | ACCEPT as dev-time subprocess, after one 1-hour replay test | M |
| 2 | A1 | git rerere + merge.conflictStyle=zdiff3 + histogram diff (git settings) | ACCEPT | S |
| 3 | A2 | In-process index: stdlib ast + SQLite FTS5 trigram, one tool call | ACCEPT | M |
| 4 | A2 | Ranked repo map (aider's PageRank idea, own ~60 lines, no install) | ACCEPT after 3 | M |
| 5 | A3 | ruff check --output-format json | ACCEPT dev-time | S |
| 6 | A3 | diff-cover + coverage.py (changed-lines coverage) | ACCEPT dev-time, limited scope | M |
| 7 | A3 | gitleaks as second opinion to patch_mode.SECRET_TEXT | CONDITIONAL (Windows asset UNVERIFIED) | S |
| 8 | A4 | grimp (import graph) feeding file_leases.plan_order | CONDITIONAL on a replay | M |
| 9 | A5 | git worktree hardening set (longpaths, lock, retry, porcelain reconcile, sparse-checkout) | ACCEPT | S |
| 10 | A6 | msgspec (strict typed decode of result JSON) | ACCEPT | M |
| 11 | A6 | json-repair (second-stage fallback only) | ACCEPT with a guard | S |
| 12 | A2 | tree-sitter + tree-sitter-language-pack (only if non-Python code must be mapped) | HOLD, not now | M |

What is NOT in the list is under section 4 (rejected, with rule number).

## 2. Details per candidate

### 1. Mergiraf - need A1
- Link: https://codeberg.org/mergiraf/mergiraf (opened through the Codeberg API).
- Licence: GPL-3.0-only (Cargo.toml). Allowed under R2 only as a separate dev-time subprocess that is never bundled into the EXE. Do not ship the binary in the repo or the EXE; keep it in a tools folder outside the package, or document the install.
- Last activity: release v0.20.0 on 2026-09-26 (v0.19.1 on 2026-09-01, v0.19.0 on 2026-08-16: about one release per month); repo updated 2026-10-20; 465 stars; 86 open issues; not archived.
- Windows: release asset mergiraf_x86_64-pc-windows-gnu.zip (6.9 MB). No MSVC build and no Windows CI seen on the project page. Windows proof = NOT run locally yet; the first step of the test below is to unzip and run `mergiraf --version`.
- Plugs into: patch_mode.py `_merge` (line 546 `git merge-tree --write-tree --name-only` probe, line 552 `git merge ... self.base` in the task worktree) and the repository's git config / .gitattributes (the first 40 lines have no `merge=` entry). Two parts: `.gitattributes`: `*.py merge=mergiraf`; git config (per repo, shared by all worktrees): `merge.mergiraf.driver = mergiraf merge --git %O %A %B -s %S -x %X -y %Y -p %P -l %L`.
- Replaces / adds: adds a syntax-aware merge step before line-based conflict. Replaces nothing; `conflict_hunks` (line 121) stays for what the driver cannot solve.
- IMPORTANT unknown to test first: whether `git merge-tree --write-tree` (the probe at line 546) uses the driver. If it does not, the probe returns 1 for conflicts the driver would solve and `_conflict` fires too early. Then the probe must be replaced by a trial `git merge --no-commit` in the task worktree (that is already done a few lines later), which is a small change.
- Why it beats what exists: today a plain line merge plus a coworker prompt "fix only the conflicts below". On 2026-10-22, 15 finished branches had to be rebuilt by hand. Mergiraf resolves cases such as two sides adding imports, methods or dict entries in the same place.
- Expected gain (target, to be measured): replay the 15 branches of 2026-10-22 against the main they hit; count textual conflicts with and without the driver. Accept if at least a third of the conflicting files merge without a conflict and no merged file fails `python -m py_compile`. Telemetry afterwards: share of branches that reach `_conflict`.
- Risk: a driver can merge wrongly but cleanly; the existing test gate (verify, line 567) and secret scan stay in force. GPL: subprocess only. Effort M (config + probe change + replay script + tests).
- Runtime / dev-time: dev-time tool on the machine that runs the merge queue (the user's PC), not in the EXE.

### 2. git rerere + zdiff3 + histogram - need A1
- Link: https://git-scm.com/docs/git-rerere and https://git-scm.com/docs/git-config (git is already used; local git 2.54.0.windows.1; page text not fetched in this run, the settings are documented git features and are present in local git; the effect is still to be measured).
- Licence: GPL-2.0 (already a hard dependency). Last activity: git 2.54 is installed locally. Windows: yes (this PC).
- Plugs into: patch_mode.py `_merge` (line 552) and `conflict_hunks` (line 121); set once per repo: `rerere.enabled=true`, `merge.conflictStyle=zdiff3`, `diff.algorithm=histogram`. Local check: rerere.enabled is unset.
- Replaces / adds: adds remembered resolutions (a conflict solved once is solved again when the same hunk returns, e.g. after `_undo`/re-merge on a retry) and a base section in conflict markers, so the conflict brief shows what the original text was, not only the two sides.
- Gain (target): the number of conflicts that return on remerge drops; coworkers' conflict prompt includes the base. Measure: count of `remerge` calls that again end in `_conflict`. rr-cache is shared by all worktrees of one repo.
- Risk: low; zdiff3 changes marker format, so `conflict_hunks` and its tests must accept the extra `|||||||` section. Effort S.

### 3. In-process code index: stdlib ast + SQLite FTS5 trigram - need A2
- Links: https://docs.python.org/3/library/ast.html and https://sqlite.org/fts5.html (technique on the standard library; no new package).
- Licence: PSF / SQLite public domain. Windows: run locally: FTS5 with the trigram tokenizer works (SQLite 3.49.1, Python 3.12.10). Not tested inside the frozen EXE (its SQLite comes from the bundled Python 3.12; check with the frozen-EXE smoke test).
- Plugs into: a new module notiontool/code_index.py (symbols, defs, call sites, text), served by devtools.py `nt_search_code` / `nt_file_outline` and registered in bridge.py `OPERATOR_TOOLS`; its output also feeds context_pack.py (40k-char pack).
- Replaces / adds: replaces the plain regex search and the outside `work/cbm.py` (codebase-memory-mcp.exe v0.11.0; stale index, needs manual reindex, indexes work/backups). Adds one call `code_context(question, path)` that returns the ranked functions plus call sites instead of 3-6 read/search calls. Incremental per worktree: re-parse only files whose mtime/sha changed; skip work/ and data/.
- Baseline: read/search are 44 % of tool calls; 6,712 steps over 28 chats; every tool call costs 2-3 Notion steps. Target: fewer read/search calls per task (measure in telemetry: read/search calls per task before and after on the same kind of task).
- Risk: dynamic calls are invisible to ast (same weakness as cbm trace_path); R12: the index must not use the main store.py connection; keep it in memory or in its own file per worktree, with a reason noted. Effort M.

### 4. Ranked repo map (aider's idea, reimplemented) - need A2
- Link: https://github.com/Aider-AI/aider (opened via the GitHub API): Apache-2.0, 49,695 stars, last push 2026-05-22, aider-chat 0.86.2 released 2026-02-12 (5 months old at the last push; under R1's 12 months).
- Do NOT install aider-chat: it requires Python <3.13 and pins about 100 dependencies (pyproject shows exact pins), which breaks R7/R10. Only the idea is used: build a graph "file/symbol references symbol" from candidate 3, rank with PageRank personalised to the files in the task prompt, print the top symbols within a token budget.
- Plugs into: code_index.py (candidate 3) -> context_pack.py (replaces filling the 40k-char pack with markers and whole small files by a ranked map), prompt_once.py / plain_protocol.py `_prompt_body`.
- Gain (target): smaller pack at equal task success; measure pack size and steps on five replayed tasks. Effort M; do after 3. Own PageRank is about 40-60 lines of Python, no networkx.
- Also here: `estimate_tokens` exists twice with the same formula, ceil(utf8 bytes / 3): chat_context.py:13 and context_archive.py:33. Merge into one (S, not an open-source find). No offline Claude tokenizer found that is checked: the real Anthropic count is an API call (R6), tiktoken is an OpenAI tokenizer. Keep bytes/3 and call it an estimate.

### 5. ruff - need A3
- Link: https://github.com/astral-sh/ruff (opened via the GitHub API) and https://pypi.org/project/ruff/ (opened).
- Licence: MIT. Last activity: 0.17.2 released 2026-10-22, last push 2026-10-22, 50,224 stars, not archived.
- Windows: win_amd64 wheel on PyPI, 10.3 MB; run `python -m ruff` or the exe in Scripts. Not run locally in this chat.
- Plugs into: patch_mode.py `verify` (line 371 area; before the unittest command) and verifier.py; result goes into the merge result dict beside `scan`.
- Command: `ruff check --isolated --select E9,F63,F7,F82,F811,F401 --output-format json <changed files>`. The project has no ruff config; use a fixed small rule set, so no noise.
- Replaces / adds: adds undefined-name, redefinition and syntax findings in about one second on changed files, before the 64-75 s suite. Honest baseline: `python -m py_compile` already catches truncated files (the SyntaxError of 2026-10-22); ruff adds F821 (undefined name), F811, F401, and structured JSON the reviewer can read.
- Gain (target): defects found before review; fewer review rounds that only list mechanical findings. Measure: findings per task and review rounds per task.
- Risk: dev-time only, not in the EXE (R7); must be installed on the merge machine (put it in a requirements-dev file). Effort S.

### 6. diff-cover + coverage.py - need A3
- Links: https://github.com/Bachmann1234/diff_cover (opened via GitHub API; Apache-2.0, 849 stars, last push 2026-10-01); https://pypi.org/project/diff-cover/ (10.6.0, released 2026-09-22, pure Python, no win wheel needed). coverage.py 7.17.0 has win wheels (checked in an earlier assignment this week; licence Apache-2.0 per project, not re-opened in this run).
- Plugs into: patch_mode.py `verify` (only for changed files and their test modules), verifier.py; report in the merge result.
- Replaces / adds: adds a short report "changed lines not run by any test" so the reviewer does not read that out of a diff.
- Cost: `coverage run -m unittest` is slower than plain unittest (the full suite takes 64-75 s today). Run only the test modules that import the changed modules. This is the main risk; measure the extra seconds before accepting.
- Gain (target): reviews judge intent, tool judges mechanics. Measure: untested changed lines per merged task. Effort M.

### 7. gitleaks - need A3 (CONDITIONAL)
- Link: https://github.com/gitleaks/gitleaks (opened via GitHub API): MIT, 30,237 stars, last push 2026-09-30, not archived. Release assets for Windows were NOT opened (403): UNVERIFIED.
- Plugs into: patch_mode.py `secret_scan` (own regex list SECRET_TEXT + ALLOW_MARKER at lines 42-112). Adds a second opinion run as a subprocess on `old_main..head`.
- Why only conditional: the own scan works and R11 asks for clear gain. Do a one-time test with 10 seeded fake secrets and the repository's history: take it only if gitleaks finds secrets the own scan misses, or the own scan produces false positives that gitleaks does not. Dev-time binary, not in the EXE. Effort S.
- Lost against it: detect-secrets 1.5.0 (see rejected: R1).

### 8. grimp - need A4 (CONDITIONAL)
- Link: https://github.com/seddonym/grimp (opened via GitHub API): BSD-2-Clause, 135 stars, last push 2026-10-15; https://pypi.org/project/grimp/ 3.17 released 2026-09-04, 8 win wheels (about 2 MB), no dependencies.
- Plugs into: parallel_plan.py `delegation_decision` (not wired yet; the plan is served at server.py:1120-1134) and file_leases.py `plan_order` (line 211, uses `_task_deps` and `_reaches`).
- Adds: answers "which modules import this file, directly or through others" from the code, so two tasks that touch a file and one of its importers are put in order or in one chat. Today the file lists are typed by hand by the Main.
- Honest alternative: the same import edges can be taken from the ast pass of candidate 3 in ~50 lines with no new package. Decision rule (R10): build on candidate 3 first; use grimp only if relative-import or re-export handling in the own code is wrong in the replay. That is why it is conditional.
- Gain (target): fewer tasks that later conflict on the same files. Measure by replaying the pairs of 2026-10-22 tasks that conflicted: how many would have been flagged. graphlib.TopologicalSorter gives nothing new: file_leases.plan_order already orders tasks (R11).
- Runtime vs dev-time: runs in the app process on the project's source tree (Rust extension, +2 MB). Effort M.

### 9. Git worktree hardening set - need A5
- Links: https://git-scm.com/docs/git-worktree (lock, prune --expire, repair, remove --force --force), https://git-scm.com/docs/git-config (core.longpaths), https://git-scm.com/docs/git-sparse-checkout. No new package. Local: git 2.54.0.windows.1, 64 worktrees currently registered in this repository, core.longpaths unset.
- Plugs into: patch_mode.py worktree add / prune (lines 267-272, 423-425), `_remove` (lines 618-622: `remove`, then `remove --force`, then `prune`), worker_gc.py, bridge.py:298 (task copy lookup).
- Adds: (a) `git config core.longpaths true`; (b) `git worktree lock --reason` while a task is live, so prune and gc never take a live copy; (c) on Windows retry `remove --force` with short waits and `--force --force` for locked ones when a file is held by a process; (d) reconcile with `git worktree list --porcelain` in worker_gc.py to find stale copies and branches after a day; (e) sparse-checkout to leave out work/ and data/ in task copies (smaller disk per copy).
- Baseline: stale worktrees/branches after a day, failed removals, disk per copy. Measure: counts from `git worktree list --porcelain` daily, and bytes per copy. Effort S per item. Not tested in this run: (b)-(e) are documented git behaviour; the effect on this repo is to be measured.
- pygit2 rejected for this (see section 4).

### 10. msgspec - need A6
- Link: https://github.com/jcrist/msgspec (opened via GitHub API): BSD-3-Clause, 4,182 stars, last push 2026-10-22; https://pypi.org/project/msgspec/ 0.22.0 released 2026-09-29, 8 win wheels (193 KB), no dependencies.
- Plugs into: new notiontool/result_schema.py with `Struct` types for the plan, review and integrator results; used by plain_protocol.py (reply parsing) and engine.py `_complete` (stores task['result'] as json.dumps, about :1861). Replaces the ad-hoc `json.loads` reads at engine.py:786, 1737, 2241, 2272, 2283 and operator_roles.py:255, 437.
- Adds: one schema, strict typed decode with a path in the error ("field summary: expected str"), so a result either is valid or is rejected with an exact reason to send back to the coworker.
- Honest baseline: stdlib dataclasses + json + a 40-line checker would also work. msgspec wins on error messages and speed and adds only 193 KB; pydantic 2.14.1 (MIT) lost because it pulls pydantic-core and more packages (R10, R7).
- Gain (target): fewer results that need repair; measure the count of results that fail the schema and the count of repair prompts per 100 tasks. Effort M (schema for 3-4 result shapes, tests with the stored results).

### 11. json-repair - need A6
- Link: https://github.com/mangiucugna/json_repair (opened via GitHub API): MIT, 5,124 stars, last push 2026-10-22; https://pypi.org/project/json-repair/ 0.64.1 released 2026-10-19, pure Python, no dependencies.
- Plugs into: plain_protocol.py reply parsing, as the second stage after the strict decode of candidate 10 fails (code fences, text around the JSON, trailing commas, a missing closing brace).
- Guard: never trust a repaired result silently. Mark `repaired: true` in the stored result and let the review step see that; if the repaired value fails the schema, ask the coworker again. Without that guard it conflicts with the rule "never trust done".
- Gain (target): share of replies saved without an extra coworker turn (each extra turn = 2-3 Notion steps). Measure with the repaired flag. Effort S.

### 12. tree-sitter + tree-sitter-language-pack - need A2 (HOLD)
- Links: https://pypi.org/project/tree-sitter/ (0.26.0, released 2026-06-30, 5 win wheels, 130 KB); https://github.com/Goldziher/tree-sitter-language-pack (MIT, 478 stars, last push 2026-10-22; PyPI 1.22.1 released 2026-10-12, one win wheel of 2.26 MB).
- Why HOLD: NotionTool's own code is Python, and stdlib ast (candidate 3) covers it. The 2.26 MB wheel is too small to hold many compiled grammars, so the pack may download grammars at first use; that would break R3/R6 (offline). UNVERIFIED: I did not open the package. Open it only if coworkers must map non-Python files (JS, PowerShell) in project work.
- Plugs into: code_index.py (candidate 3) as a second parser. Effort M.

## 3. Suggested order
1. Candidate 2 (S, today) and the replay for candidate 1 (M) - A1 is priority 1 and the 15 rebuilt branches are the best evidence we have.
2. Candidate 9 (S) and 5 (S): cheap, measurable.
3. Candidate 3, then 4 - the main lever on read/search calls.
4. Candidates 10 and 11 together; then 6.
5. 7, 8, 12 only on the conditions written above.

## 4. Rejected or lost (with the rule that killed them)

| Item | Need | Rule | Reason |
|---|---|---|---|
| grep-ast 0.9.0 | A2 | R1 | last release 2025-05-08 (17 months); repo page not opened (403). Depends on tree-sitter-language-pack. |
| aider-chat as a package | A2 | R3/R7/R10 | requires Python <3.13, about 100 pinned dependencies. Idea kept (candidate 4). |
| tree-sitter-languages 1.10.2 | A2 | R1 | last release 2024-02-04; repo last push 2025-02-17. |
| jedi 0.20.1 (get_references) | A2 | R10/R2 | pure Python, released 2026-10-12, active; but ast index (candidate 3) covers Python names without a second parser; licence field is empty on PyPI and NOASSERTION on GitHub (MIT per project, LICENSE not opened). Revisit if call-site recall of candidate 3 is too low. |
| griffe 2.4.1 (ISC) | A2 | R5 | needs Python >=3.11 (fine) but it targets API docs, not call sites; no gain over ast. |
| ripgrep 15.2.0 (Unlicense, last push 2026-08-04) | A2 | R5 | already installed on this PC, fast, but it does not cut tool calls; it needs a 5 MB binary next to the EXE (Windows asset UNVERIFIED: 403). Use only if FTS5 trigram search is too slow. |
| difftastic (MIT, push 2026-10-16) | A1 | R5 | a diff viewer, not a merge tool. |
| LLMLingua / tiktoken / offline Claude tokenizer | A2 | R6/R7 | needs a local model + torch, or is an OpenAI tokenizer, or is an API call. |
| pyright 1.1.414 | A3 | R3/R6 | the PyPI package downloads Node at first use; slow; ruff covers the mechanical part. |
| mutmut 3.8.0 (BSD-3, released 2026-09-12) | A3 | R3 (UNVERIFIED) | mutation testing needs many full test runs (suite 64-75 s); native Windows support not confirmed (project page not opened: 403). |
| semgrep 1.182.1 | A3 | R2/R7 | LGPL-2.1+, win wheel 59 MB; community rule licence not checked; far above 20 MB. |
| detect-secrets 1.5.0 | A3 | R1 | last release 2024-05-06 (the repo got a push on 2026-04-02, but there is no release for 29 months). |
| import-linter 2.15 | A4 | R5 | enforces layer contracts, does not help to split tasks. |
| pydeps 3.0.9 | A4 | R3 | draws graphs with Graphviz, which is an extra install. |
| graphlib.TopologicalSorter | A4 | R11 | file_leases.plan_order (line 211) already orders tasks with _task_deps/_reaches. |
| pygit2 | A5 | R10 | the failures on Windows come from file locks, not from calling git; libgit2 would not remove that class (reasoned, not tested). |
| pydantic 2.14.1 | A6 | R10/R7 | lost to msgspec: more packages, larger. |

## 5. Not verified in this run (do not treat as fact)
- Mergiraf: no run on this PC; whether `merge-tree --write-tree` uses the merge driver; LICENSE.txt text not opened (licence from Cargo.toml).
- Release assets / Windows binaries of gitleaks, ripgrep, ruff, difftastic (GitHub 403 after the rate limit).
- GitHub pages of mutmut, semgrep, grep-ast (403).
- tree-sitter-language-pack offline behaviour.
- ast-grep (MIT, last push 2026-10-22, 16,315 stars) showed up in the repo query for structural code search; its Windows wheel and fit were not checked.
- Anything from blogs or benchmarks: no web search was available.
- All expected gains are targets with a measurement method, not measured numbers.
