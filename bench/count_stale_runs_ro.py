"""Count runs by state in a NotionTool database opened strictly read-only (sqlite URI mode=ro).

Usage: python bench/count_stale_runs_ro.py <path to notiontool.db>
Prints the number of runs with the literal state 'stale' and the runs marked stale=True by auto cleanup.
It never writes.
"""
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path


def main(path):
    uri = 'file:' + Path(path).resolve().as_posix() + '?mode=ro'
    con = sqlite3.connect(uri, uri=True)
    con.execute('PRAGMA query_only = ON')
    states = Counter()
    literal_stale = []
    marked_stale = []
    for (doc,) in con.execute("SELECT doc FROM objects WHERE kind = 'runs'"):
        try:
            run = json.loads(doc)
        except (TypeError, ValueError):
            continue
        state = run.get('state')
        states[state] += 1
        if state == 'stale':
            literal_stale.append(run.get('id'))
        if run.get('stale'):
            marked_stale.append((run.get('id'), state))
    con.close()
    print('runs total: %d' % sum(states.values()))
    print('runs by state: ' + ', '.join('%s=%d' % (k, v) for k, v in sorted(states.items(), key=lambda i: str(i[0]))))
    print("runs with literal state 'stale': %d %s" % (len(literal_stale), literal_stale))
    print('runs marked stale=True: %d %s' % (len(marked_stale), marked_stale))


if __name__ == '__main__':
    main(sys.argv[1])
