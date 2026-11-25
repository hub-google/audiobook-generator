import re
from urllib.parse import urlsplit
from .base import SourceAdapter, SourceParseError
from .paging import catalog_entries, read_body


class SixKSource(SourceAdapter):
    source_id = "sixk"
    version = "2"
    min_timeout = 30
    transient_retries = 2
    hosts = ("6ksu.app", "www.6ksu.app")
    plain_http = True
    max_parallel = 1
    min_interval = 2.0

    def book_id(self, url):
        m = re.fullmatch(r"/novel/(\d+)(?:/chapter/\d+)?\.html", urlsplit(url).path)
        if not m:
            raise SourceParseError("無效6K網址")
        return m[1]

    def catalog_url(self, url):
        return self.absolute_url(url, f"/novel/{self.book_id(url)}.html")

    metadata_url = catalog_url

    def parse_catalog(self, html, url):
        soup = self.soup(html)
        title = soup.find("h1")
        records = catalog_entries(self, soup, url, ".chapter-grid", rf"/novel/{self.book_id(url)}/chapter/(\d+)\.html")
        # Explicit pagination cannot silently become a truncated catalog.
        for a in soup.select("a[href]"):
            if re.search(r"下一[頁页]", a.get_text(strip=True)):
                raise SourceParseError("6K目錄分頁尚未完整取得")
        return self.catalog_result(url, title.get_text(strip=True) if title else "", records)

    def parse_chapter(self, html, url):
        return read_body(self, self.soup(html), "article.chapter", "h1.reader-title")
