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
KEEP = 3 * 3600  # items stay 3 h: the snapshot at a Notion rate limit shows windows up to 3 h
# Owner rule 2026-10-22: hard stop at 1000 steps / 15 min, then ALL chats pause 15 min (step_pause_s) and
# go on. step_hard_60m 1500 stays as a safety net below the 1,760 steps / 60 min that tripped Notion.
DEFAULTS = dict(step_soft_15m=800, step_hard_15m=1000, step_hard_60m=1500, step_pause_s=900,
                wsteps_soft_15m=112_000_000, wsteps_hard_15m=140_000_000, wsteps_hard_60m=210_000_000,
                step_context_cap=200_000, max_parallel_turns=4, start_spacing_s=45, starts_5m=8,
                provider_cooldown_s=900, step_soft_48h=25000, step_hard_48h=30000,
                inf_hard_60m=450, cw_streak_h=12, cw_active_steps_h=100, cw_rest_s=3600,
                err_hard_60m=30, err_wait_s=300)
# Owner rule 2026-10-22 (la@arisya analysis + owner decision 14:30): no daily cap. The rolling 48 h budget
# only brakes COWORKERS and never stops the Main. qoleqabi had 20,350 steps in 23 h without a block, so
# 25,000 / 30,000. Soft: at most half the parallel coworker turns. Hard: no new coworker turns.
# inf_hard_60m: second net. The fair-use hit 09:44:47 came after 572 AI (agent-inference) steps in 60 min;
# the highest hour without a hit had 541. 450 AI steps / 60 min stops ALL chats like the step limit.
# cw_streak_h: la was disabled after 52 h nonstop with ~5 chats at once (looks like automation). After
# cw_streak_h full hours in a row with >= cw_active_steps_h coworker steps, coworkers rest cw_rest_s.
# err_hard_60m: la had 7x more tool errors; >= 30 error steps in 60 min -> no new coworker turns for a while.
H48 = 48 * 3600
SCALED = ('step_soft_15m', 'step_hard_15m', 'step_hard_60m', 'wsteps_soft_15m', 'wsteps_hard_15m',
          'wsteps_hard_60m', 'max_parallel_turns', 'starts_5m', 'inf_hard_60m')
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
    record.setdefault('h48_pause', {})  # unused since 14:30 (48 h never stops the Main)
    record.setdefault('hw', {})  # account -> {hour start (str): coworker steps}, 24 h
    record.setdefault('cw_rest', {})  # account -> {until, after}: coworker rest after a long nonstop streak
    record.setdefault('hits', [])  # snapshots of the windows at each Notion rate limit (last 30)
    return record


def _hw_add(record, account, steps, now):
    buckets = dict(record['hw'].get(account) or {})
    key = str(int(now // 3600 * 3600))
    buckets[key] = int(buckets.get(key) or 0) + int(steps)
    record['hw'][account] = {k: v for k, v in buckets.items() if now - float(k) < 86400}


def _cw_streak(record, account, now, lim):
    """Full hours in a row (ending with the last full hour) with >= cw_active_steps_h coworker steps."""
    buckets = record['hw'].get(account) or {}
    after = float((record['cw_rest'].get(account) or {}).get('after') or 0)
    hour = int(now // 3600 * 3600) - 3600
    streak = 0
    while hour >= after and int(buckets.get(str(hour)) or 0) >= lim['cw_active_steps_h'] and streak < 48:
        streak += 1
        hour -= 3600
    return streak


def _start_rest(store, account, now, lim):
    """Coworker rest: hours before its end no longer count for the next streak. Never raises."""
    try:
        until = now + lim['cw_rest_s']
        with _lock(store):
            record = _record(store)
            record['cw_rest'][account] = dict(until=until, after=until)
            _save(store, record, now)
        store.event('coworker_rest', account_id=account, seconds=lim['cw_rest_s'],
                    message='Coworkers ran %d h nonstop: %d min rest, the Main goes on'
                            % (lim['cw_streak_h'], lim['cw_rest_s'] // 60))
    except Exception:
        pass


def _item_val(item, index):
    return int(item[index]) if len(item) > index and item[index] else 0


def _snapshot(record, account, now):
    """All windows at this moment (for a Notion rate limit: narrows down Notion's real limit)."""
    items = [i for i in record['items'] if (i[4] if len(i) > 4 else '') == account]
    out = dict(ts=round(now, 1), account=account, steps_48h=_h48_sum(record, account, now), windows={})
    for win in (60, 300, 900, 1800, 3600, 10800):
        sel = [i for i in items if now - float(i[0]) <= win]
        out['windows'][str(win)] = dict(
            steps=sum(int(i[1]) for i in sel), ai_steps=sum(_item_val(i, 5) for i in sel),
            errors=sum(_item_val(i, 6) for i in sel), read_m=round(sum(int(i[2]) for i in sel) / 1e6, 1),
            coworker_steps=sum(int(i[1]) for i in sel if i[3] == 'w'))
    return out


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


def add_steps(store, worker, steps, now=None, inference=None, errors=0):
    """Count steps of any chat (Main or coworker). Never raises.

    inference: how many of them are AI (agent-inference) steps; None = unknown -> estimate 25 %
    (measured share on qoleqabi and la). errors: error steps among them.
    """
    try:
        steps = int(steps or 0)
        if steps <= 0:
            return 0
        now = time.time() if now is None else now
        lim = limits(store)
        weighted = steps * context_of(worker, lim['step_context_cap'])
        purpose = 'w' if (worker or {}).get('purpose') == 'worker' else 'm'
        inf = max(1, round(steps * 0.25)) if inference is None else max(0, int(inference))
        with _lock(store):
            record = _record(store)
            account = account_of(worker)
            record['items'].append([round(now, 1), steps, int(weighted), purpose, account, inf,
                                    max(0, int(errors or 0))])
            _h48_add(record, account, steps, now)
            if purpose == 'w':
                _hw_add(record, account, steps, now)
            latched = _latch_step_pause(record, lim, account, now)
            pause_s = lim['step_pause_s']
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
    if float(record['step_pause'].get(account) or 0) > now:
        return ''
    items = [i for i in record['items'] if (i[4] if len(i) > 4 else '') == account and now - float(i[0]) <= 3600]
    s15 = sum(int(i[1]) for i in items if now - float(i[0]) <= 900)
    s60 = sum(int(i[1]) for i in items)
    i60 = sum(_item_val(i, 5) for i in items)
    if s15 < lim['step_hard_15m'] and s60 < lim['step_hard_60m'] and i60 < lim['inf_hard_60m']:
        return ''
    record['step_pause'][account] = now + lim['step_pause_s']
    # The counted steps stay: after the pause the 15 min window is empty again, the 60 min net still counts.
    return '%d steps in 15 min, %d in 60 min, %d AI steps in 60 min' % (s15, s60, i60)


def count_new(store, worker, steps, counted, now=None):
    """Stream helper: counts len(steps) - counted new steps; returns the new counted value."""
    try:
        n = len(steps or [])
    except TypeError:
        return counted
    if n > counted:
        new = [s for s in list(steps)[counted:n] if isinstance(s, dict)]
        types = [str(s.get('type') or '').lower() for s in new]
        inference = sum(1 for t in types if 'inference' in t) if any(types) else None
        errors = sum(1 for t in types if 'error' in t)
        add_steps(store, worker, n - counted, now, inference=inference, errors=errors)
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
            snap = _snapshot(record, account, now)
            snap['worker'] = str((worker or {}).get('id') or '')
            record['hits'] = (record['hits'] + [snap])[-30:]
            _save(store, record, now)
        try:
            store.event('provider_rate_limited', account_id=account, worker_id=str((worker or {}).get('id') or ''),
                        seconds=lim['provider_cooldown_s'], detail=str(text or '')[:200], snapshot=snap,
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
    i60 = sum(_item_val(i, 5) for i in items)
    e60 = sum(_item_val(i, 6) for i in items)
    starts = [float(s[0]) for s in record['starts'] if (s[1] if len(s) > 1 else '') == account]
    starts5 = sum(1 for t in starts if now - t <= 300)
    rest = record['cw_rest'].get(account) or {}
    out = dict(account=account, steps_15m=s15, steps_60m=s60, weighted_15m=w15, weighted_60m=w60,
               ai_steps_60m=i60, errors_60m=e60, cw_streak_h=_cw_streak(record, account, now, lim),
               cw_rest_until=float(rest.get('until') or 0),
               starts_5m=starts5, last_start=max(starts or [0]), provider_until=until, limits=lim,
               steps_48h=s48, soft_48h=s48 >= lim['step_soft_48h'], hard_48h=s48 >= lim['step_hard_48h'])
    if s15 >= lim['step_hard_15m'] or s60 >= lim['step_hard_60m'] or i60 >= lim['inf_hard_60m'] \
            or w15 >= lim['wsteps_hard_15m'] or w60 >= lim['wsteps_hard_60m']:
        # wait until the oldest counted item of the 15 min window drops out, at least 60 s
        out.update(level='pause', wait=60,
                   reason='step limit: %d steps / %.0fM read in 15 min, %d steps / %d AI steps / %.0fM in 60 min'
                          % (s15, w15 / 1e6, s60, i60, w60 / 1e6))
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
    if st.get('hard_48h'):
        return 900, '48 h step budget used: %d of %d steps, no new coworker turns (the Main goes on)' % (
            int(st.get('steps_48h') or 0), lim['step_hard_48h'])
    rest_until = float(st.get('cw_rest_until') or 0)
    if rest_until > now:
        return int(rest_until - now) + 1, 'coworker rest after %d h nonstop (looks like automation to Notion; ' \
            'the Main goes on)' % lim['cw_streak_h']
    if int(st.get('cw_streak_h') or 0) >= lim['cw_streak_h']:
        _start_rest(store, account, now, lim)
        return lim['cw_rest_s'], 'coworkers ran %d h nonstop: %d min rest (the Main goes on)' % (
            int(st['cw_streak_h']), lim['cw_rest_s'] // 60)
    if int(st.get('errors_60m') or 0) >= lim['err_hard_60m']:
        return lim['err_wait_s'], '%d error steps in 60 min (max %d): no new coworker turns for now' % (
            int(st['errors_60m']), lim['err_hard_60m'])
    running = running_turns(store, account, str((worker or {}).get('id') or ''))
    max_par = max(1, lim['max_parallel_turns'] // 2) if st.get('soft_48h') else lim['max_parallel_turns']
    if running >= max_par:
        return 30, '%d coworker turns already run on this account (max %d%s)' % (
            running, max_par, ', 48 h budget almost used' if st.get('soft_48h') else '')
    if task is not None:
        need = min(estimate(store, task) * (running + 1), lim['step_hard_15m'] // 2)
        used = int(st.get('steps_15m') or 0)
        if used + need > lim['step_hard_15m']:
            return 60, 'step budget: %d used + ~%d expected > %d per 15 min' % (used, need, lim['step_hard_15m'])
        # steady instead of waves: keep 10 % of the 60 min budget free so no all-chat stop is hit
        used60 = int(st.get('steps_60m') or 0)
        if used60 + need > lim['step_hard_60m'] * 0.9:
            return 120, 'step budget: %d used + ~%d expected > 90 %% of %d per 60 min' % (
                used60, need, lim['step_hard_60m'])
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


def seed_h48(store, account, buckets, now=None):
    """Fill the 48 h counter from history (hour start -> steps); per hour the larger value wins."""
    now = time.time() if now is None else now
    account = str(account or '')
    if not account:
        raise ValueError('account is required')
    with _lock(store):
        record = _record(store)
        merged = dict(record['h48'].get(account) or {})
        for key, steps in (buckets or {}).items():
            hour = str(int(float(key) // 3600 * 3600))
            if now - float(hour) < H48:
                merged[hour] = max(int(merged.get(hour) or 0), int(steps or 0))
        record['h48'][account] = merged
        _save(store, record, now)
        return _h48_sum(record, account, now)


def public(store, now=None):
    now = time.time() if now is None else now
    st = state(store, now)
    return {k: v for k, v in st.items() if k != 'last_start'}
