from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent

def patch(rel, pairs):
    p = ROOT / rel
    raw = p.read_bytes().decode('utf-8')
    crlf = '\r\n' in raw
    text = raw.replace('\r\n', '\n')
    for old, new in pairs:
        if text.count(old) != 1:
            raise SystemExit(f'{rel}: expected 1 match, got {text.count(old)} for {old[:70]!r}')
        text = text.replace(old, new)
    p.write_bytes((text.replace('\n', '\r\n') if crlf else text).encode('utf-8'))
    print('patched', rel)

patch('notiontool/local_mcp.py', [
("""def _rea_command():""", """def codebase_memory_path():
    \"\"\"codebase-memory-mcp (DeusData) binary: PATH, env override, then usual install folders.\"\"\"
    override = os.environ.get('CODEBASE_MEMORY_MCP', '')
    if override and Path(override).is_file():
        return override
    found = which('codebase-memory-mcp')
    if found:
        return found
    exe = 'codebase-memory-mcp.exe' if os.name == 'nt' else 'codebase-memory-mcp'
    home, local = Path.home(), Path(os.environ.get('LOCALAPPDATA', '') or Path.home())
    for folder in (home / '.local' / 'bin', local / 'Programs' / 'codebase-memory-mcp', local / 'codebase-memory-mcp',
                   home / 'go' / 'bin', home / 'bin', home / '.codebase-memory-mcp', home / '.codebase-memory-mcp' / 'bin',
                   home / '.cache' / 'codebase-memory-mcp' / 'bin'):
        if (folder / exe).is_file():
            return str(folder / exe)
    return ''


def _rea_command():"""),
("""         missing='Install uv (uvx) and the Blender MCP add-on.'),
]""", """         missing='Install uv (uvx) and the Blender MCP add-on.'),
    dict(key='codebase', id='builtin_codebase_memory', name='Codebase Memory MCP', setting='codebase_memory_enabled',
         label='Codebase Memory', url='Local stdio: codebase-memory-mcp (code knowledge graph)',
         command=lambda: [codebase_memory_path() or 'codebase-memory-mcp'], installed=lambda: bool(codebase_memory_path()),
         ready='codebase-memory-mcp found; uses your existing indexes in ~/.cache/codebase-memory-mcp.',
         missing='codebase-memory-mcp not found. Install it (github.com/DeusData/codebase-memory-mcp) or put it on PATH.'),
]"""),
])

patch('tests/test_rea.py', [
("""['builtin_rea', 'builtin_roblox', 'builtin_blender'])""", """['builtin_rea', 'builtin_roblox', 'builtin_blender', 'builtin_codebase_memory'])"""),
("""['rea_enabled', 'roblox_enabled', 'blender_enabled'])""", """['rea_enabled', 'roblox_enabled', 'blender_enabled', 'codebase_memory_enabled'])"""),
])
