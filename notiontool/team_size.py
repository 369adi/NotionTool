"""Coworker team size per Notion account, taken from the settings.

The desktop settings modal (Mains & Coworkers) saves 'per_account' ('Coworkers per account') and
'max_workers' ('Maximum coworkers', the global cap) through /api/settings. The team size is
N = min(per_account, max_workers), clamped to 1..40. ensure_team keeps the roles worker1..workerN;
lowering N never deletes a chat: extra ledger roles stay recorded but are no longer managed, and the
run capacity counts at most N coworkers per account.
"""
import re

LOW, HIGH = 1, 40
DEFAULT_PER_ACCOUNT = 10
ROLE_RE = re.compile(r'^worker([0-9]+)$')


def _num(settings, key, default):
    try:
        return int((settings or {}).get(key, default))
    except (TypeError, ValueError):
        return default


def team_size(settings):
    """N: how many Welcome Coworkers one account keeps."""
    return max(LOW, min(HIGH, _num(settings, 'per_account', DEFAULT_PER_ACCOUNT), _num(settings, 'max_workers', HIGH)))


def team_roles(settings):
    return ['worker%d' % i for i in range(1, team_size(settings) + 1)]


def ledger_roles(ledger):
    """Every workerK role recorded in an account ledger, in number order (also roles above N)."""
    found = []
    for key in (ledger or {}):
        match = ROLE_RE.match(str(key))
        if match:
            found.append((int(match.group(1)), key))
    return [key for _, key in sorted(found)]


def coworker_capacity(settings, workers):
    """Parallel coworkers a run may use: at most N per account, at most max_workers overall."""
    size = team_size(settings)
    counts = {}
    for worker in workers:
        ident = worker.get('account_id', '')
        counts[ident] = counts.get(ident, 0) + 1
    total = sum(min(size, count) for count in counts.values())
    return max(0, min(_num(settings, 'max_workers', HIGH), total))


def team_accounts(accounts):
    """Ready accounts that already have a managed Coworker team (a settings change only resizes these)."""
    return [a['id'] for a in accounts if a.get('state') == 'ready' and ledger_roles(a.get('team_threads'))]
