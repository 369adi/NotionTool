"""6-hour token budget for the Main chats and all coworkers (local estimates, persisted).

Every turn NotionTool sends is charged here: the context Notion has to read again (the chat's
estimated context) plus the new prompt, and the reply once it has streamed. Windows are fixed
6-hour blocks of local time (00-06, 06-12, 12-18, 18-24) and reset by themselves. The ledger is
stored in its own record kind 'token_budget', independent of workers, threads and chats: deleting
or rotating chats never lowers the used amount. Counts are estimates (bytes / 3), never provider
numbers. When the window is used up, no new turn is sent until the next window starts.
"""
import math
import time

WINDOW_SECONDS = 6 * 3600
DEFAULT_LIMIT = 45_000_000
KIND = 'token_budget'
HISTORY = 8


def tokens_of(text):
    return math.ceil(len(str(text or '').encode('utf-8')) / 3)


def window_start(now=None):
    now = time.time() if now is None else now
    lt = time.localtime(now)
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    return midnight + int((now - midnight) // WINDOW_SECONDS) * WINDOW_SECONDS


def limit(store):
    try:
        value = int((store.settings() or {}).get('token_budget_6h', DEFAULT_LIMIT))
    except (TypeError, ValueError):
        value = DEFAULT_LIMIT
    return max(1, value)


def _record(store, start):
    ident = str(int(start))
    return store.get(KIND, ident) or dict(id=ident, start=start, end=start + WINDOW_SECONDS,
                                          used=0, input=0, output=0, turns=0, by_worker={})


def charge(store, worker, input_tokens=0, output_tokens=0, turn=False, now=None):
    """Add tokens to the current window. Never raises (bookkeeping must not break a turn)."""
    try:
        now = time.time() if now is None else now
        worker = worker or {}
        input_tokens, output_tokens = max(0, int(input_tokens or 0)), max(0, int(output_tokens or 0))
        if not input_tokens and not output_tokens and not turn:
            return None
        with store.lock:
            record = _record(store, window_start(now))
            record['input'] += input_tokens
            record['output'] += output_tokens
            record['used'] = record['input'] + record['output']
            record['turns'] += 1 if turn else 0
            record['updated'] = now
            key = str(worker.get('id') or 'unknown')
            row = record['by_worker'].setdefault(key, dict(tokens=0, turns=0))
            row.update(label=str(worker.get('label') or row.get('label') or key)[:60],
                       purpose=str(worker.get('purpose') or row.get('purpose') or ''))
            row['tokens'] += input_tokens + output_tokens
            row['turns'] += 1 if turn else 0
            store.put(KIND, record)
            _rate_add(store, now, input_tokens + output_tokens, row['purpose'], turn)
        return record
    except Exception:
        return None


CONTEXT_CAP = 200_000


def context_cap(store):
    """One turn never reads more than a model context window; the chat-history estimate can be 10M+."""
    try:
        value = int((store.settings() or {}).get('token_budget_context_cap') or CONTEXT_CAP)
    except (TypeError, ValueError, AttributeError):
        value = CONTEXT_CAP
    return max(10_000, value)


def charge_send(store, worker, prompt, now=None):
    """One sent turn: Notion reads the whole chat context again plus the new prompt."""
    context = 0
    try:
        context = int(((worker or {}).get('context_usage') or {}).get('estimated_tokens') or 0)
    except (TypeError, ValueError, AttributeError):
        context = 0
    context = min(context, context_cap(store))
    return charge(store, worker, context + tokens_of(prompt), 0, turn=True, now=now)


def charge_reply(store, worker, text, now=None):
    return charge(store, worker, 0, tokens_of(text), now=now)


def exhausted(store, now=None):
    """Seconds until the reset when the window is used up, else 0."""
    now = time.time() if now is None else now
    start = window_start(now)
    record = store.get(KIND, str(int(start))) or {}
    if int(record.get('used') or 0) >= limit(store):
        return max(1, int(start + WINDOW_SECONDS - now))
    return 0


def refuse_if_exhausted(store, now=None):
    """Raise RateLimited (nothing sent, no attempt used) while the 6-hour budget is used up."""
    wait = exhausted(store, now)
    if not wait:
        return
    from .engine import RateLimited
    try:
        store.event('token_budget_blocked', message='6h token budget used up; no turn is sent until the reset',
                    seconds=wait, limit=limit(store))
    except Exception:
        pass
    raise RateLimited(min(wait, 600), 'Token budget of %d tokens per 6 h is used up; resets in %d min'
                      % (limit(store), wait // 60 + 1), free=True, scope='account')


# ---- Burst guard (short-term usage) ----------------------------------------------------------
# The 6 h budget (45M) means an average pace of 7.5M/h = 1.9M per 15 min. Coworkers may run faster
# than that for a while, but not so fast that the whole window burns in a short time (that pattern
# is what a provider flags). Main chats are never slowed here; only coworker turns.
#   soft (throttle): 15 min >= 5M tokens (2.7x pace) or >= 40 coworker turns in 5 min
#                    -> at most one coworker turn every 20 s
#   hard (pause):    15 min >= 8M or 60 min >= 18M (2.4x pace, 6 h budget gone in 2.5 h)
#                    -> no new coworker turn until the rolling sums fall below the limits
RATE_KIND = 'token_rate'
RATE_KEEP = 3600
BURST_DEFAULTS = dict(burst_soft_15m=5_000_000, burst_hard_15m=8_000_000, burst_hard_60m=18_000_000,
                      burst_turns_5m=40, burst_spacing_s=20)


def _rate_add(store, now, tokens, purpose, turn):
    try:
        record = store.get(RATE_KIND, 'rolling') or dict(id='rolling', items=[])
        items = [i for i in (record.get('items') or []) if now - float(i[0]) <= RATE_KEEP]
        items.append([round(now, 1), int(tokens), 'w' if purpose == 'worker' else 'm', 1 if turn else 0])
        record['items'] = items[-4000:]
        if turn and purpose == 'worker':
            record['last_worker_turn'] = now
        store.put(RATE_KIND, record)
    except Exception:
        pass


def burst_limits(store):
    out = dict(BURST_DEFAULTS)
    try:
        settings = store.settings() or {}
        for key in out:
            if settings.get(key) not in (None, ''):
                out[key] = max(1, int(settings[key]))
    except Exception:
        pass
    return out


def burst_state(store, now=None):
    """{'level': 'ok'|'throttle'|'pause', sums, limits, wait} from the rolling ledger."""
    now = time.time() if now is None else now
    lim = burst_limits(store)
    try:
        record = store.get(RATE_KIND, 'rolling') or {}
    except Exception:
        record = {}
    items = [i for i in (record.get('items') or []) if now - float(i[0]) <= RATE_KEEP]
    # B42: only coworker tokens slow coworkers down; the Main chat's own long turns no longer freeze them.
    t15 = sum(int(i[1]) for i in items if now - float(i[0]) <= 900 and i[2] == 'w')
    t60 = sum(int(i[1]) for i in items if i[2] == 'w')
    turns5 = sum(1 for i in items if now - float(i[0]) <= 300 and i[2] == 'w' and i[3])
    level, reason, wait = 'ok', '', 0
    if t15 >= lim['burst_hard_15m'] or t60 >= lim['burst_hard_60m']:
        level = 'pause'
        reason = 'very high usage: %.1fM in 15 min / %.1fM in 60 min' % (t15 / 1e6, t60 / 1e6)
        wait = 120
    elif t15 >= lim['burst_soft_15m'] or turns5 >= lim['burst_turns_5m']:
        level = 'throttle'
        reason = 'high usage: %.1fM in 15 min, %d coworker turns in 5 min' % (t15 / 1e6, turns5)
        last = float(record.get('last_worker_turn') or 0)
        wait = max(0, int(lim['burst_spacing_s'] - (now - last)))
    return dict(level=level, reason=reason, wait=wait, tokens_15m=t15, tokens_60m=t60,
                coworker_turns_5m=turns5, limits=lim)


def refuse_if_burst(store, worker, now=None):
    """Raise RateLimited (nothing sent) for a coworker turn while usage is too high for a short time."""
    if (worker or {}).get('purpose') != 'worker':
        return
    state = burst_state(store, now)
    if state['level'] == 'ok' or (state['level'] == 'throttle' and not state['wait']):
        return
    from .engine import RateLimited
    try:
        store.event('token_burst_' + state['level'], message='Coworkers slowed down: ' + state['reason'],
                    seconds=state['wait'], worker_id=str((worker or {}).get('id') or ''))
    except Exception:
        pass
    raise RateLimited(max(5, state['wait']), 'Coworkers slowed down (' + state['reason'] + ')',
                      free=True, scope='thread')


def public(store, now=None):
    """Report for /api/state and the live UI."""
    now = time.time() if now is None else now
    start = window_start(now)
    cap = limit(store)
    record = _record(store, start)
    used = int(record.get('used') or 0)
    rows = sorted(({'id': k, **v} for k, v in (record.get('by_worker') or {}).items()),
                  key=lambda r: -int(r.get('tokens') or 0))
    history = []
    for i in range(1, HISTORY + 1):
        old = store.get(KIND, str(int(window_start(start - 1 - (i - 1) * WINDOW_SECONDS))))
        if old:
            history.append(dict(start=old['start'], end=old['end'], used=old.get('used', 0), turns=old.get('turns', 0)))
    return dict(limit=cap, used=used, input=int(record.get('input') or 0), output=int(record.get('output') or 0),
                remaining=max(0, cap - used), percent=round(min(100.0, used * 100.0 / cap), 2),
                turns=int(record.get('turns') or 0), window_start=start, window_end=start + WINDOW_SECONDS,
                resets_in=max(0, int(start + WINDOW_SECONDS - now)), blocked=used >= cap,
                main_tokens=sum(int(r.get('tokens') or 0) for r in rows if r.get('purpose') != 'worker'),
                coworker_tokens=sum(int(r.get('tokens') or 0) for r in rows if r.get('purpose') == 'worker'),
                by_worker=rows[:40], history=history, estimate=True, burst=burst_state(store, now))
