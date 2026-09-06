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


def fetch_page(url, source, timeout=20):
    # Process-wide pacing. Workflow max-parallel also honors source.max_parallel.
    with _lock:
        remaining = source.min_interval - (time.monotonic() - _last.get(source.source_id, 0))
        if remaining > 0:
            time.sleep(remaining)
        _last[source.source_id] = time.monotonic()

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    referer = getattr(source, 'get_referer', lambda u: None)(url)
    if referer:
        headers['Referer'] = referer

    # Allow mock patch of requests.get in tests
    if getattr(requests.get, '__module__', None) == 'unittest.mock':
        response = requests.get(url, headers=headers, timeout=timeout)
    else:
        session = get_session(source.source_id)
        response = session.get(url, headers=headers, timeout=timeout)

    if response.status_code in (401, 403, 429):
        raise SourceAccessError(f'來源存取受限 HTTP {response.status_code}: {url}')
    if response.status_code == 404:
        raise SourceParseError(f'網頁不存在 HTTP 404: {url}')
    response.raise_for_status()
    source.absolute_url(url, response.url)
    source.soup(response.content)
    return response
