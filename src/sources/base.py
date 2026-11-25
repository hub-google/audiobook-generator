"""Small, synchronous source contract; parsers can also consume saved HTML."""
from dataclasses import dataclass, asdict
from urllib.parse import urljoin, urlsplit
from bs4 import BeautifulSoup


class SourceParseError(ValueError):
    """Unknown/changed markup; never evidence that a chapter is missing."""


class SourceAccessError(RuntimeError):
    """The server restricted access; retry later or use an authorized export."""


@dataclass
class Chapter:
    source_chapter_id: str
    url: str
    original_title: str
    title: str
    source_order: int


class SourceAdapter:
    source_id = ""
    version = "1"
    hosts = ()
    min_interval = 3.0
    max_parallel = 17
    requires_browser = False
    opening_labels = ()
    encoding = None

    def matches(self, url):
        return urlsplit(url).hostname in self.hosts

    def get_referer(self, url):
        return None

    def catalog_url(self, url):
        return url

    def absolute_url(self, base, href):
        url = urljoin(base, href)
        if urlsplit(url).scheme not in ("http", "https") or not self.matches(url):
            raise SourceParseError(f"Unexpected source URL: {url}")
        return url

    def soup(self, html):
        if isinstance(html, bytes):
            if self.encoding:
                html = html.decode(self.encoding, errors='replace')
            else:
                # Avoid statistical mis-detection on short UTF-8 Chinese pages.
                try:
                    html = html.decode('utf-8')
                except UnicodeDecodeError:
                    pass  # BeautifulSoup honors a declared legacy encoding.
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(" ", strip=True).lower()
        if any(x in text for x in ("just a moment", "captcha", "access denied", "人機驗證", "访问过于频繁", "訪問過於頻繁")):
            raise SourceAccessError("來源網站限制存取，不能視為缺章")
        return soup

    def catalog_result(self, url, title, chapters, **metadata):
        if not title or not chapters:
            raise SourceParseError("找不到書名或完整章節目錄，網站可能改版")
        return dict(success=True, book_title=title, catalog_url=url,
                    base_url=f"{urlsplit(url).scheme}://{urlsplit(url).netloc}",
                    source_id=self.source_id, parser_version=self.version,
                    source_book_id=self.book_id(url),
                    chapters=[c.url for c in chapters],
                    chapter_titles=[c.title for c in chapters],
                    chapter_records=[asdict(c) for c in chapters],
                    total_chapters=len(chapters), **metadata)

    def parse_metadata(self, html, url):
        soup = self.soup(html)
        def meta(name):
            tag = soup.find("meta", attrs={"property": name})
            return str(tag.get("content") or "").strip() if tag else ""
        return {"title": meta("og:novel:book_name") or meta("og:title"),
                "author": meta("og:novel:author"), "category": meta("og:novel:category"),
                "description": meta("og:description"), "url": url}

    def parse_catalog(self, html, url):
        raise NotImplementedError

    def parse_chapter(self, html, url):
        raise NotImplementedError
