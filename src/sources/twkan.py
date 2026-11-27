import re
from urllib.parse import urlsplit
from .base import SourceAdapter, SourceParseError
from .paging import merge_catalog, single_catalog, next_page, read_body


class TwkanSource(SourceAdapter):
    source_id = "twkan"
    version = "3"
    hosts = ("twkan.cc", "www.twkan.cc", "twkan.com", "www.twkan.com")
    plain_http = True
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

    def _title(self, soup):
        title = soup.find("h1")
        return re.sub(r"章節列表\s*(?:[（(]?第?\s*\d+\s*頁[）)]?)?\s*$", "", title.get_text(strip=True)) if title else ""

    def _next(self, soup, url):
        target = next_page(self, soup, url, ".mulu-box a[href]")
        if target and not re.fullmatch(rf"/chapter/{self.book_id(url)}/\d+\.html", urlsplit(target).path):
            raise SourceParseError("台灣小說網目錄分頁網址不符")
        return target

    def _pattern(self, url):
        return rf"/chapter/{self.book_id(url)}/([A-Za-z0-9]+)\.html"

    def load_catalog(self, url, fetch):
        return merge_catalog(self, url, fetch, "#allchapter", self._pattern(url), self._title, self._next)

    def parse_catalog(self, html, url):
        return single_catalog(self, html, url, "#allchapter", self._pattern(url), self._title, self._next)

    @staticmethod
    def is_notice(title):
        # Only an explicit announcement title; a short numbered story chapter
        # cannot be reclassified just to make a crawl pass.
        label = re.sub(r"^\s*第\s*\d+\s*[章回節]\s*", "", title)
        return bool(re.match(r"^(?:新書[《「〈]|新書發布|新書上架|新書已發|完本感言|上架感言|請假條|請假公告|更新公告|作者公告)", label))

    def parse_chapter(self, html, url):
        soup = self.soup(html)
        title, body = read_body(self, soup, "#content.readcontent", "h1.pt10")
        if any(re.search(r"下一[頁页]", a.get_text(strip=True)) for a in soup.select("a[href]")):
            raise SourceParseError("台灣小說網正文分頁尚未完整取得")
        if len(re.findall(r"[\u3400-\u9fff]", body)) < 300 and not self.is_notice(title):
            raise SourceParseError(f"台灣小說網正文過短，疑似預告、公告或缺文：{title}")
        return title, body
