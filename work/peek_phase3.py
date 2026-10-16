"""Read-only peek for Phase 3: task events and persisted network window."""
import json
import sqlite3
import sys
import time

TASK = sys.argv[1] if len(sys.argv) > 1 else ''
RUN = sys.argv[2] if len(sys.argv) > 2 else ''
db = sqlite3.connect('file:data/notiontool.db?mode=ro', uri=True)
cols = [r[1] for r in db.execute('PRAGMA table_info(events)')]
print('events columns:', cols)
KINDS = ('remote_turn_failed', 'api_error', 'task_attempt_finished', 'circuit_open', 'circuit_closed',
         'remote_paused', 'remote_resumed', 'request_coordination_adjusted', 'network_rate_limited',
         'request_window_wait', 'remote_sent', 'remote_done', 'stage_changed')
if TASK:
    print('==== events for task', TASK)
    for row in db.execute("SELECT rowid, kind, doc FROM events WHERE doc LIKE ? ORDER BY rowid", ('%' + TASK + '%',)):
        doc = row[2]
        if len(doc) > 700:
            doc = doc[:700] + '...'
        print(row[0], row[1], doc)
print('==== coordination events in the last 15 min')
since = time.time() - 900
for row in db.execute("SELECT rowid, kind, doc FROM events ORDER BY rowid DESC LIMIT 4000"):
    if row[1] not in ('circuit_open', 'circuit_closed', 'remote_paused', 'remote_resumed',
                      'request_coordination_adjusted', 'network_rate_limited', 'request_window_wait'):
        continue
    try:
        d = json.loads(row[2])
    except Exception:
        d = {}
    if float(d.get('ts') or d.get('created') or since) < since:
        continue
    print(row[0], row[1], row[2][:400])
if RUN:
    print('==== run', RUN)
    r = db.execute("SELECT doc FROM objects WHERE kind='run' AND id=?", (RUN,)).fetchone()
    if r:
        d = json.loads(r[0])
        print('state', d.get('state'), 'stage', d.get('stage'), 'error', d.get('error'))
        print('summary', (d.get('summary') or '')[:400])
print('==== settings/network_window')
r = db.execute("SELECT doc FROM objects WHERE kind='settings' AND id='network_window'").fetchone()
if r:
    starts = json.loads(r[0]).get('starts') or []
    now = time.time()
    print('saved starts', len(starts), 'in last 60 s', sum(1 for s in starts if now - s < 60),
          'newest age s', round(now - max(starts), 1) if starts else None)
    per_min = {}
    for s in starts:
        per_min[int(s // 60)] = per_min.get(int(s // 60), 0) + 1
    print('max per calendar minute', max(per_min.values()) if per_min else 0)
else:
    print('no network_window doc')
r = db.execute("SELECT doc FROM objects WHERE kind='settings' AND id='network_budget'").fetchone()
print('network_budget', r[0][:400] if r else None)
