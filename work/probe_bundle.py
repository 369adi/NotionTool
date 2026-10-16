import sys
from PyInstaller.archive.readers import CArchiveReader

exe = sys.argv[1] if len(sys.argv) > 1 else r'dist\NotionTool.exe'
archive = CArchiveReader(exe)
names = list(archive.toc)
print('entries', len(names))
print('PIL entries in CArchive:', [n for n in names if 'PIL' in n or '_imaging' in n][:30])
try:
    pyz_name = next(n for n in names if n.endswith('.pyz') or n == 'PYZ-00.pyz' or 'PYZ' in n)
    pyz = archive.open_embedded_archive(pyz_name)
    mods = list(pyz.toc)
    print('PIL modules in PYZ:', len([m for m in mods if m.startswith('PIL')]), [m for m in mods if m.startswith('PIL')][:8])
except Exception as exc:
    print('pyz read failed', type(exc).__name__, exc)
