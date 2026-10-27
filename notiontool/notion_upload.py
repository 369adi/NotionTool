"""Upload captures into Notion so Notion AI chats can really look at them (user 2026-10-27).

Notion chats get MCP tool results as JSON text, so an inline MCP image arrives as unreadable
base64 (about 37k tokens, cut off) and the agent never sees the picture. A file uploaded the
way Notion uploads chat attachments (getUploadFileUrl with a thread record, then the signed
PUT) can be opened by the agent with its file viewer (connections.notion.viewFileUrl).

The upload runs inside the signed-in NotionTool app window over the local DevTools port, so
no credential ever leaves the app. Only the running app switches it on (ACTIVE); unit tests
and tools that build a ScreenCapture or REA client stay offline.
"""
import base64
import json
import re
import time
import urllib.parse
import urllib.request

PORT = 9333
ACTIVE = False
TIMEOUT = 30
MAX_BYTES = 12 * 1024 * 1024
VIEW_HINT = ('To SEE this image open view_url with the Notion file viewer: '
             'connections.notion.viewFileUrl({url: view_url}). Never judge the picture before you have seen it.')
_HOSTS = ('notion.so', 'notion.com')
_THREAD = re.compile(r'^[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}$', re.I)

UPLOAD_JS = r"""(async function (a) {
  function ck(n) { var m = document.cookie.match(new RegExp('(?:^|; )' + n + '=([^;]*)')); return m ? decodeURIComponent(m[1]) : ''; }
  function dash(t) { t = String(t || '').replace(/-/g, ''); return t.length === 32 ? t.slice(0, 8) + '-' + t.slice(8, 12) + '-' + t.slice(12, 16) + '-' + t.slice(16, 20) + '-' + t.slice(20) : ''; }
  var tid = dash(a.thread);
  if (!tid) { try { tid = dash(new URLSearchParams(location.search).get('t')); } catch (e) { tid = ''; } }
  if (!tid) return { error: 'no Notion chat is open in the NotionTool window' };
  var uid = ck('notion_user_id'), h = { 'content-type': 'application/json' };
  if (uid) h['x-notion-active-user-header'] = uid;
  var space = a.space || '';
  if (!space) {
    try {
      var sr = await fetch('/api/v3/syncRecordValues', { method: 'POST', headers: h, credentials: 'include',
        body: JSON.stringify({ requests: [{ pointer: { table: 'thread', id: tid }, version: -1 }] }) });
      var sj = await sr.json(), rec = sj && sj.recordMap && sj.recordMap.thread && sj.recordMap.thread[tid];
      var v = rec && rec.value && (rec.value.value || rec.value);
      space = (v && v.space_id) || (rec && rec.spaceId) || '';
    } catch (e) { space = ''; }
  }
  if (!space) return { error: 'the workspace of the open chat is unknown' };
  h['x-notion-space-id'] = space;
  var bin = atob(a.data), bytes = new Uint8Array(bin.length);
  for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  var r = await fetch('/api/v3/getUploadFileUrl', { method: 'POST', headers: h, credentials: 'include',
    body: JSON.stringify({ bucket: 'secure', name: a.name, contentType: a.mime, record: { table: 'thread', id: tid, spaceId: space },
      supportExtraHeaders: true, contentLength: bytes.length }) });
  if (!r.ok) return { error: 'getUploadFileUrl HTTP ' + r.status };
  var j = await r.json();
  if (!j || !j.signedPutUrl || !j.url) return { error: 'Notion gave no upload URL' };
  var ph = { 'Content-Type': a.mime };
  (j.putHeaders || []).forEach(function (x) { if (x && x.name && !/^content-length$/i.test(x.name)) ph[x.name] = x.value; });
  var p = await fetch(j.signedPutUrl, { method: 'PUT', headers: ph, body: bytes });
  if (!p.ok) return { error: 'upload HTTP ' + p.status };
  return { source: j.url, thread: tid, space: space };
})(%s)"""


def _http_get(url):
    with urllib.request.urlopen(url, timeout=3) as response:
        return json.loads(response.read().decode('utf-8'))


def pick_target(targets):
    """The NotionTool app window: a Notion page target, an open chat (?t=...) first."""
    pages = [t for t in targets or [] if t.get('type') == 'page' and t.get('webSocketDebuggerUrl')
             and any(h in (t.get('url') or '') for h in _HOSTS)]
    pages.sort(key=lambda t: (not ('/chat' in t['url'] and 't=' in t['url']), not str(t.get('title') or '').startswith('NotionTool')))
    return pages[0] if pages else None


def file_uri(source, thread, space):
    payload = json.dumps({'source': source, 'permissionRecord': {'table': 'thread', 'id': thread, 'spaceId': space}},
                         separators=(',', ':'))
    return 'file://' + urllib.parse.quote(payload, safe='')


def upload(data, mime='image/jpeg', name='capture.jpg', thread='', space='', port=PORT, timeout=TIMEOUT,
           http_get=None, connect=None):
    """Upload base64 data as a chat attachment; returns {view_url, source, thread, space}."""
    if not data or len(data) * 3 // 4 > MAX_BYTES:
        raise ValueError('image is empty or too large to upload')
    if thread and not _THREAD.match(str(thread)):
        thread = ''
    target = pick_target((http_get or _http_get)('http://127.0.0.1:%d/json/list' % int(port)))
    if not target:
        raise RuntimeError('the NotionTool app window is not open')
    if connect is None:
        from .notion_shell import CdpSession
        connect = CdpSession
    args = dict(data=data, mime=mime, name=re.sub(r'[^A-Za-z0-9._-]+', '-', name)[:80] or 'capture.jpg',
                thread=thread or '', space=space or '')
    session = connect(target['webSocketDebuggerUrl'])
    try:
        res = session.send('Runtime.evaluate', {'expression': UPLOAD_JS % json.dumps(args), 'awaitPromise': True,
                                                'returnByValue': True}, timeout) or {}
    finally:
        try:
            session.close()
        except Exception:  # noqa: BLE001 - closing must never hide the result
            pass
    if res.get('exceptionDetails'):
        raise RuntimeError('upload script failed: ' + str((res['exceptionDetails'].get('exception') or {}).get('description') or '')[:160])
    value = (res.get('result') or {}).get('value') or {}
    if not isinstance(value, dict) or value.get('error') or not value.get('source'):
        raise RuntimeError(str(value.get('error') if isinstance(value, dict) else 'no upload result')[:200])
    return dict(view_url=file_uri(value['source'], value['thread'], value['space']), source=value['source'],
                thread=value['thread'], space=value['space'])


def publish(info, data, mime, label='capture'):
    """Upload an image and put view_url + view_hint into info. True when the upload worked, so the
    caller can drop the inline base64 copy (it only floods a Notion chat's context)."""
    if not ACTIVE or not isinstance(info, dict):
        return False
    ext = '.png' if 'png' in str(mime) else '.jpg'
    name = 'nt-%s-%s%s' % (re.sub(r'[^a-z0-9]+', '-', str(label).lower()).strip('-')[:24] or 'capture',
                           time.strftime('%Y%m%d-%H%M%S'), ext)
    try:
        up = upload(data, mime, name)
    except Exception as exc:  # noqa: BLE001 - a failed upload keeps the old inline path
        info['view_error'] = str(exc)[:200]
        return False
    info['view_url'] = up['view_url']
    info['view_hint'] = VIEW_HINT
    return True


def decode_size(data):
    try:
        return len(base64.b64decode(data))
    except Exception:  # noqa: BLE001
        return 0
