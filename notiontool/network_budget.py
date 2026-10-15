"""One request-start budget across accounts; never rotate identities to evade limits."""
import email.utils
import threading
import time


def retry_seconds(value, now=None):
    now = time.time() if now is None else now
    try:
        return max(0, float(value))
    except (TypeError, ValueError):
        try:
            return max(0, email.utils.parsedate_to_datetime(value).timestamp() - now)
        except (TypeError, ValueError, OverflowError):
            return 0


class NetworkBudget:
    def __init__(self, store=None, clock=time.time, sleep=time.sleep):
        self.store, self.clock, self.sleep = store, clock, sleep
        self.lock = threading.Lock()
        self.interval = .25  # Ramp up at at most four request starts per second.
        self.next_start = 0
        saved = store.get('settings', 'network_budget', {}) if store else {}
        self.interval = max(.25,min(2,saved.get('interval',.25)))
        self.blocked_until = saved.get('blocked_until', 0)
        self.strikes = saved.get('strikes', 0)
        self.last_limited = saved.get('last_limited', 0)

    def acquire(self, cancel=None):
        # Sleep outside the lock, recheck on waking: a newer 429 invalidates
        # already queued reservations. Long-lived streams remain concurrent.
        while True:
            if cancel is not None and cancel.is_set():
                raise ValueError('Assignment cancelled before submission')
            with self.lock:
                now = self.clock()
                wait = max(self.next_start, self.blocked_until) - now
                if wait <= 0:
                    self.next_start = now + self.interval
                    return
            self.sleep(min(wait, 1))

    def limited(self, retry_after=None):
        with self.lock:
            now = self.clock()
            if now - self.last_limited > 600:
                self.strikes = 0
            self.strikes = min(self.strikes + 1, 5)
            delay = max(retry_seconds(retry_after, now), min(300, 30 * 2 ** (self.strikes - 1)))
            self.blocked_until = max(self.blocked_until, now + delay)
            self.last_limited = now
            self.interval = min(2, self.interval * 2)
            if self.store:
                self.store.put('settings', dict(id='network_budget', interval=self.interval, blocked_until=self.blocked_until,
                                               strikes=self.strikes, last_limited=now))
                self.store.event('network_rate_limited', message='All slots paused for the shared Notion cooldown', seconds=delay)
            return delay


SHARED_BUDGET = NetworkBudget()
