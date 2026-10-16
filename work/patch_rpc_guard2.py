from pathlib import Path
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


edit('notiontool/bridge.py', [
    ("    def log_failure(self, method, params, exc):\n        import traceback\n",
     "    def log_failure(self, method, params, exc):\n        import time\n        import traceback\n"),
    ("        except OSError:\n            pass\n", "        except Exception:  # noqa: BLE001 - logging must never break the answer\n            pass\n"),
])

edit('notiontool/server.py', [
    ("                    except Exception:  # noqa: BLE001 - never drop the connection (the gateway would report a restart)\n                        return self.response(500,{'error':'Internal MCP error'})\n",
     "                    except Exception as exc:  # noqa: BLE001 - never drop the connection (the gateway would report a restart)\n"
     "                        app.bridge.log_failure('http', {}, exc)\n"
     "                        return self.response(500,{'error':'Internal MCP error ('+type(exc).__name__+')'})\n"),
])

edit('NotionTool.spec', [
    ("hiddenimports=['webview.platforms.edgechromium', 'notiontool._build']",
     "hiddenimports=['webview.platforms.edgechromium', 'notiontool._build', 'PIL', 'PIL.Image', 'PIL.ImageGrab']"),
])

print('FAILS:', fails or 'none')
sys.exit(1 if fails else 0)
