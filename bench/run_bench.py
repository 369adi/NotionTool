"""Benchmark runner: alternates B0 and final runs through an injected client.

The runner never talks to Notion or the network itself. Every request goes
through a client object with three methods:

    send_b0(prompt) -> handle
    send_harness(prompt) -> handle
    wait_done(handle) -> {"accepted": bool, "corrections": int, "model_calls": int}

One JSON line per run is appended to the output file.
"""

import argparse
import importlib
import json
import os
import sys
import time
from collections import deque

DEFAULT_OUT = "bench/results/raw.jsonl"
BLOCKED_PREFIX = "971f"
DEFAULT_LIMIT = 179
WINDOW_SECONDS = 60.0
DEFAULT_RUNS = 3
VARIANTS = (("B0", "send_b0"), ("final", "send_harness"))
CLIENT_METHODS = ("send_b0", "send_harness", "wait_done")

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_SETUP = 3


class RefusedAccount(Exception):
    """Raised when the configured account must never receive benchmark runs."""


class RateLimiter:
    """Sliding-window limiter: at most `limit` requests in any `window` seconds."""

    def __init__(self, limit=DEFAULT_LIMIT, window=WINDOW_SECONDS,
                 clock=time.time, sleep=time.sleep):
        if int(limit) < 1:
            raise ValueError("limit must be at least 1")
        self.limit = int(limit)
        self.window = float(window)
        self.clock = clock
        self.sleep = sleep
        self.calls = deque()
        self.waits = 0

    def acquire(self):
        """Block (via the injected sleep) until one more request is allowed."""
        while True:
            now = self.clock()
            while self.calls and now - self.calls[0] >= self.window:
                self.calls.popleft()
            if len(self.calls) < self.limit:
                self.calls.append(now)
                return now
            delay = self.window - (now - self.calls[0])
            self.waits += 1
            self.sleep(max(delay, 0.001))


def check_account(account_id):
    """Raise RefusedAccount when the account id starts with the blocked prefix."""
    value = str(account_id or "").strip().lower()
    if value.startswith(BLOCKED_PREFIX):
        raise RefusedAccount(
            "refusing to run the benchmark on account %s...: accounts starting "
            "with %s are never used" % (value[:4], BLOCKED_PREFIX))
    return value


def task_fields(task):
    """Return (id, prompt) from a dict or an object with id/prompt."""
    if isinstance(task, dict):
        return str(task["id"]), str(task["prompt"])
    return str(getattr(task, "id")), str(getattr(task, "prompt"))


def append_record(out_path, record):
    """Append one JSON line (LF) and create the parent folder if needed."""
    parent = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(parent, exist_ok=True)
    with open(out_path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def run_once(client, limiter, clock, task_id, prompt, variant, method, run_index):
    """Run one task once; a client error is recorded as accepted false."""
    started = clock()
    record = {
        "task_id": task_id,
        "variant": variant,
        "run_index": run_index,
        "started": started,
        "accepted_at": None,
        "accepted": False,
        "corrections": 0,
        "model_calls": 0,
    }
    try:
        limiter.acquire()
        handle = getattr(client, method)(prompt)
        limiter.acquire()
        result = client.wait_done(handle)
        if not isinstance(result, dict):
            raise TypeError("wait_done must return a dict, got %s"
                            % type(result).__name__)
        accepted = bool(result.get("accepted"))
        record["accepted"] = accepted
        record["corrections"] = int(result.get("corrections") or 0)
        record["model_calls"] = int(result.get("model_calls") or 0)
        if accepted:
            given = result.get("accepted_at")
            if isinstance(given, (int, float)) and not isinstance(given, bool):
                record["accepted_at"] = given
            else:
                record["accepted_at"] = clock()
    except Exception as exc:  # the run continues; the failure is recorded
        record["accepted"] = False
        record["accepted_at"] = None
        record["error"] = "%s: %s" % (type(exc).__name__, exc)
    return record


def run_bench(tasks, client, account_id, out_path, runs=DEFAULT_RUNS,
              clock=time.time, sleep=time.sleep, limit_per_minute=DEFAULT_LIMIT):
    """Run every task `runs` times per variant, alternating B0 and final.

    Order per task: B0 0, final 0, B0 1, final 1, ... Returns the records.
    The account guard runs before any client call or file access.
    """
    check_account(account_id)
    if int(runs) < 1:
        raise ValueError("runs must be at least 1")
    limiter = RateLimiter(limit_per_minute, WINDOW_SECONDS, clock, sleep)
    records = []
    for task in tasks:
        task_id, prompt = task_fields(task)
        for run_index in range(int(runs)):
            for variant, method in VARIANTS:
                record = run_once(client, limiter, clock, task_id, prompt,
                                  variant, method, run_index)
                append_record(out_path, record)
                records.append(record)
    return records


def load_client(spec):
    """Load a client from a 'module:attr' path; call it if it is a factory."""
    if not spec or ":" not in spec:
        raise ValueError("--client must be an import path like package.module:attr")
    module_name, attr = spec.split(":", 1)
    module = importlib.import_module(module_name)
    obj = getattr(module, attr)
    if not all(hasattr(obj, name) for name in CLIENT_METHODS) and callable(obj):
        obj = obj()
    missing = [name for name in CLIENT_METHODS if not callable(getattr(obj, name, None))]
    if missing:
        raise TypeError("client %s is missing: %s" % (spec, ", ".join(missing)))
    return obj


def build_parser():
    parser = argparse.ArgumentParser(
        prog="run_bench",
        description="Run the benchmark suite, alternating B0 and final.")
    parser.add_argument("--account", required=True,
                        help="account id the client runs on (971f... is refused)")
    parser.add_argument("--out", default=DEFAULT_OUT,
                        help="JSONL file to append to (default: %(default)s)")
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS,
                        help="runs per task and variant (default: %(default)s)")
    parser.add_argument("--client", default=None,
                        help="client import path, module:attr (required to run)")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        check_account(args.account)
    except RefusedAccount as exc:
        print("error: %s" % exc, file=sys.stderr)
        return EXIT_REFUSED
    if not args.client:
        print("error: no client given; pass --client module:attr "
              "(the real harness client is not part of this tool)", file=sys.stderr)
        return EXIT_SETUP
    try:
        client = load_client(args.client)
    except (ImportError, AttributeError, ValueError, TypeError) as exc:
        print("error: cannot load client %r: %s" % (args.client, exc), file=sys.stderr)
        return EXIT_SETUP
    try:
        from bench.suite import SUITE
    except ImportError as exc:
        print("error: cannot load bench.suite.SUITE: %s" % exc, file=sys.stderr)
        return EXIT_SETUP
    try:
        records = run_bench(SUITE, client, args.account, args.out, runs=args.runs)
    except RefusedAccount as exc:
        print("error: %s" % exc, file=sys.stderr)
        return EXIT_REFUSED
    accepted = sum(1 for record in records if record["accepted"])
    print("wrote %d runs (%d accepted) to %s" % (len(records), accepted, args.out))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
