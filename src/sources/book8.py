"""Parse only legitimate same-site reader links; never rewrite third-party links."""
import re
from urllib.parse import urlsplit
from .base import SourceAdapter, Chapter, SourceParseError
from .paging import read_body


class Book8Source(SourceAdapter):
    source_id = "book8"
    version = "2"
    hosts = ("8book.com", "www.8book.com", "v.8book.com", "m.8book.com", "i.8book.com")
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
        title = self.parse_metadata(html, url)["title"]
        if not title and soup.title:
            title = soup.title.get_text(strip=True).split(",")[0].strip()
        records, seen = [], set()
        for link in root.select("a.episode_li[href], a[href]"):
            target = self.absolute_url(url, link["href"])
            parts = urlsplit(target)
            if not re.fullmatch(rf"/read/{self.book_id(url)}/?", parts.path):
                continue
            cid = parts.query
            if not re.fullmatch(r"\d+=?", cid):
                raise SourceParseError("無限小說章節識別不明")
            cid = cid.rstrip("=")
            label = link.get_text(" ", strip=True)
            if not label:
                raise SourceParseError("無限小說章節標題空白")
            if cid not in seen:
                records.append(Chapter(cid, target, label, label, len(records) + 1))
                seen.add(cid)
        return self.catalog_result(url, title, records)

    def parse_chapter(self, html, url):
        soup = self.soup(html)
        body = soup.select_one("#content")
        heading = soup.select_one("h1, h2, #chapter-title, .chapter-title")
        if body is None or heading is None:
            raise SourceParseError("無限小說尚未取得可辨識的完整閱讀頁")
        for node in body.select("script, style, iframe, nav, button, form, h1, h2"):
            node.decompose()
        text = body.get_text("\n", strip=True)
        if len(re.findall(r"[\u3400-\u9fff]", text)) < 300:
            raise SourceParseError("無限小說閱讀頁沒有完整正文")
        return heading.get_text(" ", strip=True), text
