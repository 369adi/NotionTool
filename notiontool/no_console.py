"""Every child process NotionTool starts runs without a console window (Windows).

A windowed app that starts a console program (python, git, powershell, cmd) gets a new visible
console window that takes the focus, e.g. out of a full-screen game. install() adds
CREATE_NO_WINDOW to every subprocess.Popen that does not ask for its own console or a detached
process. GUI programs ignore the flag.
"""
import os
import subprocess

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_CONSOLE = 0x00000010


def flags_for(flags):
    flags = int(flags or 0)
    if flags & (DETACHED_PROCESS | CREATE_NEW_CONSOLE):
        return flags
    return flags | CREATE_NO_WINDOW


def install():
    if os.name != 'nt' or getattr(subprocess.Popen, '_notiontool_no_console', False):
        return False
    original = subprocess.Popen.__init__

    def __init__(self, *args, **kwargs):
        kwargs['creationflags'] = flags_for(kwargs.get('creationflags', 0))
        return original(self, *args, **kwargs)

    subprocess.Popen.__init__ = __init__
    subprocess.Popen._notiontool_no_console = True
    return True
