"""Read-only: events since a local time (HH:MM today), kind counts plus the matching tail.
Usage: python work\peek_events.py 16:05 [substring] [tail]"""
import collections, datetime, json, sqlite3, sys

hh, mm = (int(x) for x in sys.argv[1].split(':'))
needle = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] != '-' else ''
tail = int(sys.argv[3]) if len(sys.argv) > 3 else 60
since = datetime.datetime.now().replace(hour=hh, minute=mm, second=0, microsecond=0).timestamp()
db = sqlite3.connect('file:data/notiontool.db?mode=ro', uri=True)
rows = db.execute('select seq, ts, run_id, kind, doc from events where ts>? order by seq', (since,)).fetchall()
counts = collections.Counter(r[3] for r in rows)
print('events', len(rows), 'since', sys.argv[1])
for kind, n in counts.most_common():
    print(f'  {n:5d} {kind}')
hits = [r for r in rows if not needle or needle in (r[3] + ' ' + (r[4] or ''))]
print('--- tail', min(tail, len(hits)), 'of', len(hits), 'matching', repr(needle))
for seq, ts, run_id, kind, doc in hits[-tail:]:
    stamp = datetime.datetime.fromtimestamp(ts).strftime('%H:%M:%S')
    try:
        d = json.loads(doc or '{}')
    except ValueError:
        d = {'raw': doc}
    for k in ('seq', 'ts', 'kind', 'run_id'):
        d.pop(k, None)
    print(stamp, seq, kind, run_id[:10], json.dumps(d, ensure_ascii=False)[:260])
