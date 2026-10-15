"""Read-only compact peek into data\\notiontool.db.

Usage: peek_run.py                  -> object counts, maintenance job records, last runs
       peek_run.py RUN [SINCE] [SEQ]  -> run, its tasks, open or recent context checkpoints, replies, events after SEQ
"""
import json
import sqlite3
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
DB = __import__('os').path.join(__import__('os').path.dirname(__import__('os').path.dirname(__import__('os').path.abspath(__file__))), 'data', 'notiontool.db')
db = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
TITLE = 'Maintain Tailscale MCP and account connections'


def docs(kind):
    return [json.loads(d) for (d,) in db.execute('SELECT doc FROM objects WHERE kind=? ORDER BY rowid', (kind,))]


def short(value, size=140):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= size else text[:size] + '...'


if len(sys.argv) < 2:
    print('objects', dict(db.execute('SELECT kind, COUNT(*) FROM objects GROUP BY kind').fetchall()))
    jobs = [j for j in docs('jobs') if j.get('title') == TITLE]
    print('maintenance job records', len(jobs), [(j['id'], j['state']) for j in jobs[-2:]])
    print('maintenance done events', db.execute("SELECT COUNT(*) FROM events WHERE kind='operation_finished' AND json_extract(doc,'$.message')=? AND json_extract(doc,'$.state')='done'", (TITLE,)).fetchone()[0])
    print('max event seq', db.execute('SELECT MAX(seq) FROM events').fetchone()[0])
    for run in docs('runs')[-3:]:
        print('run', run['id'], run.get('state'), short(run.get('error') or '', 100), run.get('request_id', ''))
    sys.exit(0)

run_id = sys.argv[1]
since = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
after = int(sys.argv[3]) if len(sys.argv) > 3 else None
run = next((r for r in docs('runs') if r['id'] == run_id), None)
if run:
    print('run', {k: run.get(k) for k in ('state', 'stage', 'error', 'request_id', 'chat_id') if run.get(k)})
for task in docs('tasks'):
    own = task.get('run_id') == run_id
    recent = max(task.get('sent_at') or 0, task.get('created') or 0) >= since
    checkpoint = task.get('run_id') == '__context__' and (task.get('state') != 'done' or recent)
    if not (own or checkpoint):
        continue
    print('task', task['id'][:16], task.get('role'), short(task.get('title') or task.get('name') or '', 24), task.get('state'),
          (task.get('worker_id') or '')[-12:], 'sent', int(task.get('sent_at') or 0), 'att', task.get('attempts'),
          'mv', short(task.get('model_verification') or '', 50), 'err', short(task.get('remote_error') or task.get('error') or '', 60),
          'res', repr(short(task.get('result') or '', 120)))
for message in docs('messages'):
    if message.get('run_id') == run_id and message.get('role') != 'user':
        print('message', message.get('role'), repr(short(message.get('text') or '', 300)))
if after is not None:
    for seq, kind, doc in db.execute('SELECT seq, kind, doc FROM events WHERE seq>? ORDER BY seq', (after,)):
        print('event', seq, kind, short(doc, 150))
