"""Read-only: show the maintenance job record."""
import json
import sqlite3
import sys
import time

DB = r'file:C:\Users\liket\Downloads\NotionTool-harness\NotionTool\data\notiontool.db?mode=ro'
TIME_KEYS = {'created', 'updated', 'started', 'finished', 'ts', 'at'}

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
con = sqlite3.connect(DB, uri=True)
row = con.execute("select doc from objects where kind='jobs' and id=?", ('j-bridge-maintenance',)).fetchone()
if not row:
    print('no maintenance job')
    sys.exit(0)
doc = json.loads(row[0])
for key, value in doc.items():
    if key in TIME_KEYS and isinstance(value, (int, float)):
        value = time.strftime('%H:%M:%S', time.localtime(value))
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    print(f'{key}: {text[:300]}')
