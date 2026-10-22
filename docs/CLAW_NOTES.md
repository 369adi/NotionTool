# claw-code review notes

Source reviewed: https://github.com/ultraworkers/claw-code, shallow clone in `work/claw/`
(HEAD 22fe36072a37cb1c489589f2e98ff3dd5357bb5a, 2026-10-19). No code was copied into
`notiontool/`. Everything below is a description in our own words; any idea we adopt must be
written from scratch.

## 1. License and provenance

- License: **MIT**, "Copyright (c) 2026 UltraWorkers and Claw Code contributors".
- README: "the public Rust implementation of the `claw` CLI agent harness". The canonical code is
  `rust/`. `src/` and `tests/` are a Python reference and audit workspace. The README calls the
  repo an "agent-managed exhibit" and points to other tools for real work.
- Provenance: the README **does not claim a clean-room implementation**. It says the repo "does
  not claim ownership of the original Claude Code source material" and is "not affiliated with,
  endorsed by, or maintained by Anthropic". `PARITY.md` is titled "Parity Status - claw-code Rust
  Port", `rust/README.md` calls it a "Rust rewrite", and `PHILOSOPHY.md` describes earlier Python
  and Rust "rewrites". The runtime even reads an env var named
  `CLAUDE_CODE_AUTO_COMPACT_INPUT_TOKENS`. So it presents itself as a port with behavioral parity
  to Claude Code, and its origin is not documented as clean-room.
- Consequence for us: the MIT license would allow reuse, but because the provenance is unclear we
  take **ideas only, no code, no prompt text, no copied tool schemas**.

## 2. How the claw-code agent loop works (plain English)

- **Loop** (`runtime/src/conversation.rs`, `run_turn`): it sends the session to a model **API**
  (Anthropic or OpenAI-compatible) as a streamed request. Then it collects the assistant
  events. For each tool-use block it runs a pre-tool hook, the permission check, the tool, and a
  post-tool hook (or a failure hook), and appends the tool result to the session. It repeats until
  the model stops asking for tools or until `max_iterations` is reached. It supports interrupts,
  "steer" messages queued into a running turn, aborts, an event callback, a session tracer, and
  session forks.
- **Tool-call parsing:** there is no text parsing. The provider returns structured
  `ToolUse { id, name, input }` content blocks with JSON input. The tool executor receives
  `(tool_name, input_json)` and returns a string result or an error.
- **Tool set** (`tools/src/lib.rs`, about 50 tools): bash, PowerShell, REPL, read/write/edit file,
  NotebookEdit, Git* (status/diff/log/show/blame), WebFetch, WebSearch, LSP, MCP plus MCP resources,
  TodoWrite, Task* (create/get/list/update/stop/output), Agent, Team*, Worker* (spawn and drive
  other worker sessions), Cron*, Memory read/write, Skill, ToolSearch, AskUserQuestion,
  Enter/ExitPlanMode, StructuredOutput, Sleep, Config. The tools a model sees can be filtered
  with an `allowed_tools` set.
- **Todo lists:** a `TodoWrite` tool takes the whole list (`todos` array with content and status)
  on each call. The model keeps its plan visible and updates it as it works. Separate `Task*`
  tools manage background tasks in a task registry.
- **Context compaction** (`runtime/src/compact.rs`): when estimated input tokens pass a threshold
  (default 100k, can be overridden by an env var), the older messages are replaced by a summary
  and the most recent messages are kept word for word. The summary is built **locally and
  heuristically**: role summaries, inferred pending work, key files mentioned, current work. It is
  merged with any earlier summary and comes with a continuation instruction ("resume directly,
  do not recap").
- **Subagents:** an `Agent` tool with a `subagent_type` starts a child conversation with its own
  prompt and a restricted tool set, and returns its final answer as the tool result. `Worker*` and
  `Team*` tools coordinate longer-lived parallel sessions.
- **Edit/diff tools:** the edit tool takes `path`, `old_string`, `new_string` and optional
  `replace_all`. Without `replace_all` the old string must match exactly once. Write and edit
  return a structured patch or diff of what changed. Git tools give the model read-only diff
  views.
- **Permission checks:** there are modes `read-only`, `workspace-write`, `danger-full-access`,
  `prompt` (ask the user) and `allow`. Each tool declares the mode it needs. A permission
  enforcer and a policy engine decide allow, deny or ask before execution. Separate bash validation
  classifies shell commands (read-only vs. destructive), and a sandbox layer is used where the OS
  supports it. Hooks can also block a tool call.

## 3. Comparison with the NotionTool harness

| Aspect | claw-code | NotionTool |
|---|---|---|
| Model access | Direct model API, streamed, structured tool-use JSON | No model API. The model runs only inside Notion Welcome chats. `engine.py` (`Engine`) drives chats and handles pending remote replies, rate limits, transient inference errors and context checkpoints. |
| Tool calls | Native tool-use blocks | MCP tools exposed by `devtools.py` (`call`, `_schema`, `shell_guard`, sha/backup helpers) and called by the chat model through Notion's MCP integration |
| Loop control | `run_turn` with max iterations, steer, interrupt | `operator_main.py` `delegate` plus the required `RESULT: DONE / NOT DONE` final line (`result_done`). `operator_roles.py` stages (design, review, fix list, open points). |
| Edits | Exact-string edit with a uniqueness check plus patch output | `nt_edit_file` / `nt_write_file` with `expected_sha256`, backups, and a per-task file list. `patch_mode.py` provides git-based patch isolation, conflict hunks, secret detection and a root lock. |
| Context | Heuristic local compaction at a token threshold | Notion chat context and checkpoint handling (`ContextCheckpointPending`). No structured summary of our own. |
| Permissions | Mode tiers plus a policy engine plus bash validation | Assignment tokens, per-task write lists, locked files, `shell_guard` |
| Subagents | `Agent` tool with restricted tools | Delegated runs in separate Welcome chats (other chats, tokens) |
| Planning | TodoWrite, plan mode | Free-text in chat. Progress feed in the desktop UI. |

Our side is already stronger on write safety (sha checks, file lists, patch isolation). The gaps are
structured state that outlives a chat (todos, compact summaries), explicit permission tiers, and
deterministic tests of the loop.

## 4. Ranked ideas worth rebuilding ourselves (max 8)

1. **Structured per-run todo list tool**
   - Benefit: the model's plan becomes machine-readable. It feeds the progress feed and
     Live UI ("step 2 of 5"), survives context checkpoints, and makes `NOT DONE` reasons concrete.
   - Effort: S
   - Files: `notiontool/devtools.py` (new `nt_todo` tool, whole list replaced on each call),
     `notiontool/progress_feed.py`, `notiontool/operator_main.py` (include the open todos in the
     summary), new tests.
   - Risk: low. The model may forget to update it, so treat it as advisory and never let it gate
     completion.
2. **Local handoff summary at context checkpoints**
   - Benefit: when a Welcome chat nears its limit or is switched, the harness builds its own
     summary (goal, pending work, key files touched, last results, recent turns verbatim) and seeds
     the next chat with it. It does not rely only on the model's own summary.
   - Effort: M
   - Files: `notiontool/engine.py` (checkpoint path), `notiontool/chat_context.py`,
     `notiontool/main_handoff_ops.py`.
   - Risk: medium. A lossy summary can drop constraints such as locked files or tokens, so
     always re-inject the original task text in full.
3. **Explicit permission tiers per task** (read-only / workspace-write / full)
   - Benefit: review and research tasks like this one could get a read-only tier enforced in
     code, not only in prompt text. It would also make the scope readable in the UI.
   - Effort: M
   - Files: `notiontool/devtools.py` (tier check in `call`), `notiontool/operator_roles.py`
     (tier per stage), `notiontool/operator_main.py` (tier in the delegate payload), tests.
   - Risk: medium. A wrong default tier could block existing delegated runs, so roll it out with
     the default `workspace-write`.
4. **Restricted tool sets per role stage** (subagent-style allow-lists)
   - Benefit: design and review stages see only read and search tools, and only the fix stage can
     write. This cuts accidental edits and prompt size.
   - Effort: M
   - Files: `notiontool/operator_roles.py`, `notiontool/devtools.py` (`_schema` filtered by
     allow-list), `notiontool/server.py` if the tool listing is built there.
   - Risk: medium. Notion may cache MCP tool lists per connection, so filtering might need to
     happen at call time instead of list time.
5. **Edit tool hardening: uniqueness, `replace_all`, diff in the result**
   - Benefit: `nt_edit_file` returns a short unified diff and refuses ambiguous matches with
     the match count. The model and the reviewer stage can see exactly what changed.
   - Effort: S
   - Files: `notiontool/devtools.py`, tests.
   - Risk: low. Long diffs must be capped (reuse `_cap`).
6. **Pre/post tool hooks**
   - Benefit: one place for policy (shell guard, secret scan like `patch_mode._secret_kind`,
     audit log, progress events) instead of checks scattered across tools.
   - Effort: M
   - Files: `notiontool/devtools.py`, `notiontool/patch_mode.py`, `notiontool/progress_feed.py`.
   - Risk: medium. Hook errors must fail closed for writes and fail open for reads, and they add
     latency to every call.
7. **Plan mode stage**
   - Benefit: for large tasks, the first stage may only read and produce a plan or todo list,
     and writes unlock after the plan is accepted (automatically or by the operator). This fits
     the existing design/review stages.
   - Effort: S/M (cheap once ideas 1 and 3 exist)
   - Files: `notiontool/operator_roles.py`, `notiontool/operator_main.py`.
   - Risk: low/medium. One more round trip per task through slow Notion chats.
8. **Mock chat harness for deterministic loop tests**
   - Benefit: scripted fake Welcome-chat replies (tool calls, rate limits, checkpoints,
     `RESULT` lines) let us test `Engine` and the operator flow end to end without Notion. This is
     the same goal as their mock parity harness, built our way.
   - Effort: L
   - Files: new `tests/fake_chat.py` plus tests, a small injection seam in
     `notiontool/engine.py` (and possibly `notiontool/notion.py`).
   - Risk: medium. The fake can drift from real Notion behavior. Keep scenarios recorded from
     real runs.

Not recommended: model-API streaming, provider abstraction, OAuth flows, LSP and cron tools. They
conflict with the rule that the model runs only through Notion Welcome chats, or they add surface
without clear benefit.
