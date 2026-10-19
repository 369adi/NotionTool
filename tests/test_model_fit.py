"""Rule I35 backtest and unit tests for notiontool.model_fit (pure, no DB access in tests).

RAW and HOLDOUT are real past tasks read read-only from data/notiontool.db (objects,
kind='tasks'). Title, role, task_class, risk, files count and the stored model/effort
are copied as stored; the prompt column is a short excerpt of the prompt start.
Labels follow Rule I35. Run with -v / python -m unittest to print the before/after report.

HOLDOUT was selected by ID only (sorted task ids whose last two hex digits mod 16 == 5,
not in RAW, first 40), before the rules were looked at; the rules were not tuned on it.
Re-list the candidates with: python tests/test_model_fit.py --dump-holdout
"""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from notiontool import model_fit  # noqa: E402
from notiontool import task_router  # noqa: E402
from notiontool.effort_policy import choose_reasoning_effort  # noqa: E402

H, S, O = 'haiku', 'sonnet', 'opus'
L, M, X = 'low', 'medium', 'high'
E = ''

# id | role | task_class | risk | files | stage | stored model | stored effort | title | prompt excerpt
# | label model | label effort | reason
RAW = [
    ('td340f0772', 'planner', E, E, 0, E, O, E, 'Planner', 'Split the goal into parts with files and dependencies', O, X, 'planning is architecture work'),
    ('t4c4fab5e9', 'executor', E, E, 0, E, O, E, 'Compress check', 'Reply with exactly one line: COMPRESS-OK', H, L, 'echo check, mechanical'),
    ('t5ec39e7c1', 'executor', E, E, 0, E, H, E, 'Cleanup reply 4', 'Reply with exactly: CLEANUP-4', H, L, 'echo check'),
    ('te3109beb8', 'executor', E, E, 0, E, H, E, 'Recovery reply 1', 'Reply with exactly: RECOVERY-1', H, L, 'echo check'),
    ('t9933360c8', 'executor', E, E, 0, E, H, E, 'Part reply 3', 'Reply with exactly: PART-3', H, L, 'echo check'),
    ('tccb5956d3', 'executor', E, E, 0, E, O, E, 'Telemetry benefits', 'List three short benefits of telemetry, one line each', H, L, 'short text answer'),
    ('td98f80fe6', 'executor', E, E, 0, E, H, E, 'Addition', 'Add 41 and 1. State the result only.', H, L, 'arithmetic'),
    ('t828e4e84f', 'executor', E, E, 0, E, H, E, 'Multiply 17 by 23', 'Work out 17 x 23 and reply with the number', H, L, 'arithmetic'),
    ('t9d82f839a', 'executor', E, E, 2, E, S, E, 'Router model_reason', 'Read notiontool/model_routing.py and make the reason text name the rule', S, M, 'normal code change in 2 files'),
    ('tb5db3f02c', 'executor', E, E, 2, E, S, E, 'Fix: Document turn-watch events', 'Find where turn_watch emits events and document each event in the docs file', H, M, 'docs only; fix stage +1'),
    ('t8944719b8', 'executor', E, E, 0, E, H, E, 'Fix: Recheck parts 4 and 5', 'Run tests.test_part4 and tests.test_part5 and report the counts', H, M, 'rerun tests and report; fix stage +1'),
    ('tf6bfb84f8', 'executor', E, E, 1, E, O, E, 'Fix the import in busy-Main test', 'In tests/test_busy_main.py change only line 3: the import path', H, L, 'one-line mechanical edit; not a fix stage'),
    ('t2cb803688', 'executor', E, E, 2, E, S, E, 'Enforce the capture policy', 'Read the capture doc and plain_protocol.py, then enforce the policy in code', S, M, 'normal code'),
    ('t57efdeda8', 'executor', E, E, 2, E, S, E, 'Part B: context budget module', 'Write notiontool/context_budget.py with token budget helpers and tests', S, M, 'normal module + tests'),
    ('t6c440fd9b', 'executor', E, E, 2, E, S, E, 'SSE resume with ?since', 'In server.py let the event stream resume from ?since=<seq>', S, M, 'normal server code'),
    ('t02cc5d69b', 'executor', E, E, 2, E, O, E, 'Fix: Part 5: reviewer unittest shell', 'Only after the operator releases bridge.py: let the reviewer run unittest through the shell', S, X, 'normal code; fix stage +1'),
    ('t538100369', 'executor', E, E, 2, E, O, E, 'Part B: wire the policy', 'Call the capture policy from engine.py where results are stored', S, M, 'normal wiring code'),
    ('td0500187c', 'executor', E, E, 0, E, S, E, 'Fix: test_engine failure repeat', 'Run test_retry_bounded_when_provider_unavailable on its own and find out why the failure repeats', O, X, 'root-cause hunt of a repeating test failure'),
    ('t0406626c7', 'executor', E, E, 2, E, S, E, 'Fix: Part A capture policy', 'Write the capture policy module and its tests', S, X, 'normal code; fix stage +1'),
    ('t0efa14408', 'executor', E, E, 0, E, S, E, 'Fix: Find who changed protected files', 'Read-only. Find which task changed protected files', O, X, 'forensic root-cause hunt'),
    ('t32c817b1b', 'executor', E, E, 4, E, O, E, 'Verifier cascade', 'Read work/audit/ARCHITECTURE_AUDIT.md and build the verifier cascade', O, X, 'architecture of the verify chain'),
    ('tf466c0288', 'executor', E, E, 0, E, O, E, 'Integration review', "Don't edit any file. Re-run each module's tests and judge the combined result", O, X, 'critical integration review'),
    ('t1f851ebb9', 'executor', E, E, 2, E, S, E, 'DAG helpers', 'Write pure DAG helper functions (topological order, cycles) with tests', S, M, 'normal code'),
    ('tb5a76be46', 'executor', E, E, 1, E, O, E, 'Fault injection tests', 'Create tests/test_fault_injection.py using the test_engine fixtures', S, M, 'normal test code'),
    ('t92b3582dc', 'executor', E, E, 7, E, S, E, 'Fix: Interface glue', 'Connect merge_verdicts and the callers across the modules', S, X, 'normal code over 7 files'),
    ('t73c2b66c7', 'executor', E, E, 2, E, O, E, 'Fix: Write fault injection tests', 'Clear or cancel the stale lock from the task first. Then create tests/test_fault_injection.py', S, X, 'normal test code; fix stage +1'),
    ('tfc7b37b30', 'executor', E, E, 4, E, O, E, 'Part A: Benchmark suite', 'Create the benchmark suite with 6 task objects and a runner', S, X, 'normal code over 4 files'),
    ('ta3cf428d2', 'executor', E, E, 0, E, S, E, 'Fix: Final independent review run', 'Read-only, write nothing. Run python -m unittest and judge every change', O, X, 'critical final review'),
    ('t9ddcb8e6c', 'executor', E, E, 2, E, S, E, 'Fix accepted_at type', 'accepted_at must be a float timestamp; fix the writer and add a test', S, M, 'normal code fix in 2 files; not a fix stage'),
    ('taf2183251', 'executor', E, E, 2, E, S, E, 'Fix work_queue dependency field', 'Use depends_on in work_queue and fix the function signature', S, M, 'normal code fix in 2 files; not a fix stage'),
    ('t0d515afd2', 'executor', E, E, 2, E, S, E, 'Part B: Worker cleanup', 'Add worker states and let the engine tie workers to tasks', S, M, 'normal code'),
    ('t2f5018ebc', 'executor', E, E, 4, E, O, E, 'Part A handoff files', 'Find the real handoff file name and write the handoff helpers', S, X, 'normal code over 4 files'),
    ('t97344e454', 'executor', E, E, 3, E, O, E, 'Handoff ops module and tests', 'Write handoff_ops.py and tests for it', S, M, 'normal module + tests'),
    ('tbafdc9c52', 'executor', E, E, 0, E, S, E, 'Fix: Check route and module work together', 'Read-only. Import notiontool.server and confirm the call signature matches', S, M, 'read-only check; fix stage +1'),
    ('tfc90a8ff9', 'executor', E, E, 3, E, H, E, 'Point the first message to the resume instructions', 'Change the first message text so it names the resume file', H, L, 'small text change'),
    ('t264cde04f', 'executor', E, E, 3, E, O, E, 'Engine leftovers Phase 4', 'Move the remaining helpers out of engine.py, batch 3', S, M, 'normal refactor in 3 files'),
    ('t7407cd677', 'executor', E, E, 5, E, S, E, 'Review and combine', 'Read both parts, change no files, run the test modules and node --check', O, X, 'critical integration review'),
    ('t4195d9347', 'executor', E, E, 2, E, S, E, 'Snapshots module', 'Write notiontool/snapshots.py and tests', S, M, 'normal module'),
    ('t33873d2f9', 'executor', E, E, 2, E, H, E, 'Fix: Task-IDs für Reconcile im Zustand', 'build_state soll eine Liste reconcile_task_ids liefern', S, M, 'small code change; fix stage'),
    ('t9d572db6b', 'executor', E, E, 0, E, S, E, 'Review Part B and snapshots', 'Read-only: confirm the files exist, LF not CRLF, and the tests pass', S, L, 'read-only check list'),
    ('tb67f28710', 'executor', E, E, 0, E, S, E, 'Review main_wake', 'Keine Datei ändern. 1) Test-Path 2) CRLF count 3) sha256', S, L, 'read-only check list'),
    ('tcd0727b6c', 'executor', E, E, 0, E, S, E, 'Review stall_watch', 'Change nothing. Check bytes, CRLF and the test counts', S, L, 'read-only check list'),
    ('ta15236db3', 'executor', E, E, 0, E, S, E, 'Review the run_context fix', 'Change nothing. Run the run_context tests and report', S, L, 'read-only check list'),
    ('t67a04c482', 'executor', E, E, 5, E, S, E, 'Analyse prüfen', 'Nur lesen. Öffne jede Datei:Zeile und prüfe das Zitat', S, L, 'read-only check of an analysis'),
    ('t2f97f1eb0', 'executor', E, E, 0, E, S, E, 'Fix: Bericht zur Modellwahl korrigieren', 'Nur lesen in der DB, Zahlen nachzählen und den Bericht korrigieren', S, M, 'read-only recount; fix stage +1'),
    ('tb6658d52b', 'executor', E, E, 0, E, S, E, 'Own chat and coworker buttons check', 'Do not edit any file. Recompute the hashes and compare', S, L, 'read-only check list'),
    ('t2a7676f48', 'executor', E, E, 2, E, O, E, 'Review file_leases with shell', 'Review the file_leases code and run its tests', O, X, 'lease/lock concurrency review'),
    ('te04d9ceff', 'executor', E, E, 0, E, O, E, 'test_native failures', 'Read-only task: three tests in test_native fail; find the cause', O, X, 'root-cause hunt'),
    ('t61e26e5c0', 'executor', E, E, 3, E, O, E, 'Operator acceptance check', 'Check the operator flow end to end against the goal', O, X, 'critical acceptance review'),
    ('tc4e15c656', 'executor', E, E, 2, E, O, E, 'Fix setup_status tests or code', 'Three tests in test_setup_status have failed; decide if tests or code are wrong', O, X, 'root-cause hunt'),
    ('t592b861aa', 'executor', E, E, 3, E, O, E, 'Own task must not be blocked by its own file lock', 'A task waits on its own file lock', O, X, 'lock/concurrency bug'),
    ('t28e4ecfec', 'executor', E, E, 2, E, O, E, 'Handoff file cleanup after 24 h', 'Delete handoff files after success or after 24 hours', S, M, 'normal code'),
    ('tbf88db13b', 'executor', E, E, 2, E, O, E, 'Main confirmation stage awaiting_main', 'Add the awaiting_main state and confirm_run in engine.py', S, M, 'normal state code'),
    ('tfd8507f92', 'executor', E, E, 2, E, O, E, 'Repository line-ending guard test', 'Add a test that all .py files use LF', H, L, 'simple test'),
    ('t50006dabc', 'executor', E, E, 1, E, O, E, 'Audit old blocked runs', 'Audit (read-only): list old blocked runs and say if each is superseded', S, M, 'read-only audit, needs judgment'),
    ('taf412639e', 'executor', E, E, 2, E, O, E, 'engine.py task model on resume + shared path key', 'Keep the task model on resume and use one path key for absolute claims', O, X, 'claims/concurrency'),
    ('td0251802f', 'executor', E, E, 4, E, O, E, 'P8b ui_mode start check', 'Check ui_mode at start and keep the overlay consistent', S, X, 'UI building over 4 files'),
    ('t0bfad54ae', 'executor', E, E, 2, E, O, E, 'Docs: benchmark offline mode', 'Refresh the docs for the benchmark offline mode', H, L, 'docs only'),
    ('t4b04ae894', 'executor', E, E, 1, E, O, E, 'Read-only audit: UI vs /api/state fields', 'List every field the UI reads that /api/state does not send', S, M, 'read-only extraction across JS and Python'),
    ('t4e25e1598', 'executor', E, E, 2, E, O, E, 'turn_watch auto-continue', 'Let the operator Main chat auto-continue, with its own setting', S, M, 'normal code'),
    ('tfe283e76d', 'executor', E, E, 3, E, O, E, 'Operator mode for MainHandoffOps', 'Add operator mode to MainHandoffOps and mains.run_create', S, M, 'normal code'),
    ('t9af21c8bf', 'executor', E, E, 3, E, O, E, 'Desktop overlay sidebar', 'Sidebar with Main chats only, New chat goes to Welcome', S, M, 'UI building'),
    ('t26c866327', 'executor', E, E, 1, E, O, E, 'Write the Main switch brief', 'Write a short brief file for the Main switch', H, L, 'docs only'),
    ('tbd6c220f7', 'executor', E, E, 4, E, O, E, 'Design system and global theme', 'Create the design tokens and the global theme for the app', S, X, 'UI building over 4 files'),
    ('tc3e164df5', 'executor', E, E, 4, E, O, E, 'Settings panel redesign', 'Rebuild the settings panel layout', S, X, 'UI building over 4 files'),
    ('t1cd54925b', 'executor', 'code', 'high', 5, E, O, E, 'Composer and toast live bugs', 'Live screenshot bugs: a watermark label overlaps the composer', S, X, 'UI fixes, risk high'),
    ('t7e0e2f3f5', 'executor', 'code', 'high', 3, E, O, E, 'Sidebar shows wrong chats', 'The sidebar shows only coworker chats; find out', O, X, 'root-cause hunt of wrong state'),
    ('tbf4554384', 'executor', 'review', 'high', 0, E, O, E, 'Integration and review', 'Review all changes critically and run all tests', O, X, 'critical review'),
    ('ta060b4efb', 'executor', 'code', 'high', 2, E, O, E, 'Theme v2', 'Make the whole app clearly a different product: typography, colors', S, X, 'UI building, risk high'),
    ('t7520ea84d', 'executor', 'code', 'medium', 4, E, O, E, 'Composer power tools', 'Add slash commands and attachments to the composer', S, X, 'UI building over 4 files'),
    ('t3ba75a600', 'executor', 'code', 'high', 5, E, O, E, 'Simple Live view', 'Rebuild the live view in a simple layout', S, X, 'UI building, risk high'),
    ('t09a3286f1', 'executor', 'code', 'high', 4, E, O, 'max', 'Grow the team automatically', 'Server code that adds coworkers when the queue grows', S, X, 'normal code, risk high'),
    ('t2c7507f63', 'executor', 'code', 'medium', 5, E, O, E, 'Request templates and history search', 'Add templates and a history search box', S, X, 'UI building over 5 files'),
    ('t008801428', 'executor', 'code', 'medium', 4, E, O, E, 'Live coworker progress display', 'Show each coworker progress live', S, X, 'UI building over 4 files'),
    ('tc94fcb378', 'executor', 'code', 'high', 2, E, O, 'max', 'Connect to private GitHub repo', 'Add the repo remote and a secret scan before push', S, X, 'normal code, risk high'),
    ('t6806bad0d', 'executor', 'code', 'high', 3, E, O, 'max', 'Stop creating a second Main', 'ensure_team sometimes creates a second Main chat', O, X, 'race/duplicate creation'),
    ('t0be95e58b', 'executor', 'code', 'medium', 6, E, O, X, 'Fix tab bar and sidebar layout', 'Fix the tab bar and the sidebar layout', S, X, 'UI fixes over 6 files'),
    ('t972810b2d', 'executor', 'code', 'low', 3, E, O, M, 'Make coworker list collapsible', 'Let the coworker list collapse', S, L, 'small UI change, risk low'),
    ('t331d373b5', 'executor', E, 'medium', 2, E, O, X, 'Auto continue when coworkers finish', 'When all coworkers finish, continue the Main chat', S, M, 'normal code'),
    ('t6db911c1c', 'executor', E, 'low', 1, E, O, M, 'Plan: Tester', 'Plan how to prove the feature works live', O, X, 'test planning'),
    ('tbf7f2f312', 'reviewer', E, E, 0, E, O, X, 'Review: Auto continue when coworkers finish', 'Independently review the change', O, X, 'critical review of real code'),
    ('t7626bd7b7', 'reviewer', E, E, 0, E, O, X, 'Review: Feature gap list', 'Review the gap list old window vs new app', O, X, 'critical review'),
    ('ta55d64095', 'executor', E, E, 1, E, O, X, 'Tester: backtest harness for the router', 'Write a backtest harness for task_router', S, M, 'normal code'),
    ('tb2cfee1d7', 'executor', E, E, 5, E, O, X, 'Fix 2: MCP servers tab', 'Build the MCP servers tab in the new app', S, X, 'UI building over 5 files'),
    ('t3faa997b0', 'executor', E, E, 1, E, O, X, 'Fix 1: Critic: what is broken', 'As a critic, list what is broken in the new app', O, X, 'critical review'),
    ('t19991cec2', 'executor', E, E, 1, E, O, X, 'Fix 2: Designer critique + redesign spec', 'Write the designer critique and a redesign spec', O, X, 'design spec'),
    ('tf246780bd', 'executor', E, E, 2, E, O, X, 'Review + backtest of the rules injection', 'Review the rules injection and backtest it', O, X, 'critical review'),
    ('tcf4c9ceb5', 'executor', E, E, 3, E, O, X, 'Restart note in coworker prompts', 'Add a restart note constant to coworker prompts', S, M, 'normal code'),
    ('tb78c5374a', 'reviewer', E, E, 0, E, O, X, 'Review: Restart note in coworker prompts', 'Independently review the change', O, X, 'critical review'),
    ('tbb454fc1e', 'executor', E, E, 2, E, O, X, 'Fix 2: Inject fixed rules block', 'Inject the fixed rules block into prompts', S, X, 'normal code; fix stage +1'),
    ('t7dab9caa6', 'executor', E, E, 2, E, O, X, 'Write backtest file', 'Create work/bt.txt with exactly the single line OK and a test', H, L, 'one-line file + simple test'),
    ('tfad881e96', 'executor', E, E, 2, E, O, X, 'Fix 2: Write backtest file', 'Create work/bt.txt with exactly the single line OK', H, M, 'one-line file; fix stage +1'),
    ('t77a091fe6', 'executor', E, E, 2, E, O, X, 'Fix 1: Write review backtest file and test', 'Create the file with the single line REVIEW-OK and a test', H, M, 'one-line file; fix stage +1'),
    ('tde952b98f', 'executor', E, E, 2, E, O, X, 'Write review backtest 3 file', 'Create the file with exactly the single line R3', H, L, 'one-line file'),
    ('t35d9bad63', 'executor', E, E, 1, E, O, X, 'Write deps backtest test reading A', 'Task A created a file; create a test that reads it', H, L, 'simple test'),
    ('t73a7a99ff', 'reviewer', E, E, 0, E, O, X, 'Review: Write deps2 backtest test reading A', 'Review the test', S, L, 'review of trivial work is not critical'),
    ('t7b5568d79', 'executor', E, E, 2, E, O, X, 'split_csv_line', 'Write split_csv_line without the csv module, with quote handling and tests', S, M, 'normal code'),
    ('t7b8ca6f94', 'executor', E, E, 2, E, O, X, 'Fix 1: parse_duration', 'Fix parse_duration for 1h30m and add tests', S, X, 'normal code; fix stage +1'),
    ('t4bd13cb7f', 'executor', E, E, 2, E, O, X, 'parse_duration benchmarks', 'Write parse_duration and the benchmark cases', S, M, 'normal code'),
    ('ta02231faf', 'executor', E, E, 1, E, O, X, 'Labeled backtest set', 'Create a JSON file with 40 labeled task objects', S, M, 'data creation with judgment'),
    ('tf15763b0d', 'executor', E, E, 1, E, O, X, 'Fix 1: Labeled backtest set', 'Fix the labeled set JSON', S, X, 'data creation; fix stage +1'),
    ('t0d86949f8', 'executor', E, E, 4, E, O, X, 'Wire result files into prompts', 'Wire result files into prompts and context', S, X, 'normal code over 4 files'),
    ('t4b115cb8e', 'executor', E, E, 4, E, O, X, 'Fix 1: Wire result files', 'Fix the result file wiring', S, X, 'normal code over 4 files; fix stage'),
]

# Selected by ID only (see module docstring); labeled from title/prompt by Rule I35 before
# model_fit was run on them. Prompt excerpts are the first ~200 stored characters (shortened).
HOLDOUT = [
    ('t0098668e2', 'reviewer', E, E, 0, E, O, X, 'Review: I34: triage old blocked runs', 'Independently review the delegated part "I34: triage old blocked runs". You are read-only: do not edit any file.', O, X, 'critical review of a judgment audit'),
    ('t1108532cc', 'executor', E, E, 2, E, O, X, 'Write deps2 backtest test reading A', 'Task A created docs/plan/backtest_deps2_a.txt. Create tests/test_backtest_deps2.py: a unittest that finds the repo root', H, L, 'one simple named test'),
    ('t14fe63415', 'reviewer', E, E, 0, E, O, E, 'Reviewer', 'Check that all requested work and acceptance criteria are satisfied.', O, X, 'final critical review'),
    ('t1aa7d0281', 'executor', E, E, 4, E, O, X, 'Result file store', 'Create notiontool/result_files.py (pure stdlib): ROOT = <app data dir>/results (look at how handoff_files.py chooses its folder', S, X, 'normal module over 4 files'),
    ('t1b77c940e', 'executor', E, E, 2, E, S, E, 'Part A: capture policy', 'Create the pure function capture_allowed(task_class, project_settings, already_taken) returning (allowed, reason).', S, M, 'normal pure function + tests'),
    ('t3424582e3', 'executor', E, E, 2, E, O, X, 'Write deps backtest source file', 'Create docs/plan/backtest_deps_a.txt with the single line: deps-a 7 Use a plain relative path.', H, L, 'one-line file'),
    ('t39b2d39bc', 'reviewer', E, E, 0, E, O, E, 'Reviewer', 'Check that all requested work and acceptance criteria are satisfied.', O, X, 'final critical review'),
    ('t404cb1650', 'integrator', E, E, 0, E, O, E, 'Integrator', 'Check your own completed work against every acceptance criterion and give the final answer, stating actual checks.', O, X, 'integration is hard work'),
    ('t4b69e3d6c', 'executor', E, E, 2, E, O, X, 'Review + backtest restart note and recovery', 'Independently review task restart-note against the goal. Backtest: build the real coworker prompt for a new chat', O, X, 'critical review + backtest'),
    ('t50b60efa7', 'integrator', E, E, 0, E, O, E, 'Integrator', 'Check your own completed work against every acceptance criterion and give the final answer, stating actual checks.', O, X, 'integration is hard work'),
    ('t6227b3af3', 'planner', E, E, 0, E, O, E, 'Planner', 'Choose whether to work alone or delegate, and choose each task model explicitly.', O, X, 'planning'),
    ('t62869cae4', 'reviewer', E, E, 0, E, O, E, 'Reviewer', 'Check that all requested work and acceptance criteria are satisfied. Please correct this problem in the response: Review needs an explicit checked verdict', O, X, 'final critical review'),
    ('t68376e438', 'executor', E, E, 2, E, S, E, 'Fix: Apply work_queue deps and requeue fix', 'In notiontool/work_queue.py make exactly 3 one-line changes. In graph_view, read depends_on and fall back to deps', H, M, 'three spelled-out one-line edits; fix stage +1'),
    ('t70cf0a77f', 'executor', E, E, 3, E, O, E, 'Classic UI: attention banner, task Accept/Abandon, last output age, context settings, cowo', 'Tool access: in the NotionTool connection, call mcp_list_tools for the schemas, then mcp_call with tool_name, arguments', S, M, 'UI building in 3 files'),
    ('t7525102fa', 'executor', E, E, 2, E, S, E, 'Screen capture docs', 'Read notiontool/screen_capture.py, the screen hints in notiontool/plain_protocol.py and tests/test_screen_prompt.py.', H, L, 'docs only'),
    ('t7bbb860af', 'executor', E, E, 2, E, O, X, 'Inject the fixed rules block into the Main switch', 'In notiontool/main_switch.py (LF file): add a function fixed_rules_block(root) that reads project-memory/NotionTool.md', S, M, 'normal code'),
    ('t832b86241', 'planner', E, E, 0, E, O, E, 'Planner', 'Choose whether to work alone or delegate, and choose each task model explicitly. DEEP mode: analyse or review first', O, X, 'planning'),
    ('t83514edab', 'executor', E, E, 0, E, H, E, 'Cleanup reply 5', 'Reply with exactly one line: CLEANUP-OK 5', H, L, 'echo check'),
    ('t8e5e4ced2', 'planner', E, E, 0, E, O, E, 'Planner', 'Choose whether to work alone or delegate, and choose each task model explicitly. DEEP mode: analyse or review first', O, X, 'planning'),
    ('t8f88bd2ce', 'executor', E, E, 3, E, S, E, 'Run controls and model hints in the UI (Phase 8 batch 2)', 'Read app.js, index.html and style.css, and see how the routes and /api/stream are used today. Add Pause, Cancel and Retry', S, M, 'UI building in 3 files'),
    ('t963228768', 'executor', E, E, 2, E, O, E, 'Refuse manual retry when blocked work already reached Notion (I5)', 'Tool access: in the NotionTool connection, call mcp_list_tools for the schemas, then mcp_call with tool_name', S, M, 'normal guard code'),
    ('t99cb170b0', 'executor', E, 'low', 1, E, O, M, 'Plan: Engine - plan-first team mode', 'Role: ENGINE PLANNER. Planning only, do not change code. Design a plan-first team mode for operator delegations', O, X, 'architecture planning'),
    ('ta28d8e75e', 'reviewer', E, E, 0, E, O, X, 'Review: Write review backtest 2 file and test', 'Independently review the delegated part "Write review backtest 2 file and test". You are read-only: do not edit any file.', S, L, 'review of trivial work is not critical'),
    ('ta2ccfe615', 'reviewer', E, E, 0, E, O, X, 'Review: Automatic coworker count per task', 'Independently review the delegated part "Automatic coworker count per task". You are read-only: do not edit any file.', O, X, 'critical review of real code'),
    ('ta42269b23', 'executor', 'code', E, 5, E, O, X, 'Fix 1: Backtest model and effort choice per task', 'Project: C:\\Users\\liket\\Downloads\\NotionTool-harness\\NotionTool (absolute paths). Rule I35: Haiku 5.5 only Haiku-class tasks (small, clear, mechanical', S, X, 'normal code + backtest over 5 files; fix stage'),
    ('tad0120303', 'executor', 'analysis', E, 4, E, O, X, 'Does Notion really run the chosen model and effort?', 'Project: C:\\Users\\liket\\Downloads\\NotionTool-harness\\NotionTool (absolute paths). Known earlier finding (project-memory, ~line 207)', O, X, 'forensic proof hunt'),
    ('tbba8b2366', 'reviewer', E, E, 0, E, O, E, 'Reviewer', 'Check that all requested work and acceptance criteria are satisfied.', O, X, 'final critical review'),
    ('tbbad0e6c5', 'executor', E, 'low', 1, E, O, M, 'Plan: Engine - plan-first team mode', 'Role: ENGINE PLANNER. Planning only, never change code. Write your file EARLY with nt_write_file and refine it', O, X, 'architecture planning'),
    ('tbbc2a8057', 'executor', E, E, 4, E, S, E, 'Part A: projects module', 'Read notiontool/store.py first and do not edit it. Also read engine.py, bridge.py, devtools.py and chat_context.py', S, X, 'normal module over 4 files'),
    ('tbedf6b922', 'planner', E, E, 0, E, O, E, 'Planner', 'Choose whether to work alone or delegate, and choose each task model explicitly. DEEP mode: analyse or review first', O, X, 'planning'),
    ('tbee2a605b', 'reviewer', E, E, 0, E, O, X, 'Review: I32: hard 23 h coworker age guard', 'Independently review the delegated part "Fix 1: I32: hard 23 h coworker age guard". You are read-only', O, X, 'critical review'),
    ('tc27fc3e8f', 'executor', E, E, 2, E, S, E, 'Fix: Write Part A snapshot module', 'Use a fresh assignment token, and first make sure the hold by Reviewer t6fb14cec on both files is released.', S, X, 'normal module; fix stage +1'),
    ('tc7d42ab8d', 'executor', E, 'low', 1, E, O, M, 'Plan: Designer - UI for missing features', 'Role: DESIGNER. Planning only, never change code. Write your file EARLY with nt_write_file and refine it', O, X, 'design planning'),
    ('tcc1ed21b7', 'executor', E, E, 2, E, O, E, 'engine.py: task_router.decide in add_task for routed runs; verifier risk from the task', 'Tool access: in the NotionTool connection, call mcp_list_tools for the schemas, then mcp_call with tool_name', S, M, 'normal wiring code'),
    ('td1106dd58', 'reviewer', E, E, 0, E, O, X, 'Review: Write backtest file (fix 2)', 'Independently review the delegated part "Write backtest file (fix 2)". You are read-only: do not edit any file.', S, L, 'review of trivial work is not critical'),
    ('td710c8260', 'planner', E, E, 0, E, O, E, 'Planner', 'Choose whether to work alone or delegate, and choose each task model explicitly. DEEP mode: analyse or review first', O, X, 'planning'),
    ('td7e737a69', 'reviewer', E, E, 0, E, O, X, 'Review: I32: hard 23 h coworker age guard', 'Independently review the delegated part "I32: hard 23 h coworker age guard". You are read-only', O, X, 'critical review'),
    ('te018e5f7a', 'executor', 'code', E, 5, E, O, X, 'Backend: create named chat (Main or private) and promote later', 'Project: C:\\Users\\liket\\Downloads\\NotionTool-harness\\NotionTool (absolute paths). Known gap (work/audit/UI_GAPS.md): app.js:422 calls the missing route', S, X, 'normal backend code over 5 files'),
    ('te0a155a5f', 'executor', E, E, 0, E, O, E, 'Screen capture', 'Call screen_capture with target "roblox" and describe what is visible in at most three sentences; if no image is visible, reply exactly: IMAGE NOT VISIBLE', H, L, 'mechanical tool call + short description'),
    ('te7c430cc1', 'executor', E, E, 0, E, O, E, 'Check worker_gc main key', 'Read-only task, do not change any file. Open notiontool/worker_gc.py around line 71, where the key main_worker_id is used.', S, M, 'read-only check that needs a judgment'),
]

KEYS = ('id', 'role', 'task_class', 'risk', 'files_count', 'stage_role', 'stored_model', 'stored_effort',
        'title', 'prompt', 'label_model', 'label_effort', 'label_reason')
CASES = [dict(zip(KEYS, row)) for row in RAW]
HOLD = [dict(zip(KEYS, row)) for row in HOLDOUT]


def as_task(case):
    return {k: case[k] for k in ('role', 'task_class', 'risk', 'files_count', 'stage_role', 'title', 'prompt')}


def pick_stored(case):
    return case['stored_model'], str(case['stored_effort'] or '').lower()


def pick_rule(case):
    rule = task_router.rule_for(case['task_class'], case['risk'])
    model = rule.get('model') if isinstance(rule, dict) else rule
    if isinstance(model, (tuple, list)):
        model = model[0]
    effort = choose_reasoning_effort('', case['task_class'], case['risk'],
                                     supported=('low', 'medium', 'high', 'xhigh', 'max'), default='medium')
    if isinstance(effort, (tuple, list)):
        effort = effort[0]
    return str(model or ''), str(effort or '').lower()


def merged(picker):
    """Explicitly labeled variant: xhigh and max counted as high (only for the report)."""
    def pick(case):
        m, e = picker(case)
        return m, 'high' if e in ('xhigh', 'max') else e
    return pick


def pick_fit(case):
    model, effort, _ = model_fit.choose(as_task(case))
    return model, effort


def _fmt(name, res):
    out = ['== %s: n=%d model %.1f%% effort %.1f%% both %.1f%% waste_units %d risk_units %d' % (
        name, res['n'], 100 * res['model_accuracy'], 100 * res['effort_accuracy'], 100 * res['both_accuracy'],
        res['waste_units'], res['risk_units'])]
    out.append('   label\\got  ' + ' '.join('%7s' % m for m in model_fit.MODELS + ('none',)))
    for want in model_fit.MODELS:
        out.append('   %-10s ' % want + ' '.join('%7d' % res['confusion'][want][g] for g in model_fit.MODELS + ('none',)))
    out.append('   efforts sent %s' % dict(sorted(res['effort_counts'].items())))
    out.append('   waste %s risk %s' % (res['waste'], res['risk']))
    out.append('   misses %s' % res['misses'])
    return '\n'.join(out)


def report(cases, label):
    lines = []
    for name, picker in (('before A: stored model/effort (what ran)', pick_stored),
                         ('before B: task_router.rule_for + choose_reasoning_effort', pick_rule),
                         ('after: model_fit.choose', pick_fit)):
        lines.append(_fmt('%s [%s]' % (name, label), model_fit.score(cases, picker)))
    for name, picker in (('before A', pick_stored), ('before B', pick_rule)):
        res = model_fit.score(cases, merged(picker))
        lines.append('   %s [%s] effort accuracy if xhigh/max were counted as high: %.1f%%' % (
            name, label, 100 * res['effort_accuracy']))
    for c in cases:
        m, e, why = model_fit.choose(as_task(c))
        if (m, e) != (c['label_model'], c['label_effort']):
            lines.append('   diff [%s] %s %s/%s want %s/%s :: %s' % (
                label, c['id'], m, e, c['label_model'], c['label_effort'], why))
    return '\n'.join(lines)


class LabeledSetTest(unittest.TestCase):
    def _check(self, cases):
        self.assertEqual(len({c['id'] for c in cases}), len(cases))
        for c in cases:
            self.assertIn(c['label_model'], model_fit.MODELS, c['id'])
            self.assertIn(c['label_effort'], model_fit.EFFORTS, c['id'])
            self.assertTrue(c['label_reason'].strip(), c['id'])

    def test_at_least_60_real_cases_with_labels_and_reasons(self):
        self.assertGreaterEqual(len(CASES), 60)
        self._check(CASES)

    def test_holdout_has_30_cases_not_in_raw(self):
        self.assertGreaterEqual(len(HOLD), 30)
        self._check(HOLD)
        self.assertEqual({c['id'] for c in HOLD} & {c['id'] for c in CASES}, set())


class BacktestTest(unittest.TestCase):
    def test_report_before_after(self):
        print('\n' + report(CASES, 'RAW'))
        stored = model_fit.score(CASES, pick_stored)
        self.assertGreater(model_fit.score(CASES, pick_fit)['model_accuracy'], stored['model_accuracy'])

    def test_holdout_report(self):
        print('\n' + report(HOLD, 'HOLDOUT'))
        fit = model_fit.score(HOLD, pick_fit)
        self.assertGreater(fit['model_accuracy'], model_fit.score(HOLD, pick_stored)['model_accuracy'])
        self.assertGreaterEqual(fit['model_accuracy'], 0.75)

    def test_model_accuracy_at_least_90_percent(self):
        self.assertGreaterEqual(model_fit.score(CASES, pick_fit)['model_accuracy'], 0.90)

    def test_zero_haiku_on_hard_tasks(self):
        bad = [c['id'] for c in CASES + HOLD if c['label_model'] == O and pick_fit(c)[0] == H]
        self.assertEqual(bad, [])

    def test_effort_accuracy_reasonable(self):
        self.assertGreaterEqual(model_fit.score(CASES, pick_fit)['effort_accuracy'], 0.75)

    def test_efforts_are_not_merged(self):
        cases = [dict(id='a', label_model=O, label_effort=X)]
        res = model_fit.score(cases, lambda c: (O, 'xhigh'))
        self.assertEqual(res['effort_accuracy'], 0.0)
        self.assertEqual(res['effort_counts'], {'xhigh': 1})


class ChooseTest(unittest.TestCase):
    def test_pure_and_deterministic(self):
        task = {'title': 'Fix 1: Wire result files', 'prompt': 'x', 'files': ['a.py', 'b.py'], 'risk': 'medium'}
        before = copy.deepcopy(task)
        first = model_fit.choose(task)
        self.assertEqual(task, before)
        self.assertEqual(first, model_fit.choose(task))
        self.assertEqual(len(first), 3)
        self.assertIn(first[0], model_fit.MODELS)
        self.assertIn(first[1], model_fit.EFFORTS)
        self.assertTrue(first[2])

    def test_empty_task(self):
        self.assertEqual(model_fit.choose({})[:2], (S, M))
        self.assertEqual(model_fit.choose(None)[:2], (S, M))

    def test_roles(self):
        self.assertEqual(model_fit.choose({'role': 'planner'})[:2], (O, X))
        self.assertEqual(model_fit.choose({'role': 'integrator', 'title': 'Reply with exactly OK'})[:2], (O, X))
        self.assertEqual(model_fit.choose({'role': 'reviewer', 'title': 'Review: Composer tools'})[:2], (O, X))
        self.assertEqual(model_fit.choose({'role': 'reviewer', 'title': 'Review: Rename foo to bar'})[:2], (S, L))
        self.assertEqual(model_fit.choose({'role': 'context_compactor'})[:2], (H, L))

    def test_design_stage_is_opus(self):
        self.assertEqual(model_fit.choose({'stage_role': 'design', 'title': 'Docs: x'})[:2], (O, X))

    def test_mechanical_is_haiku_low(self):
        self.assertEqual(model_fit.choose({'title': 'Rename helper', 'prompt': 'Rename foo to bar'})[:2], (H, L))
        self.assertEqual(model_fit.choose({'title': 'Correct typo in README'})[:2], (H, L))

    def test_hard_signals_are_opus_and_block_haiku(self):
        for title in ('Fix race in claim release', 'Deadlock between workers', 'Find the root cause of the hang',
                      'Architecture for the queue', 'Rename lock on concurrency path', 'Why does the queue stall'):
            self.assertEqual(model_fit.choose({'title': title})[:2], (O, X), title)

    def test_root_cause_words_in_prompt_only_when_cause_is_asked(self):
        self.assertEqual(model_fit.choose({'title': 'Retry helper', 'prompt': 'Retry when a call fails, max 3 times'})[0], S)
        self.assertEqual(model_fit.choose({'title': 'Help text', 'prompt': 'Explain to the user why a setting exists'})[0], S)
        self.assertEqual(model_fit.choose({'title': 'Queue stall', 'prompt': 'The queue stalls; find the cause'})[0], O)
        self.assertEqual(model_fit.choose({'title': 'Queue stall', 'prompt': 'Explain why the queue stalls'})[0], O)

    def test_claims_only_with_lock_lease_or_file(self):
        self.assertEqual(model_fit.choose({'title': 'Insurance claims form', 'prompt': 'Add a claims list page'})[0], S)
        self.assertEqual(model_fit.choose({'title': 'Release file claims on cancel'})[0], O)
        self.assertEqual(model_fit.choose({'title': 'Claims expire with the lease'})[0], O)

    def test_docs_and_brief_only_at_title_start(self):
        self.assertEqual(model_fit.choose({'title': 'Docs: update setup'})[:2], (H, L))
        self.assertEqual(model_fit.choose({'title': 'Write the release brief'})[:2], (H, L))
        self.assertNotEqual(model_fit.choose({'title': 'Parser module', 'prompt': 'Write a brief note and the parser'})[0], H)
        self.assertNotEqual(model_fit.choose({'title': 'Server route with docs'})[0], H)

    def test_rename_only_rename_title_with_at_most_2_files(self):
        self.assertEqual(model_fit.choose({'title': 'Rename helper', 'files': ['a.py', 'b.py']})[0], H)
        self.assertEqual(model_fit.choose({'title': 'Rename helper', 'files': ['a.py', 'b.py', 'c.py']})[0], S)
        self.assertEqual(model_fit.choose({'title': 'Parser', 'prompt': 'rename the variable and add the parser'})[0], S)

    def test_simple_test_one_named_test_only(self):
        self.assertEqual(model_fit.choose({'title': 'Write parser test'})[:2], (H, L))
        self.assertEqual(model_fit.choose({'title': 'Write integration test'})[0], S)
        self.assertEqual(model_fit.choose({'title': 'Write end-to-end test'})[0], S)
        self.assertEqual(model_fit.choose({'title': 'Write parser tests'})[0], S)

    def test_ui_build_is_sonnet_never_haiku(self):
        model, _, _ = model_fit.choose({'title': 'Docs page for the sidebar UI', 'files': ['web/app.js']})
        self.assertEqual(model, S)
        self.assertEqual(model_fit.choose({'title': 'Settings panel layout', 'files': ['web/style.css']})[:2], (S, M))

    def test_many_files_block_haiku_and_raise_effort(self):
        files = ['a.py', 'b.py', 'c.py', 'd.py']
        self.assertEqual(model_fit.choose({'title': 'Rename helper everywhere', 'files': files})[:2], (S, X))

    def test_fix_stage_detection(self):
        self.assertTrue(model_fit.is_fix_stage({'title': 'Fix: parser'}))
        self.assertTrue(model_fit.is_fix_stage({'title': 'Fix 2: parser'}))
        self.assertTrue(model_fit.is_fix_stage({'title': 'fix 12 : parser'}))
        self.assertTrue(model_fit.is_fix_stage({'title': 'Parser', 'fix_of': 't1'}))
        self.assertFalse(model_fit.is_fix_stage({'title': 'Fix the import in busy-Main test'}))
        self.assertFalse(model_fit.is_fix_stage({'title': 'Fix accepted_at type'}))
        self.assertFalse(model_fit.is_fix_stage({}))

    def test_fix_stage_raises_effort(self):
        self.assertEqual(model_fit.choose({'title': 'Write parser'})[1], M)
        self.assertEqual(model_fit.choose({'title': 'Fix 1: Write parser'})[1], X)
        self.assertEqual(model_fit.choose({'title': 'Write parser', 'fix_of': 't1'})[1], X)
        self.assertEqual(model_fit.choose({'title': 'Fix 2: Docs: refresh'})[:2], (H, M))
        self.assertEqual(model_fit.choose({'title': 'Fix the parser bug'})[1], M)

    def test_read_only_effort(self):
        audit = {'title': 'Audit old runs', 'prompt': 'Read-only: list old runs and say if each is superseded'}
        self.assertEqual(model_fit.choose(audit)[:2], (S, M))
        fields = {'title': 'Read-only audit: fields', 'prompt': 'List every field the UI reads'}
        self.assertEqual(model_fit.choose(fields)[:2], (S, M))
        check = {'title': 'Check files', 'prompt': 'Change nothing. Check sha256, CRLF and the test counts'}
        self.assertEqual(model_fit.choose(check)[:2], (S, L))

    def test_review_class_and_risk(self):
        self.assertEqual(model_fit.choose({'title': 'Check it', 'task_class': 'review'})[:2], (O, X))
        self.assertEqual(model_fit.choose({'title': 'Rename x', 'risk': 'high'})[:2], (S, X))

    def test_prompt_tail_is_ignored(self):
        prompt = 'Write the parser. ' + 'x ' * 400 + 'If a call fails because of a restart, find the cause.'
        self.assertEqual(model_fit.choose({'title': 'Parser', 'prompt': prompt})[0], S)

    def test_notion_effort_decision(self):
        self.assertEqual(model_fit.notion_effort('high'), 'high')
        self.assertEqual(model_fit.notion_effort('high', escalation=1), 'xhigh')
        self.assertEqual(model_fit.notion_effort('high', escalation=2), 'max')
        self.assertEqual(model_fit.notion_effort('high', escalation=5), 'max')
        self.assertEqual(model_fit.notion_effort('high', ('low', 'medium', 'high'), escalation=2), 'high')
        self.assertEqual(model_fit.notion_effort('low', ('medium', 'high')), 'medium')
        self.assertEqual(model_fit.notion_effort('medium', ('low', 'medium', 'high', 'xhigh', 'max')), 'medium')
        for case in CASES + HOLD:
            self.assertNotIn(model_fit.choose(as_task(case))[1], ('xhigh', 'max'))

    def test_escalation_raises_any_effort_one_step(self):
        self.assertEqual(model_fit.notion_effort('low', escalation=1), 'medium')
        self.assertEqual(model_fit.notion_effort('medium', escalation=1), 'high')
        self.assertEqual(model_fit.notion_effort('medium', escalation=2), 'xhigh')
        self.assertEqual(model_fit.notion_effort('low', escalation=0), 'low')
        self.assertEqual(model_fit.notion_effort('medium', escalation=-3), 'medium')

    def test_effort_ignores_difficulty_route(self):
        # A difficulty version that calls everything 'team' must not raise Sonnet effort.
        real = model_fit.difficulty.classify
        try:
            model_fit.difficulty.classify = lambda text, context=None: {
                'route': 'team', 'score': 0.6, 'reasons': ['stub']}
            self.assertEqual(model_fit.choose({})[:2], (S, M))
            self.assertEqual(model_fit.choose({'title': 'Write parser'})[:2], (S, M))
            self.assertEqual(model_fit.choose({'title': 'Settings panel layout', 'files': ['web/style.css']})[:2], (S, M))
            model_fit.difficulty.classify = lambda text, context=None: {
                'route': 'direct', 'score': model_fit.BROAD_SCORE, 'reasons': ['stub']}
            self.assertEqual(model_fit.choose({'title': 'Write parser'})[:2], (S, X))
        finally:
            model_fit.difficulty.classify = real

    def test_forensic_proof_is_opus(self):
        task = {'title': 'Does Notion really run the chosen model and effort?', 'files': ['a.py', 'b.py', 'c.py', 'd.py']}
        self.assertEqual(model_fit.choose(task)[:2], (O, X))
        self.assertEqual(model_fit.choose({'title': 'Forensic check of model reports'})[:2], (O, X))

    def test_review_of_backtest_fixture_is_mechanical(self):
        for title in ('Review: Write review backtest 2 file and test', 'Review: Write backtest file (fix 2)'):
            self.assertEqual(model_fit.choose({'role': 'reviewer', 'title': title})[:2], (S, L), title)
        self.assertEqual(model_fit.choose({'role': 'reviewer', 'title': 'Review: Composer tools'})[:2], (O, X))


class FileFormatTest(unittest.TestCase):
    def test_model_fit_is_lf_and_has_no_io(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'notiontool', 'model_fit.py')
        with open(path, 'rb') as fh:
            data = fh.read()
        self.assertNotIn(b'\r\n', data)
        text = data.decode('utf-8')
        for word in ('open(', 'sqlite3', 'requests', 'urllib', 'subprocess', 'os.environ'):
            self.assertNotIn(word, text)


DB = r'C:\Users\liket\Downloads\NotionTool-harness\NotionTool\data\notiontool.db'


def dump_holdout(db=DB, limit=40):
    """Print holdout candidates chosen by ID only (DB opened read-only)."""
    import json
    import sqlite3
    con = sqlite3.connect('file:%s?mode=ro' % db.replace('\\', '/'), uri=True)
    raw_ids = {r[0] for r in RAW}
    rows = sorted(con.execute("SELECT id, doc FROM objects WHERE kind='tasks'"))
    out = 0
    for tid, doc in rows:
        tail = tid[-2:]
        if not all(ch in '0123456789abcdef' for ch in tail) or int(tail, 16) % 16 != 5:
            continue
        if tid[:10] in raw_ids:
            continue
        t = json.loads(doc)
        files = t.get('files') or t.get('assigned_files') or []
        print(' | '.join(str(v) for v in (
            tid[:10], t.get('role') or '', t.get('task_class') or '', t.get('risk') or '', len(files),
            t.get('stage_role') or '', t.get('model') or '', t.get('effort') or t.get('reasoning_effort') or '',
            'fix_of' if t.get('fix_of') else '', 'review_of' if t.get('review_of') else '',
            ' '.join(str(t.get('title') or '').split())[:90],
            ' '.join(str(t.get('prompt') or '').split())[:200])))
        out += 1
        if out >= limit:
            break
    print('candidates=%d of tasks=%d' % (out, len(rows)))


if __name__ == '__main__':
    if '--dump-holdout' in sys.argv:
        dump_holdout()
    else:
        unittest.main()
