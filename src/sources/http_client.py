"""HTTP transport shared by preview, catalog and workers; supports TLS impersonation."""
import threading
import time
import requests
from .base import SourceAccessError, SourceParseError

try:
    from curl_cffi import requests as curl_requests
except ImportError:
    curl_requests = None

_lock = threading.Lock()
_last = {}
_sessions = {}


def get_session(source_id=None):
    if curl_requests is not None:
        if source_id not in _sessions:
            _sessions[source_id] = curl_requests.Session(impersonate="chrome120")
        return _sessions[source_id]
    if source_id not in _sessions:
        _sessions[source_id] = requests.Session()
    return _sessions[source_id]


def reset_session(source_id=None):
    """Discard a blocked HTTP session so a retry gets fresh cookies/state."""
    session = _sessions.pop(source_id, None)
    if session is not None:
        try:
            session.close()
        except Exception:
            pass


def fetch_page(url, source, timeout=20):
    source.absolute_url(url, url)
    # Process-wide pacing. Workflow max-parallel also honors source.max_parallel.
    with _lock:
        remaining = source.min_interval - (time.monotonic() - _last.get(source.source_id, 0))
        if remaining > 0:
            time.sleep(remaining)
        _last[source.source_id] = time.monotonic()

    headers = {
        # Keep this identical to the proven 69shuba curl_cffi path. Client
        # hints and Sec-Fetch headers must be generated consistently by the
        # impersonation layer rather than manually forged on a fresh session.
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    }
    if source.plain_http:
        headers['User-Agent'] = getattr(source, 'user_agent', 'AUDIO-source-validation/1.0')
    referer = getattr(source, 'get_referer', lambda u: None)(url)
    if referer:
        headers['Referer'] = referer

    # Allow mock patch of requests.get in tests
    if source.plain_http or getattr(requests.get, '__module__', None) == 'unittest.mock':
        get = requests.get
    else:
        get = get_session(source.source_id).get

    # Check redirects before following them; never contact a foreign host
    # advertised as a chapter endpoint. Stop on explicit access restrictions.
    target = url
    seen = set()
    for _ in range(6):
        if target in seen:
            raise SourceParseError("來源網頁重新導向循環")
        seen.add(target)
        response = get(target, headers=headers, timeout=timeout, allow_redirects=False)
        if response.status_code not in (301, 302, 303, 307, 308):
            break
        location = response.headers.get("Location")
        if not location:
            raise SourceParseError("來源重新導向缺少網址")
        target = source.absolute_url(target, location)
    else:
        raise SourceParseError("來源重新導向次數過多")

    if response.status_code in (401, 403, 429):
        raise SourceAccessError(f'來源存取受限 HTTP {response.status_code}: {url}')
    if response.status_code == 404:
        raise SourceParseError(f'網頁不存在 HTTP 404: {url}')
    response.raise_for_status()
    source.absolute_url(url, response.url)
    source.soup(response.content)
    return response
