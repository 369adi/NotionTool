from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules
root = Path(SPECPATH)
# Every module of the package goes into the EXE; no hand-typed notiontool names that can fall behind.
# notiontool._build is stamped by the release workflow before the build, so it is added by name as well.
hidden = collect_submodules('notiontool') + ['notiontool._build']
a = Analysis([str(root / 'main.py')], pathex=[str(root)],
             datas=[(str(root / 'notiontool/web'), 'notiontool/web'),
                    (str(root / 'notiontool/template.json'), 'notiontool'),
                    (str(root / 'notiontool/welcome.json'), 'notiontool'),
                    (str(root / 'notiontool/standard_rules_v1.md'), 'notiontool')],
             hiddenimports=['webview.platforms.edgechromium', 'PIL', 'PIL.Image', 'PIL.ImageGrab'] + hidden,
             hookspath=[], runtime_hooks=[], excludes=[])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, name='NotionTool', debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False,
          icon=str(root / 'assets' / 'notiontool.ico'))
