# Screen capture

How NotionTool workers see desktop apps. Sources: `notiontool/screen_capture.py`, the screen hint in `notiontool/plain_protocol.py`, the tool registration in `notiontool/bridge.py`, and `tests/test_screen_prompt.py`. Line numbers were read on 16.10.2026. `bridge.py` is being edited by the operator, so its line numbers may have shifted.

## Purpose

- Workers on visual tasks (Roblox Studio, Blender, UI, renders) can look at the current screen state before and after a change (`plain_protocol.py:128-132`).
- Two MCP tools: `screen_windows` lists top-level windows (at most `LIST_LIMIT` = 80, `screen_capture.py:37`, `:390`), and `screen_capture` returns one image (`screen_capture.py:52-53`). Both are registered in the bridge (`bridge.py:54`, `:113`, `:120-121`). Screen access is on by default (`bridge.py:107-109`).

## How a worker captures

### When a worker is told about it

- The prompt adds the screen hint only when the tools are available and the task or run looks visual (`plain_protocol.py:145-146`).
- "Visual" is a keyword match on roblox, blender, screen(s), screenshot(s), capture, viewport, gui, ui, layout, render(ing), visual(ly), bildschirm, fenster or aussehen (`plain_protocol.py:129`).
- Hint text: no target = the whole screen; target "roblox" or "blender" = only that window; no token or code needed (`plain_protocol.py:130-132`).
- Tests: visual executor and reviewer tasks get the token and the hint (`tests/test_screen_prompt.py:10-17`). Non-visual tasks, or tasks without tools, get no token (`tests/test_screen_prompt.py:19-23`).

### Tool and scope

- Called through `mcp_call` with `tool_name` `screen_capture`. No assignment token or access code is needed (`screen_capture.py:52`; `bridge.py:218-220` serves screen tools without a token, with role `chat`; group label "not needed" at `bridge.py:459`).
- Full screen by default: without `target` or `hwnd`, `pick` returns `None` and the whole screen is captured (`screen_capture.py:301`, `:309`, `:404-407`) with `BitBlt SRCCOPY|CAPTUREBLT` (`screen_capture.py:290`).
- One window only when a target is given: `target` (alias such as `roblox`, `blender`, `screen_capture.py:27-31`) or `hwnd` captures one top-level window with `PrintWindow` and `PW_RENDERFULLCONTENT` (`screen_capture.py:4`, `:257`). If that image is blank, it falls back to a screen copy of the window rect (`screen_capture.py:260-263`). Minimized windows are handled at `screen_capture.py:239-243`. An unknown target is an error, not a silent full-screen capture (`screen_capture.py:322-325`).
- An optional region crop is applied after the grab (`screen_capture.py:409-411`).
- Screen calls run outside the bridge lock, so they do not block other tool calls (`bridge.py:242-243`, `:324-326`).
- The image is sent as an MCP image item, not as text (`screen_capture.py:438`; `bridge.py:473-476`).

### Limits

- Longest side defaults to `DEFAULT_SIDE` = 1600 and is at most `MAX_SIDE` = 3840 (`screen_capture.py:32-33`, `:43`, `:356`).
- At most `MAX_PER_TASK` = 500 captures per task, as a runaway-loop guard (`screen_capture.py:35`, `:399-400`).
- Dedupe: a per-target difference hash is kept in memory (`screen_capture.py:10`, `:413`, `:420`). At most `SAME_DISTANCE` = 3 differing bits counts as unchanged (`screen_capture.py:36`, `:417`). With `only_if_changed`, an unchanged image is left out (`screen_capture.py:45`, `:430-434`). The hash cache keeps at most 256 entries (`screen_capture.py:421`).
- Measured timings, user-reported on 16.10.2026: full screen 3840x1246 in 219 ms, Roblox Studio window 156 ms, Blender window 125 ms. NOT VERIFIED in this run. Each capture reports its own `ms` (`screen_capture.py:435`).

## Privacy limits

- Images are never written to disk. A capture exists only in memory until its image is read, then it is dropped (`screen_capture.py:8-9`, `:53`). Results carry `stored=False` (`screen_capture.py:428`).
- The SHA-256 of the pixels is used only for identity; the pixels are not kept (`screen_capture.py:418`).
- Captures stored on disk by older versions (PNG files kept for 7 days) are deleted on first use (`screen_capture.py:12`, `:455-462`).
- The `capture_taken` event logs time, reason, task, role, hashes and image size, never pixels (`screen_capture.py:11`, `:446-451`).
- A full-screen capture shows everything visible on the screen, including other apps. Only target captures limit this to one window (`screen_capture.py:309`, `:404-407`). There is no masking of other windows. NOT VERIFIED beyond the absence of masking code in `screen_capture.py`.

## Evidence levels

The code defines no evidence levels. The levels below are the documented convention (NOT VERIFIED as enforced by code):

1. Capture evidence: a `capture_taken` event for the task, with its `sha256` and `phash` (`screen_capture.py:428`, `:446-451`), taken after the change. This is the strongest evidence for visual results.
2. Tool evidence: test output or file:line from the coworker tools. Used for code and docs.
3. Claim only: the worker's own description without 1 or 2. Must be marked NOT VERIFIED.

Open point: verifier handoff. The bridge verifier (`bridge.py:222-230`) does not receive the capture image, and images are dropped after reading, so a reviewer must take its own capture. Passing captures to the verifier is NOT VERIFIED and not implemented.

## Capture policy per task_class

Since Phase 7, worker captures are tied to `task_class` by the pure function `capture_allowed(task_class, project_settings, already_taken)` in `notiontool/capture_policy.py:55-75`. It reads only its arguments and returns `(allowed, reason)`, where reason is a short snake_case code. The checks run in this order:

- Classes: only the visual classes `ui`, `visual`, `game`, `design` and `screenshot` (`capture_policy.py:5`), matched case-insensitively. A project can replace the list with the setting `capture_classes` (`:30-37`). Missing or other classes, such as `code`, `docs` or `backtest`, are refused with `class_not_visual` (`:62-63`). Separately, the screen hint in the prompt is also tied to the class: `code` and `docs` never get it, `visual` and `ui` always do, and other classes keep the keyword match (`plain_protocol.py:135-148`).
- Opt-in and limit: the project setting `capture_enabled` must be `True`; it is off by default, so a missing setting refuses with `not_enabled` (`capture_policy.py:65-67`). An explicit screenshot request (class `screenshot`, or the setting `explicit_screenshot` set to `True`) skips only this opt-in and returns `explicit_request` (`:64`, `:75`); it does not allow a non-visual class. At most 4 captures per task (`DEFAULT_MAX_CAPTURES`, `:7`), changeable with the setting `max_captures` (`:40-44`); after that, `limit_reached` (`:68-70`). This limit is separate from the runaway guard `MAX_PER_TASK` = 500 in `screen_capture.py:35`.
- Before/after pairs and refusal codes: an even count of captures already taken means the next one is a before capture. A before capture that would be the last one allowed is refused with `pair_incomplete`, so every before capture can get its after capture (`capture_policy.py:71-74`). A negative or non-integer count is refused with `invalid_count` (`:47-52`, `:59-60`). All codes (`allowed`, `explicit_request`, `class_not_visual`, `not_enabled`, `limit_reached`, `pair_incomplete`, `invalid_count`) have an English text in `REASON_TEXTS` (`:9-17`), returned by `reason_text(code)` (`:78-79`).
- Gate in plain_protocol: `capture_gate(task, project_settings=None, already_taken=0, operator=False)` (`plain_protocol.py:172-187`) is the shared gate. It returns `None` when the capture may go ahead. Otherwise it returns `dict(refused=True, text=..., reason={reason, task_class, already_taken})`, with the English text from `reason_text` plus "No capture was taken." (`:163-169`). If `capture_policy` cannot be imported, workers are refused with `capture_policy_unavailable` (`:154-160`, `:179-180`). The bridge does not use `guarded_capture` (`:190-195`), because it needs to know whether a capture actually happened.
- Worker wiring in the bridge: a worker's `screen_capture` call comes in through `mcp_call` (`bridge.py:600-602`) to `Bridge.call` (`:245`), which gets the task and run from the assignment token under the engine lock (`:259-260`). Outside the lock, `:362-364` sends it to `worker_capture` (`:392-411`). That calls `plain_protocol.capture_gate(task, settings, already_taken)` at `:400` with the task (and so its `task_class`), the project settings from `capture_settings(run)` (`:374-380`: the app settings, overridden by `run['project_settings']` when that is a dict) and the number of captures this task has already taken (`captures_taken`, `:386-390`). Only `screen_capture` is gated; worker `screen_windows` keeps its ungated call at `:365-367`.
- Per-task counter: kept in memory per `task_id`, behind a lock (`bridge.py:382-384`). It goes up only after the capture returned without an error (`:407-410`), so refused and failed captures are not counted. It resets when the app restarts, and two parallel captures of one task could both pass the gate before either is counted.
- Refusal and the `capture_refused` event: when the gate refuses, no capture is taken. The bridge emits the event `capture_refused` for the run with the fields `task_id`, `task_class`, `reason` (the policy code), `already_taken` and `message` (the refusal text) (`bridge.py:404-405`), and returns `{refused: True, reason, text}` to the worker (`:406`). Example text: "Screen capture refused: screen capture is not enabled for this project. No capture was taken."
- Not gated: explicit operator captures (`nt_screen_capture`, `bridge.py:497`, role `operator`) and Main/chat captures without an assignment token (`bridge.py:246-248`, role `chat`) never reach `worker_capture`. They get no gate, no counter and no event, as before.
- Open point: `engine.py` does not set `task_class` on tasks yet (0 matches), so until it does, live worker captures are refused with `class_not_visual`. A project must also set `capture_enabled` to `True`.
- Verifier in capture_verify: `verify(before, after)` (`notiontool/capture_verify.py:70-80`) compares two in-memory PIL images and returns `{changed, distance, diff_ratio, summary}`. `distance` is the Hamming distance of two 64-bit difference hashes (`:33-47`). `diff_ratio` is the share of pixels whose largest channel difference is above 16, compared at a longest side of at most 256 pixels (`:9-10`, `:56-67`). `changed` is `True` when the distance is at least 6 or the ratio is at least 1 % (`:11-12`, `:76`). Nothing is read from or written to disk. It is the handover for Phase 6 and is not called anywhere yet. Its limits were calibrated only on synthetic test images, NOT VERIFIED on real screenshots.
- LOKAL GEPRUEFT 16.10.2026: `tests/test_capture_bridge_wiring.py` (9 tests, mocks only) covers the allowed path, the refused path with `capture_refused`, a non-visual class, the operator and chat paths without the gate, separate counters per task, `limit_reached` after counted captures, a failed capture that is not counted, and `screen_windows` without the gate. Together with `tests.test_capture_policy`, `tests.test_capture_policy_wiring`, `tests.test_capture_verify` and `tests.test_bridge_save_to` all tests pass. Not checked live: no restart and no real capture.

Still a documented rule only: backtests capture only on the hg test account. No code checks the account (NOT VERIFIED as enforced). For visual tasks, evidence level 1 still means a before and an after capture, which matches the pair rule above.
