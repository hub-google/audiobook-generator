import re
from urllib.parse import urlsplit
from .base import SourceAdapter, SourceParseError
from .paging import merge_catalog, single_catalog, next_page, read_body


class XszjSource(SourceAdapter):
    source_id = "xszj"
    hosts = ("xszj.tw", "www.xszj.tw")
    plain_http = True
    max_parallel = 1
    min_interval = 2.0

    def book_id(self, url):
        m = re.fullmatch(r"/book/(\d+)(?:/catalog/\d+|/chapter-\d+)?/?", urlsplit(url).path)
        if not m:
            raise SourceParseError("無效小說之家網址")
        return m[1]

    def catalog_url(self, url):
        return self.absolute_url(url, f"/book/{self.book_id(url)}/catalog/1")

    def metadata_url(self, url):
        return self.absolute_url(url, f"/book/{self.book_id(url)}")

    def _title(self, soup):
        tag = soup.select_one("h1#chap-list-title")
        return re.sub(r"章節目錄\s*$", "", tag.get_text(strip=True)) if tag else ""

    def _next(self, soup, url):
        target = next_page(self, soup, url, ".chapterlist-nav a[href]")
        if target and not re.fullmatch(rf"/book/{self.book_id(url)}/catalog/\d+", urlsplit(target).path):
            raise SourceParseError("unexpected catalog pagination URL")
        return target

    def load_catalog(self, url, fetch):
        return merge_catalog(self, url, fetch, ".chapter-grid", rf"/book/{self.book_id(url)}/chapter-(\d+)", self._title, self._next)

    def parse_catalog(self, html, url):
        return single_catalog(self, html, url, ".chapter-grid", rf"/book/{self.book_id(url)}/chapter-(\d+)", self._title, self._next)

    def parse_chapter(self, html, url):
        return read_body(self, self.soup(html), "#reading-body", "h1#reading-chap-title")
