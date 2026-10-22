"""Account health: repeated Notion turn errors on one account cool the whole account down for longer.

A single 'temporarily-unavailable' turn error keeps the usual short cooldown. When the same account fails
again within WINDOW_S, the cooldown grows (2, 5, 10, then 20 min) so new tasks go to other ready accounts
instead of failing on the same account again and again (seen live: three reviewer attempts in a row on one
account, the run blocked although another account had ten free coworkers).
"""
import time

WINDOW_S = 600
BASE_S = 20
STEPS = (20, 120, 300, 600, 1200)


def account_of(store, worker_id):
    worker_id = str(worker_id or '')
    worker = store.get('workers', worker_id, {}) if worker_id else {}
    return (worker or {}).get('account_id') or worker_id.split(':')[0]


def turn_error_cooldown(store, account_id, now=None):
    """Record one turn error on the account and return the cooldown in seconds; never raises."""
    now = time.time() if now is None else now
    try:
        account = store.get('accounts', account_id, {}) if account_id else {}
        if not account:
            return BASE_S
        recent = [float(t) for t in (account.get('turn_errors') or []) if now - float(t) < WINDOW_S]
        recent.append(now)
        account['turn_errors'] = recent[-10:]
        seconds = STEPS[min(len(recent), len(STEPS)) - 1]
        if seconds > BASE_S:
            account['cooldown_until'] = max(float(account.get('cooldown_until') or 0), now + seconds)
        store.put('accounts', account)
        if seconds > BASE_S:
            store.event('account_degraded', '', account_id=account_id, errors=len(recent), seconds=seconds,
                        message='%d Notion turn errors within %d min on this account; it pauses for %d s and '
                                'new tasks go to other ready accounts' % (len(recent), WINDOW_S // 60, seconds))
        return seconds
    except Exception:
        return BASE_S
