# NotionTool — current handoff, 2026-10-15

## Update 2026-10-15 23:50 - project cleanup
- Archived to `../NotionTool-cleanup-archive-20261015-234622.zip` (verified), then removed: all `work/` backups, one-off patch/inspect scripts and logs; historical reports (chat-request, short-burst, Welcome accounting, single-account validation, model-routing black box, CURRENT_STATUS, harness-multi-main.diff); the old upload package files (MANIFEST.json, UPLOAD_README.md); RELEASE-NOTES.txt; the schema-1 DB backup. Code originals are in the archive under `pre-cleanup-code/`. References to these files further below are historical.
- Deleted without archive (regenerable or foreign): `build/`, `__pycache__/` and the unused browser profile of a removed account (519 MB). The active account profile, sessions and database are unchanged.
- Roblox Studio MCP removed (not part of the Notion harness). Its stdio client now lives in `notiontool/rea.py`, so the optional REA MCP toggle works as before; `tests/test_roblox.py` was replaced by `tests/test_rea.py`.
- `work/` keeps only restart-notiontool.ps1, local-build.ps1, exe-smoke.ps1, peek_run.py, peek_job.py, peek_context.py, restart.log and build-history.log.
- Only Welcome chats are used (1 Main + 5 Coworkers); unchanged.
- Verified after the cleanup: 192 unit tests passed; restart OK; EXE rebuilt 23:59 (16802879 bytes, SHA256 9498F69D9333005E1FA36A03984E8D921D475738EDA030F563E5EEC9B7268DAE); EXE smoke test passed.

## Update 2026-10-15 23:30 (supersedes conflicting statements below)

- User instructions 22:17-22:20: the old NotionTool (Desktop exe, watchdog, guard task) was removed from autostart and stopped (backup in the cleanup archive under `work/backup-autostart-20261015-222448/`, files kept); the model-routing analysis was dropped; the optional items are done; improvements are allowed and earlier handoff constraints no longer bind, except that only Welcome chats are used (1 Main + 5 Coworkers).
- Maintenance: Tailscale MCP and account connections are kept by one job with a stable id (`j-bridge-maintenance`) that records an event only when its result changes (jobs 280 -> 16, maintenance records 265 -> 1). The Funnel route is applied only when it is missing or changed; before, it was re-applied about every 60 s.
- Live: run r3cfe6eb5137a41fe848ef10365dfb545 (each Coworker answered only its own part; Coworker 1 wrote its checkpoint in the same chat; a reviewer turn error was resent automatically on another Coworker) and run rf80dc076de9a43659cb9dd9453e65a2e (memory dedupe: each earlier answer once in the run context).
- Event logs keep numeric token counts readable; credentials and login codes stay redacted.
- Tests: 191 passed (23:18). Helpers in `work/`: `restart-notiontool.ps1`, `local-build.ps1`, `exe-smoke.ps1`, `peek_run.py`, `peek_context.py`.
- EXE: `work/local-build.ps1` -> `dist/NotionTool.exe`, 16,805,486 bytes, SHA256 55D9D955C718CCE9ABF9A61BC27427764086D4900207CDD293697AA4653FA8E4; start and stop checked with `work/exe-smoke.ps1`. The frozen EXE uses `<exe dir>/data` as data dir (`--data-dir` overrides) and the exe dir as tools root. Not pushed, not copied to the Desktop.

## Update 2026-10-15 22:00 (supersedes conflicting statements below)

- Active account: one signed-in test account; no other account is scheduled. Paid account, no trial activation; mail, relay, invitation and trial automation were removed. Only Welcome chats are used: 1 Main + 5 Coworkers.
- Context cleanup and Compress run in the same Welcome chat (checkpoint, `context_replace_mode=soft` by default). Continue in New Chat was removed. Details: [CONTEXT_AND_RECOVERY.md](CONTEXT_AND_RECOVERY.md).
- Model: requested Opus 5.5 is still executed as almond-croissant-low on this account. This is handled as a non-blocking `model_fallback` with a hint; genuine Opus execution is still NOT demonstrated.
- Live 2026-10-15: parallel cowork run with 6 tasks done, automatic Coworker cleanup in the same chat, live `temporarily-unavailable` turn errors recovered automatically, no new chats.
- Tests: 176 passed (21:45).
- EXE: built locally with `python -m PyInstaller --noconfirm NotionTool.spec` -> `dist/NotionTool.exe`, 16,804,816 bytes, SHA256 6B60D0ECAAAD4E1C85FFEA2D41C65D917EFF808DA7997DF28B0351C7E7FCDD3C. Not pushed, not copied to the Desktop, not started (static check only: bundled web UI, welcome.json, template.json, edgechromium). Distribution must still exclude data/sessions and credentials.

## Current user requirements — supersede historical multi-slot plans

- Use exactly ONE active Notion account/slot. Do not distribute work across multiple accounts. Multiple chats/workers, if needed, belong to that single account and share its limits.
- Trial activation is NOT required for account readiness. Do not automatically activate a trial, require a Business trial, or buy/change a plan. Actual AI and model eligibility still have to be checked independently.
- The former eight-slot/40-worker rollout, automatic eight-account signup, and eight-alias provisioning are no longer product targets. Do not resume those tests or create replacement accounts for them.
- Existing accounts, sessions, chats, evidence, and subscriptions have not been deleted or changed. The retained active account has not yet been selected. A one-slot architecture does not imply deleting existing data or cancelling an existing trial.
- Windows desktop EXE remains the product. Chat requests use the API even when the signed-in Notion browser is opened visibly. Keep Harness on/off, MCP-server management, Tailscale bridge, optional browser/PC access and the optional REA MCP (Settings toggle, off by default) as existing feature scope, subject to actual provider eligibility.
- Preserve the requested model policy: architect/main explicitly requests Opus 5.5; workers are selected explicitly per task, never Notion Auto. Requested model selection is not proof of execution. The user previously rejected a finished EXE unless real Opus execution is demonstrated; this has not been retracted.

## Implementation status and next work

This handoff records a NEW requirement, not a completed migration. Current source/data still contain multi-slot bootstrap, trial-readiness and eight-slot assumptions. No runtime settings, accounts, billing, active process, or EXE were changed while updating this document.

Next implementation: replace multi-slot capacity/bootstrap assumptions with one active account; make trial activation optional and remove trial as a mandatory readiness gate; adapt UI, onboarding and relay defaults accordingly. Retain actual AI-access checks, model-execution verification, central request queue, provider cooldowns and non-duplicating recovery. Choose the active existing account before disabling other account scheduling. Do not reactivate administratively blocked accounts or silently fall back to a wrong model. Verify the one-account path before building a replacement EXE.

## Verified model and AI-access limitations

The current trial-account tests did NOT demonstrate genuine Opus 5.5 execution. Native inference metadata reported almond-croissant-low (Sonnet 4.6 Low) despite explicit requested Opus/other IDs. The model guard rejects mismatched/unverified execution; do not describe configured IDs or initial Welcome greetings as proof of Opus for subsequent requests. User has said no eligible paid/manual-Opus account is currently available. Repeating that question or seeking unchecked routes is not a solution.

Fresh read-only checks: slots 1 and 3 have enable_ai_feature=false, disable_ai_feature=true and notion_admin_disabled_ai_feature=true; both also have an enforcement token (only its presence was recorded). They remain locally ai_disabled. Slots 2/4/5/6/8 were observed enabled; slot 7's availability check timed out. These are historical checks, not a choice of the retained account.

The administrative-disable predicate in captured client code has no paid-plan/trial exemption. This does not reveal the backend trigger or establish identical thresholds for paid and trial accounts. Do not change administrative flags.

## Latest causal investigation: chat bursts and administrative restriction

Read docs/chat-request-cause-report.md and docs/chat-request-burst-audit.json. Read-only inspection covered 18 managed chats, 986 referenced message records and 197 persisted user steps on slots 1/3/4. Coverage is complete for those managed threads, not for failed/unpersisted submissions, unmanaged chats or all HTTP traffic.

Observed maxima for persisted user messages per minute: slot 1 = 10; slot 3 = 12; still-enabled slot 4 = 7. All three created six managed chats in under a minute. No error explicitly attributes the restriction to high short-term usage, and no numeric AI short-burst limit is established.

IMPORTANT temporal correction: native ai-not-enabled HTTP 403 errors already occurred on 2026-10-14 at 16:43:00 Berlin (slot 1) and 18:49:43 (slot 3). The later 40-worker assignment burst at 19:52:05 cannot have initiated that already observed disabled state. Earlier bursts remain a hypothesis, not a proven cause. Exact administrative transition times and server decision reasons are absent.

Both disabled slots currently report within_limit, zero six-hour/monthly percentage usage and 300 premium credits. Present allowance exhaustion does not explain the current restriction. Public API connection request limits are not established thresholds for private AI endpoints. The local four-starts/second NetworkBudget is our own pacing configuration, not a provider-safe guarantee. No new load test was performed. See docs/ai-disabled-usage-crosscheck.json and docs/short-burst-usage-report.md.

## Historical bounded Welcome-accounting findings

Read docs/welcome-proof-round2-report.md. On the same >26-hour AI-enabled Slot4 Business-trial workspace, the old >24h Welcome produced a measured usage increase; a second newly created Welcome with identical prompt and reported model produced no additional observed usage over 331 seconds. Earlier first new-Welcome isolation also showed no delta over 322 seconds. This does not prove free-plan/nontrial behavior, a full 24-hour lifetime, unlimited renewal, or bounty acceptance.

The public M5 client predicate uses thread age, onboarding key and experiment assignment, without reading trial status/account age; other onboarding paths use workspace age. Eleven local mocked client cases pass. Client predicates do not prove backend eligibility. No subscription/admin/timestamp modifications were used in these tests.

Do not turn these observations into automatic quota-reset behavior. Do not repeat reserved submissions or create duplicate test chats. Private one-shot markers remain in data/welcome_proof_round2.json, data/welcome_usage_probe_slot4.json, data/old_welcome_usage_control_slot4.json, data/welcome_usage_probe.json and data/inference_control_probe.json. The round2 scheduler hold was released. Nothing submitted externally and no follow-up automation scheduled.

## Source, tests and release state

Latest source fixes: date-correct native IDs; malformed/empty stream detection; native confirmation of managed chat creation; AI-disabled readiness/dispatch guards. 206 tests passed in the prior source-fix run (docs/tests-welcome-fix.log). Those checks predate the new single-slot requirement and do not validate its implementation. Diagnostic scripts/reports added afterwards do not constitute a completed migration.

No new EXE was built for the latest source fixes or the single-slot change. Older model-guard binary SHA256: 767B30E82ECD499A51238D122648BCA0833D0F3A2EAD19B7928F8167E23E9BAC. Do not ship it as a completed Opus or single-slot release. Distribution must exclude data/sessions and credentials.

## Historical references

The former handoff is preserved in docs/HANDOFF-pre-single-slot-history.md; older routing history is in docs/HANDOFF-routing-history.md. Treat their eight-slot requirements, mandatory-trial assumptions, older model-success claims and release hashes as historical, not current instructions. tools_update_handoff.py is an obsolete snapshot generator containing superseded claims; do not run it to overwrite this handoff.
