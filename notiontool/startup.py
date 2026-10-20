"""Use the same existing data directory for a launcher and a direct dist EXE start."""
from pathlib import Path


def default_data_dir(executable,source_root,frozen):
    if not frozen: return str(Path(source_root)/'data')
    install=Path(executable).resolve().parent
    if install.name.lower()=='dist' and (install.parent/'data'/'notiontool.db').is_file():
        return str(install.parent/'data')
    return str(install/'data')
