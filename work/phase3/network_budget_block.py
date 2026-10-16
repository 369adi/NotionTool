PRIORITIES = {'critical': 0, 'verify': 1, 'rest': 2}
BASE_INTERVAL = .25            # at most four request starts per second
MAX_INTERVAL = 2.0
BREAKER_FAILURES = 5           # consecutive transient failures of one endpoint open its circuit
BREAKER_BASE_SECONDS = 30.0
BREAKER_MAX_SECONDS = 300.0
TRIAL_TIMEOUT = 120.0          # a half-open trial that never reports back frees the slot again
TRIAL_BUSY_SECONDS = 5.0
LATENCY_FACTOR = 2.0           # recent latency above twice the baseline ...
LATENCY_MIN_RISE = 1.0         # ... and at least one second slower lowers parallelism
LATENCY_CUT_EVERY = 10.0
SAVE_EVERY = 30.0

_local = threading.local()


def backoff(attempt, base=1.0, cap=300.0, floor=0.0, jitter=.5, rng=random.random):
    """Exponential delay with upward jitter, capped; never below floor (e.g. a server Retry-After)."""
    delay = base * 2 ** min(30, max(0, int(attempt)))
    return max(floor, min(cap, delay * (1 + jitter * rng())))


@contextlib.contextmanager
def request_priority(level):
    """Requests this thread starts inside the block use the level (critical > verify > rest)."""
    previous = getattr(_local, 'priority', None)
    _local.priority = level if level in PRIORITIES else 'rest'
    try:
        yield
    finally:
        _local.priority = previous


def current_priority():
    return getattr(_local, 'priority', None) or 'rest'


def run_with_priority(level, fn, *args, **kwargs):
    with request_priority(level):
        return fn(*args, **kwargs)


class RemotePaused(Exception):
    """ACCOUNT_BLOCKED: every remote call is refused locally until a manual resume."""


class CircuitOpen(Exception):
    """One endpoint failed repeatedly; calls to it are refused locally until its cooldown ends."""

    def __init__(self, source, seconds):
        self.source = source
        self.seconds = max(1.0, float(seconds))
        super().__init__(f'Circuit open for {source or "the Notion endpoint"} after repeated failures; '
                         f'nothing was sent, next trial in {self.seconds:.0f} s')


class Ticket:
    """One granted request start; hand it back to NetworkBudget.release()."""
    __slots__ = ('source', 'level', 'started', 'trial')

    def __init__(self, source='', level=2, started=0.0, trial=False):
        self.source, self.level, self.started, self.trial = source, level, started, trial


class NetworkBudget:
    """Process-wide request coordinator shared by all accounts (one I11 window).

    acquire() grants starts in priority order inside the hard cap; release() feeds
    the per-endpoint circuit breakers and the adaptive parallelism. Lock order is
    save_lock -> lock, and the store is never touched while lock is held.
    """

    def __init__(self, store=None, clock=time.time, sleep=time.sleep, rng=random.random):
        self.store, self.clock, self.sleep, self.rng = store, clock, sleep, rng
        self.lock = threading.Lock()
        self.save_lock = threading.Lock()
        self.next_start = 0
        saved = (store.get('settings', 'network_budget', {}) if store else {}) or {}
        self.interval = max(BASE_INTERVAL, min(MAX_INTERVAL, saved.get('interval', BASE_INTERVAL)))
        self.blocked_until = saved.get('blocked_until', 0)
        self.strikes = saved.get('strikes', 0)
        self.last_limited = saved.get('last_limited', 0)
        self.limit_per_minute = clamp_limit(saved.get('limit_per_minute', HARD_LIMIT_PER_MINUTE))
        # ACCOUNT_BLOCKED survives restarts; only resume() lifts it and nothing probes the account.
        paused = saved.get('paused')
        self.paused = paused if isinstance(paused, dict) else (dict(reason='saved pause') if paused else None)
        self.starts = collections.deque(self._load_window())  # start times inside the sliding window (I11)
        self.window_waits = 0
        self.waiting = set()
        self.sequence = itertools.count()
        self.breakers = {}
        self.parallel = None  # adaptive per-account task cap; None means the configured value
        self.parallel_max = None
        self.successes = 0
        self.last_latency_cut = 0
        self.saved_interval = self.interval
        self.last_saved = 0

    def _load_window(self):
        # Starts of the last 60 s from the previous process: a restart must not reset I11.
        if not self.store:
            return []
        now = self.clock()
        try:
            saved = self.store.get('settings', 'network_window', {}) or {}
            starts = sorted(float(t) for t in saved.get('starts') or [])
        except Exception:
            return [now] * self.limit_per_minute  # unreadable: assume the window is full
        return [t for t in starts if now - t < WINDOW_SECONDS][-HARD_LIMIT_PER_MINUTE:]

    #@TRIM@

    #@WINDOW_COUNT@

    def acquire(self, cancel=None, priority=None, source=''):
        # Sleep outside the lock, recheck on waking: a newer 429 invalidates
        # already queued reservations. Long-lived streams remain concurrent.
        # I11: never more than limit_per_minute starts in any sliding 60 s
        # window; surplus callers wait here and are never let through early.
        # Phase 3: waiting callers start by priority (critical > verify > rest,
        # FIFO inside a level). A pause or an open circuit refuses before any
        # request leaves; such refusals use no window slot.
        level = PRIORITIES.get(priority or current_priority(), PRIORITIES['rest'])
        held_since = None
        with self.lock:
            self._refuse(source, self.clock())
            mine = (level, next(self.sequence))
            self.waiting.add(mine)
        try:
            while True:
                if cancel is not None and cancel.is_set():
                    raise ValueError('Assignment cancelled before submission')
                deferred = False
                with self.lock:
                    now = self.clock()
                    self._refuse(source, now)
                    self._trim(now)
                    wait = max(self.next_start, self.blocked_until) - now
                    if len(self.starts) >= self.limit_per_minute:
                        wait = max(wait, self.starts[0] + WINDOW_SECONDS - now)
                        if held_since is None:
                            held_since = now
                            self.window_waits += 1
                    if wait <= 0 and min(self.waiting) != mine:
                        wait, deferred = .05, True  # an older or more important caller goes first
                    if wait <= 0:
                        self.next_start = now + self.interval
                        self.starts.append(now)
                        self.waiting.discard(mine)
                        ticket = Ticket(source, level, now, self._begin_trial(source, now))
                        break
                self.sleep(min(wait, 1))
                if deferred:
                    time.sleep(0)  # let the caller ahead of us take its slot
        except BaseException:
            with self.lock:
                self.waiting.discard(mine)
            raise
        self._save_window()
        if held_since is not None and self.store:
            try:
                self.store.event('request_window_wait', message='Request start held by the per-minute cap',
                                 seconds=round(self.clock() - held_since, 3), limit=self.limit_per_minute)
            except Exception:
                pass
        return ticket

    def _refuse(self, source, now):
        # Fail fast, without any network call, while paused or while this endpoint's circuit is open.
        if self.paused:
            raise RemotePaused('All remote Notion calls are paused: account blocked ('
                               + str(self.paused.get('reason') or 'sign-in required')
                               + '). Sign in again, then resume manually; nothing was sent.')
        breaker = self.breakers.get(source)
        if not breaker or breaker['state'] == 'closed':
            return
        if breaker['state'] == 'open':
            if now < breaker['until']:
                raise CircuitOpen(source, breaker['until'] - now)
            breaker.update(state='half_open', trial_at=0)
        if breaker['trial_at'] and now - breaker['trial_at'] < TRIAL_TIMEOUT:
            raise CircuitOpen(source, TRIAL_BUSY_SECONDS)  # one trial request at a time

    def _begin_trial(self, source, now):
        breaker = self.breakers.get(source)
        if breaker and breaker['state'] == 'half_open':
            breaker['trial_at'] = now
            return True
        return False

    def _save_window(self):
        # Written before the request leaves, so a crash or restart never loses a start (I11).
        if not self.store:
            return
        with self.save_lock:
            with self.lock:
                starts = list(self.starts)[-HARD_LIMIT_PER_MINUTE:]
            try:
                self.store.put('settings', dict(id='network_window', starts=starts))
            except Exception:
                pass

    def _save_settings(self):
        if not self.store:
            return
        with self.save_lock:
            with self.lock:
                doc = dict(id='network_budget', interval=self.interval, blocked_until=self.blocked_until,
                           strikes=self.strikes, last_limited=self.last_limited,
                           limit_per_minute=self.limit_per_minute, paused=self.paused)
                self.saved_interval, self.last_saved = self.interval, self.clock()
            self.store.put('settings', doc)

    def _emit(self, events):
        if not self.store:
            return
        for kind, fields in events:
            try:
                self.store.event(kind, **fields)
            except Exception:
                pass

    def release(self, ticket, outcome='ok', status=0, latency=None, account_id=''):
        """Report how a started request ended: ok, failed, rate_limited, blocked, invalid or neutral."""
        if not isinstance(ticket, Ticket):
            return
        try:
            events, blocked, save = self._record(ticket, outcome, status or 0, latency)
            if save:
                self._save_settings()
            self._emit(events)
            if blocked:
                self.pause(f'HTTP {status}' if status else 'sign-in required', account_id=account_id, status=status or 0)
        except Exception:
            pass  # coordination bookkeeping must never break a request

    def _record(self, ticket, outcome, status, latency):
        events, save = [], False
        with self.lock:
            now = self.clock()
            breaker = self.breakers.setdefault(ticket.source, dict(
                state='closed', failures=0, trips=0, until=0, opened_at=0, trial_at=0,
                latency_base=None, latency_recent=None))
            if ticket.trial:
                breaker['trial_at'] = 0
            if outcome == 'ok':
                breaker['failures'] = 0
                if breaker['state'] == 'half_open':
                    breaker.update(state='closed', trips=0)
                    events.append(('circuit_closed', dict(
                        message='Circuit closed: the trial request succeeded', source=ticket.source,
                        open_seconds=round(now - breaker['opened_at'], 3))))
                events += self._succeeded(now, breaker, latency)
            elif outcome in ('failed', 'rate_limited'):
                events += self._lower_parallel('transient_error' if outcome == 'failed' else 'rate_limited')
                trip = False
                if outcome == 'failed' and breaker['state'] == 'closed':
                    breaker['failures'] += 1
                    trip = breaker['failures'] >= BREAKER_FAILURES
                elif outcome == 'failed' and breaker['state'] == 'half_open' and ticket.trial:
                    trip = True
                if trip:
                    breaker['trips'] += 1
                    seconds = backoff(breaker['trips'] - 1, BREAKER_BASE_SECONDS, BREAKER_MAX_SECONDS,
                                      jitter=.25, rng=self.rng)
                    events.append(('circuit_open', dict(
                        message='Circuit opened: this Notion endpoint failed repeatedly; calls to it are refused locally',
                        source=ticket.source, failures=max(1, breaker['failures']), seconds=round(seconds, 3),
                        trips=breaker['trips'], status=status)))
                    breaker.update(state='open', until=now + seconds, opened_at=now, failures=0)
            if self.interval != self.saved_interval and (
                    self.interval == BASE_INTERVAL or now - self.last_saved >= SAVE_EVERY):
                save = True
                events.append(('request_coordination_adjusted', dict(
                    message='Request start interval lowered after a minute without rate limits',
                    reason='interval_recovered', interval_before=self.saved_interval, interval=self.interval)))
        return events, outcome == 'blocked', save

    def _parallel_now(self):
        return self.parallel if self.parallel is not None else self.parallel_max

    def _lower_parallel(self, reason):
        # Multiplicative decrease: halve the per-account task parallelism, never below one.
        self.successes = 0
        current = self._parallel_now()
        if not current or current <= 1:
            return []
        self.parallel = max(1, (current + 1) // 2)
        return [('request_coordination_adjusted', dict(
            message='Parallel tasks lowered after a transient Notion error', reason=reason,
            parallel_before=current, parallel=self.parallel))]

    def _succeeded(self, now, breaker, latency):
        events = []
        if latency is not None:
            latency = float(latency)
            base, recent = breaker['latency_base'], breaker['latency_recent']
            recent = latency if recent is None else recent * .7 + latency * .3
            base = latency if base is None else base * .95 + latency * .05
            breaker.update(latency_base=base, latency_recent=recent)
            current = self._parallel_now()
            if (recent > base * LATENCY_FACTOR and recent - base > LATENCY_MIN_RISE and current and current > 1
                    and now - self.last_latency_cut >= LATENCY_CUT_EVERY):
                self.last_latency_cut, self.successes = now, 0
                self.parallel = current - 1
                events.append(('request_coordination_adjusted', dict(
                    message='Parallel tasks lowered after rising Notion latency', reason='latency',
                    parallel_before=current, parallel=self.parallel, latency=round(recent, 3),
                    baseline=round(base, 3))))
                return events
        # Additive increase: one more parallel task after enough successes in a row.
        self.successes += 1
        current = self._parallel_now()
        if current and self.parallel_max and current < self.parallel_max and self.successes >= max(3, current):
            raised = current + 1
            self.parallel = None if raised >= self.parallel_max else raised
            self.successes = 0
            events.append(('request_coordination_adjusted', dict(
                message='Parallel tasks raised after successful requests', reason='recovered',
                parallel_before=current, parallel=raised)))
        # The start interval comes back down once a full minute passed without a 429.
        if self.interval > BASE_INTERVAL and now - self.last_limited >= WINDOW_SECONDS:
            self.interval = max(BASE_INTERVAL, round(self.interval * .9, 4))
        return events

    def parallel_limit(self, configured):
        """Adaptive per-account task parallelism; never above the configured value."""
        configured = max(1, int(configured or 1))
        with self.lock:
            self.parallel_max = configured
            if self.parallel is None or self.parallel >= configured:
                return configured
            return max(1, self.parallel)

    def limited(self, retry_after=None):
        with self.lock:
            now = self.clock()
            if now - self.last_limited > 600:
                self.strikes = 0
            self.strikes = min(self.strikes + 1, 5)
            # Jittered so callers do not return together; never below the server's Retry-After.
            delay = backoff(self.strikes - 1, 30, 300, floor=retry_seconds(retry_after, now), jitter=.25, rng=self.rng)
            self.blocked_until = max(self.blocked_until, now + delay)
            self.last_limited = now
            before = self.interval
            self.interval = min(MAX_INTERVAL, self.interval * 2)
            interval = self.interval
        if self.store:
            self._save_settings()
            self.store.event('network_rate_limited', message='All slots paused for the shared Notion cooldown', seconds=delay)
            self.store.event('request_coordination_adjusted', message='Request start interval adjusted after a rate limit',
                             interval_before=before, interval=interval, paused_seconds=delay)
        return delay

    def pause(self, reason, account_id='', status=0):
        """ACCOUNT_BLOCKED: refuse every remote call until resume(); no probe calls are made."""
        with self.lock:
            if self.paused:
                return False
            paused = dict(reason=str(reason or 'sign-in required')[:300], account_id=account_id or '',
                          status=status or 0, since=self.clock())
            self.paused = paused
        self._save_settings()
        self._emit([('remote_paused', dict(
            message='All remote Notion calls paused: account blocked. Sign in again, then resume manually.',
            reason=paused['reason'], account_id=paused['account_id'], status=paused['status'],
            error_class='ACCOUNT_BLOCKED'))])
        return True

    def resume(self):
        with self.lock:
            paused, self.paused = self.paused, None
            now = self.clock()
        if not paused:
            return False
        self._save_settings()
        self._emit([('remote_resumed', dict(
            message='Remote Notion calls resumed manually', account_id=paused.get('account_id', ''),
            paused_seconds=round(now - paused.get('since', now), 3)))])
        return True

    def status(self):
        with self.lock:
            now = self.clock()
            self._trim(now)
            return dict(
                paused=bool(self.paused), pause=dict(self.paused) if self.paused else None,
                limit_per_minute=self.limit_per_minute, window_count=len(self.starts),
                interval=self.interval, cooldown_seconds=round(max(0, self.blocked_until - now), 3),
                parallel=self._parallel_now(), parallel_max=self.parallel_max,
                circuits={source or 'unknown': dict(state=b['state'], trips=b['trips'],
                                                    seconds=round(max(0, b['until'] - now), 3))
                          for source, b in self.breakers.items() if b['state'] != 'closed'})
