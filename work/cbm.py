r"""Call a codebase-memory-mcp tool via its CLI with proper JSON quoting.

Usage (PowerShell):  '{"pattern": "def event"}' | & $py work\cbm.py search_code
JSON arguments come from stdin; 'project' defaults to the NotionTool index.
"""
import json
import os
import subprocess
import sys

EXE = os.environ.get('CODEBASE_MEMORY_MCP') or os.path.join(
    os.environ.get('USERPROFILE', ''), '.local', 'bin', 'codebase-memory-mcp.exe')
PROJECT = 'C-Users-liket-Downloads-NotionTool-harness-NotionTool-notiontool'
NO_PROJECT = {'index_repository', 'list_projects'}


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    tool = sys.argv[1]
    raw = sys.stdin.read().strip() if not sys.stdin.isatty() else ''
    args = json.loads(raw) if raw else {}
    if tool not in NO_PROJECT:
        args.setdefault('project', PROJECT)
    proc = subprocess.run([EXE, 'cli', '--quiet', tool, json.dumps(args)],
                          capture_output=True, text=True, encoding='utf-8',
                          errors='replace', timeout=170)
    sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        err = '\n'.join(l for l in proc.stderr.splitlines() if 'preloading' not in l)
        sys.stdout.write('\n[cbm exit %s] %s\n' % (proc.returncode, err[-1500:]))
    return proc.returncode


if __name__ == '__main__':
    sys.exit(main())
