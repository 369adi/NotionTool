"""Child processes must never inherit the PyInstaller state of the running app (stale _MEI folder)."""
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
fails = []


def edit(rel, steps):
    path = ROOT / rel
    raw = path.read_bytes().decode('utf-8')
    crlf = '\r\n' in raw
    text = raw.replace('\r\n', '\n')
    for old, new in steps:
        if old not in text:
            fails.append(f'{rel}: missing {old[:70]!r}')
            continue
        text = text.replace(old, new, 1)
    if crlf:
        text = text.replace('\n', '\r\n')
    path.write_bytes(text.encode('utf-8'))


edit('work/restart-harness.ps1', [
    ("Set-Location $root\n",
     "Set-Location $root\n"
     "# This script is usually started from inside the running app. Never let the new EXE inherit that app's\n"
     "# PyInstaller state: it would reuse an old, stale _MEI folder (e.g. without newly bundled libraries).\n"
     "Get-ChildItem env: | Where-Object { $_.Name -like '_PYI*' -or $_.Name -eq '_MEIPASS2' } | ForEach-Object { Remove-Item \"env:$($_.Name)\" }\n"
     "$env:PYINSTALLER_RESET_ENVIRONMENT = '1'\n"),
])

edit('notiontool/devtools.py', [
    ("            r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, timeout=timeout, creationflags=flags)\n",
     "            env = {k: v for k, v in os.environ.items() if not k.startswith('_PYI') and k != '_MEIPASS2'}\n"
     "            env['PYINSTALLER_RESET_ENVIRONMENT'] = '1'  # started programs get their own, fresh bundle state\n"
     "            r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, timeout=timeout, creationflags=flags, env=env)\n"),
])

print('FAILS:', fails or 'none')
for f in sorted((ROOT / 'notiontool').glob('*.py')):
    for n, line in enumerate(f.read_text(encoding='utf-8', errors='replace').splitlines(), 1):
        if re.search(r'subprocess\.(run|Popen|call|check_output)\(', line):
            print(f'{f.name}:{n}: {line.strip()[:150]}')
sys.exit(1 if fails else 0)
