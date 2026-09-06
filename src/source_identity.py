"""URL identity and storage isolation. Display titles are never storage keys."""
import hashlib
from urllib.parse import urlsplit, urlunsplit


def source_fingerprint(url):
    parts = urlsplit(str(url or '').strip())
    if parts.scheme.lower() not in ('http', 'https') or not parts.hostname:
        raise ValueError('Invalid source URL')
    # Preserve path, trailing slash, query, host aliases and protocol differences.
    identity = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ''))
    return hashlib.sha256(identity.encode('utf-8')).hexdigest()


def workspace_name(config):
    fingerprint = config.get('source_fingerprint')
    if fingerprint:
        if fingerprint != source_fingerprint(config.get('catalog_url')):
            raise ValueError('Source URL fingerprint mismatch')
        return fingerprint
    # Historical configs retain their original directory for strict resume.
    return config['book_title']
