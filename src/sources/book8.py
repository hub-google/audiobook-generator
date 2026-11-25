import re
from urllib.parse import urlsplit
from .base import SourceAdapter, SourceParseError


class Book8Source(SourceAdapter):
    source_id = "book8"
    hosts = ("8book.com", "www.8book.com", "v.8book.com", "m.8book.com")
    plain_http = True
    max_parallel = 1
    min_interval = 2.0

    def book_id(self, url):
        m = re.fullmatch(r"/(?:novelbooks|read)/(\d+)/?", urlsplit(url).path)
        if not m:
            raise SourceParseError("無效無限小說網址")
        return m[1]

    def catalog_url(self, url):
        return self.absolute_url(url, f"/novelbooks/{self.book_id(url)}/")

    metadata_url = catalog_url

    def parse_catalog(self, html, url):
        soup = self.soup(html)
        root = soup.select_one("#chapters")
        if not root:
            raise SourceParseError("無限小說目錄不存在")
        for link in root.select("a[href]"):
            self.absolute_url(url, link["href"])
        raise SourceParseError("無限小說正文結構尚未驗證；不啟用")

    def parse_chapter(self, html, url):
        raise SourceParseError("無限小說正文未驗證；不啟用")
