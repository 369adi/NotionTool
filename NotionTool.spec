from pathlib import Path
root = Path(SPECPATH)
a = Analysis([str(root / 'main.py')], pathex=[str(root)],
             datas=[(str(root / 'notiontool/web'), 'notiontool/web'),
                    (str(root / 'notiontool/template.json'), 'notiontool'),
                    (str(root / 'notiontool/welcome.json'), 'notiontool'),
                    (str(root / 'notiontool/standard_rules_v1.md'), 'notiontool')],
             hiddenimports=['webview.platforms.edgechromium', 'notiontool._build', 'PIL', 'PIL.Image', 'PIL.ImageGrab',
                            'notiontool.autopilot', 'notiontool.context_archive', 'notiontool.dag', 'notiontool.role_policy'], hookspath=[], runtime_hooks=[], excludes=[])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, name='NotionTool', debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False,
          icon=str(root / 'assets' / 'notiontool.ico'))
