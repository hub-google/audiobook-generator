import re
from urllib.parse import urlsplit
from .base import SourceAdapter, SourceParseError
from .paging import catalog_entries, read_body


class TwkanSource(SourceAdapter):
    source_id = "twkan"
    hosts = ("twkan.cc", "www.twkan.cc")
    plain_http = True
    max_parallel = 1
    min_interval = 2.0

    def book_id(self, url):
        m = re.fullmatch(r"/(?:book|chapter)/([A-Za-z0-9]+)(?:/[A-Za-z0-9]+)?\.html", urlsplit(url).path)
        if not m:
            raise SourceParseError("無效台灣小說網網址")
        return m[1]

    def catalog_url(self, url):
        return self.absolute_url(url, f"/chapter/{self.book_id(url)}.html")

    def metadata_url(self, url):
        return self.absolute_url(url, f"/book/{self.book_id(url)}.html")

    def parse_catalog(self, html, url):
        soup = self.soup(html)
        title = soup.find("h1")
        name = re.sub(r"章節列表\s*$", "", title.get_text(strip=True)) if title else ""
        records = catalog_entries(self, soup, url, "#allchapter", rf"/chapter/{self.book_id(url)}/([A-Za-z0-9]+)\.html")
        return self.catalog_result(url, name, records)

    def parse_chapter(self, html, url):
        return read_body(self, self.soup(html), "#content.readcontent", "h1.pt10")
