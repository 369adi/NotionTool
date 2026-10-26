"""Live status of a chat turn, read from its Notion thread record. Report only (rule I5).

Owner 2026-10-24 (RBX chat): a long 'mach weiter' turn (46-96 steps, ~1.5 min each) only shows Notion's
loading state, so one cannot tell whether the chat thinks or hangs. While a turn runs, the record has
latest_user_or_trigger_time > last_turn_outcome.completed_time, a current_inference_id and a 30 min lease;
every new step adds a message id. The time since the message count last grew tells working from stuck.
Never stops, cancels or resends anything.
"""
import time

WAIT_AFTER_S = 150   # no new step for 2.5 min: a long tool call or a long think
STUCK_AFTER_S = 480  # no new step for 8 min: very probably stuck
DONE_SHOW_S = 600    # 'done' stays visible 10 min
POLL_S = 15


def _sec(value):
    try:
        return float(value or 0) / 1000.0
    except (TypeError, ValueError):
        return 0.0


def dur(sec):
    sec = int(max(0, sec))
    if sec < 60:
        return '%d s' % sec
    if sec < 3600:
        return '%d min' % (sec // 60)
    return '%d h %d min' % (sec // 3600, sec % 3600 // 60)


def key(thread_id):
    return str(thread_id or '').replace('-', '').lower()


def live_status(record, now=None, steps=None, last_step_at=None):
    """{state, text, title, ...} for one thread record; state is working|waiting|stuck|done|idle."""
    if not isinstance(record, dict):
        return None
    now = time.time() if now is None else now
    data = record.get('data') if isinstance(record.get('data'), dict) else {}
    last = data.get('last_turn_outcome') if isinstance(data.get('last_turn_outcome'), dict) else {}
    started = _sec(record.get('latest_user_or_trigger_time') or data.get('latest_user_or_trigger_time'))
    completed = _sec(last.get('completed_time'))
    lease = _sec(record.get('current_inference_lease_expiration'))
    if not started or started <= completed:
        if completed and now - completed < DONE_SHOW_S:
            n = last.get('step_count')
            text = 'Done %s ago' % dur(now - completed) + (' \u00b7 %s steps' % n if n else '')
            return dict(state='done', text=text, title=text, since_s=0, idle_s=0, steps=n)
        return dict(state='idle', text='', title='', since_s=0, idle_s=0, steps=None)
    step_at = last_step_at or _sec(record.get('updated_time')) or started
    since = now - started
    idle = max(0.0, now - max(step_at, started))
    part = (' \u00b7 %d steps' % steps) if isinstance(steps, int) and steps > 0 else ''
    if lease and lease < now:
        state, text = 'stuck', 'Stuck: Notion dropped the turn (lease ran out)'
    elif idle >= STUCK_AFTER_S:
        state, text = 'stuck', 'No new step for %s \u00b7 probably stuck' % dur(idle)
    elif idle >= WAIT_AFTER_S:
        state, text = 'waiting', 'Waiting %s for the next step (long tool call or think)' % dur(idle)
    else:
        state, text = 'working', 'Working %s%s \u00b7 last step %s ago' % (dur(since), part, dur(idle))
    title = ('Turn running for %s, last new step %s ago%s. Yellow after %s without a step, red after %s.'
             % (dur(since), dur(idle), part, dur(WAIT_AFTER_S), dur(STUCK_AFTER_S)))
    return dict(state=state, text=text, title=title, since_s=int(since), idle_s=int(idle), steps=steps)


class ChatLiveWatch:
    """Keeps per thread the turn marker, the message count at turn start and when the count last grew."""

    def __init__(self):
        self.seen = {}
        self.status = {}

    def update(self, records, now=None):
        now = time.time() if now is None else now
        out = {}
        for tid, rec in (records or {}).items():
            if not isinstance(rec, dict):
                continue
            k = key(rec.get('id') or tid)
            marker = str(rec.get('latest_user_or_trigger_time') or '')
            count = len(rec.get('messages') or [])
            prev = self.seen.get(k)
            if not prev or prev['marker'] != marker:
                # new turn (or first sight): count steps from here; first sight trusts Notion's updated_time
                prev = dict(marker=marker, base=count, count=count,
                            changed=(now if prev else (_sec(rec.get('updated_time')) or now)))
            elif count > prev['count']:
                prev = dict(prev, count=count, changed=now)
            self.seen[k] = prev
            st = live_status(rec, now, steps=prev['count'] - prev['base'], last_step_at=prev['changed'])
            if st and st['state'] != 'idle':
                out[k] = st
        self.status = out
        return out
