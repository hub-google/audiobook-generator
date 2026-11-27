"""Strictly offline adapter transport for user-authorized HTML exports."""
import json
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from .base import SourceParseError


def page_key(url):
    p = urlsplit(url)
    if p.scheme not in ("https", "http") or not p.hostname or p.username or p.password:
        raise SourceParseError("invalid saved page URL")
    # Query order and spelling can encode source chapter IDs; never rewrite them.
    return urlunsplit((p.scheme, p.netloc, p.path, p.query, ""))


class SavedPageBundle:
    """pages.json maps exact original URLs to relative HTML files.

    No network fallback. Never read outside the export directory.
    """
    def __init__(self, directory, adapter, book_url):
        self.root = Path(directory).resolve()
        self.adapter = adapter
        self.book_id = adapter.book_id(book_url)
        data = json.loads((self.root / "pages.json").read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("pages"), dict):
            raise SourceParseError("pages.json must contain a pages mapping")
        if data.get("source_id") != adapter.source_id:
            raise SourceParseError("saved pages source_id mismatch")
        self.pages = {}
        for raw_url, relpath in data["pages"].items():
            key = page_key(raw_url)
            adapter.absolute_url(book_url, key)
            if adapter.book_id(key) != self.book_id:
                raise SourceParseError("saved page belongs to another book")
            if not isinstance(relpath, str) or not relpath or Path(relpath).is_absolute():
                raise SourceParseError("invalid relative saved HTML path")
            file = (self.root / relpath).resolve()
            if not file.is_relative_to(self.root) or file.suffix.lower() not in (".html", ".htm"):
                raise SourceParseError("saved HTML path escapes export directory")
            if key in self.pages:
                raise SourceParseError("duplicate saved page URL")
            self.pages[key] = file

    def fetch(self, url):
        self.adapter.absolute_url(url, url)
        if self.adapter.book_id(url) != self.book_id:
            raise SourceParseError("requested page belongs to another book")
        key = page_key(url)
        if key not in self.pages:
            raise SourceParseError(f"缺少完整匯出頁面：{key}")
        return self.pages[key].read_bytes()
