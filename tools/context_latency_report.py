"""Read-only report: Coworker context size vs latency, and the cost of a context switch.

Usage:  python tools/context_latency_report.py [--days 14] [--db PATH] [--cap-entity N]

Opens the NotionTool DB with mode=ro (uri=True) and PRAGMA query_only=ON. Writes nothing.
Prints a compact table (< 3900 chars). Only Coworker chats count: tasks without use_main whose
worker is not purpose 'main'; transcripts only of threads that belong to such tasks or to non-main
workers (threads of 'main' workers are left out).
Definitions (counting rule copied from ChatContext.usage, notiontool/chat_context.py:54-74):
  reset    latest reset of the chat before the moment: thread_generations same_thread on this thread
           (base_at=archived_at, base_seq=base_seq), thread_generations new_welcome whose successor is
           this thread (base_at=archived_at, base_seq=0), or the worker's own context_base_at/_seq.
  local    sum estimate_tokens(submitted_prompt + '\\n' + raw_response) of earlier tasks on the thread
           with sent_at > base_at
  ctx      production rule = max(local, observed). observed = bytes/3 of the thread_context entities
           with base_seq < sequence <= before_seq when base_seq > 0; all entities up to before_seq when
           there is no reset time at all (base_at = 0); otherwise 0. So after a new-chat switch only
           prompt + reply count. (native_tokens of archived chats is not modelled.)
  ctx_obs  physical size as counted before: max(local, entities since the last same_thread reset up to
           before_seq), also in switched chats. Column nobs = tasks per bucket by ctx_obs.
  fo       sent_at -> first_output_at;  done = sent_at -> remote_done_at (one Notion turn)
  gap      created_at difference of consecutive transcript entries inside one turn (a user_message
           starts a new turn; gaps <= 0 or > 30 min are dropped). Includes local tool run time.
           gapP = bucketed by the physical size (counter cleared at each same_thread reset),
           gapR = bucketed by the production rule; thr = threads, top = largest thread's share in %.
  switch   thread_generations mode new_welcome, matched to the task whose dispatch spans archived_at
  baseline the fresh-chat baseline is the 0-100k bucket everywhere.
"""
import argparse
import bisect
import datetime
import json
import math
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    from notiontool.chat_context import estimate_tokens
except Exception:  # same formula as notiontool/chat_context.py:13-15
    def estimate_tokens(value):
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return math.ceil(len(text.encode('utf-8')) / 3)

DEFAULT_DB = os.path.join(ROOT, 'data', 'notiontool.db')
EDGES = [0, 100_000, 200_000, 400_000, 800_000, 1_200_000, 2_000_000, 3_200_000]
LABELS = ['0-100k', '100-200k', '200-400k', '400-800k', '800k-1.2M', '1.2M-2M', '2M-3.2M', '3.2M+']
IDX_EDGES = [0, 100, 300, 600, 1200]
IDX_LABELS = ['1-100', '101-300', '301-600', '601-1200', '1201+']
TURN_EDGES = [0, 5, 10, 20, 40]
TURN_LABELS = ['1-5', '6-10', '11-20', '21-40', '41+']
BAD = ('failed', 'uncertain', 'blocked')
MAX_GAP = 1800
CAP = 3900


def connect(path):
    con = sqlite3.connect('file:' + path.replace('\\', '/') + '?mode=ro', uri=True, timeout=5)
    con.execute('PRAGMA query_only=ON')
    return con


def stamp(value):
    if value in (None, ''):
        return None
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e11 else float(value)
    try:
        return datetime.datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def bucket(tokens):
    return min(len(LABELS) - 1, bisect.bisect_right(EDGES, tokens) - 1)


def ibucket(edges, i):
    """1-based index into ranges like 1-100, 101-300, ..."""
    return max(0, min(len(edges) - 1, bisect.bisect_left(edges, i) - 1))


def pct(values, q):
    v = sorted(values)
    if not v:
        return None
    k = (len(v) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def f(x):
    if x is None:
        return '-'
    return '%.0f' % x if abs(x) >= 100 else '%.1f' % x


def fit(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if not sxx:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return my - b * mx, b


def docs(con, kind):
    return [json.loads(d) for (d,) in con.execute('SELECT doc FROM objects WHERE kind=?', (kind,))]


def entity_rows(con, cap=0):
    """{thread_id: sorted [(seq, created_ts, kind, tokens)]} read field-wise by SQLite, not whole docs.

    cap > 0 limits the tokens counted per entity (sensitivity check against huge tool entities)."""
    out = {}
    query = ("SELECT o.id, json_extract(e.value,'$.sequence'), json_extract(e.value,'$.created_at'), "
             "json_extract(e.value,'$.kind'), length(CAST(e.value AS BLOB)) "
             "FROM objects o, json_each(o.doc,'$.entities') e WHERE o.kind='thread_context'")
    for ident, seq, created, kind, size in con.execute(query):
        try:
            seq = float(seq or 0)
        except (TypeError, ValueError):
            seq = 0.0
        tok = math.ceil((size or 0) / 3)
        out.setdefault(ident.split(':', 1)[-1], []).append((seq, stamp(created), kind or '', min(tok, cap) if cap else tok))
    for rows in out.values():
        rows.sort(key=lambda r: r[0])
    return out


def gap_line(name, cells):
    """n med/p90 per bucket, plus threads and the largest thread's share."""
    parts = []
    for label, c in zip(LABELS, cells):
        if c['g']:
            top = max(c['th'].values()) * 100.0 / len(c['g'])
            parts.append('%s %d/%.0f%%' % (label, len(c['th']), top))
    return '%s thr/top: %s' % (name, ' '.join(parts) or '-')


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=float, default=14)
    ap.add_argument('--db', default=DEFAULT_DB)
    ap.add_argument('--cap-entity', type=int, default=0,
                    help='count at most N tokens per transcript entity (0 = off, same as ChatContext.usage)')
    args = ap.parse_args(argv)
    t0 = time.time()
    con = connect(args.db)
    now = time.time()
    since = now - args.days * 86400
    tasks = docs(con, 'tasks')
    workers = {w.get('id'): w for w in docs(con, 'workers')}
    gens = docs(con, 'thread_generations')
    settings = docs(con, 'settings')
    ents = entity_rows(con, args.cap_entity)
    ev_rot = con.execute("SELECT count(*) FROM events WHERE kind='context_rotated'").fetchone()[0]
    ev_min = con.execute('SELECT min(ts) FROM events').fetchone()[0]
    con.close()
    t_load = time.time() - t0

    def coworker(t):
        w = workers.get(t.get('worker_id'))
        return not t.get('use_main') and (w or {}).get('purpose') != 'main'

    main_threads = set()
    for w in workers.values():
        if w.get('purpose') == 'main':
            main_threads.add(w.get('thread_id'))
            main_threads.update(w.get('retired_threads') or [])
    cow_threads = {t.get('thread_id') for t in tasks if t.get('thread_id') and coworker(t)}
    for w in workers.values():
        if w.get('purpose') != 'main':
            cow_threads.add(w.get('thread_id'))
            cow_threads.update(w.get('retired_threads') or [])
    cow_threads -= main_threads
    cow_threads.discard(None)

    resets = {}  # thread -> sorted [(base_at, base_seq)] as ChatContext.usage sees them
    for g in gens:
        at = g.get('archived_at') or 0
        if g.get('mode') == 'same_thread' and g.get('thread_id'):
            resets.setdefault(g['thread_id'], []).append((at, g.get('base_seq') or 0))
        elif g.get('mode') == 'new_welcome' and g.get('successor'):
            resets.setdefault(g['successor'], []).append((at, 0))
    for w in workers.values():
        if w.get('thread_id') and w.get('context_base_at'):
            resets.setdefault(w['thread_id'], []).append((w['context_base_at'], w.get('context_base_seq') or 0))
    for v in resets.values():
        v.sort()
    same = {tid: sorted({s for _, s in v if s}) for tid, v in resets.items()}

    def reset_before(tid, at):
        best = (0, 0)
        for a, s in resets.get(tid, []):
            if a < at:
                best = (a, s)
        return best

    def phys_base(tid, at):
        best = -1
        for a, s in resets.get(tid, []):
            if a < at and s:
                best = s
        return best

    size = {t['id']: estimate_tokens(str(t.get('submitted_prompt') or '') + '\n' + str(t.get('raw_response') or ''))
            for t in tasks}
    by_thread = {}
    for t in tasks:
        if t.get('thread_id') and t.get('sent_at'):
            by_thread.setdefault(t['thread_id'], []).append(t)
    for v in by_thread.values():
        v.sort(key=lambda t: t['sent_at'])
    task_times = {tid: [(t['sent_at'], size[t['id']]) for t in v] for tid, v in by_thread.items()}

    def local_at(tid, base_at, at):
        return sum(sz for s, sz in task_times.get(tid, ()) if base_at < s < at)

    prefix = {}  # thread -> (seqs, cumulative tokens)
    for tid, rows in ents.items():
        acc, cum = 0, []
        for r in rows:
            acc += r[3]
            cum.append(acc)
        prefix[tid] = ([r[0] for r in rows], cum)

    def observed(tid, base_seq, upto):
        if tid not in prefix:
            return 0
        seqs, cum = prefix[tid]
        hi = bisect.bisect_right(seqs, upto)
        lo = bisect.bisect_right(seqs, base_seq)
        return (cum[hi - 1] if hi else 0) - (cum[lo - 1] if lo else 0)

    stats = [dict(n=0, nobs=0, fo=[], done=[], steps=[], bad=0, multi=0) for _ in LABELS]
    task_x, task_fo, growth, growth_local = [], [], [], []
    for tid, ts in by_thread.items():
        for i, t in enumerate(ts):
            base_at, base_seq = reset_before(tid, t['sent_at'])
            local = sum(size[u['id']] for u in ts[:i] if u['sent_at'] > base_at)
            upto = t.get('before_seq') or 0
            if not t.get('before_seq'):
                rule = raw = 0
            else:
                raw = observed(tid, phys_base(tid, t['sent_at']), upto)
                if base_seq:
                    rule = observed(tid, base_seq, upto)
                elif not base_at:
                    rule = observed(tid, -1, upto)
                else:
                    rule = 0  # switched chat: usage() counts prompt + reply only
            ctx, ctx_obs = max(local, rule), max(local, raw)
            t['_ctx'] = ctx
            if t['sent_at'] < since or not coworker(t):
                continue
            s = stats[bucket(ctx)]
            stats[bucket(ctx_obs)]['nobs'] += 1
            s['n'] += 1
            s['bad'] += t.get('state') in BAD
            s['multi'] += (t.get('attempts') or 0) > 1
            if t.get('first_output_at') and t['first_output_at'] >= t['sent_at']:
                s['fo'].append(t['first_output_at'] - t['sent_at'])
                task_x.append(ctx / 1e5)
                task_fo.append(t['first_output_at'] - t['sent_at'])
            if t.get('remote_done_at') and t['remote_done_at'] >= t['sent_at']:
                s['done'].append(t['remote_done_at'] - t['sent_at'])
            growth_local.append(size[t['id']])
            if tid in prefix and t.get('before_seq') is not None:
                nxt = ts[i + 1].get('before_seq') if i + 1 < len(ts) else None
                rows = [r for r in ents[tid] if r[0] > (t.get('before_seq') or 0) and (nxt is None or r[0] <= nxt)]
                if rows:
                    s['steps'].append(len(rows))
                    growth.append(sum(r[3] for r in rows))

    # Step gaps: Coworker chats only; the physical counter is cleared at each same_thread reset.
    gst = {k: [dict(g=[], th={}) for _ in LABELS] for k in 'PR'}
    xs, ys = [], []
    idx_gaps = [[] for _ in IDX_LABELS]    # by entity index since reset
    turn_gaps = [[] for _ in TURN_LABELS]  # by turn (task) number since reset = usage() 'turns'
    n_gap_threads = n_skipped = 0
    for tid, rows in ents.items():
        if tid not in cow_threads:
            n_skipped += 1
            continue
        bases, bi = same.get(tid, []), 0
        acc = idx = turn = 0
        prev, used = None, False
        for seq, created, kind, tok in rows:
            while bi < len(bases) and bases[bi] < seq:
                acc = idx = turn = 0  # same-thread reset: everything counts from the new base
                bi += 1
            idx += 1
            if kind == 'user_message':
                turn += 1
                prev = (seq, created)
                acc += tok
                continue
            if prev and created and prev[1] and created >= since:
                gap = created - prev[1]
                if 0 < gap <= MAX_GAP:
                    a, s = reset_before(tid, created)
                    rule = max(local_at(tid, a, created), acc if (s or not a) else 0)
                    for key, val in (('P', acc), ('R', rule)):
                        c = gst[key][bucket(val)]
                        c['g'].append(gap)
                        c['th'][tid] = c['th'].get(tid, 0) + 1
                    idx_gaps[ibucket(IDX_EDGES, idx)].append(gap)
                    turn_gaps[ibucket(TURN_EDGES, turn)].append(gap)
                    xs.append(acc / 1e5)
                    ys.append(gap)
                    used = True
            prev = (seq, created)
            acc += tok
        n_gap_threads += used

    # Switch cost from new_welcome generations: the task whose dispatch spans archived_at triggered it.
    sw_create, sw_total, sw_fo, sw_handoff, unmatched = [], [], [], [], 0
    trig = set()
    nw = [g for g in gens if g.get('mode') == 'new_welcome']
    for g in nw:
        at = g.get('archived_at') or 0
        hit = [t for t in tasks if t.get('worker_id') == g.get('worker_id') and t.get('started') and t.get('sent_at')
               and t['started'] - 1 <= at <= t['sent_at'] + 1]
        if not hit:
            unmatched += 1
            continue
        t = min(hit, key=lambda t: t['sent_at'] - t['started'])
        trig.add(t['id'])
        sw_create.append(at - t['started'])
        sw_handoff.append(estimate_tokens(str(t.get('submitted_prompt') or '')))
        if t.get('first_output_at') and t['first_output_at'] >= t['sent_at']:
            sw_fo.append(t['first_output_at'] - t['sent_at'])
            sw_total.append(t['first_output_at'] - t['started'])
    base_total = [t['first_output_at'] - t['started'] for t in tasks
                  if t['id'] not in trig and t.get('started') and t.get('first_output_at') and t.get('sent_at')
                  and t['first_output_at'] >= t['started'] and t.get('_ctx', 0) < 100_000 and coworker(t)]

    out = []
    p = out.append
    p('Context latency report (read-only, mode=ro)  window=%gd  load=%.1fs' % (args.days, t_load))
    for st in settings:
        keys = ('context_token_limit', 'context_turn_limit', 'coworker_fresh_context', 'coworker_rotate_mode')
        vals = {k: st.get(k) for k in keys if k in st}
        if vals:
            p('settings %s: %s' % (st.get('id', '?'), json.dumps(vals)))
    p('tasks=%d coworker tasks in window=%d; transcripts=%d: coworker=%d (with gaps %d), skipped non-coworker=%d' % (
        len(tasks), sum(s['n'] for s in stats), len(ents), len(ents) - n_skipped, n_gap_threads, n_skipped))
    p('')
    p('%-9s %4s %4s | %-11s | %-11s | %-15s | %-15s | %4s %4s %3s' % (
        'ctx', 'n', 'nobs', 'fo med/p90', 'done med/p90', 'gapR n med/p90', 'gapP n med/p90', 'stp', 'bad%', 're%'))
    for i, (label, s) in enumerate(zip(LABELS, stats)):
        n = s['n'] or 1
        gr, gp = gst['R'][i]['g'], gst['P'][i]['g']
        p('%-9s %4d %4d | %5s/%-5s | %5s/%-5s | %4d %4s/%-5s | %4d %4s/%-5s | %4s %4.0f %3.0f' % (
            label, s['n'], s['nobs'], f(pct(s['fo'], .5)), f(pct(s['fo'], .9)), f(pct(s['done'], .5)),
            f(pct(s['done'], .9)), len(gr), f(pct(gr, .5)), f(pct(gr, .9)), len(gp), f(pct(gp, .5)), f(pct(gp, .9)),
            f(pct(s['steps'], .5)), 100 * s['bad'] / n, 100 * s['multi'] / n))
    p('(fo/done n=%s / %s; bad=failed|uncertain|blocked, re=attempts>1)' % (
        '/'.join(str(len(s['fo'])) for s in stats), '/'.join(str(len(s['done'])) for s in stats)))
    p(gap_line('gapR', gst['R']))
    p(gap_line('gapP', gst['P']))
    p('')
    step = fit(xs, ys)
    fo_fit = fit(task_x, task_fo)
    p('fit gapP = a + b*ctx[100k]: %s  (n=%d)' % ('a=%.1fs b=%.3fs' % step if step else '-', len(xs)))
    p('fit fo   = a + b*ctx[100k]: %s  (n=%d)' % ('a=%.1fs b=%.2fs' % fo_fit if fo_fit else '-', len(task_x)))
    k = pct([x for s in stats for x in s['steps']], .5)
    g_obs, g_loc = pct(growth, .5), pct(growth_local, .5)
    p('per task: steps med=%s  growth med observed=%s tok, local=%s tok' % (f(k), f(g_obs), f(g_loc)))
    per_chat = [sum(1 for t in v if coworker(t)) for v in by_thread.values()]
    per_chat = [c for c in per_chat if c]
    p('coworker tasks per chat (thread, all time): n=%d med=%s p90=%s max=%s' % (
        len(per_chat), f(pct(per_chat, .5)), f(pct(per_chat, .9)), max(per_chat) if per_chat else '-'))
    p('gap by entity index (n med/p90 s): ' + '  '.join(
        '%s:%d %s/%s' % (lb, len(g), f(pct(g, .5)), f(pct(g, .9))) for lb, g in zip(IDX_LABELS, idx_gaps)))
    p('gap by turn number  (n med/p90 s): ' + '  '.join(
        '%s:%d %s/%s' % (lb, len(g), f(pct(g, .5)), f(pct(g, .9))) for lb, g in zip(TURN_LABELS, turn_gaps)))
    if args.cap_entity:
        p('NOTE: --cap-entity %d: ctx counts at most that many tokens per transcript entity' % args.cap_entity)
    p('')
    p('switch (thread_generations new_welcome): n=%d matched=%d unmatched=%d; context_rotated events=%d (events since %s)' % (
        len(nw), len(sw_create), unmatched, ev_rot,
        datetime.datetime.fromtimestamp(ev_min).strftime('%m-%d %H:%M') if ev_min else '-'))
    p('  started->archived (create chat)    med=%s p90=%s n=%d' % (f(pct(sw_create, .5)), f(pct(sw_create, .9)), len(sw_create)))
    p('  sent->first output (handoff turn)  med=%s p90=%s n=%d' % (f(pct(sw_fo, .5)), f(pct(sw_fo, .9)), len(sw_fo)))
    p('  started->first output, switch      med=%s p90=%s n=%d' % (f(pct(sw_total, .5)), f(pct(sw_total, .9)), len(sw_total)))
    p('  started->first output, normal<100k med=%s p90=%s n=%d' % (f(pct(base_total, .5)), f(pct(base_total, .9)), len(base_total)))
    p('  handoff prompt tokens med=%s' % f(pct(sw_handoff, .5)))
    S = None
    if sw_total and base_total:
        S = pct(sw_total, .5) - pct(base_total, .5)
    if (S is None or S <= 0) and sw_create:
        S = pct(sw_create, .5)
    H = pct(sw_handoff, .5) or 0
    p('')
    if step and step[1] > 0 and k and S:
        T = H + S / (step[1] * k) * 1e5
        p('MODEL (estimate): S=%.1fs; linear gapP -> T*=%.0f tokens' % (S, T))
    else:
        p('MODEL: no positive slope or switch cost -> no linear threshold (S=%s, b=%s, k=%s)' % (
            f(S), '%.3f' % step[1] if step else '-', f(k)))
    g0 = pct(gst['P'][0]['g'], .5)
    if g0 is not None and k and S:
        p('baseline = gapP 0-100k med %.1fs; extra/task = (med - base) x %s steps; S=%.1fs' % (g0, f(k), S))
        for i, label in enumerate(LABELS):
            cells = []
            for key in 'PR':
                g = gst[key][i]['g']
                cells.append('%s=%s' % (key, f((pct(g, .5) - g0) * k) if g else '-'))
            p('  %-9s extra/task %s' % (label, ' '.join(cells)))
        p('  by turn: ' + ' '.join('%s=%s' % (lb, f((pct(g, .5) - g0) * k) if g else '-')
                                   for lb, g in zip(TURN_LABELS, turn_gaps)))
    p('runtime %.1fs' % (time.time() - t0))
    text = '\n'.join(out)
    print(text if len(text) <= CAP else text[:CAP] + '\n[truncated]')
    return 0


if __name__ == '__main__':
    sys.exit(main())
