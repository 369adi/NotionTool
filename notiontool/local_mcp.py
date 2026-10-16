"""Built-in local stdio MCP servers that can be switched on/off in Settings.

Each server exposes two lazy tools to workers: <key>_tools (discover) and
<key>_call (call). The process only starts on first use while enabled and is
closed again when the toggle is switched off.
"""
import os
from pathlib import Path
from shutil import which


def roblox_bat():
    return Path(os.environ.get('LOCALAPPDATA', '')) / 'Roblox' / 'mcp.bat'


def uvx_path():
    found = which('uvx')
    if found:
        return found
    fallback = Path.home() / '.local' / 'bin' / ('uvx.exe' if os.name == 'nt' else 'uvx')
    return str(fallback) if fallback.is_file() else ''


def codebase_memory_path():
    """codebase-memory-mcp (DeusData) binary: PATH, env override, then usual install folders."""
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


def _rea_command():
    from .rea import COMMAND
    return list(COMMAND)


def _rea_installed():
    from .rea import node_available
    return node_available()


SERVERS = [
    dict(key='rea', id='builtin_rea', name='REA (Reverse Engineer Anything)', setting='rea_enabled', label='REA',
         url='Local stdio: npx rea-agents@6.5.0 mcp', command=_rea_command, installed=_rea_installed,
         ready='Node.js found; REA starts on first use.', missing='Install Node.js (npx) to use REA.'),
    dict(key='roblox', id='builtin_roblox', name='Roblox Studio MCP', setting='roblox_enabled', label='Roblox',
         url='Local stdio: %LOCALAPPDATA%\\Roblox\\mcp.bat (StudioMCP)',
         command=lambda: (['cmd.exe', '/d', '/c'] if os.name == 'nt' else []) + [str(roblox_bat())],
         installed=lambda: roblox_bat().is_file(),
         ready='Studio MCP found. Open Roblox Studio and enable the MCP server in Studio settings.',
         missing='Install Roblox Studio and enable MCP in Studio settings.'),
    dict(key='blender', id='builtin_blender', name='Blender MCP', setting='blender_enabled', label='Blender',
         url='Local stdio: uvx blender-mcp', command=lambda: [uvx_path() or 'uvx', 'blender-mcp'],
         installed=lambda: bool(uvx_path()),
         ready='uv found. Start Blender and click "Connect to MCP server" in the BlenderMCP sidebar (N panel).',
         missing='Install uv (uvx) and the Blender MCP add-on.'),
    dict(key='codebase', id='builtin_codebase_memory', name='Codebase Memory MCP', setting='codebase_memory_enabled',
         label='Codebase Memory', url='Local stdio: codebase-memory-mcp (code knowledge graph)',
         command=lambda: [codebase_memory_path() or 'codebase-memory-mcp'], installed=lambda: bool(codebase_memory_path()),
         ready='codebase-memory-mcp found; uses your existing indexes in ~/.cache/codebase-memory-mcp.',
         missing='codebase-memory-mcp not found. Install it (github.com/DeusData/codebase-memory-mcp) or put it on PATH.'),
]
BY_KEY = {s['key']: s for s in SERVERS}
SETTINGS = tuple(s['setting'] for s in SERVERS)


def tool_specs(server):
    label = server['label']
    return {
        server['key'] + '_tools': ('Discover ' + label + ' MCP tools and their argument schemas', {}),
        server['key'] + '_call': ('Call a discovered ' + label + ' MCP tool',
                                  {'name': {'type': 'string'}, 'arguments': {'type': 'object'}}),
    }


ALL_TOOLS = {}
TOOL_SERVER = {}
for _server in SERVERS:
    for _name in tool_specs(_server):
        TOOL_SERVER[_name] = _server['key']
    ALL_TOOLS.update(tool_specs(_server))


def enabled(settings, server):
    return bool(settings.get(server['setting'], False))


def public(settings):
    out = []
    for s in SERVERS:
        try:
            installed = bool(s['installed']())
        except Exception:
            installed = False
        out.append(dict(id=s['id'], name=s['name'], url=s['url'], builtin=True, installed=installed,
                        enabled=enabled(settings, s), toggle=s['setting'], label=s['label'],
                        hint=s['ready'] if installed else s['missing'], read_auto=True, write_auto=False))
    return out
