"""Native HTTP transport for Notion server API - no CDP/browser required.

Implements the shared interface from docs/native-api-contract.md:
    NativeNotionTransport(session, base_url, session_store, session_key)
    .request(path, body, headers=None, timeout=60, ndjson=False) -> (status, text)
    .stream(path, body, headers=None, timeout=300) -> iterator over decoded NDJSON lines

Uses stdlib urllib + http.cookiejar only.
Strict HTTPS-only, exact-hostname allowlist, no redirects, path traversal rejection.
No credentials or raw server error bodies in logs or exceptions.
"""

import http.cookiejar
import json
import posixpath
import threading
import urllib.error
import urllib.parse
import urllib.request
import re
from ..network_budget import SHARED_BUDGET

# Primary Notion web client origin (contract-required default).
_NOTION_ORIGIN = "https://app.notion.com"

# Strict allowlist: only these exact HTTPS hostnames may receive requests.
_ALLOWED_HOSTS = frozenset([
    "app.notion.com",
    "www.notion.so",
    "notion.so",
    "api.notion.so",
])

# Cookie domain allowlist: these root domains and their subdomains may set/send cookies.
# Includes root notion.com + notion.so so .notion.com token_v2 cookies are accepted.
_COOKIE_ALLOWED_ROOTS = frozenset([
    "notion.com",
    "notion.so",
    "www.notion.so",
    "app.notion.com",
    "api.notion.so",
])

_API_PREFIX = "/api/v3/"
# Maximum body size read from responses.
_MAX_BODY = 10 * 1024 * 1024    # 10 MB
_MAX_ERR  =       4 * 1024      # 4 KB for error bodies (discarded after status extraction)


class NotionHttpError(OSError):
    """Non-2xx HTTP response from Notion.

    .status  -- HTTP status code (int)
    .text    -- generic status-only message (no raw server body echoed)
    """
    def __init__(self, status: int, text: str = "", retry_after=30):
        self.status = status
        self.retry_after = retry_after
        # Never echo raw server body — return a generic status message only.
        self.text = f"HTTP {status}"
        super().__init__(self.text)


class NotionStreamError(OSError):
    """A successful HTTP status did not produce a valid inference event stream.

    Submission may already have reached Notion: reconcile, never blindly resend.
    Raw response content is intentionally excluded from the error message.
    """


# ---------------------------------------------------------------------------
# Redirect handler: refuse ALL redirects (no credentials forwarded anywhere)
# ---------------------------------------------------------------------------

class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect; credentials must not follow an uncontrolled Location."""

    def redirect_request(self, *args, **kwargs):
        return None  # signals opener to stop following

    def http_error_301(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(req.full_url, code,
                                     "Redirect not followed", headers, fp)

    def http_error_302(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(req.full_url, code,
                                     "Redirect not followed", headers, fp)

    def http_error_303(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(req.full_url, code,
                                     "Redirect not followed", headers, fp)

    def http_error_307(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(req.full_url, code,
                                     "Redirect not followed", headers, fp)

    def http_error_308(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(req.full_url, code,
                                     "Redirect not followed", headers, fp)


def _cookie_domain_allowed(domain: str) -> bool:
    """Return True if the cookie domain is within the Notion allowed roots."""
    d = domain.lstrip(".")
    if d in _COOKIE_ALLOWED_ROOTS:
        return True
    # Allow subdomains: *.notion.com, *.notion.so
    for root in ("notion.com", "notion.so"):
        if d == root or d.endswith("." + root):
            return True
    return False


class NativeNotionTransport:
    """Stateless-per-request native Notion HTTP transport.

    Parameters
    ----------
    session : dict
        Session dict with optional 'cookies' list of {name,value,domain,path,secure,expires}.
    base_url : str
        Notion HTTPS origin (must be in _ALLOWED_HOSTS, empty path or '/',
        no port/credentials/query/fragment). Default: app.notion.com.
    session_store : object or None
        Optional store; .save(key, session_dict) called after Set-Cookie responses.
        'store' is accepted as a backward-compat alias.
    session_key : str or None
        Key passed to session_store.save.
    _opener : urllib.request.OpenerDirector or None
        Injected opener for tests; production always uses the internal factory.
    """

    def __init__(self, session, base_url=_NOTION_ORIGIN,
                 session_store=None, session_key=None,
                 store=None,          # backward-compat alias for session_store
                 _opener=None, budget=None):       # test injection only
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme != "https":
            raise ValueError("base_url must use HTTPS")
        if parsed.hostname not in _ALLOWED_HOSTS:
            raise ValueError("base_url hostname not in Notion allowlist")
        if parsed.port is not None:
            raise ValueError("base_url must not specify a port")
        if parsed.username or parsed.password:
            raise ValueError("base_url must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("base_url must not contain query or fragment")
        if parsed.path not in ("", "/"):
            raise ValueError("base_url must have an empty path or '/'")
        self._base = "https://" + parsed.hostname
        self._store = session_store or store
        self._session_key = session_key
        self._lock = threading.Lock()
        self._jar = http.cookiejar.CookieJar()
        self._session = session if isinstance(session, dict) else {}
        self._load_cookies(self._session.get("cookies") or [])
        self._opener_override = _opener  # None in production
        self._budget = budget if budget is not None else (SHARED_BUDGET if _opener is None else None)
        self._response = threading.local()

    @property
    def retry_after(self):
        return getattr(self._response, 'retry_after', 30)

    # ------------------------------------------------------------------
    # Cookie helpers
    # ------------------------------------------------------------------

    def _load_cookies(self, cookies):
        """Load session cookies; skip any cookie whose domain is not in the Notion allowlist."""
        for c in cookies:
            if not isinstance(c, dict) or not c.get("name") or not c.get("domain"):
                continue
            domain = str(c["domain"])
            if not _cookie_domain_allowed(domain):
                continue  # silently drop foreign-domain cookies
            # Normalise expires: CDP uses -1 for session cookies; treat <=0 as session.
            expires_raw = c.get("expires")
            try:
                expires = int(expires_raw)
                if expires <= 0:
                    expires = None
            except (TypeError, ValueError):
                expires = None
            try:
                morsel = http.cookiejar.Cookie(
                    version=0,
                    name=str(c["name"]),
                    value=str(c.get("value") or ""),
                    port=None, port_specified=False,
                    domain=domain,
                    domain_specified=True,
                    domain_initial_dot=domain.startswith("."),
                    path=str(c.get("path") or "/"),
                    path_specified=True,
                    secure=bool(c.get("secure", True)),
                    expires=expires,
                    discard=expires is None,
                    comment=None, comment_url=None, rest={},
                )
                self._jar.set_cookie(morsel)
            except Exception:  # noqa: BLE001
                pass

    def _dump_cookies(self):
        """Export current CookieJar as list of dicts (names only, no values logged)."""
        return [
            {"name": c.name, "value": c.value,
             "domain": c.domain, "path": c.path,
             "secure": c.secure, "expires": c.expires}
            for c in self._jar
        ]

    # ------------------------------------------------------------------
    # URL / path validation
    # ------------------------------------------------------------------

    def _build_url(self, path):
        """Build and validate a Notion API URL from a path.

        Rejects: full URLs, non-/api/v3/ paths, traversal sequences.
        """
        if path.startswith("http://") or path.startswith("https://"):
            raise ValueError("Full URL not allowed as path")
        if not path.startswith("/"):
            path = "/api/v3/" + path
        if not path.startswith("/api/v3/"):
            raise ValueError("Path must start with /api/v3/")
        decoded = urllib.parse.unquote(path)
        if "../" in decoded or decoded.endswith("..") or "/." in decoded:
            raise ValueError("Path traversal rejected")
        norm = posixpath.normpath(path)
        if not norm.startswith("/api/v3"):
            raise ValueError("Path normalisation produced non-API path")
        return self._base + norm

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _persist(self):
        if self._store and self._session_key:
            updated = dict(self._session)
            updated["cookies"] = self._dump_cookies()
            try:
                self._store.save(self._session_key, updated)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # Opener factory
    # ------------------------------------------------------------------

    # Notion rejects requests without a browser-like User-Agent (HTTP 403).
    _UA = ('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
           'AppleWebKit/537.36 (KHTML, like Gecko) '
           'Chrome/124.0.0.0 Safari/537.36')

    def _make_opener(self):
        if self._opener_override is not None:
            return self._opener_override
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar),
            urllib.request.HTTPDefaultErrorHandler(),
            _NoRedirectHandler(),
        )
        opener.addheaders = [self._UA]
        return opener

    # ------------------------------------------------------------------
    # Internal HTTP dispatch
    # ------------------------------------------------------------------

    def _dispatch(self, req, timeout):
        """Open req and return (status, text); 4xx/5xx also return (status, generic_text)."""
        opener = self._make_opener()
        try:
            resp = opener.open(req, timeout=timeout)
            status = resp.getcode()
            try:
                text = resp.read(_MAX_BODY).decode("utf-8", errors="replace")
            finally:
                resp.close()
            self._persist()
            return status, text
        except urllib.error.HTTPError as exc:
            status = exc.code
            if status == 429 and self._budget:
                self._response.retry_after = self._budget.limited(exc.headers.get('Retry-After') if exc.headers else None)
            reason = ''
            try:
                payload = json.loads(exc.read(_MAX_ERR).decode('utf-8',errors='replace'))
                reason = str(payload.get('debugMessage') or payload.get('message') or payload.get('name') or '')
                reason = re.sub(r'[\w.+-]+@[\w.-]+', '[email]', reason)
                reason = re.sub(r'[A-Za-z0-9_-]{24,}|\b\d{5,}\b', '[redacted]', reason)
                reason = re.sub(r'(?i)(token|csrf|password|cookie|code)["\s]*[:=][^,\s}]+', r'\1=[redacted]', reason)[:180]
            except Exception:  # noqa: BLE001
                pass
            finally:
                try:
                    exc.close()
                except Exception:  # noqa: BLE001
                    pass
            self._persist()
            # Return generic message — no raw server body echoed
            return status, f"HTTP {status}" + (': '+reason if reason else '')
        except urllib.error.URLError as exc:
            raise OSError(f"Notion request failed") from exc

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def request(self, path, body, headers=None, timeout=60, ndjson=False):
        """POST path with JSON body. Returns (status, text) for all responses.

        Raises OSError only for network/connection failures.
        Thread-safe.
        """
        url = self._build_url(path)
        data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_unredirected_header("Content-Type", "application/json")
        if ndjson:
            req.add_unredirected_header("Accept", "application/x-ndjson")
        for k, v in (headers or {}).items():
            req.add_unredirected_header(k, v)
        with self._lock:
            if self._budget:
                self._budget.acquire()
            return self._dispatch(req, timeout)

    def stream(self, path, body, headers=None, timeout=300, cancel=None):
        """POST path and yield decoded NDJSON lines.

        Raises NotionHttpError for non-2xx initial response.
        Raises OSError for connection failures.
        """
        url = self._build_url(path)
        data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_unredirected_header("Content-Type", "application/json")
        req.add_unredirected_header("Accept", "application/x-ndjson")
        for k, v in (headers or {}).items():
            req.add_unredirected_header(k, v)

        with self._lock:
            if self._budget:
                self._budget.acquire(cancel)
            opener = self._make_opener()
            try:
                resp = opener.open(req, timeout=timeout)
            except urllib.error.HTTPError as exc:
                status = exc.code
                delay = self._budget.limited(exc.headers.get('Retry-After') if exc.headers else None) if status == 429 and self._budget else 30
                try:
                    exc.read(_MAX_ERR)
                except Exception:  # noqa: BLE001
                    pass
                finally:
                    try:
                        exc.close()
                    except Exception:  # noqa: BLE001
                        pass
                raise NotionHttpError(status, retry_after=delay) from exc
            except urllib.error.URLError as exc:
                raise OSError("Notion stream failed") from exc

        received_event = False
        try:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if line:
                    try:
                        event = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        raise NotionStreamError('Notion returned an incomplete or invalid inference stream; reconcile the submitted request') from None
                    if not isinstance(event, dict):
                        raise NotionStreamError('Notion returned an invalid inference event; reconcile the submitted request')
                    received_event = True
                    yield line
            if not received_event:
                raise NotionStreamError('Notion returned an empty inference stream; reconcile the submitted request')
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass
            with self._lock:
                self._persist()
