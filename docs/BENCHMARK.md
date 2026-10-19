# Benchmark (Phase 9)

Status: **offline (mocked) run only; no live run.** Date of this review: 2026-10-19.

## Offline / mock mode

The benchmark has a real offline mode: the client module `bench/offline_client.py`
(module flag `MOCKED = True`). It never contacts Notion, a model or the network.
All numbers it produces are **mock numbers, not live measurements**. Reports label
them **MOCKED** (the code uses `MOCKED` and `"mocked": True`; the German evidence
label for the same thing in `docs/CAPABILITIES.md` is **[GEMOCKT]**).

How it works (function names in `bench/offline_client.py` and `bench/run_bench.py`):

- `run_bench.main()` / `build_parser()`: CLI flags `--account` (required),
  `--out` (default `bench/results/raw.jsonl`), `--runs` (default 3) and
  `--client module:attr`. Without `--client` the runner exits with code 3
  ("the real harness client is not part of this tool"). `load_client()` imports
  the `module:attr` factory.
- `run_bench()` / `run_once()`: for every task in `bench.suite.SUITE`, every run and
  both variants (`B0` via `send_b0`, `final` via `send_harness`), the client is called
  and `wait_done()` decides the record; `append_record()` writes one JSONL line.
  `check_account()` refuses accounts starting with `971f`; `RateLimiter` caps requests.
- `OfflineClient` (factories `client()` and `control_client()`): `send_b0` and
  `send_harness` find the task by its prompt (`_task_for`; an unknown prompt raises
  `KeyError`, which the runner records as not accepted). `wait_done()` runs
  `task.setup()` in a fresh temporary folder, applies the fixed reference script from
  `SCRIPTED_SOLUTIONS` (`solve_routine`, `solve_research_file`, `solve_programming`,
  `solve_multistep`, `solve_dependent_workers`, `solve_failure_case`; `bench/suite.py`
  ships no solutions), and reports `accepted = task.check()`. It always reports
  `model_calls` 0, `corrections` 0, `mocked` true and a `scripted` flag. The temporary
  folder is removed afterwards.
- `client()` uses the reference scripts. `control_client()` applies no solution, which
  shows that `check()` really judges the folder.
- B0 and final take the same scripted path; only the variant label differs. Timings,
  corrections and model calls therefore carry no meaning.

Run from the project folder:

```
python -m bench.run_bench --account offline --runs 1 --client bench.offline_client:client --out work/bench/offline_reference.jsonl
python -m bench.run_bench --account offline --runs 1 --client bench.offline_client:control_client --out work/bench/offline_control.jsonl
```

Summarize with `bench.metrics.summarize(bench.metrics.load_jsonl(path))`.

Tests: `tests/test_bench_offline.py` (setup and check in the same folder, a failing
check gives `accepted` false, reference scripts pass and the control fails on the real
`SUITE`, temporary folders are removed, unknown prompt recorded as not accepted) and
`tests/test_bench_runner.py` (runner with a fake client, fake clock and temp paths).

## Metrics

From `summarize()` in `bench/metrics.py`, computed per task and variant (`B0` = plain
chat, `final` = harness):

| Metric | Meaning |
|---|---|
| runs | Number of runs in the group |
| acceptance_rate | Accepted runs divided by all runs |
| median_time_to_acceptance | Median of `accepted_at - started` over accepted runs; empty if no run was accepted |
| median_corrections | Median number of corrections the client reported |
| median_model_calls | Median number of model calls the client reported |
| claims_superiority | True only if one variant's median is better and the gap is larger than both spreads (max - min) |

## Known limits

- Live results need a real harness client against Notion, which this repository
  does not include. No live run has been made.
- `run_bench()` only passes each task's `id` and `prompt`; acceptance is whatever the
  client reports. The offline client calls the suite checks itself; a live client
  would have to do the same.
- Accounts starting with `971f` are always refused (`check_account()`).

## Mocked offline baseline

**MOCKED / [GEMOCKT] (scripted, not live).** These numbers are not live results and
say nothing about B0 or the harness. No model, Notion chat or network was used.

- Date: 2026-10-18 (Europe/Berlin)
- Client: `bench/offline_client.py`, as described above. Both commands finished in
  about 0.5 s together.

| Client (MOCKED) | Runs | Accepted | B0 accepted | final accepted |
| --- | --- | --- | --- | --- |
| `client` (reference scripts) | 12 (6 tasks x 2 variants x 1) | 12 | 6/6 | 6/6 |
| `control_client` (no solution) | 12 (6 tasks x 2 variants x 1) | 0 | 0/6 | 0/6 |

All six tasks (routine, research_file, programming, multistep, dependent_workers,
failure_case) passed with the reference scripts and failed without them.
Raw output: `work/bench/offline_reference.jsonl`, `work/bench/offline_control.jsonl`.
