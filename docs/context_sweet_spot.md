# Coworker context switching: measured sweet spot

Status: recounted on 2026-10-22 ~01:30 (Europe/Berlin) from the live DB, read-only.
Tags: **[M]** = measured from data, **[E]** = estimate / model / judgement, **[C]** = read from code.

Sources (every number below names one of these):

- **A** = `python tools/context_latency_report.py --days 14` (exit 0, runtime 0.7 s)
- **B** = `python tools/context_latency_report.py --days 14 --cap-entity 200000` (exit 0, runtime 0.7 s; each transcript entity counted at most 200k tokens)
- **Q** = `python work/ro_query.py '<SELECT>'` (read-only helper, mode=ro + query_only); the queries are quoted where used
- **C** = `notiontool/chat_context.py` (usage() lines 54-74, rotate() 221-305, fresh_welcome() 307-338)

## 1. Live settings and the counting rule

- [M] (Q `SELECT id, json_extract(doc,'$.coworker_rotate_mode') ... FROM objects WHERE kind='settings'`, and A line 2): settings `app` has `coworker_rotate_mode = new_welcome`, `context_token_limit = 5000000`, `context_turn_limit = 600`, `coworker_fresh_context = threshold`.
- [C] Code defaults: 1,200,000 tokens / 600 turns.
- [C] usage() counts `tokens = max(local, observed, native + since_archive)`. `local` is the sum of `estimate_tokens(prompt + reply)` of the tasks since the reset. `observed` is the transcript size since `context_base_seq` when that is above 0, the whole transcript when there is no reset time at all, and **0 otherwise**.
- [C] fresh_welcome() sets `context_base_seq = 0` and `context_base_at = now` for the new chat. **So after a new-chat switch, usage() counts only prompt + reply, not the real chat size.**
- [M] (A) the median prompt + reply per task is 3,612 tokens. The median real transcript growth per task is 155,828 tokens. So on the counted scale a switched chat grows about 43x slower than it really does. Any `context_token_limit` set from the real chat size therefore **needs a code change**: usage() should count the new thread's transcript from sequence 0 after a new_welcome switch. Until that change lands, the limit only acts on chats that were never switched.
- The report shows both counts:
  - `ctx` / `gapR`: the production rule.
  - `nobs` / `gapP`: the physical size, counted as before.
- [M] (A) In the current 14-day data both counts put every task and every gap into the same buckets. The `n` and `nobs` columns are identical, and `gapR` equals `gapP`. The reason: no switched Coworker chat with a transcript has grown past 100k yet.

## 2. Data volume (why parts are thin)

- [M] (A) There are 995 tasks; 938 are Coworker tasks in the 14-day window.
- [M] (A) There are 16 transcripts (`thread_context`). 11 belong to Coworkers and all of them have gaps. 5 non-Coworker transcripts were skipped (Main and the like).
- [M] (Q, entity min `created_at` per thread_context) The step data starts on 2026-10-20 23:53 UTC. So **per-step gaps cover less than 2 days**, and only the newest part of each chat. The largest Coworker transcript has 125 sequence numbers (thread …804c).
- [M] (A) Coworker tasks per chat: n = 105 chats, median 4, p90 25, max 33 tasks.
- [M] (A) The `events` table only reaches back to 2026-10-21 00:32. It has 0 `context_rotated` events, so the switch cost comes from `thread_generations`. [M] (Q `SELECT count(*), sum(mode='new_welcome'), min/max(archived_at) FROM objects WHERE kind='thread_generations'`) There are 92 generations, 50 of them new_welcome, archived between 2026-10-15 18:47 and 2026-10-19 22:43 UTC.
- [M] (Q, workers `LIKE '%3ffa1b13-e54d-80f9%'` and the thread_context list) Coworker 1's thread 3ffa1b13-e54d-80f9-92ee-00a9eb20442b appears in no worker doc and has no transcript record. **Its slowness cannot be measured from this DB.**

## 3. Buckets (Coworker tasks, last 14 days) [M] (A)

- fo = sent_at → first_output_at.
- done = sent_at → remote_done_at.
- gap = the time between consecutive transcript steps inside one turn. It covers Coworker threads only, and the counter is cleared at each same-thread reset.
- thr / top = the number of threads, and the largest single thread's share of the gaps.

| ctx (rule) | tasks n | fo n · med / p90 s | done n · med / p90 s | gap n · med / p90 s | thr / top | bad % |
|---|---|---|---|---|---|---|
| 0-100k | 891 | 683 · 56.9 / 244 | 760 · 87.6 / 312 | 96 · 2.2 / 9.3 | 11 / 17 % | 2 |
| 100-200k | 34 | 29 · 40.5 / 153 | 32 · 60.0 / 190 | 59 · 2.5 / 17.7 | 4 / 59 % | 9 |
| 200-400k | 13 | 13 · 50.1 / 386 | 13 · 57.3 / 398 | 57 · 2.0 / 25.5 | 6 / 37 % | 8 |
| 400-800k | 0 | – | – | 28 · 1.6 / 30.3 | 2 / 64 % | – |
| 800k+ | 0 | – | – | 29 · 9.8 / 50.0 (all in 3.2M+) | 1 / 100 % | – |

- [M] (B, each entity capped at 200k tokens):
  - The 3.2M+ gaps move down.
  - 400-800k: 38 gaps, 6.2 / 41.8 s (2 threads, top 74 %).
  - 800k-1.2M: 19 gaps, 4.1 / 27.7 s (1 thread, 100 %).
  - Buckets up to 400k are unchanged.
- [M] (A) bad = failed / uncertain / blocked. The rate is 2 % / 9 % / 8 % for n = 891 / 34 / 13, which is only 3 and 1 bad tasks above 100k. That hints at more failures above 100k, but n is too small to prove it.
- [M] (A) Task first-output time does **not** rise with ctx: the fit is `fo = 105.2 s − 23.05 s per 100k` (n = 725). Below 400k the step gaps are flat too: the 2.0-2.5 s medians match the 2.2 s baseline.
- [M] (A) Gap by step number (entity index since reset, Coworker only): all 269 gaps are at steps 1-100 (median 2.6 s, p90 19.9 s), and all are at turns 1-5. **Nothing is measured beyond step 100 or turn 5.**

## 4. Cost of one switch [M] (A)

From the new_welcome thread_generations: n = 50, all matched to their triggering task.

| part | n | median | p90 |
|---|---|---|---|
| started → archived (creating the new chat) | 50 | 13.2 s | 23.5 s |
| sent → first output (the handoff turn) | 41 | 117 s | 262 s |
| started → first output, switching task | 41 | 141 s | 289 s |
| started → first output, normal task at 0-100k (baseline) | 642 | 64.9 s | 248 s |

- **Switch cost S = 141 − 64.9 = 76 s** (76.4 s) [M].
- The median handoff prompt is 12,452 tokens [M].
- About 13 s of S is chat creation. About 60 s is the slower handoff turn: 117 s against the 56.9 s baseline fo [M].

## 5. Threshold model (one baseline everywhere: the 0-100k bucket)

- Extra time per task from continuing = (bucket median gap − 2.2 s) × 14 steps, using the median of 14 steps per task [M] (A). Switching pays off when this extra exceeds S = 76 s. [E] (model)

| bucket | A: extra / task | B: extra / task |
|---|---|---|
| 100-200k | +3.5 s | +3.5 s |
| 200-400k | −3.2 s | −3.2 s |
| 400-800k | −9.2 s | +54.9 s |
| 800k-1.2M | – | +25.6 s |
| 3.2M+ | +106 s | – |

- Linear fit over the gaps:
  - A: `gap = 6.1 s + 0.251 s per 100k` (n = 269), giving T* ≈ 2,187,266 tokens [E].
  - B: `gap = 3.3 s + 2.159 s per 100k`, giving T* ≈ 265,235 tokens [E].
  - The fit swings 8x depending on how the single huge tool results are counted. **It is not robust.**
- [M] What is measured:
  - Up to 400k the extra time per task stays at or below +3.5 s, which is ≪ 76 s.
  - The only bucket over S is 3.2M+, at +106 s per task. But it comes from one thread (top = 100 %).
  - The break-even therefore lies between 400k and 3.2M. Above 400k, every bucket rests on at most 2 threads.

## 6. The ~6.8M-token steps: did they pass 5,000,000 and switch?

[M] (Q, per-thread max entity size from `json_each(thread_context.entities)`, workers / thread_generations / tasks `LIKE '%b9fd-803c%' / '%b9fd-804c%'`)

- **Thread …803c**:
  - 2,460 entities; the largest single entity is 6,787,983 tokens; 21,439,841 tokens in total.
  - The worker has purpose `main` and context_usage 21,511,994 tokens / 77 turns. It has **0 thread_generations rows**.
  - It is the Main chat. Main does not auto-rotate. It only got 5 `main_compress_hint` events, between 2026-10-21 22:29 and 22:34 UTC.
  - It is excluded from all Coworker numbers above.
- **Thread …804c**:
  - A Coworker (purpose `worker`). 81 entities; the largest single entity is 6,786,432 tokens; 7,467,746 tokens in total. It also has **0 thread_generations rows**.
  - Its worker's stored context_usage is 1,569 tokens / 0 turns.
  - Its only task was sent 2026-10-21 23:11:44 UTC with before_seq 11. The 6.8M entity is a single tool result inside that task.
  - rotate() is only evaluated before the next dispatch [C], and no next task has come yet. So it **went over 5,000,000 but has not switched**.
  - That chat has no reset time, so the next dispatch will count the whole transcript (≈7.47M) and switch it [C].
  - This single thread is the whole 3.2M+ gap bucket in A.

## 7. Recommendation

**`context_token_limit`**

- Set it to **1,200,000** [E], counted on the physical scale. That means making the usage() change from §1, so that switched chats count their real transcript.
- Reasons:
  - Everything measured ≤ 400k costs far less than S [M].
  - 3.2M+ costs more than S [M].
  - Linear model A puts T* near 2.2M [E], and B puts it lower.
  - 1.2M stays below both the A estimate and the measured loss zone, while not switching chats that measure as fast.
- The live 5,000,000 is above the only measured bucket that loses time.
- Without the code change, a value on the prompt + reply scale would mean about 330 tasks per chat at 3,612 tokens each [E]. That never triggers, given the measured maximum of 33 tasks per chat. Do not tune it on that scale.

**`context_turn_limit`**

- Keep **600** for now [E].
- The Coworker-only gaps by step number only reach steps 1-100 (turns 1-5), and the median there is 2.6 s against the 2.2 s baseline [M]. There is no measured basis for a lower turn limit.
- At the measured p90 of 25 and max of 33 tasks per chat [M], a turn limit of 600 never fires either.
- If Coworker 1's "600+ steps" are tool steps rather than tasks, the right guard is a step-count guard rather than a turn guard. That needs code and the measurement in §9.

## 8. Design proposal

1. **Switch only between tasks.**
   - [C] rotate() runs at dispatch, before the next task is sent. Keep it that way: never cut a running task.
   - Add the usage() fix from §1 so the check sees the real size. For …804c that also means one giant tool result gets counted at the next dispatch.
2. **Warm spare Welcome chats (1-2 per account).**
   - Create the Welcome chat ahead of time, off the critical path.
   - Prepare the handoff summary while the last task in the old chat is still running.
   - Estimated saving per switch:
     - Spare chat alone: about the 13.2 s chat creation (median; p90 23.5 s) [M basis, E saving].
     - Spare chat plus a pre-sent handoff: up to the full 76 s [E].
   - Over the 50 switches measured: about 660 s to 3,800 s in total [E].
   - Keep only 1 spare when switches are rare (50 in about 4 days of data [M]). Recycle spares older than e.g. 24 h.
3. **Dynamic threshold.**
   - Before each dispatch, switch if (rolling median gap at the current size − rolling baseline gap at 0-100k) × the expected steps of the next task > the rolling median switch cost of the last 20 switches. [E]
   - Clamp it between 400k and 3.2M, the zone where the measured break-even lies.
   - With warm spares S shrinks, so the threshold falls on its own.

## 9. What is still missing and how to measure it

The data is too thin above 400k (≤ 2 threads per bucket) and beyond step 100 / turn 5. Proposal:

- Run report A daily for 14 days, keeping `thread_context` step timestamps.
- Split by account (06e03988… / cbdec17c…): one account at `context_token_limit` 1,200,000 after the usage() fix, the other at the current 5,000,000.
- Compare the done time per task and the bad % per bucket.
- Accept a bucket only with ≥ 5 threads and top share < 40 %.
- Log a `context_rotated` event with started / archived / first-output times for every switch, so S can be read from events directly.
