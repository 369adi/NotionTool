# 06 - Gap fill: the open needs from the critic's list

Role: researcher (second pass). Input: section 5 (gap list) of `05-candidates-critique.md`. Rules R1-R12 are the ones in `02-plan-critique.md`.

## 0. Method

- Date of all lookups: 2026-10-22. Sources: signed-in `gh` (account 369adi, 5000 requests/hour), the PyPI JSON API, the Hugging Face model API, LICENSE files read from the repositories, and local `git grep` / file reads in this worktree.
- GitHub's licence field shows NOASSERTION for mypy, basedpyright, pyright and the MCP reference servers. For those I read the LICENSE file head or the PyPI licence field and say which one I used.
- Nothing was installed or run on this PC. Every 'gain' below is a target with a way to measure it, not a result.
- Local tools seen on this PC while checking: `uvx` 0.12.20 (`C:\Users\liket\.local\bin\uvx.exe`), Node v24.21.0, `gh` 2.102.0.
- Result in one line per gap:

| Gap | Need | Verdict |
|---|---|---|
| G1 | A2 token estimate is a guess | BUILD IT OURSELVES (no usable outside tokenizer) |
| G2 | A3 offline type check | ONE CANDIDATE: mypy (try first); pyrefly and ty as alternates |
| G3 | A4 est_s error | BUILD IT OURSELVES (30 lines, plus one storage detail) |
| G4 | B1 known-failures comparison | BUILD IT OURSELVES (stdlib `xml.etree`); junitparser rejected |
| G5 | B3 rotating structured log with a coworker read path | BUILD IT OURSELVES (stdlib `logging`); two traps found |
| G6 | B4 update signature | CONDITIONAL: cryptography (Ed25519), only after a key-custody decision |
| G7 | B7 weekly MCP server | ONE CANDIDATE: duckduckgo-mcp-server (search + fetch), optional, off by default |
| G8 | B8 | not research: wire `eol_guard.py` into the write paths |
| G9 | B5 | closed on purpose (`network_budget.py`, `step_guard.py` beat every candidate) |

---

## G1. A2 - token estimate (bytes/3 guess, no calibration)

**Verdict: build it ourselves.** No outside library gives a Claude token count offline.

Checked and why they do not fit:

| Candidate | Facts | Result |
|---|---|---|
| [openai/tiktoken](https://github.com/openai/tiktoken) | MIT, 19,506 stars, release 0.14.0 on 2026-08-17 | Counts OpenAI vocabularies, not the model NotionTool talks to. Would replace one wrong guess with another that looks exact. Rejected (R5). |
| [tokenizers](https://pypi.org/project/tokenizers/) (Hugging Face) | 0.23.3 on 2026-10-09, `cp310-abi3-win_amd64` wheel 2.74 MB | Only a runtime; it needs a vocabulary file for the target model, and none is current (next two rows). Rejected (R5). |
| [Xenova/claude-tokenizer](https://huggingface.co/Xenova/claude-tokenizer) | MIT, last modified 2024-04-30 | An old Claude vocabulary. Not the current model. Rejected (R1, R5). |
| [anthropics/anthropic-tokenizer-typescript](https://github.com/anthropics/anthropic-tokenizer-typescript) | MIT, **archived**, last push 2024-03-04, TypeScript | Dead, needs Node. Rejected (R1). |
| GitHub searches 'claude tokenizer', 'anthropic tokenizer offline' | Only agent tools (caveman, OmniRoute, codegraph, 9router, tcount with 8 stars and others) | No tokenizer library. |

What to build (effort S):

- Today `estimate_tokens` is defined in `notiontool/chat_context.py:13` and again in `notiontool/context_archive.py:33`, with a third copy in `bench/context_switch.py:80`. It is used by `context_budget.py` (lines 9, 52), `run_context.py` (22, 239-255), and tests in `tests/test_context_archive.py`, `test_context_budget.py`, `test_context_switch_policy.py`, `test_harness_context.py`, `test_run_context_v2.py` plus `tools/context_latency_report.py`.
- Step 1: keep exactly one definition (suggest `chat_context.py`; the other two import it). Before moving, check that this creates no circular import (`context_archive` and `chat_context` already reference each other's area; I did not test the import order).
- Step 2: calibrate. Collect pairs (estimated tokens, real size) from turns where NotionTool receives a real count. **I did not verify that Notion's step records carry a token count**; this is the first thing to check. If they do, fit the divisor (now 3) per content kind (code, prose, JSON) on one week of data and accept the new numbers only if the median error drops. If they do not, keep the guess, say so in the code comment and stop; do not add a library.
- Measure: median and 90th-percentile of |estimate - real| / real before and after, over at least 200 turns.

---

## G2. A3 - type check that runs offline without Node, in seconds on changed files

All are **dev-time** tools (run in `patch_mode.verify`, not inside the EXE), so R7 (EXE size) does not apply; they only have to be installed in the Python that `patch_mode` uses (`python=None` default at `patch_mode.py:187`).

| Candidate | Licence | Last activity | Windows | Size | Node | Notes |
|---|---|---|---|---|---|---|
| [python/mypy](https://github.com/python/mypy) 2.4.0 | MIT (LICENSE head and PyPI field) | pushed 2026-10-22, PyPI upload 2026-10-01, 3,232 open issues | `mypy-2.4.0-cp312-cp312-win_amd64.whl` 10.85 MB (compiled) and `py3-none-any` 2.67 MB (pure) | 10.85 MB | none | Deps: typing_extensions, mypy_extensions, pathspec, librt, ast-serialize. Incremental cache. `--output json` exists. |
| [facebook/pyrefly](https://github.com/facebook/pyrefly) 1.4.0 | MIT | pushed 2026-10-22, release 2026-10-15 | `pyrefly-1.4.0-py3-none-win_amd64.whl` 15 MB; also `pyrefly-windows-x86_64.zip` 14.9 MB | 15 MB | none | No Python deps. Rust binary. 7,126 stars. |
| [astral-sh/ty](https://github.com/astral-sh/ty) 0.0.91 | MIT | pushed 2026-10-22, release 2026-10-22 | `ty-0.0.91-py3-none-win_amd64.whl` 13.12 MB | 13 MB | none | Version 0.0.x: rules still change between releases. 19,911 stars. Same maker as ruff (K5). |
| [detachhead/basedpyright](https://github.com/detachhead/basedpyright) 1.40.2 | MIT (LICENSE head) | release 2026-10-04 | wheel 13.06 MB **plus** `nodejs-wheel-binaries` win_amd64 40.48 MB | about 53 MB | bundled Node, no separate install | Works offline, but 53 MB for a check that has lighter options. Not chosen. |
| [microsoft/pyright](https://github.com/microsoft/pyright) 1.1.414 | MIT (LICENSE head) | release 2026-09-09 | PyPI wrapper needs Node | - | needs Node | Already rejected in 05 for the Node need. Stays rejected. |

**Pick to trial: mypy (G2a).** Reasons: oldest and best known behaviour, MIT, the smallest download, a pure-Python fallback wheel, no 0.0.x risk. pyrefly and ty are the alternates if mypy is too slow; they are single binaries and probably faster, but I did not run any of them.

- Plug-in: `notiontool/patch_mode.py`, `test_commands()` (line 344) returns a list of `(name, argv)` and `verify()` (line 371) runs them in order and stops at the first red one. Add `('types', [python, '-m', 'mypy', ...changed files...])` after ruff (K5). Exit code 1 from mypy counts as red because `_run_tests` accepts only 0 and 5.
- Trap 1: NotionTool's code is mostly unannotated. mypy does not look inside functions without annotations unless `--check-untyped-defs` is given. Without the flag the check is nearly empty; with it the first run on main will report many findings.
- Trap 2: therefore the check must compare with main. `patch_mode.py` already has `main_baseline` (line 402) and a cache `self._baseline` (line 203) for the test result of a clean main. The type check needs the same: fail only on findings that are new compared with main (compare by file, message, not by line number).
- Effort M (S for the call, M for the baseline).
- Measure (this decides it, I did not run it): (a) wall time on 5 changed files with a warm cache must be under 15 s, cold full run under 60 s; (b) the number of findings on current main with `--check-untyped-defs`; (c) over 20 past merges, how many real bugs it would have caught that the 64-75 s suite and ruff did not. If (c) is 0, drop it.
- Why it beats what exists: ruff (K5) finds syntax errors and undefined names; it does not find a wrong argument type or a call to a missing attribute across modules. Today only the test suite catches those, and only where a test covers the line.

---

## G3. A4 - error of est_s (estimated seconds) per part

**Verdict: build it ourselves.** The GitHub search 'task duration estimation history' returned no repository; this is NotionTool-specific data.

What is already there: `notiontool/parallel_plan.py` has `stats['turn_s_by_role']` as the default when a task has no `est_s` (docstring at line 28, use near line 187), `delegation_decision()` at line 232 (called from `notiontool/server.py:1122-1134`) and `agent_guard.py:24` telling the Main to send `est_s`. What is missing is the comparison estimate versus real seconds.

Build (effort S):

- At the two `delegation_decision` call sites in `server.py` record `(plan id, part id, role, est_s, wall time the plan expected)`.
- When the task finishes, store the real seconds next to it.
- A report function: median ratio real/est per role and the 90th percentile.
- **Trap:** `Store.event()` (`store.py:123`) keeps only the newest 2000 events and turns older ones into daily counts per kind (`event_rollup`). Estimates and actuals would be gone after a busy day. Store them as a record kind in the objects table (the table that `store.py` writes just above `event()`), not as events.
- Measure: after 20 plans, the median |est - real| / real and whether `delegation_decision` picks a different split when it uses the corrected estimate. Accept the correction factor only if the error shrinks.

---

## G4. B1 - automatic known-failures comparison

**Verdict: build it ourselves with the standard library; reject junitparser.**

| Candidate | Facts | Result |
|---|---|---|
| [weiwei/junitparser](https://github.com/weiwei/junitparser) 5.0.3 | Apache-2.0 (PyPI), pure `py3-none-any` wheel 0.01 MB, no deps, upload 2026-09-01, pushed 2026-10-13, 138 stars | Alive and light, but `xml.etree.ElementTree` reads a junit file in about 25 lines. A new dependency for that fails R10. |
| GitHub search 'junit xml compare baseline failures' | no repository | - |

What exists: `patch_mode.py` line 155 `_FAILED_TEST` reads names of failed unittest tests from text output with a regex (`failing_tests()` at line 160); `main_baseline` (line 402) runs a clean main and caches the answer per commit (`self._baseline`, line 203); `auto_accept.py:43-48` has a second copy of the regex with `MAX_FAILED_TESTS=3`; `fix_loop.py` (lines 35, 166) has its own baseline round. So the comparison with main exists for unittest output; the gap is that it works by regex on text.

Build (effort S, **after** K8 moves the run to pytest):

- Add `--junitxml=<file>` to the pytest command in `test_commands()` and read failing test ids from the XML in one function. Replace the regex in `patch_mode`, `auto_accept` and `fix_loop` by that function (this also removes the duplicate regex).
- Rule: task is green if its failing set is a subset of the main failing set; names are the pytest ids.
- Measure: replay the 15 branches that were rebuilt by hand on 2026-10-22 and count how many hand rebuilds the subset rule would have avoided; and verify that a test that is red on main and red on the task does not block the merge.
- Not verified: I did not read the body of `main_baseline`, only where it is defined and cached.

---

## G5. B3 - structured rotating log outside the database, readable by coworkers

**Verdict: build it ourselves with the standard library. Two traps found.**

Facts about today:

- `git grep` over `notiontool/*.py` finds no use of `logging` handlers (`FileHandler`, `RotatingFileHandler`, `getLogger`). Each subsystem appends to its own plain file: `data/model_pin.log` (`notion_shell.py:171-176`, JSON lines), `data/ai_disable_detector.log`, `data/window_login_timing.log`, `protocol-forward.log` (`protocol_forward.py:169`), `mcp-errors.log` (`bridge.py:1092`), `data/runtime/update/update.log` (`updater.py:182`). I found no rotation code for them (not verified by reading all of them).
- The only structured log is the `events` table (`store.py:123`), capped at 2000 rows (see G3).
- Coworkers cannot read `data/`: `bridge.py` `hidden_area()` (line 690) hides `store.directory`, and `devtools.py:17` has `data` in its SKIP set.

Candidates:

| Candidate | Facts | Result |
|---|---|---|
| `logging.handlers.RotatingFileHandler` ([docs](https://docs.python.org/3/library/logging.handlers.html)) | stdlib (PSF) | Thread-safe inside one process. **Trap 1:** the updater text says the MCP gateway runs as a second process. Two processes rotating one file give `PermissionError` on rename on Windows. Use one file per process (`runlog-main.jsonl`, `runlog-gateway.jsonl`). |
| [nhairs/python-json-logger](https://github.com/nhairs/python-json-logger) 4.2.0 | BSD-2-Clause, pure Python 0.01 MB, no deps, release 2026-08-15, pushed 2026-10-20, 276 stars | Alive and light, but it only provides a JSON `Formatter`, which is 8-10 lines with `json.dumps`. Fails R10. |

Build (effort M): a small module `notiontool/runlog.py` with a JSON-lines formatter, one `RotatingFileHandler` per process (for example 5 MB x 5 files), the existing `redact()` from `store.py` applied to every record, and calls from the places that now write their own files. Keep the old files until the new one is trusted.

- **Trap 2: the read path.** A file under `data/` stays invisible to coworkers. Either write to a folder that is not in the hidden area, or add a read-only bridge tool (for example a log tail returning the last N redacted lines for a given role or task id). The second keeps secrets out of coworker reach by construction. I did not check which bridge tools a coworker can call beyond the list at `bridge.py:72`; check this before choosing.
- Measure: for the next 10 failed tasks, time from 'failed' to 'cause found' with the log versus today; and file size staying under the cap after one week.

---

## G6. B4 - update file trusted by sha256 only

What exists: `notiontool/updater.py` (lines 284-302) downloads `NotionTool.exe` and `NotionTool.exe.sha256` from the **same** GitHub release and compares the hash; `.github/workflows/release.yml` (lines 31-36) creates both files in one `gh release create`. The check protects against a damaged download. It does **not** protect against someone who can publish a release (stolen token): they publish an EXE and the matching hash. The repository is private and has one owner, so the threat is a stolen GitHub token or a hijacked workflow.

| Candidate | Licence | Last activity | Windows | Size | Fit |
|---|---|---|---|---|---|
| [pyca/cryptography](https://github.com/pyca/cryptography) 50.0.2 | Apache-2.0 OR BSD-3-Clause (PyPI) | pushed 2026-10-22, upload 2026-09-30 | `cp311-abi3-win_amd64` 3.65 MB (works with Python 3.12) | 3.65 MB plus cffi | `Ed25519PublicKey.verify` is a single call. Adds a compiled dependency to the EXE; `requirements.txt` has none today (`git grep` for cryptography, nacl, ed25519, minisig in `notiontool/`, `requirements.txt`, `NotionTool.spec` found nothing). Needs a frozen-EXE test (R7 is met by size). |
| [pyca/pynacl](https://github.com/pyca/pynacl) 1.6.2 | Apache-2.0 | pushed 2026-10-22, upload 2026-01-01 | `cp38-abi3-win_amd64` 0.23 MB | small | Also needs cffi>=2.0.0. I did not open the wheel to see whether libsodium is inside; the size looks too small for that, so treat it as unverified. Not preferred. |
| [jedisct1/minisign](https://github.com/jedisct1/minisign) 0.12 | ISC | pushed 2026-10-11, last release 2025-01-15 (21 months), 0 open issues | `minisign-0.12-win64.zip` 0.2 MB | 0.2 MB | A command-line tool: fine for **signing** on a build machine, but verifying inside the EXE needs a subprocess and a bundled binary. Release age is past R1's 12 months; the repository itself is alive (borderline). Not preferred for verify. |
| [minisign](https://pypi.org/project/minisign/) (PyPI) | MPL-2.0 | 0.1.0 from 2020-09-20 | pure Python | - | Stale. Rejected (R1). |
| `pyminisign` (PyPI) | - | - | - | - | Does not exist (404). |
| own pure-Python Ed25519 | - | - | - | - | Rejected: own crypto code. |

**Verdict: conditional.** The tool question is easy (cryptography, Ed25519, public key embedded in the EXE as a constant). The real question is key custody, and it decides whether any of this has a gain:

- If the private key lives in GitHub Actions secrets, anyone who can change the workflow or steal the token can also sign. Gain: none.
- If the key stays on the owner's PC and the signature is made there after the build, the 'every push to main publishes' flow becomes semi-manual. Gain: real, but only for an attacker who has GitHub but not the PC.

Decision needed from the owner before any work. Effort if yes: M (sign step, `.sig` asset, verify in `updater.py` near line 300, frozen-EXE test, a key-rotation plan). Measure: a test with a release whose EXE and hash were swapped must be refused.

---

## G7. B7 - an MCP server that someone calls every week

How servers plug in today: `notiontool/local_mcp.py` has a `SERVERS` list. Each entry has a key, a Settings toggle (`setting=`), a `command` and an `installed` check; workers get two lazy tools per server (`<key>_tools`, `<key>_call`) and the process starts on first use. `blender` is a close model: `command=lambda: [uvx_path() or 'uvx', 'blender-mcp']`, `installed=lambda: bool(uvx_path())`, `setting='blender_enabled'`. So a new Python MCP server is one dict entry and one setting.

The gap named: both researchers had no web search and no page fetch and used `gh` through `nt_shell`.

| Candidate | Licence | Last activity | Windows / runtime | Weekly role and calls removed (R9) | Result |
|---|---|---|---|---|---|
| [nickclyde/duckduckgo-mcp-server](https://github.com/nickclyde/duckduckgo-mcp-server) 0.7.0 | MIT (GitHub) | release 2026-09-04, 1,550 stars | pure Python wheel via `uvx`; deps beautifulsoup4, httpcore, httpx, `mcp[cli]<3,>=2.1.1`; `uvx` 0.12.20 is on this PC | Research roles (like this one): 'search the web, open a page as text'. Today there is no tool for it at all, so it **adds** a capability. Called in every research chain. | **Try it** (G7a). |
| [modelcontextprotocol/servers](https://github.com/modelcontextprotocol/servers): `mcp-server-fetch` 2026.10.19 | MIT (PyPI; GitHub shows NOASSERTION) | pushed 2026-10-22 | pure wheel; deps httpx, markdownify, mcp, protego, pydantic, readabilipy, requests | Fetch only, no search. Helps for HTML pages; JSON APIs already work with PowerShell as this research did. | Alternate if G7a's fetch is poor. |
| [zcaceres/fetch-mcp](https://github.com/zcaceres/fetch-mcp) | MIT | last push 2026-03-12, no release object, 831 stars | Node | Same as above; slower activity, Node. | Not chosen. |
| [github/github-mcp-server](https://github.com/github/github-mcp-server) v2.0.3 | MIT | release 2026-10-21, 33,862 stars | `github-mcp-server_Windows_x86_64.zip` 9.1 MB | Would replace `gh` calls, but `gh` 2.102.0 is installed and signed in; a coworker already does this through `nt_shell`. No call removed. | Rejected (R9, R11). |
| [searxng/searxng](https://github.com/searxng/searxng) | AGPL-3.0 | pushed 2026-10-19, 38,741 stars | a server process | A hosted search server to run and keep alive. | Rejected (R2 licence for anything bundled, R10). |

**G7a plan (effort S):**

- Add `dict(key='web', setting='web_enabled', command=lambda: [uvx_path() or 'uvx', 'duckduckgo-mcp-server'], installed=lambda: bool(uvx_path()), ...)` to `SERVERS` in `local_mcp.py`, **off by default**, like the other servers.
- Pin the version in the command (like REA is pinned to 6.5.0) so an upstream change cannot alter a coworker's tools silently.
- Not verified, to read before enabling: the server's README (tool names, how it handles local or internal addresses when fetching a URL), and whether DuckDuckGo's HTML scraping is allowed for this use. Queries go to DuckDuckGo (R8): search text only, never source code.
- Measure: run the 10 queries that this research round needed; accept if at least 8 return usable results and the page fetch returns readable text for 8 of 10 pages; compare the number of steps for a research task with and without it (the baseline is that 44 % of tool calls are read/search).
- Honest limit: 'weekly' is a forecast. The use is every research chain, but the gain is only proven by the trial; if research chains happen less than weekly, R9 fails and the entry stays off.

---

## G8. B8 - not a research gap

`notiontool/eol_guard.py` already exists with `normalize`, `detect`, `report` and tests (`tests/test_eol_guard.py`, `tests/test_repo_eol.py`). No outside tool is needed. The work is to call `eol_guard.normalize(text, existing)` where files are written: the bridge write path (the write check near `bridge.py:825`) and `patch_mode`. A `git grep` for `eol` in `bridge.py` and `devtools.py` found no use today. Effort S. Measure: a write of a CRLF file with LF text must keep CRLF, and `tests.test_repo_eol` stays green.

## G9. B5 - closed

No change from 05: `network_budget.py` and `step_guard.py` beat every outside candidate. No new search done.

---

## Rejected in this round

| Item | Gap | Rule | Reason |
|---|---|---|---|
| tiktoken | G1 | R5 | OpenAI vocabulary, a wrong count that looks exact |
| tokenizers + Xenova/claude-tokenizer | G1 | R1, R5 | vocabulary from 2024-04-30 |
| anthropic-tokenizer-typescript | G1 | R1 | archived 2024, Node |
| basedpyright | G2 | R10 | about 53 MB with bundled Node for a check mypy does in 11 MB |
| pyright | G2 | R3/R10 | needs Node (as in 05) |
| junitparser | G4 | R10 | standard library does it in 25 lines |
| python-json-logger | G5 | R10 | 8-10 lines of `json.dumps` |
| pynacl | G6 | - | needs cffi, wheel contents unchecked, cryptography is the better known choice |
| minisign (binary) for verify | G6 | R1/R10 | release 21 months old; needs a subprocess and bundled binary |
| minisign (PyPI) | G6 | R1 | last release 2020 |
| github-mcp-server | G7 | R9, R11 | `gh` already works, no call removed |
| searxng | G7 | R2, R10 | AGPL server to host |
| fetch-mcp (zcaceres) | G7 | R10 | Node, slower activity, same function as the Python option |

## Not verified, needs a local run

1. G1: whether Notion step records contain a real token count to calibrate against.
2. G2: mypy (and pyrefly, ty) run time and findings on current main; none was run.
3. G3: that the objects table is the right place for plan records (read the code just above `Store.event`).
4. G5: which bridge tools coworkers may call (to pick the read path); whether any of the existing `.log` writers already rotates.
5. G6: key custody decision by the owner; a frozen-EXE test of cryptography.
6. G7: the DuckDuckGo server's README, tool names, local-address handling and the 10-query trial.

## Work order for the lead

1. G8 (S, no research, removes a known defect) and G1 step 1 (merge the three `estimate_tokens` copies, no behaviour change).
2. G4 together with K8, because both touch the pytest command.
3. G3 and G5 (own code, small).
4. G2 trial with mypy (decides itself by the three measures).
5. G7a trial (off by default).
6. G6 only after the owner decides about key custody.
