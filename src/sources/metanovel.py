import re
from urllib.parse import urlsplit, parse_qs
from .base import SourceAdapter, SourceParseError
from .paging import merge_catalog, single_catalog, next_page, read_body


class MetaNovelSource(SourceAdapter):
    source_id = "metanovel"
    hosts = ("metanovel.org", "www.metanovel.org", "m.metanovel.org")
    plain_http = True
    min_interval = 2.0

    def book_id(self, url):
        m = re.fullmatch(r"/book/(\d+)(?:/chapters?/\d+)?/?", urlsplit(url).path)
        if not m:
            raise SourceParseError("無效元小說網址")
        return m[1]

    def catalog_url(self, url):
        return self.absolute_url(url, f"/book/{self.book_id(url)}/chapters/1")

    def metadata_url(self, url):
        return self.absolute_url(url, f"/book/{self.book_id(url)}")

    def _title(self, soup):
        tag = soup.select_one("h1.layout-tit")
        return tag.get_text(strip=True).split(">", 1)[0].strip() if tag else ""

    def _next(self, soup, url):
        target = next_page(self, soup, url, ".listpage a[href]")
        if target and not re.fullmatch(rf"/book/{self.book_id(url)}/chapters/\d+", urlsplit(target).path):
            raise SourceParseError("unexpected catalog pagination URL")
        return target

    def load_catalog(self, url, fetch):
        return merge_catalog(self, url, fetch, ".section-list", rf"/book/{self.book_id(url)}/chapter/(\d+)", self._title, self._next)

    def parse_catalog(self, html, url):
        return single_catalog(self, html, url, ".section-list", rf"/book/{self.book_id(url)}/chapter/(\d+)", self._title, self._next)

    def _page(self, html, url):
        soup = self.soup(html)
        title, body = read_body(self, soup, "#content.content", "h1.title")
        marker = re.search(r"\s*[（(](\d+)/(\d+)[）)]\s*$", title)
        page, total = map(int, marker.groups()) if marker else (1, 1)
        title = title[:marker.start()].strip() if marker else title
        target = next_page(self, soup, url, ".section-opt a[href]")
        if target:
            if urlsplit(target).path != urlsplit(url).path or parse_qs(urlsplit(target).query).get("page") != [str(page + 1)]:
                raise SourceParseError("unexpected chapter pagination URL")
        if (page < total) != bool(target):
            raise SourceParseError("chapter pagination incomplete")
        return title, body, page, total, target

    def parse_chapter(self, html, url):
        title, body, page, total, target = self._page(html, url)
        if page != 1 or total != 1 or target:
            raise SourceParseError("saved HTML contains only part of chapter")
        return title, body

    def load_chapter(self, url, fetch):
        title, body, page, total, target = self._page(fetch(url), url)
        if page != 1 or total > 100:
            raise SourceParseError("invalid chapter page count")
        parts = [body]
        for expected in range(2, total + 1):
            if not target:
                raise SourceParseError("missing chapter page")
            other, body, page, count, target = self._page(fetch(target), target)
            if other != title or page != expected or count != total or body in parts:
                raise SourceParseError("inconsistent chapter pagination")
            parts.append(body)
        return title, "\n".join(parts)
