"""Step guard: short-term usage counted the way Notion counts it, for ALL chats of an account.

Live 2026-10-22 09:44:47: Notion's fair-use limit (UserRateLimitResponse at fairUseAIRateLimit)
rejected a new Main turn after ~1,290 steps in 15 min and ~1,760 steps in 60 min on one account
(up to 8 chats at once, 6 tasks started within 25 s). Earlier that day ~600 steps / 15 min and
~1,020 steps / 60 min were fine. The token budget charged the context once per turn, while Notion
reads the context again on every step, so it saw 20-50x less than Notion and never braked.

Three layers (defaults are about 50-60 % of the hit, settings override them):
1. weighted steps (steps x context, capped) per 15 / 60 min, Main and coworkers together,
2. plain steps per 15 / 60 min as a safety net,
3. fewer parallel turns: at most max_parallel_turns coworker turns per account, at least
   start_spacing_s between two coworker starts and at most starts_5m starts in 5 min.
On a Notion rate-limit / AI-disabled error in any chat: level 'pause' for provider_cooldown_s
(all chats stop, also the Main), then the same time again at half the limits.
Everything here is bookkeeping: no function raises except refuse_start (RateLimited, nothing sent).
"""
import time

KIND = 'step_guard'
KEEP = 3600
# Owner rule 2026-10-22: hard stop at 1000 steps / 15 min, then ALL chats pause 15 min (step_pause_s) and
# go on. step_hard_60m 1500 stays as a safety net below the 1,760 steps / 60 min that tripped Notion.
DEFAULTS = dict(step_soft_15m=800, step_hard_15m=1000, step_hard_60m=1500, step_pause_s=900,
                wsteps_soft_15m=112_000_000, wsteps_hard_15m=140_000_000, wsteps_hard_60m=210_000_000,
                step_context_cap=200_000, max_parallel_turns=5, start_spacing_s=45, starts_5m=8,
                provider_cooldown_s=900, step_soft_48h=8500, step_hard_48h=10000)
# Owner rule 2026-10-22 (la@arisya analysis): the AI-disabled block came at 329 steps / 15 min and
# 4,994 / 6 h, far below earlier peaks (765 / 15 min, 5,627 / 6 h) that were fine, after ~20,000 steps
# in ~51 h. So no daily cap: a rolling 48 h budget of real steps (about half of that total).
# Soft: no new coworker turns. Hard: all chats stop until the 48 h sum is below the soft value again.
H48 = 48 * 3600
SCALED = ('step_soft_15m', 'step_hard_15m', 'step_hard_60m', 'wsteps_soft_15m', 'wsteps_hard_15m',
          'wsteps_hard_60m', 'max_parallel_turns', 'starts_5m')
MARKERS = ('userratelimitresponse', 'fairuseairatelimit', 'high usage in a short amount of time',
           'notion ai was disabled', 'notion ai has been disabled', 'ai was disabled for this workspace',
           'ai is disabled for this workspace')
MAX_ITEMS = 6000
MAX_KEYS = 200


def limits(store):
    out = dict(DEFAULTS)
    try:
        settings = store.settings() or {}
        for key in out:
            if settings.get(key) not in (None, ''):
                out[key] = max(1, int(settings[key]))
    except Exception:
        pass
    return out


def account_of(worker):
    worker = worker or {}
    return str(worker.get('account_id') or str(worker.get('id') or '').split(':')[0] or '')


def _record(store):
    try:
        record = store.get(KIND, 'rolling')
    except Exception:
        record = None
    record = dict(record or {})
    record.setdefault('id', 'rolling')
    record.setdefault('items', [])
    record.setdefault('starts', [])
    record.setdefault('provider', {})
    record.setdefault('keys', [])
    record.setdefault('step_pause', {})  # account -> until: the 15 min stop after the hard step limit
    record.setdefault('h48', {})  # account -> {hour start (str): steps}
    record.setdefault('h48_pause', {})  # account -> until: stop after the hard 48 h budget
    return record


def _h48_add(record, account, steps, now):
    buckets = dict(record['h48'].get(account) or {})
    key = str(int(now // 3600 * 3600))
    buckets[key] = int(buckets.get(key) or 0) + int(steps)
    record['h48'][account] = {k: v for k, v in buckets.items() if now - float(k) < H48}


def _h48_sum(record, account, now):
    return sum(int(v) for k, v in (record['h48'].get(account) or {}).items() if now - float(k) < H48)


def _h48_until(record, account, now, target):
    """Time when the 48 h sum drops below target (old hours fall out)."""
    items = sorted((float(k), int(v)) for k, v in (record['h48'].get(account) or {}).items() if now - float(k) < H48)
    total = sum(v for _, v in items)
    until = now + 60
    for start, v in items:
        if total < target:
            break
        total -= v
        until = start + H48
    return max(now + 60, until)


def _save(store, record, now):
    record['items'] = [i for i in record['items'] if now - float(i[0]) <= KEEP][-MAX_ITEMS:]
    record['starts'] = [s for s in record['starts'] if now - float(s[0]) <= KEEP][-MAX_ITEMS:]
    record['keys'] = record['keys'][-MAX_KEYS:]
    store.put(KIND, record)


def _lock(store):
    lock = getattr(store, 'lock', None)
    if lock is None:
        import contextlib
        return contextlib.nullcontext()
    return lock


def context_of(worker, cap):
    try:
        value = int(((worker or {}).get('context_usage') or {}).get('estimated_tokens') or 0)
    except (TypeError, ValueError, AttributeError):
        value = 0
    return min(max(value, 20_000), cap)  # an empty chat still reads system prompt and tools


def add_steps(store, worker, steps, now=None):
    """Count steps of any chat (Main or coworker). Never raises."""
    try:
        steps = int(steps or 0)
        if steps <= 0:
            return 0
        now = time.time() if now is None else now
        lim = limits(store)
        weighted = steps * context_of(worker, lim['step_context_cap'])
        purpose = 'w' if (worker or {}).get('purpose') == 'worker' else 'm'
        with _lock(store):
            record = _record(store)
            account = account_of(worker)
            record['items'].append([round(now, 1), steps, int(weighted), purpose, account])
            _h48_add(record, account, steps, now)
            latched = _latch_step_pause(record, lim, account, now)
            pause_s = int(float(record['h48_pause'].get(account) or 0) - now) if latched.startswith('48 h') \
                else lim['step_pause_s']
            _save(store, record, now)
        if latched:
            try:
                store.event('step_limit_stop', account_id=account, seconds=pause_s, detail=latched,
                            message='Step limit reached (%s): all chats of this account stop for %d min'
                                    % (latched, max(1, pause_s // 60)))
            except Exception:
                pass
        return steps
    except Exception:
        return 0


def _latch_step_pause(record, lim, account, now):
    """At the hard step limit: set step_pause[account] = now + step_pause_s once; returns the reason or ''."""
    if float(record['step_pause'].get(account) or 0) > now or float(record['h48_pause'].get(account) or 0) > now:
        return ''
    s48 = _h48_sum(record, account, now)
    if s48 >= lim['step_hard_48h']:
        record['h48_pause'][account] = _h48_until(record, account, now, lim['step_soft_48h'])
        return '48 h budget: %d steps in 48 h (max %d)' % (s48, lim['step_hard_48h'])
    items = [i for i in record['items'] if (i[4] if len(i) > 4 else '') == account and now - float(i[0]) <= 3600]
    s15 = sum(int(i[1]) for i in items if now - float(i[0]) <= 900)
    s60 = sum(int(i[1]) for i in items)
    if s15 < lim['step_hard_15m'] and s60 < lim['step_hard_60m']:
        return ''
    record['step_pause'][account] = now + lim['step_pause_s']
    # The counted steps stay: after the pause the 15 min window is empty again, the 60 min net still counts.
    return '%d steps in 15 min, %d in 60 min' % (s15, s60)


def count_new(store, worker, steps, counted, now=None):
    """Stream helper: counts len(steps) - counted new steps; returns the new counted value."""
    try:
        n = len(steps or [])
    except TypeError:
        return counted
    if n > counted:
        add_steps(store, worker, n - counted, now)
        return n
    return counted


def note_start(store, worker, now=None):
    """A coworker turn was sent (spacing, starts per 5 min). Never raises."""
    try:
        if (worker or {}).get('purpose') != 'worker':
            return
        now = time.time() if now is None else now
        with _lock(store):
            record = _record(store)
            record['starts'].append([round(now, 1), account_of(worker)])
            _save(store, record, now)
    except Exception:
        pass


def is_provider_limit(text):
    low = str(text or '').lower()
    return any(marker in low for marker in MARKERS)


def note_provider_limit(store, worker, text, now=None, key=''):
    """Notion said 'rate limit' or 'AI disabled' in some chat: pause every chat of the account.

    key (the entry id) makes one error count once, also when the watcher reads it again. Never raises.
    """
    try:
        now = time.time() if now is None else now
        lim = limits(store)
        account = account_of(worker)
        with _lock(store):
            record = _record(store)
            if key and key in record['keys']:
                return False
            if key:
                record['keys'].append(key)
            old = record['provider'].get(account) or {}
            fresh = float(old.get('until') or 0) <= now
            record['provider'][account] = dict(since=now if fresh else old.get('since', now),
                                               until=now + lim['provider_cooldown_s'],
                                               text=str(text or '')[:300], count=int(old.get('count') or 0) + 1)
            _save(store, record, now)
        try:
            store.event('provider_rate_limited', account_id=account, worker_id=str((worker or {}).get('id') or ''),
                        seconds=lim['provider_cooldown_s'], detail=str(text or '')[:200],
                        message='Notion rate limit / AI disabled: all chats of this account pause for %d min'
                                % (lim['provider_cooldown_s'] // 60))
        except Exception:
            pass
        return True
    except Exception:
        return False


def _account_state(record, lim, account, now):
    base = dict(lim)
    provider = record['provider'].get(account) or {}
    until = float(provider.get('until') or 0)
    if until > now:
        return dict(level='pause', reason='Notion rate limit: cooldown after a provider error', wait=int(until - now) + 1,
                    account=account, provider_until=until, limits=base)
    s48 = _h48_sum(record, account, now)
    paused48 = float((record.get('h48_pause') or {}).get(account) or 0)
    if paused48 > now:
        return dict(level='pause', reason='48 h step budget used: all chats stop until %s' % time.strftime(
                    '%H:%M', time.localtime(paused48)), wait=int(paused48 - now) + 1, account=account,
                    provider_until=until, step_pause_until=paused48, steps_48h=s48, limits=base)
    paused = float((record.get('step_pause') or {}).get(account) or 0)
    if paused > now:
        return dict(level='pause', reason='step limit: all chats stop for %d min' % (lim['step_pause_s'] // 60),
                    wait=int(paused - now) + 1, account=account, provider_until=until, step_pause_until=paused,
                    limits=base)
    if until and now < until + lim['provider_cooldown_s']:
        for key in SCALED:
            lim[key] = max(1, lim[key] // 2)  # after the cooldown: half speed for the same time
    items = [i for i in record['items'] if (i[4] if len(i) > 4 else '') == account and now - float(i[0]) <= 3600]
    s15 = sum(int(i[1]) for i in items if now - float(i[0]) <= 900)
    s60 = sum(int(i[1]) for i in items)
    w15 = sum(int(i[2]) for i in items if now - float(i[0]) <= 900)
    w60 = sum(int(i[2]) for i in items)
    starts = [float(s[0]) for s in record['starts'] if (s[1] if len(s) > 1 else '') == account]
    starts5 = sum(1 for t in starts if now - t <= 300)
    out = dict(account=account, steps_15m=s15, steps_60m=s60, weighted_15m=w15, weighted_60m=w60,
               starts_5m=starts5, last_start=max(starts or [0]), provider_until=until, limits=lim,
               steps_48h=s48, soft_48h=s48 >= lim['step_soft_48h'])
    if s15 >= lim['step_hard_15m'] or s60 >= lim['step_hard_60m'] \
            or w15 >= lim['wsteps_hard_15m'] or w60 >= lim['wsteps_hard_60m']:
        # wait until the oldest counted item of the 15 min window drops out, at least 60 s
        out.update(level='pause', wait=60,
                   reason='step limit: %d steps / %.0fM read in 15 min, %d steps / %.0fM in 60 min'
                          % (s15, w15 / 1e6, s60, w60 / 1e6))
    elif s15 >= lim['step_soft_15m'] or w15 >= lim['wsteps_soft_15m']:
        out.update(level='throttle', wait=0,
                   reason='high step usage: %d steps / %.0fM read in 15 min' % (s15, w15 / 1e6))
    else:
        out.update(level='ok', wait=0, reason='')
    return out


def state(store, now=None, account=None):
    """Worst state over the accounts (or of one account). Never raises (fail-open 'ok')."""
    now = time.time() if now is None else now
    try:
        lim = limits(store)
        record = _record(store)
        if account is not None:
            return _account_state(record, dict(lim), account, now)
        accounts = {i[4] for i in record['items'] if len(i) > 4} | set(record['provider'])
        accounts |= {s[1] for s in record['starts'] if len(s) > 1}
        accounts |= set(record['h48']) | set(record['h48_pause'])
        rank = dict(ok=0, throttle=1, pause=2)
        worst = dict(level='ok', reason='', wait=0, account='', limits=lim, steps_15m=0, steps_60m=0,
                     weighted_15m=0, weighted_60m=0, starts_5m=0)
        for acc in sorted(accounts):
            one = _account_state(record, dict(lim), acc, now)
            if rank[one['level']] > rank[worst['level']] or (
                    one['level'] == worst['level'] and one.get('steps_15m', 0) > worst.get('steps_15m', 0)):
                worst = one
        return worst
    except Exception:
        return dict(level='ok', reason='', wait=0, account='', limits=DEFAULTS)


def running_turns(store, account, exclude_worker=''):
    """Coworker turns of the account that were sent and have not finished streaming."""
    count = 0
    try:
        for task in store.all('tasks'):
            worker_id = str(task.get('worker_id') or '')
            if worker_id == exclude_worker or worker_id.split(':')[0] != account:
                continue
            # remote_done_at of an older attempt is older than the new sent_at: that turn still streams
            if task.get('state') == 'running' and task.get('sent_at') \
                    and float(task.get('remote_done_at') or 0) < float(task.get('sent_at') or 0):
                count += 1
    except Exception:
        return 0
    return count


# ---- step estimate per coworker turn ------------------------------------------
# Moving average of the steps one coworker turn needs, per task_class|model (fallback: task_class, all).
# A new turn starts only while steps_15m + estimate x (running turns + 1) stays below the hard limit,
# so the 15 min stop is (almost) never reached.
EST_KIND, EST_DEFAULT, EST_ALPHA = 'step_estimates', 60, 0.3


def _est_keys(task):
    task = task or {}
    cls = str(task.get('task_class') or 'any')[:40]
    model = str(task.get('model') or '')[:60]
    return [cls + '|' + model, cls + '|', 'all|']


def note_turn_steps(store, task, steps):
    """Learn the steps of one finished coworker turn. Never raises."""
    try:
        steps = int(steps or 0)
        if steps <= 0:
            return
        with _lock(store):
            doc = dict(store.get(EST_KIND, 'avg') or {})
            doc['id'] = 'avg'
            avg = dict(doc.get('avg') or {})
            for key in _est_keys(task):
                old, n = (avg.get(key) or [0, 0])[:2]
                avg[key] = [round(steps if not n else old + EST_ALPHA * (steps - old), 1), int(n) + 1]
            doc['avg'] = dict(list(avg.items())[-MAX_KEYS:])
            store.put(EST_KIND, doc)
    except Exception:
        pass


def estimate(store, task):
    """Expected steps of one turn of this task (int)."""
    try:
        avg = (store.get(EST_KIND, 'avg') or {}).get('avg') or {}
        for key in _est_keys(task):
            if key in avg and int(avg[key][1]) >= 1:
                return max(1, int(round(float(avg[key][0]))))
    except Exception:
        pass
    return EST_DEFAULT


def start_wait(store, worker, now=None, task=None):
    """(seconds, reason) a coworker turn must wait before it is sent; (0, '') when it may go."""
    if (worker or {}).get('purpose') != 'worker':
        return 0, ''
    now = time.time() if now is None else now
    account = account_of(worker)
    st = state(store, now, account)
    lim = st.get('limits') or DEFAULTS
    if st['level'] == 'pause':
        return max(5, int(st.get('wait') or 60)), st['reason']
    if st.get('soft_48h'):
        return 600, '48 h step budget almost used: %d of %d steps, no new coworker turns' % (
            int(st.get('steps_48h') or 0), lim['step_hard_48h'])
    running = running_turns(store, account, str((worker or {}).get('id') or ''))
    if running >= lim['max_parallel_turns']:
        return 30, '%d coworker turns already run on this account (max %d)' % (running, lim['max_parallel_turns'])
    if task is not None:
        need = min(estimate(store, task) * (running + 1), lim['step_hard_15m'] // 2)
        used = int(st.get('steps_15m') or 0)
        if used + need > lim['step_hard_15m']:
            return 60, 'step budget: %d used + ~%d expected > %d per 15 min' % (used, need, lim['step_hard_15m'])
    spacing = lim['start_spacing_s'] * (2 if st['level'] == 'throttle' else 1)
    gap = now - float(st.get('last_start') or 0)
    if gap < spacing:
        return max(1, int(spacing - gap) + 1), 'start spacing %d s between coworker turns' % spacing
    if st.get('starts_5m', 0) >= lim['starts_5m']:
        return 30, '%d coworker starts in 5 min (max %d)' % (st['starts_5m'], lim['starts_5m'])
    return 0, ''


def refuse_start(store, worker, now=None, task=None):
    """Raise RateLimited (nothing sent) while a coworker turn must wait."""
    wait, reason = start_wait(store, worker, now, task)
    if not wait:
        return
    from .engine import RateLimited
    try:
        store.event('step_guard_wait', message='Coworker start delayed: ' + reason, seconds=wait,
                    worker_id=str((worker or {}).get('id') or ''))
    except Exception:
        pass
    raise RateLimited(wait, 'Coworker start delayed (' + reason + ')', free=True, scope='thread')


def public(store, now=None):
    now = time.time() if now is None else now
    st = state(store, now)
    return {k: v for k, v in st.items() if k != 'last_start'}
