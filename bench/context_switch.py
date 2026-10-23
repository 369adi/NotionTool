"""Context-switch bench for Coworkers: keep the long chat or move to a new Welcome chat + handoff.

Stdlib only. Subcommands (run from the project root):

  python bench/context_switch.py db [--db PATH] [--raw-per-cell 6]
      Read-only evaluation of data/notiontool.db (sqlite URI mode=ro). Prints Markdown.
  python bench/context_switch.py bench [--client bench.context_switch:offline_client]
          [--account offline] [--levels 50000,200000,500000,1000000,2000000] [--reps 3]
          [--out work/bench/context_switch.jsonl] [--report]
      Runs the fixed task cs_recall_sum per level x variant (long, fresh) x reps.
      One JSON line per run (fields of bench/metrics.py plus the cs_* fields below).
  python bench/context_switch.py analyze PATH
      Curves (time, errors, quality over tokens) and break-even from bench rows.
  python bench/context_switch.py selftest
      Offline end-to-end check with the MOCKED client and the control client.

Token estimate: same rule as notiontool/chat_context.py estimate_tokens (ceil(utf-8 bytes / 3)).

Break-even: switch when P(T) * N_remaining > C_switch, so
    T* = T0 + C_switch / (N_remaining * (b + e * R))
b = extra seconds per token (turn-time slope), e = extra error probability per token,
R = seconds lost per failed turn, T0 = context right after a switch (handoff prompt),
C_switch = rotation + create_welcome + handoff overhead of the first turn in the new chat.

Client interface for bench (compatible with bench/run_bench.py):
    prepare(level_tokens, variant) -> {context_tokens, switch_s, run_ref}   (optional)
    send_b0(prompt) -> handle        variant long: same chat, context at the level
    send_harness(prompt) -> handle   variant fresh: directly after a switch to a new Welcome chat
    wait_done(handle) -> {accepted, checklist{Q1..Q5}, ttfo_s, turn_s, errors, retries, run_ref}
A live client must record every run as a task in data/notiontool.db and return run_ref = task id.
"""
import argparse
import json
import math
import os
import random
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench.metrics import load_jsonl, validate_row  # noqa: E402
from bench.offline_client import OfflineClient  # noqa: E402
from bench.run_bench import RateLimiter, RefusedAccount, append_record, check_account, load_client  # noqa: E402
from bench.suite import BenchTask  # noqa: E402

LEVELS = (50000, 200000, 500000, 1000000, 2000000)
LEVEL_EDGES = (100000, 350000, 750000, 1500000)
REMAINING = (1, 3, 5, 10, 20)
LIVE_ACCOUNT_PREFIX = 'd84a'
DEFAULT_OUT = 'work/bench/context_switch.jsonl'
VARIANTS_CS = (('long', 'send_b0', 'B0'), ('fresh', 'send_harness', 'final'))


# ---------------------------------------------------------------- helpers

def find_db():
    env = os.environ.get('NT_DB')
    if env:
        return env
    for parent in Path(__file__).resolve().parents:
        candidate = parent / 'data' / 'notiontool.db'
        if candidate.is_file():
            return str(candidate)
    return 'data/notiontool.db'


def connect_ro(path):
    """Open the database strictly read-only (sqlite URI mode=ro)."""
    uri = 'file:' + Path(path).resolve().as_posix() + '?mode=ro'
    return sqlite3.connect(uri, uri=True)


# The one token estimate (rank 9, step 1): imported, not copied. ceil(utf-8 bytes / 3).
from notiontool.context_archive import estimate_tokens  # noqa: E402,F401


def num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value > 0 else None


def pct(values, p):
    if not values:
        return None
    k = (len(values) - 1) * p
    f = int(math.floor(k))
    c = min(f + 1, len(values) - 1)
    return values[f] + (values[c] - values[f]) * (k - f)


def quant(values):
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    return dict(n=len(v), median=pct(v, 0.5), q1=pct(v, 0.25), q3=pct(v, 0.75), lo=v[0], hi=v[-1])


def med(values):
    q = quant(values)
    return q['median'] if q else None


def f(value, digits=1):
    if value is None:
        return '-'
    if isinstance(value, bool):
        return 'yes' if value else 'no'
    if isinstance(value, int):
        return str(value)
    return ('%.' + str(digits) + 'f') % value


def fq(q, digits=1):
    if not q:
        return 'n=0'
    return '%s [%s-%s] n=%d' % (f(q['median'], digits), f(q['q1'], digits), f(q['q3'], digits), q['n'])


def ktok(tokens):
    return '-' if tokens is None else '%dk' % round(tokens / 1000.0)


def iso(ts):
    return time.strftime('%Y-%m-%d %H:%M', time.gmtime(ts)) if ts else '-'


def ols(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx = sum(xs) / float(n)
    my = sum(ys) / float(n)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return dict(n=n, a=my - b * mx, b=b)


def fit(xs, ys, rounds=400, seed=7):
    """OLS slope with a 90% bootstrap interval (b_lo, b_hi)."""
    base = ols(xs, ys)
    if not base:
        return None
    rng = random.Random(seed)
    n = len(xs)
    slopes = []
    for _ in range(rounds):
        idx = [rng.randrange(n) for _ in range(n)]
        one = ols([xs[i] for i in idx], [ys[i] for i in idx])
        if one:
            slopes.append(one['b'])
    slopes.sort()
    base['b_lo'] = pct(slopes, 0.05)
    base['b_hi'] = pct(slopes, 0.95)
    return base


def level_of(tokens):
    for level, edge in zip(LEVELS, LEVEL_EDGES):
        if tokens < edge:
            return level
    return LEVELS[-1]


def break_even(b, e, retry_cost, c_switch, t0, remaining=REMAINING):
    """Context size T* above which a switch pays off for N remaining tasks."""
    per_token = (b or 0.0) + (e or 0.0) * (retry_cost or 0.0)
    if per_token <= 0 or c_switch is None or c_switch < 0:
        return dict(valid=False, per_token=per_token, thresholds={})
    return dict(valid=True, per_token=per_token,
                thresholds=dict((n, (t0 or 0) + c_switch / (n * per_token)) for n in remaining))


# ---------------------------------------------------------------- probes

def schema(path, samples=()):
    con = connect_ro(path)
    try:
        for (name,) in con.execute("select name from sqlite_master where type='table' order by name"):
            count = con.execute('select count(*) from "%s"' % name).fetchone()[0]
            print('table %s: %d rows' % (name, count))
        print('event kinds:', dict(con.execute('select kind, count(*) from events group by kind').fetchall()))
        print('object kinds:', dict(con.execute('select kind, count(*) from objects group by kind').fetchall()))
        for kind in samples:
            if kind.startswith('obj:'):
                row = con.execute('select doc from objects where kind=? limit 1', (kind[4:],)).fetchone()
            else:
                row = con.execute('select doc from events where kind=? order by seq desc limit 1', (kind,)).fetchone()
            print('sample %s: %s' % (kind, (row[0] if row else '')[:1500]))
    finally:
        con.close()


def fields(path, kind='tasks'):
    from collections import Counter
    con = connect_ro(path)
    try:
        keys = Counter()
        for doc in load_docs(con, kind):
            keys.update(doc.keys())
        print('KEYS', dict(keys))
    finally:
        con.close()


# ---------------------------------------------------------------- A. existing data

def load_docs(con, kind):
    out = []
    for (doc,) in con.execute('select doc from objects where kind=?', (kind,)):
        try:
            value = json.loads(doc)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            out.append(value)
    return out


def load_events(con, kind):
    out = []
    for ts, doc in con.execute('select ts, doc from events where kind=?', (kind,)):
        try:
            value = json.loads(doc) if isinstance(doc, str) else {}
        except ValueError:
            value = {}
        if isinstance(value, dict):
            value.setdefault('ts', ts)
            out.append(value)
    return out


def is_coworker(task, purpose, main_threads):
    if task.get('use_main') or task.get('thread_id') in main_threads:
        return False
    return purpose.get(task.get('worker_id')) in (None, 'worker')


def task_row(task, before, prompt_tokens, response_tokens, prior_turns):
    sent = num(task.get('sent_at'))
    first = num(task.get('first_output_at'))
    done = num(task.get('remote_done_at'))
    log = [a for a in (task.get('attempt_log') or []) if isinstance(a, dict)]
    last = log[-1] if log else {}
    started = num(last.get('started')) or num(task.get('started'))
    sent_last = num(last.get('sent')) or sent
    finished = num(task.get('finished'))
    first_started = (num(log[0].get('started')) if log else None) or started
    # a turn error is an attempt with an error class (state names of good attempts vary)
    errors = sum(1 for a in log if a.get('error_class'))
    if not log and (task.get('remote_error') or task.get('state') == 'failed'):
        errors += 1
    attempts = int(task.get('attempts') or len(log) or 1)
    worker = str(task.get('worker_id') or '')
    tokens = before + prompt_tokens
    return dict(
        task_id=task.get('id', ''), run_id=task.get('run_id', ''), role=task.get('role', ''),
        account=worker.split(':')[0][:4], worker_id=worker, thread_id=task.get('thread_id', ''),
        sent_at=sent, tokens_before=before, prompt_tokens=prompt_tokens, tokens_at_send=tokens,
        response_tokens=response_tokens, prior_turns=prior_turns, level=level_of(tokens),
        ttfo_s=(first - sent) if first and sent and first >= sent else None,
        turn_s=(done - sent) if done and sent and done >= sent else None,
        presend_s=(sent_last - started) if sent_last and started and sent_last >= started else None,
        total_s=(finished - first_started) if finished and first_started and finished >= first_started else None,
        errors=errors, retries=max(0, attempts - 1), state=task.get('state', ''),
        handoff=bool(task.get('handoff_included')), fixed=bool(task.get('fixed_by')))


def per_task_rows(tasks, generations, workers):
    """Coworker task rows with the context estimate before sending (ChatContext.usage rule)."""
    purpose = dict((w.get('id'), w.get('purpose')) for w in workers)
    main_threads = set(w.get('thread_id') for w in workers if w.get('purpose') == 'main')
    resets = {}
    for g in generations:
        at = num(g.get('archived_at'))
        if g.get('mode') == 'same_thread' and at:
            resets.setdefault(g.get('thread_id'), []).append(at)
    by_thread = {}
    for t in tasks:
        if num(t.get('sent_at')) and t.get('thread_id'):
            by_thread.setdefault(t['thread_id'], []).append(t)
    rows, timeline = [], {}
    for thread, items in by_thread.items():
        items.sort(key=lambda t: t['sent_at'])
        cuts = sorted(resets.get(thread, []))
        acc, turns, last_cut = 0, 0, None
        points = timeline.setdefault(thread, [])
        for t in items:
            cut = max([c for c in cuts if c < t['sent_at']] or [None], key=lambda c: c or 0)
            if cut != last_cut:
                acc, turns, last_cut = 0, 0, cut
            prompt = str(t.get('submitted_prompt') or '')
            response = str(t.get('raw_response') or '')
            if is_coworker(t, purpose, main_threads):
                rows.append(task_row(t, acc, estimate_tokens(prompt), estimate_tokens(response), turns))
            acc += estimate_tokens(prompt + '\n' + response)
            turns += 1
            points.append((t['sent_at'], acc))
    return rows, timeline


def tokens_at(timeline, thread, when):
    value = 0
    for sent, acc in timeline.get(thread, []):
        if when is None or sent <= when:
            value = acc
    return value


def switch_rows(generations, rows, timeline):
    """One row per Coworker switch to a new Welcome chat (thread_generations mode new_welcome)."""
    first_on = {}
    for r in sorted(rows, key=lambda r: r['sent_at'] or 0):
        first_on.setdefault(r['thread_id'], r)
    base_presend = med(r['presend_s'] for r in rows if r['prior_turns'] > 0 and not r['handoff'])
    out = []
    for g in generations:
        if g.get('mode') != 'new_welcome':
            continue
        at = num(g.get('archived_at'))
        new = g.get('successor') or ''
        first = first_on.get(new)
        presend = first['presend_s'] if first else None
        out.append(dict(
            generation=g.get('id', ''), account=str(g.get('account_id') or '')[:4],
            old_thread=g.get('thread_id', ''), new_thread=new, archived_at=at,
            old_tokens=tokens_at(timeline, g.get('thread_id'), at),
            checkpoint_tokens=estimate_tokens(g.get('checkpoint') or ''), welcome_clean=g.get('welcome_clean'),
            first_task=first['task_id'] if first else '', first_run=first['run_id'] if first else '',
            first_handoff=bool(first and first['handoff']), presend_s=presend,
            rotate_extra_s=(presend - base_presend) if presend is not None and base_presend is not None else None,
            handoff_ttfo_s=first['ttfo_s'] if first else None, handoff_turn_s=first['turn_s'] if first else None,
            handoff_prompt_tokens=first['prompt_tokens'] if first else None,
            handoff_errors=first['errors'] if first else None))
    return out, base_presend


def variant_rows(rows, switches):
    old_by_first = dict((s['first_task'], s['old_tokens']) for s in switches if s['first_task'])
    out = []
    for r in rows:
        if r['handoff'] or r['task_id'] in old_by_first:
            level_tokens = old_by_first.get(r['task_id'], r['tokens_at_send'])
            out.append(dict(r, variant='fresh', level=level_of(level_tokens), level_tokens=level_tokens))
        elif r['prior_turns'] > 0:
            out.append(dict(r, variant='long', level_tokens=r['tokens_at_send']))
        else:
            out.append(dict(r, variant='first', level_tokens=r['tokens_at_send']))
    return out


def rate(rows, pred):
    rows = list(rows)
    return (sum(1 for r in rows if pred(r)) / float(len(rows))) if rows else None


def analyze_db(path):
    con = connect_ro(path)
    try:
        tasks = load_docs(con, 'tasks')
        generations = load_docs(con, 'thread_generations')
        workers = load_docs(con, 'workers')
        main_prompts = load_docs(con, 'main_handoff_prompts')
        presend = load_events(con, 'presend_timing')
        rollup = con.execute(
            "select kind, sum(total) from event_rollup where kind in ('context_rotated', 'handoff_acked', "
            "'handoff_file_written', 'team_rotation_done', 'first_output', 'context_built', 'remote_turn_failed') "
            "group by kind order by kind").fetchall()
    finally:
        con.close()
    rows, timeline = per_task_rows(tasks, generations, workers)
    switches, base_presend = switch_rows(generations, rows, timeline)
    vrows = variant_rows(rows, switches)
    cont = [r for r in vrows if r['variant'] in ('long', 'first')]
    turn_fit = fit([r['tokens_at_send'] for r in cont if r['turn_s'] is not None],
                   [r['turn_s'] for r in cont if r['turn_s'] is not None])
    ttfo_fit = fit([r['tokens_at_send'] for r in cont if r['ttfo_s'] is not None],
                   [r['ttfo_s'] for r in cont if r['ttfo_s'] is not None])
    err_fit = fit([r['tokens_at_send'] for r in cont], [1.0 if r['errors'] else 0.0 for r in cont])
    first_turn = med(r['turn_s'] for r in vrows if r['variant'] == 'first')
    handoff_turn = med(s['handoff_turn_s'] for s in switches)
    rotate_extra = med(s['rotate_extra_s'] for s in switches)
    handoff_overhead = (handoff_turn - first_turn) if handoff_turn is not None and first_turn is not None else None
    c_switch = None
    if rotate_extra is not None or handoff_overhead is not None:
        c_switch = max(0.0, rotate_extra or 0.0) + max(0.0, handoff_overhead or 0.0)
    bad = med(r['total_s'] for r in rows if r['errors'])
    good = med(r['total_s'] for r in rows if not r['errors'])
    retry_cost = (bad - good) if bad is not None and good is not None and bad > good else med(r['turn_s'] for r in rows)
    t0 = med(s['handoff_prompt_tokens'] for s in switches) or med(r['prompt_tokens'] for r in vrows if r['variant'] == 'fresh')
    be = break_even(turn_fit['b'] if turn_fit else None, err_fit['b'] if err_fit else None, retry_cost, c_switch, t0)
    be_hi = break_even(turn_fit['b_hi'] if turn_fit else None, err_fit['b_hi'] if err_fit else None, retry_cost, c_switch, t0)
    be_lo = break_even(turn_fit['b_lo'] if turn_fit else None, err_fit['b_lo'] if err_fit else None, retry_cost, c_switch, t0)
    return dict(tasks=tasks, rows=rows, vrows=vrows, switches=switches, base_presend=base_presend,
                presend=presend, main_prompts=main_prompts, rollup=rollup, generations=generations,
                turn_fit=turn_fit, ttfo_fit=ttfo_fit, err_fit=err_fit, first_turn=first_turn,
                handoff_turn=handoff_turn, rotate_extra=rotate_extra, handoff_overhead=handoff_overhead,
                c_switch=c_switch, retry_cost=retry_cost, t0=t0, be=be, be_hi=be_hi, be_lo=be_lo)


def slope_text(fitted, unit, scale=100000.0, digits=2):
    if not fitted:
        return 'not enough data'
    return '%s %s per 100k tokens (90%% bootstrap %s to %s, n=%d, intercept %s)' % (
        f(fitted['b'] * scale, digits), unit, f(fitted['b_lo'] * scale, digits), f(fitted['b_hi'] * scale, digits),
        fitted['n'], f(fitted['a'], 2))


def be_text(be):
    if not be['valid']:
        return 'no break-even: the slope is not positive, a switch never pays off on time'
    return ', '.join('N=%d: %s' % (n, ktok(t)) for n, t in sorted(be['thresholds'].items()))


def db_report(path, raw_per_cell=6, switch_limit=40):
    a = analyze_db(path)
    rows, vrows, switches = a['rows'], a['vrows'], a['switches']
    out = []
    w = out.append
    sent = [r['sent_at'] for r in rows if r['sent_at']]
    accounts = {}
    for r in rows:
        accounts[r['account']] = accounts.get(r['account'], 0) + 1
    modes = {}
    for g in a['generations']:
        modes[g.get('mode')] = modes.get(g.get('mode'), 0) + 1
    w('## A. Existing data (read-only: %s, mode=ro)' % Path(path).name)
    w('')
    w('- tasks: %d in total, %d Coworker tasks with a send time; first send %s UTC, last send %s UTC' % (
        len(a['tasks']), len(rows), iso(min(sent)) if sent else '-', iso(max(sent)) if sent else '-'))
    w('- Coworker tasks per account prefix: ' + ', '.join('%s=%d' % kv for kv in sorted(accounts.items())))
    w('- thread_generations: ' + ', '.join('%s=%d' % (k, v) for k, v in sorted(modes.items(), key=lambda kv: str(kv[0]))))
    w('- event_rollup totals: ' + ', '.join('%s=%d' % (k, v or 0) for k, v in a['rollup']))
    w('- presend_timing detail events still kept: %d (the events table keeps only the newest rows)' % len(a['presend']))
    w('')
    w('### A1. Per task: time and errors over the context estimate')
    w('')
    w('Variants: long = continued chat (earlier turns in the same chat), first = first turn in a chat without handoff, '
      'fresh = first turn after a switch (level = size of the old chat at the switch).')
    w('')
    w('| Level | Variant | n | tokens at send | TTFO s med [IQR] | turn s med [IQR] | task total s | turns with error | retried | done |')
    w('|---|---|---|---|---|---|---|---|---|---|')
    for level in LEVELS:
        for variant in ('long', 'first', 'fresh'):
            cell = [r for r in vrows if r['level'] == level and r['variant'] == variant]
            if not cell:
                w('| %s | %s | 0 | - | - | - | - | - | - | - |' % (ktok(level), variant))
                continue
            w('| %s | %s | %d | %s | %s | %s | %s | %s | %s | %s |' % (
                ktok(level), variant, len(cell), ktok(med(r['tokens_at_send'] for r in cell)),
                fq(quant(r['ttfo_s'] for r in cell)), fq(quant(r['turn_s'] for r in cell)),
                fq(quant(r['total_s'] for r in cell)), f(rate(cell, lambda r: r['errors'] > 0), 2),
                f(rate(cell, lambda r: r['retries'] > 0), 2), f(rate(cell, lambda r: r['state'] == 'done'), 2)))
    w('')
    w('Fits over long + first rows (context estimate at send):')
    w('')
    w('- turn time: ' + slope_text(a['turn_fit'], 's'))
    w('- time to first output: ' + slope_text(a['ttfo_fit'], 's'))
    w('- share of turns with an error: ' + slope_text(a['err_fit'], '', digits=4))
    w('')
    w('### A2. Cost of each switch (new Welcome chat)')
    w('')
    w('- pre-send time of a normal continued turn (median): %s s' % f(a['base_presend']))
    w('- pre-send time of the first task in the new chat (rotation + create_welcome inside): %s' % fq(quant(s['presend_s'] for s in switches)))
    w('- extra pre-send time per switch (rotation + create_welcome): %s' % fq(quant(s['rotate_extra_s'] for s in switches)))
    w('- handoff prompt to first output: %s' % fq(quant(s['handoff_ttfo_s'] for s in switches)))
    w('- handoff prompt to end of the first turn: %s; first turn in a chat without handoff: %s s' % (
        fq(quant(s['handoff_turn_s'] for s in switches)), f(a['first_turn'])))
    w('- size of the old chat at the switch: %s tokens' % fq(quant(s['old_tokens'] for s in switches), 0))
    w('- handoff prompt size: %s tokens; checkpoint size: %s tokens' % (
        fq(quant(s['handoff_prompt_tokens'] for s in switches), 0), fq(quant(s['checkpoint_tokens'] for s in switches), 0)))
    for key in ('client', 'transcript', 'busy', 'rotate', 'config', 'body'):
        q = quant(num(e.get(key)) for e in a['presend'])
        if q:
            w('- presend_timing.%s: %s s (max %s)' % (key, fq(q, 2), f(q['hi'], 2)))
    acks = [num(p.get('acked_at')) - num(p.get('at')) for p in a['main_prompts']
            if num(p.get('acked_at')) and num(p.get('at')) and p.get('acked_at') >= p.get('at')]
    w('- Main handoff prompt to ack (for comparison, Main chats): %s s' % fq(quant(acks)))
    w('')
    w('| generation | acct | old chat tokens | first task | run | presend s | extra s | handoff TTFO s | handoff turn s | errors |')
    w('|---|---|---|---|---|---|---|---|---|---|')
    for s in sorted(switches, key=lambda s: s['archived_at'] or 0)[-switch_limit:]:
        w('| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |' % (
            s['generation'][:13] + '..' + s['generation'][-3:], s['account'], ktok(s['old_tokens']), s['first_task'] or '-',
            s['first_run'] or '-', f(s['presend_s']), f(s['rotate_extra_s']), f(s['handoff_ttfo_s']),
            f(s['handoff_turn_s']), f(s['handoff_errors'])))
    w('')
    w('### A3. Break-even from the existing data')
    w('')
    w('- C_switch = max(0, extra pre-send) + max(0, handoff turn - first turn) = %s + %s = %s s' % (
        f(a['rotate_extra']), f(a['handoff_overhead']), f(a['c_switch'])))
    w('- R (seconds lost per failed turn) = %s s; T0 (context after a switch) = %s tokens' % (f(a['retry_cost']), ktok(a['t0'])))
    w('- point estimate: ' + be_text(a['be']))
    w('- with the upper slope bound (switch earliest): ' + be_text(a['be_hi']))
    w('- with the lower slope bound (switch latest): ' + be_text(a['be_lo']))
    w('')
    w('### A4. Raw rows (newest %d per level and variant)' % raw_per_cell)
    w('')
    w('| Level | Variant | task | run | acct | tokens at send | TTFO s | turn s | errors | retries | state |')
    w('|---|---|---|---|---|---|---|---|---|---|---|')
    for level in LEVELS:
        for variant in ('long', 'first', 'fresh'):
            cell = sorted([r for r in vrows if r['level'] == level and r['variant'] == variant],
                          key=lambda r: r['sent_at'] or 0)[-raw_per_cell:]
            for r in cell:
                w('| %s | %s | %s | %s | %s | %s | %s | %s | %d | %d | %s |' % (
                    ktok(level), variant, r['task_id'], r['run_id'], r['account'], ktok(r['tokens_at_send']),
                    f(r['ttfo_s']), f(r['turn_s']), r['errors'], r['retries'], r['state']))
    return out


# ---------------------------------------------------------------- B. fixed bench task

CODEWORD = 'AMBER-4417'
NUMBERS = (17, 42, 8, 91, 23, 56, 4, 77, 35, 60, 12, 29)
CS_FILES = {'numbers.txt': ''.join('%d\n' % n for n in NUMBERS)}
PRIMER = ('Project note for all later tasks: the project codeword is %s. '
          'Remember it; a later task will ask for it.' % CODEWORD)
CS_PROMPT = ('Context-switch bench task (fixed, known answer). In the task folder, read numbers.txt '
             '(one integer per line). Create answer.json with one JSON object that has exactly three keys: '
             'count (number of lines), sum (sum of all integers) and codeword (the project codeword that was '
             'given to you earlier in this chat, before this task; do not guess it, use an empty string if you '
             'do not have it). Do not change numbers.txt and create no other files. Reply with one line: DONE.')
CHECKLIST = (
    ('Q1', 'answer.json is one JSON object with exactly count, sum, codeword'),
    ('Q2', 'count equals the number of lines (12)'),
    ('Q3', 'sum equals the sum of the integers (454)'),
    ('Q4', 'codeword equals the codeword given before the task (context kept)'),
    ('Q5', 'numbers.txt unchanged and no other files'),
)
CHECK_IDS = tuple(k for k, _ in CHECKLIST)
CODEWORD_RE = re.compile(r'project codeword is ([A-Z]+-[0-9]+)')


def checklist(folder):
    """Fixed quality checklist: Q1..Q5 -> bool. Q4 false means context is missing."""
    root = Path(folder)
    try:
        data = json.loads((root / 'answer.json').read_bytes().decode('utf-8'))
    except Exception:
        data = None
    d = data if isinstance(data, dict) else {}
    names = sorted(p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file())
    numbers = root / 'numbers.txt'
    return {
        'Q1': isinstance(data, dict) and sorted(data) == ['codeword', 'count', 'sum'],
        'Q2': type(d.get('count')) is int and d.get('count') == len(NUMBERS),
        'Q3': type(d.get('sum')) is int and d.get('sum') == sum(NUMBERS),
        'Q4': d.get('codeword') == CODEWORD,
        'Q5': names == ['answer.json', 'numbers.txt'] and numbers.is_file()
        and numbers.read_bytes() == CS_FILES['numbers.txt'].encode('utf-8'),
    }


def _cs_checker(folder):
    labels = dict(CHECKLIST)
    return ['%s failed: %s' % (k, labels[k]) for k, ok in sorted(checklist(folder).items()) if not ok]


CS_TASK = BenchTask(id='cs_recall_sum', title='Context switch: recall + sum', prompt=CS_PROMPT,
                    files=CS_FILES, checker=_cs_checker)


def filler(tokens):
    """Deterministic neutral chat history of about `tokens` (estimate_tokens rule)."""
    target = int(tokens) * 3
    parts, size, i = [], 0, 0
    while size < target:
        line = 'status %07d: routine log line, no action needed.\n' % i
        parts.append(line)
        size += len(line)
        i += 1
    return ''.join(parts)


def build_handoff(long_context, drop_fact=False):
    """Offline stand-in for the harness handoff: keeps the facts, drops the bulk."""
    lines = ['Handoff from the previous chat (harness checkpoint).']
    match = CODEWORD_RE.search(long_context or '')
    if match and not drop_fact:
        lines.append(PRIMER)
    lines.append('Earlier work: %d routine status lines, nothing open.' % (long_context or '').count('\n'))
    return '\n'.join(lines)


def solve_cs(folder, context):
    """Scripted reference solution (MOCKED): uses only the folder and the given context."""
    numbers = [int(x) for x in (Path(folder) / 'numbers.txt').read_text(encoding='utf-8').split()]
    match = CODEWORD_RE.search(context or '')
    answer = {'count': len(numbers), 'sum': sum(numbers), 'codeword': match.group(1) if match else ''}
    with open(Path(folder) / 'answer.json', 'w', encoding='utf-8', newline='\n') as handle:
        handle.write(json.dumps(answer) + '\n')


class ContextSwitchOfflineClient(OfflineClient):
    """MOCKED client: no model, no Notion, no chat. Times are local script times only.

    long  = primer + neutral filler at the level, solved from that context.
    fresh = simulated handoff built from the long context, solved from the handoff only.
    drop_handoff_fact=True is the control: the handoff loses the codeword, Q4 must fail.
    """

    mocked = True

    def __init__(self, drop_handoff_fact=False, keep_dirs=False, base_dir=None):
        OfflineClient.__init__(self, tasks=[CS_TASK], solutions={}, keep_dirs=keep_dirs, base_dir=base_dir)
        self.drop_handoff_fact = drop_handoff_fact
        self.context = ''

    def prepare(self, level_tokens, variant):
        start = time.perf_counter()
        long_context = PRIMER + '\n' + filler(level_tokens)
        if variant == 'fresh':
            self.context = build_handoff(long_context, self.drop_handoff_fact)
            switch_s = time.perf_counter() - start
        else:
            self.context = long_context
            switch_s = None
        return dict(context_tokens=estimate_tokens(self.context), switch_s=switch_s, run_ref='')

    def wait_done(self, handle):
        variant, task = self.pending.pop(handle)
        folder = tempfile.mkdtemp(prefix='bench-cs-', dir=self.base_dir)
        start = time.perf_counter()
        try:
            task.setup(folder)
            solve_cs(folder, self.context)
            items = checklist(folder)
        finally:
            if not self.keep_dirs:
                shutil.rmtree(folder, ignore_errors=True)
        elapsed = time.perf_counter() - start
        result = dict(accepted=all(items.values()), corrections=0, model_calls=0, checklist=items,
                      ttfo_s=elapsed, turn_s=elapsed, errors=0, retries=0, mocked=True,
                      run_ref='offline-' + handle)
        self.log.append(dict(result, task_id=task.id, variant=variant))
        return result


def offline_client():
    """Factory for --client bench.context_switch:offline_client (MOCKED)."""
    return ContextSwitchOfflineClient()


def offline_control_client():
    """Factory for the control: the handoff drops the codeword, so fresh runs must fail Q4."""
    return ContextSwitchOfflineClient(drop_handoff_fact=True)


def live_client():
    raise RuntimeError(
        'no live client is built in: a live run must drive a Coworker younger than 23 h on account '
        'd84a through NotionTool, record each run as a task in data/notiontool.db and return '
        'run_ref = task id (see docs/CONTEXT_SWITCH_BENCH.md)')


def _number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def run_cs_once(client, limiter, clock, level, variant, method, bench_variant, rep):
    rec = dict(task_id=CS_TASK.id, variant=bench_variant, run_index=rep, started=clock(), accepted_at=None,
               accepted=False, corrections=0, model_calls=0, cs_variant=variant, level_tokens=level,
               context_tokens=None, switch_s=None, ttfo_s=None, turn_s=None, errors=0, retries=0,
               checklist={}, quality=0.0, context_missing=None, run_ref='',
               mocked=bool(getattr(client, 'mocked', False)))
    try:
        prepare = getattr(client, 'prepare', None)
        if callable(prepare):
            info = prepare(level, variant) or {}
            rec['context_tokens'] = info.get('context_tokens')
            rec['switch_s'] = _number(info.get('switch_s'))
            rec['run_ref'] = str(info.get('run_ref') or '')
        limiter.acquire()
        handle = getattr(client, method)(CS_TASK.prompt)
        limiter.acquire()
        result = client.wait_done(handle)
        if not isinstance(result, dict):
            raise TypeError('wait_done must return a dict')
        items = result.get('checklist') or {}
        rec['checklist'] = dict((k, bool(items.get(k))) for k in CHECK_IDS)
        rec['quality'] = sum(rec['checklist'].values()) / float(len(CHECK_IDS))
        rec['context_missing'] = not rec['checklist']['Q4']
        rec['accepted'] = bool(result.get('accepted')) and all(rec['checklist'].values())
        rec['ttfo_s'] = _number(result.get('ttfo_s'))
        rec['turn_s'] = _number(result.get('turn_s'))
        rec['errors'] = int(result.get('errors') or 0)
        rec['retries'] = int(result.get('retries') or 0)
        rec['corrections'] = int(result.get('corrections') or 0)
        rec['model_calls'] = int(result.get('model_calls') or 0)
        if result.get('run_ref'):
            rec['run_ref'] = str(result['run_ref'])
        if rec['accepted']:
            rec['accepted_at'] = clock()
    except Exception as exc:  # recorded, the bench continues
        rec['accepted'] = False
        rec['accepted_at'] = None
        rec['errors'] += 1
        rec['error'] = '%s: %s' % (type(exc).__name__, exc)
    return rec


def run_cs_bench(client, account_id, out_path, levels=LEVELS, reps=3, clock=time.time, sleep=time.sleep):
    """Per level and repetition: one long run, then one fresh run. Returns the rows."""
    account = check_account(account_id)
    if not getattr(client, 'mocked', False) and not account.startswith(LIVE_ACCOUNT_PREFIX):
        raise RefusedAccount('live context-switch runs are only allowed on account %s...' % LIVE_ACCOUNT_PREFIX)
    if int(reps) < 1:
        raise ValueError('reps must be at least 1')
    limiter = RateLimiter(clock=clock, sleep=sleep)
    records = []
    for level in levels:
        for rep in range(int(reps)):
            for variant, method, bench_variant in VARIANTS_CS:
                rec = run_cs_once(client, limiter, clock, int(level), variant, method, bench_variant, rep)
                problem = validate_row(rec)
                if problem:
                    raise ValueError('bench row invalid: ' + problem)
                append_record(out_path, rec)
                records.append(rec)
    return records


# ---------------------------------------------------------------- C. curves and break-even

def bench_report(rows):
    out = []
    w = out.append
    cells = {}
    for r in rows:
        cells.setdefault((r.get('level_tokens'), r.get('cs_variant')), []).append(r)
    levels = sorted(set(k[0] for k in cells if isinstance(k[0], int)))
    mocked = any(r.get('mocked') for r in rows)
    w('## B. Bench runs of cs_recall_sum (%s)' % (
        'MOCKED offline client: no model, no Notion; times are local script times' if mocked else 'live'))
    w('')
    w('| Level | Variant | n | accepted | quality mean | context missing | TTFO s | turn s | switch s | turns with error |')
    w('|---|---|---|---|---|---|---|---|---|---|')
    penalties, switch_all = [], []
    for level in levels:
        turn_med = {}
        for variant in ('long', 'fresh'):
            cell = cells.get((level, variant), [])
            turn_med[variant] = med(r.get('turn_s') for r in cell)
            if variant == 'fresh':
                switch_all.extend(r.get('switch_s') for r in cell if r.get('switch_s') is not None)
            w('| %s | %s | %d | %s | %s | %s | %s | %s | %s | %s |' % (
                ktok(level), variant, len(cell), f(rate(cell, lambda r: r.get('accepted')), 2),
                f(sum(r.get('quality') or 0.0 for r in cell) / len(cell), 2) if cell else '-',
                f(rate(cell, lambda r: r.get('context_missing')), 2), fq(quant(r.get('ttfo_s') for r in cell), 4),
                fq(quant(r.get('turn_s') for r in cell), 4), fq(quant(r.get('switch_s') for r in cell), 4),
                f(rate(cell, lambda r: (r.get('errors') or 0) > 0), 2)))
        if turn_med.get('long') is not None and turn_med.get('fresh') is not None:
            penalties.append((level, turn_med['long'] - turn_med['fresh']))
    c_switch = med(switch_all)
    w('')
    w('| Level | P(L) = turn long - turn fresh, s | C_switch s | N* = C/P |')
    w('|---|---|---|---|')
    for level, p in penalties:
        n_star = (c_switch / p) if p > 0 and c_switch is not None else None
        w('| %s | %s | %s | %s |' % (ktok(level), f(p, 4), f(c_switch, 4), f(n_star, 1) if n_star else 'never'))
    fitted = ols([float(p[0]) for p in penalties], [p[1] for p in penalties]) if len(penalties) >= 3 else None
    t0 = med(r.get('context_tokens') for r in rows if r.get('cs_variant') == 'fresh')
    be = break_even(fitted['b'] if fitted else None, 0.0, 0.0, c_switch, t0)
    w('')
    w('- P(L) slope: %s s per 100k tokens; T0 = %s tokens' % (f(fitted['b'] * 1e5, 6) if fitted else '-', ktok(t0)))
    w('- break-even T*: ' + be_text(be))
    w('')
    w('### B1. Raw runs')
    w('')
    w('| Level | Variant | rep | run_ref | context tokens | TTFO s | turn s | switch s | Q1 | Q2 | Q3 | Q4 | Q5 | quality | accepted | mocked |')
    w('|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|')
    for r in sorted(rows, key=lambda r: (r.get('level_tokens') or 0, r.get('run_index'), r.get('cs_variant') != 'long')):
        items = r.get('checklist') or {}
        w('| %s | %s | %d | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |' % (
            ktok(r.get('level_tokens')), r.get('cs_variant'), r.get('run_index'), r.get('run_ref') or '-',
            f(r.get('context_tokens')), f(r.get('ttfo_s'), 4), f(r.get('turn_s'), 4), f(r.get('switch_s'), 4),
            f(bool(items.get('Q1'))), f(bool(items.get('Q2'))), f(bool(items.get('Q3'))), f(bool(items.get('Q4'))),
            f(bool(items.get('Q5'))), f(r.get('quality'), 2), f(bool(r.get('accepted'))), f(bool(r.get('mocked')))))
    return out


def selftest():
    tmp = tempfile.mkdtemp(prefix='cs-selftest-')
    try:
        rows = run_cs_bench(offline_client(), 'offline', os.path.join(tmp, 'rows.jsonl'), LEVELS, 3)
        if len(rows) != len(LEVELS) * 2 * 3:
            raise SystemExit('selftest failed: %d rows' % len(rows))
        bad = [r for r in rows if not r['accepted']]
        if bad:
            raise SystemExit('selftest failed: %s' % json.dumps(bad[0], sort_keys=True))
        if len(load_jsonl(os.path.join(tmp, 'rows.jsonl'))) != len(rows):
            raise SystemExit('selftest failed: JSONL rows do not validate')
        control = run_cs_bench(offline_control_client(), 'offline', os.path.join(tmp, 'control.jsonl'), LEVELS, 1)
        fresh = [r for r in control if r['cs_variant'] == 'fresh']
        long_rows = [r for r in control if r['cs_variant'] == 'long']
        if not all(r['context_missing'] for r in fresh) or any(r['context_missing'] for r in long_rows):
            raise SystemExit('selftest failed: control did not detect the missing handoff fact')
        refused = False
        try:
            run_cs_bench(offline_client(), '971f0000', os.path.join(tmp, 'x.jsonl'), LEVELS[:1], 1)
        except RefusedAccount:
            refused = True
        try:
            run_cs_bench(_LiveMarker(), 'cbdec17c', os.path.join(tmp, 'y.jsonl'), LEVELS[:1], 1)
        except RefusedAccount:
            refused = refused and True
        else:
            refused = False
        if not refused:
            raise SystemExit('selftest failed: account guard')
        bench_report(rows)
        print('selftest ok: %d rows accepted (quality 1.0), control flags missing context in %d/%d fresh runs, '
              '971f and non-d84a live runs refused' % (len(rows), len(fresh), len(fresh)))
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class _LiveMarker(object):
    """Not mocked: used only to test that live runs off d84a are refused before any call."""
    mocked = False


def main(argv=None):
    parser = argparse.ArgumentParser(prog='context_switch', description='Coworker context-switch bench')
    sub = parser.add_subparsers(dest='cmd')
    p = sub.add_parser('db')
    p.add_argument('--db', default=None)
    p.add_argument('--raw-per-cell', type=int, default=6)
    p.add_argument('--switch-limit', type=int, default=40)
    p = sub.add_parser('bench')
    p.add_argument('--client', default='bench.context_switch:offline_client')
    p.add_argument('--account', default='offline')
    p.add_argument('--levels', default=','.join(str(x) for x in LEVELS))
    p.add_argument('--reps', type=int, default=3)
    p.add_argument('--out', default=DEFAULT_OUT)
    p.add_argument('--report', action='store_true')
    p = sub.add_parser('analyze')
    p.add_argument('path')
    sub.add_parser('selftest')
    p = sub.add_parser('schema')
    p.add_argument('samples', nargs='*')
    p.add_argument('--db', default=None)
    p = sub.add_parser('fields')
    p.add_argument('kind', nargs='?', default='tasks')
    p.add_argument('--db', default=None)
    args = parser.parse_args(argv)
    if args.cmd == 'db':
        path = args.db or find_db()
        print('db: %s' % path)
        print('\n'.join(db_report(path, args.raw_per_cell, args.switch_limit)))
        return 0
    if args.cmd == 'bench':
        try:
            client = load_client(args.client)
        except Exception as exc:
            print('error: cannot load client %r: %s' % (args.client, exc), file=sys.stderr)
            return 3
        levels = [int(x) for x in args.levels.split(',') if x.strip()]
        try:
            rows = run_cs_bench(client, args.account, args.out, levels, args.reps)
        except RefusedAccount as exc:
            print('error: %s' % exc, file=sys.stderr)
            return 2
        print('wrote %d rows to %s' % (len(rows), args.out))
        if args.report:
            print('\n'.join(bench_report(rows)))
        return 0
    if args.cmd == 'analyze':
        print('\n'.join(bench_report(load_jsonl(args.path))))
        return 0
    if args.cmd == 'selftest':
        return selftest()
    if args.cmd == 'schema':
        schema(args.db or find_db(), args.samples)
        return 0
    if args.cmd == 'fields':
        fields(args.db or find_db(), args.kind)
        return 0
    parser.print_help()
    return 1


if __name__ == '__main__':
    sys.exit(main())
