"""Windows desktop entry point. --serve is useful for local UI verification."""
from pathlib import Path
import argparse
import sys
import time
import hashlib
import os
from notiontool.server import Application
from notiontool.startup import default_data_dir


def instance_lock(directory):
    if os.name!='nt':return None
    import ctypes
    library=ctypes.WinDLL('kernel32',use_last_error=True)
    library.CreateMutexW.restype=ctypes.c_void_p
    name='Local\\NotionTool-'+hashlib.sha256(str(Path(directory).resolve()).casefold().encode()).hexdigest()[:24]
    handle=library.CreateMutexW(None,False,name)
    if ctypes.get_last_error()==183:
        ctypes.windll.user32.MessageBoxW(None,'NotionTool is already running for this data folder.','NotionTool',0)
        return False
    return handle


def main():
    if '--gateway' in sys.argv:  # process 2: MCP gateway / backup MCP, no window, no instance lock
        from notiontool.gateway import main as gateway_main
        sys.exit(gateway_main(sys.argv))
    from notiontool.updater import handoff
    sys.argv = handoff(sys.argv)  # waits for the previous instance; installs a downloaded update
    parser = argparse.ArgumentParser()
    parser.add_argument('--serve',action='store_true')
    parser.add_argument('--data-dir',default=default_data_dir(sys.executable,Path(__file__).parent,getattr(sys,'frozen',False)))
    parser.add_argument('--port',type=int,default=None,help='Host API port (default: setting host_api_port, 8790)')
    args = parser.parse_args()
    instance=instance_lock(args.data_dir)
    if instance is False:return
    app = Application(args.data_dir)
    url = app.launch(args.port)
    try:
        if args.serve:
            print(url,flush=True)
            while True:
                time.sleep(1)
        else:
            import webview
            app.window = webview.create_window('NotionTool',url,width=1480,height=940,min_size=(1050,700),background_color='#161719')
            webview.start(gui='edgechromium')
    except KeyboardInterrupt:
        pass
    finally:
        app.close()


if __name__=='__main__':
    main()
