import json, sqlite3, sys
keys = sys.argv[1:] or ['update_', 'capture', 'gateway', 'error', 'crash']
c = sqlite3.connect(r'data\notiontool.db')
rows = c.execute("select data from objects where kind='events' order by rowid desc limit 600").fetchall()
out = 0
for (raw,) in rows:
    if any(k in raw for k in keys):
        e = json.loads(raw)
        print(e.get('ts') or e.get('created_at'), e.get('kind'), {k: v for k, v in e.items() if k in ('message', 'error', 'scope', 'tool', 'reason', 'detail')})
        out += 1
        if out >= 12:
            break
