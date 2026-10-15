"""Read-only: how often each earlier final answer of a chat appears in a run's prompt context."""
import json
import sqlite3
import sys

DB = 'file:' + __import__('os').path.join(__import__('os').path.dirname(__import__('os').path.dirname(__import__('os').path.abspath(__file__))), 'data', 'notiontool.db') + '?mode=ro'


def docs(db, kind):
    return [json.loads(doc) for (doc,) in db.execute('SELECT doc FROM objects WHERE kind=?', (kind,))]


def main(run_id):
    db = sqlite3.connect(DB, uri=True)
    runs = docs(db, 'runs')
    run = next(r for r in runs if r['id'] == run_id)
    context = run.get('context', '')
    done = {r['id'] for r in runs if r['chat_id'] == run['chat_id'] and r['state'] == 'done' and r['id'] != run_id}
    answers = [m for m in docs(db, 'messages') if m['chat_id'] == run['chat_id'] and m['role'] == 'assistant'
               and m.get('run_id') in done and m['text'].strip()]
    print(f"run {run['id']} state {run['state']} context {len(context)} chars, memory label {context.count('Project memory:')}x")
    for m in answers:
        print(f"  {m['run_id']} quoted {context.count(m['text'])}x  {m['text'][:48]!r}")


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    main(sys.argv[1])
