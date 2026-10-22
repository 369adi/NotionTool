"""Detector for 'Notion AI is disabled for this workspace'.

Runs in the background (every 60 s) and on every /api/state. For each account it remembers the
last seen AI state. On the switch to disabled it writes one report with the usage leading up to it
(sends and estimated tokens over 15 min / 1 h / 6 h / 24 h, peak sends per minute, parallel chats,
errors, the 6 h token budget) and a ranked list of probable causes. Every 15 min it also writes a
usage snapshot per account so the run-up is visible afterwards.

Output: event 'ai_disabled_detected' / 'ai_reenabled_detected' / 'ai_usage_snapshot' in the activity
log, the record kind 'ai_disable_reports' (survives chat deletion) and data/ai_disable_detector.log
(JSON lines). Token numbers are estimates (bytes / 3), never provider numbers. The detector only
observes; it never changes accounts or settings.
"""
import json
import threading
import time
from datetime import datetime
from pathlib import Path

KIND_STATE = 'ai_disable_watch'
KIND_REPORT = 'ai_disable_reports'
INTERVAL = 60
SNAPSHOT_EVERY = 900
LOG_PATH = Path(__file__).resolve().parents[1] / 'data' / 'ai_disable_detector.log'
_log_target = [LOG_PATH]  # scan() points it at the store's data folder (the frozen EXE has no repo data/)
ERROR_HINTS = ('error', 'rate', 'limit', 'stall', 'cooldown', 'blocked', 'failed', 'denied')

_started = False
_start_lock = threading.Lock()
_last_snapshot = 0.0


def _ts(value):
    if isinstance(value, (int, float)):
        return float(value) / (1000.0 if value > 1e12 else 1.0)
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _disabled(account):
    return account.get('state') == 'ai_disabled' or (account.get('ai_access') or {}).get('state') == 'disabled'


def _log(line):
    try:
        _log_target[0].parent.mkdir(parents=True, exist_ok=True)
        with _log_target[0].open('a', encoding='utf-8') as fh:
            fh.write(json.dumps(line, ensure_ascii=False, default=str) + '\n')
    except OSError:
        pass


def _recent_events(store, since):
    try:
        with store.lock:
            rows = store.db.execute('SELECT seq,ts,kind,doc FROM events ORDER BY seq DESC LIMIT 40000').fetchall()
    except Exception:
        return []
    out = []
    for seq, ts, kind, doc in rows:
        t = _ts(ts)
        if t and t < since:
            break
        try:
            doc = json.loads(doc) if isinstance(doc, str) else (doc or {})
        except ValueError:
            doc = {}
        out.append((t, kind, doc))
    return out


def _context_cap(store):
    try:
        from .token_budget import context_cap
        return int(context_cap(store))
    except Exception:
        return 200_000


SIGNAL_WINDOW = 1800
SIGNAL_TEXT = ('aiNotEnabled', 'AiNotEnabled', 'Notion AI is disabled')


def _inference_signal(store, account, now):
    """A recent 'AI not enabled' inference error of one of the account's chats (before the account state flips)."""
    ident = account.get('id')
    mine = {w.get('id') for w in store.all('workers') if w.get('account_id') == ident}
    for t, kind, doc in _recent_events(store, now - SIGNAL_WINDOW):
        if kind.startswith('ai_'):
            continue
        if doc.get('worker_id') in mine or (ident and doc.get('account_id') == ident):
            text = str(doc.get('message') or doc.get('error') or '')
            if any(s in text for s in SIGNAL_TEXT):
                return '%s: %s' % (kind, text[:200])
    return ''


def usage(store, account_id, now=None):
    """Sends, estimated tokens, peaks and errors of one account over several windows."""
    now = time.time() if now is None else now
    workers = {w.get('id'): w for w in store.all('workers')}
    mine = {k for k, w in workers.items() if w.get('account_id') == account_id}
    events = _recent_events(store, now - 86400)
    windows = {'15m': 900, '1h': 3600, '6h': 21600, '24h': 86400}
    cap = _context_cap(store)
    result = {name: dict(sends=0, prompt_tokens=0, context_tokens=0, errors=0) for name in windows}
    per_minute, chats, errors, last_send = {}, set(), {}, 0.0
    for t, kind, doc in events:
        wid = doc.get('worker_id', '')
        if wid and wid not in mine:
            continue
        if not wid and doc.get('account_id') not in ('', None, account_id):
            continue
        age = now - t
        if kind == 'remote_sent' and wid:
            last_send = max(last_send, t)
            chats.add(wid) if age <= 3600 else None
            per_minute[int(t // 60)] = per_minute.get(int(t // 60), 0) + 1
            ctx = min(int(((workers.get(wid) or {}).get('context_usage') or {}).get('estimated_tokens') or 0), cap)
            for name, span in windows.items():
                if age <= span:
                    result[name]['sends'] += 1
                    result[name]['prompt_tokens'] += int(doc.get('prompt_chars') or 0) // 3
                    result[name]['context_tokens'] += ctx
        elif any(h in kind.lower() for h in ERROR_HINTS):
            if age <= 3600:
                errors[kind] = errors.get(kind, 0) + 1
            for name, span in windows.items():
                if age <= span:
                    result[name]['errors'] += 1
    for row in result.values():
        row['estimated_tokens'] = row['prompt_tokens'] + row['context_tokens']
    return dict(windows=result, peak_sends_per_minute=max(per_minute.values() or [0]),
                parallel_chats_1h=len(chats), errors_1h=errors,
                last_send=last_send or None, seconds_since_last_send=int(now - last_send) if last_send else None)


def probable_causes(account, report, budget):
    """Ranked guesses from what NotionTool can observe. Notion does not report the reason."""
    causes = []
    access = account.get('ai_access') or {}
    reason = ' '.join(str(access.get(k) or '') for k in ('reason', 'message', 'detail', 'error')).strip()
    if reason:
        causes.append(dict(score=0.9, cause='Notion reported: ' + reason[:200]))
    w1, w6, w24 = (report['windows'][k] for k in ('1h', '6h', '24h'))
    peak, parallel = report['peak_sends_per_minute'], report['parallel_chats_1h']
    if peak >= 10 or parallel >= 6:
        causes.append(dict(score=0.75, cause='Automated-usage / abuse protection: %d sends in the busiest minute, '
                           '%d chats in parallel in the last hour' % (peak, parallel)))
    if w6['estimated_tokens'] >= 0.8 * budget.get('limit', 45_000_000) or w24['estimated_tokens'] >= 60_000_000:
        causes.append(dict(score=0.7, cause='Usage / AI credit limit: ~%d tokens in 6 h, ~%d in 24 h (estimate)'
                           % (w6['estimated_tokens'], w24['estimated_tokens'])))
    if report['errors_1h']:
        top = ', '.join('%s x%d' % kv for kv in sorted(report['errors_1h'].items(), key=lambda kv: -kv[1])[:4])
        causes.append(dict(score=0.5, cause='Errors shortly before (rate limits / server errors): ' + top))
    idle = report.get('seconds_since_last_send')
    if idle is None or idle > 1800 or w1['sends'] == 0:
        causes.append(dict(score=0.65, cause='Admin/workspace setting: AI was switched off without heavy traffic right '
                           'before (no send in the last 30 min). Check Settings > Notion AI and the billing plan'))
    else:
        causes.append(dict(score=0.4, cause='Workspace admin turned Notion AI off or the plan/billing changed '
                           '(check Settings > Notion AI)'))
    return sorted(causes, key=lambda c: -c['score'])


def scan(store, now=None):
    """Detect AI-disabled transitions and write snapshots. Never raises."""
    global _last_snapshot
    try:
        now = time.time() if now is None else now
        if getattr(store, 'directory', None):
            _log_target[0] = Path(store.directory) / 'ai_disable_detector.log'
        from .token_budget import public as budget_public
        budget = budget_public(store, now)
        write_snapshot = now - _last_snapshot >= SNAPSHOT_EVERY
        for account in store.all('accounts'):
            ident = str(account.get('id') or '')
            if not ident:
                continue
            who = account.get('email') or account.get('label') or ident
            signal = _inference_signal(store, account, now)
            disabled = _disabled(account) or bool(signal)
            seen = store.get(KIND_STATE, ident) or dict(id=ident, disabled=None)
            if write_snapshot and not disabled:
                snap = usage(store, ident, now)
                line = dict(kind='ai_usage_snapshot', at=now, account=who, account_id=ident,
                            sends_1h=snap['windows']['1h']['sends'],
                            estimated_tokens_1h=snap['windows']['1h']['estimated_tokens'],
                            estimated_tokens_6h=snap['windows']['6h']['estimated_tokens'],
                            peak_sends_per_minute=snap['peak_sends_per_minute'],
                            budget_used_tokens=budget['used'], budget_limit_tokens=budget['limit'])
                _log(line)
            if disabled and seen.get('disabled') is not True:
                report = usage(store, ident, now)
                causes = probable_causes(account, report, budget)
                doc = dict(id='%s-%d' % (ident, int(now)), account_id=ident, account=who, detected_at=now,
                           first_seen_disabled=seen.get('disabled') is None, error=account.get('error', ''),
                           ai_access=account.get('ai_access') or {}, usage=report, budget=budget, causes=causes)
                doc['signal'] = signal or 'account state: ' + str(account.get('state'))
                store.put(KIND_REPORT, doc)
                _log(dict(kind='ai_disabled_detected', at=now, **{k: v for k, v in doc.items() if k != 'id'}))
                w = report['windows']
                store.event('ai_disabled_detected', account_id=ident, account=who,
                            message='Notion AI disabled for %s. Probable cause: %s' % (who, causes[0]['cause']),
                            sends_1h=w['1h']['sends'], sends_6h=w['6h']['sends'],
                            estimated_tokens_1h=w['1h']['estimated_tokens'],
                            estimated_tokens_6h=w['6h']['estimated_tokens'],
                            estimated_tokens_24h=w['24h']['estimated_tokens'],
                            budget_used_tokens=budget['used'],
                            peak_sends_per_minute=report['peak_sends_per_minute'], report_id=doc['id'])
            elif not disabled and seen.get('disabled') is True:
                _log(dict(kind='ai_reenabled_detected', at=now, account=who, account_id=ident))
                store.event('ai_reenabled_detected', account_id=ident, account=who,
                            message='Notion AI is available again for %s' % who)
            if seen.get('disabled') is not disabled:
                seen.update(disabled=disabled, changed_at=now)
                store.put(KIND_STATE, seen)
        if write_snapshot:
            _last_snapshot = now
    except Exception as exc:  # observing must never break the app
        _log(dict(kind='detector_error', at=time.time(), error=repr(exc)[:300]))


def reports(store, limit=10):
    return sorted(store.all(KIND_REPORT), key=lambda r: -float(r.get('detected_at') or 0))[:limit]


def start(store):
    """Start the background loop once per process."""
    global _started
    with _start_lock:
        if _started:
            return
        _started = True

    def loop():
        while True:
            scan(store)
            time.sleep(INTERVAL)

    threading.Thread(target=loop, name='ai-disable-detector', daemon=True).start()
