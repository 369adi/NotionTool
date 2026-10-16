"""Never drop an MCP connection on an unexpected error: answer and log the traceback."""
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
    ("        except (ValueError,OSError,KeyError,TypeError,RuntimeError) as exc:\n            return {'jsonrpc':'2.0','id':ident,'result':{'isError':True,'content':[{'type':'text','text':str(exc)[:400]}]}}\n",
     "        except (ValueError,OSError,KeyError,TypeError,RuntimeError) as exc:\n            return {'jsonrpc':'2.0','id':ident,'result':{'isError':True,'content':[{'type':'text','text':str(exc)[:400]}]}}\n"
     "        except Exception as exc:  # noqa: BLE001 - an MCP call must always get an answer\n"
     "            self.log_failure(method, params, exc)\n"
     "            return {'jsonrpc':'2.0','id':ident,'result':{'isError':True,'content':[{'type':'text','text':'Internal NotionTool error ('+type(exc).__name__+'): '+str(exc)[:300]}]}}\n"
     "\n"
     "    def log_failure(self, method, params, exc):\n"
     "        import traceback\n"
     "        try:\n"
     "            folder = Path(getattr(self.store,'directory','.'))/'runtime'\n"
     "            folder.mkdir(parents=True, exist_ok=True)\n"
     "            tool = (params or {}).get('name','')\n"
     "            inner = ((params or {}).get('arguments') or {}).get('tool_name','') if tool=='mcp_call' else ''\n"
     "            stamp = time.strftime('%Y-%m-%d %H:%M:%S')\n"
     "            text = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-4000:]\n"
     "            with open(folder/'mcp-errors.log','a',encoding='utf-8') as handle:\n"
     "                handle.write(f'[{stamp}] {method} {tool} {inner}\\n{text}\\n')\n"
     "        except OSError:\n"
     "            pass\n"),
])

edit('notiontool/server.py', [
    ("                    except (ValueError,TypeError,AttributeError):\n                        return self.response(400,{'error':'Invalid MCP request'})\n",
     "                    except (ValueError,TypeError,AttributeError):\n                        return self.response(400,{'error':'Invalid MCP request'})\n"
     "                    except Exception:  # noqa: BLE001 - never drop the connection (the gateway would report a restart)\n"
     "                        return self.response(500,{'error':'Internal MCP error'})\n"),
])

req = ROOT / 'requirements.txt'
text = req.read_text(encoding='utf-8')
if 'pillow' not in text.lower():
    req.write_text(text.rstrip('\n') + '\npillow>=10.0\n', encoding='utf-8')

print('FAILS:', fails or 'none')
sys.exit(1 if fails else 0)
