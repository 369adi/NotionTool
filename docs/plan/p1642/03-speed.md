# Speed plan (p1642): performance engineer

Planning only. No code was changed. Data: data/notiontool.db (read-only), events of the last 3 hours
(up to 2026-10-19 16:41).

## 1. What the data shows

| Measure | Value | Source |
|---|---|---|
| Remote turn time (remote_done.seconds) | median 375 s, p90 582 s, max 912 s (n=46) | remote_done |
| Time to first output | median 235 s, p90 480 s, max 649 s (n=23) | first_output |
| Stream end to task finished | median 24 s, p90 800 s, max 1165 s (n=39) | remote_done -> task_finished |
| Verify rounds per task | median 4, p90 19, max 32 (337 verify_started for 43 tasks) | verify_started |
| Turn outcomes | 39 stream_end, 4 unknown, 3 unconfirmed (15 % not clean) | remote_done.outcome |
| Retries refused (side effects) | 15 | retry_refused_side_effects |
| Main had to resolve | 12 awaiting_main, 6 task_resolved_by_main | events |
| Stalls | 17, idle median 140 s, p90 170 s, on 6 workers | task_stalled |
| Parallel limit | 38 changes; 5 drops to 5 on transient_error, back up by 1 per step | request_coordination_adjusted |
| Task starts per 10 min | mostly 1-3, peak 10 | task_started |
| Tab wake-ups | 0 edge_tab_woken events in the whole DB | events |

## 2. Causes

1. The run waits on unclear finishes, not on slow models. 7 of 46 turns (15 %) end unknown or
   unconfirmed. Because they used tools, a resend is refused (correct, I5), so the task waits for the
   Main (12 awaiting_main). One such planner task blocks every task that depends on it, so the whole
   run stops. This explains the 4 blocked planner tasks.
2. Slow finish detection. Half the tasks finish 24 s after the stream ends, but the slowest 10 % take
   13-19 min. Some tasks run 19-32 verify rounds: the verify loop keeps polling the transcript instead
   of deciding once the turn is clearly over.
   In code: engine.py line ~1064 sets reconcile_at to 5-60 s between checks, but to 300 s once a check
   is overdue, so each overdue round adds 5 min; this matches the 13-19 min tail.
3. Sleeping background tabs. Edge throttles hidden tabs (timer throttling after 5 min, sleeping tabs
   later), so a coworker tab stops streaming for 2-3 min until it is woken. The 17 stalls of about
   140 s match that. browser.py already starts Edge with NO_THROTTLE_FLAGS (no timer throttling, no
   renderer backgrounding, no occlusion, no IntensiveWakeUpThrottling), so the flags alone do not stop
   Edge sleeping tabs or page freezing. edge_transport.py (line ~64) emits edge_tab_woken through
   self.store.event, but the DB has 0 such rows: the store is missing on the transport or the error is
   swallowed, so the 10 wake-ups are not measurable yet.
4. Parallelism falls back too hard. Each transient error cuts the limit from 10 to 5, and it takes 5
   successful windows to get back to 10. This happened 5 times in 3 hours.
5. Too few ready tasks. Usually only 1-3 tasks start per 10 minutes, so most of the 10 coworkers wait.
   Plans have few, large, chained tasks.
6. First output is slow (median 4 min). This is mostly Notion model time; prompts are small (about
   4.7k characters), so prompt size is not the cause.

## 3. Ranked fixes

| Rank | Fix | Where (to confirm in code) | Expected gain |
|---|---|---|---|
| 1 | Auto-confirm a clearly finished turn: when the session is idle, the newest entry is an assistant reply after the last tool result, no tool call is open and no confirmation is pending, mark the attempt done. Only confirm, never resend (I5). Otherwise keep it uncertain as today. | engine.py (outcome unknown/unconfirmed), notion.py transcript reading, reuse turn_watch.turn_idle | Most of the 7 unclear turns and the blocked runs disappear; a blocked run currently loses 10-20 min each |
| 2 | Faster finish detection: after stream end, read the transcript once; if not clear, poll at 5 s, 15 s, 30 s, then 60 s, and cap verify at 6 rounds before asking the Main. | engine.py reconcile backoff (line ~1064: 300 s when overdue, cap it at 60 s), desktop_state_cache.py | p90 stream-end-to-finished from 800 s to under 120 s |
| 3 | Keep background tabs awake: the throttling flags already exist (browser.py NO_THROTTLE_FLAGS). Add the missing parts: keep notion.so out of sleeping tabs (Edge policy SleepingTabsBlockedForUrls, or --disable-features=SleepingTabs if supported), keep coworker pages active via CDP (Page.setWebLifecycleState active, Emulation.setFocusEmulationEnabled true), and fix the edge_tab_woken logging (pass the store to the transport and log failures). | browser.py, edge_transport.py (~line 64), stall_watch.py | Most of the 17 stalls (about 40 min of coworker time in 3 h); wake-ups become measurable |
| 4 | Wake on stall: when stall_watch sees 60 s without output on a streaming tab, wake that tab (CDP bring-to-front or lifecycle active) and log it. Report only; never resend. | stall_watch.py | Remaining stalls drop from about 140 s to about 60 s |
| 5 | Finer task splitting: the planner splits work into file-disjoint tasks of 2-5 min each, at least as many as free coworkers, and adds dependencies only for real file or result needs. | engine.py planner prompt; plan-mode (see 02-plan-mode.md) | More tasks in parallel; about 1.5x to 2x faster runs with 10 coworkers |
| 6 | Gentler parallel limit: on a transient error drop by 2 (not to 5), and raise by 2 per successful window. | request coordination (network budget) | About 20 % more parallel time after errors |
| 7 | Measure first output: add queue time (task_ready to remote_sent) and model time (remote_sent to first_output) to the usage view, to see whether waiting is in NotionTool or in Notion. | engine.py events, usage view | No direct gain; shows where the next minutes are |

## 4. Expected result

With fixes 1-3, a run should no longer block on unclear finishes, and the long tail should
shrink: p90 task time from about 11 min to about 7-8 min. The median turn (7.5 min) is mostly Notion
model time; fix 5 gives the larger win by keeping all 10 coworkers busy.

## 5. Checks for each fix

- Fix 1: a fake transcript with a final assistant reply gives done; one with an open tool call stays uncertain; nothing is sent again.
- Fix 2: fake clock test: a clear finish is detected in 1 read; verify rounds stay at 6 or fewer.
- Fix 3 and 4: edge_tab_woken events appear; task_stalled count per 3 h drops.
- Fix 5: task starts per 10 min reach the number of free coworkers in a test plan.
- Fix 6: unit test of the limit steps.