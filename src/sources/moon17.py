import re
from urllib.parse import urlsplit
from .base import SourceAdapter, SourceParseError
from .paging import catalog_entries, read_body


class Moon17Source(SourceAdapter):
    source_id = "moon17"
    hosts = ("17moon.com", "www.17moon.com")
    plain_http = True
    max_parallel = 1
    min_interval = 2.0

    def book_id(self, url):
        m = re.fullmatch(r"/book/([A-Za-z0-9]+)(?:/[A-Za-z0-9_]+\.html)?/?", urlsplit(url).path)
        if not m:
            raise SourceParseError("無效月亮小說網網址")
        return m[1]

    def catalog_url(self, url):
        return self.absolute_url(url, f"/book/{self.book_id(url)}/")

    metadata_url = catalog_url

    def parse_catalog(self, html, url):
        soup = self.soup(html)
        title = soup.select_one("h1.info-title")
        records = catalog_entries(self, soup, url, "#catalogGroups", rf"/book/{self.book_id(url)}/([A-Za-z0-9]+)\.html")
        return self.catalog_result(url, title.get_text(strip=True) if title else "", records)

    def parse_chapter(self, html, url):
        soup = self.soup(html)
        title, body = read_body(self, soup, "article#article", "h1.reader-header__title")
        if any(a.get_text(strip=True) == "下一頁" for a in soup.select(".reader-header__nav a")):
            raise SourceParseError("月亮小說網章節有分頁；完整且可驗證的正文尚未取得")
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        if sum(line.startswith(("。", "，", "？", "！", "”", "」")) for line in lines) >= 3:
            raise SourceParseError("疑似文字順序異常，不可用於有聲小說")
        return re.sub(r"^《[^》]+》", "", title).strip(), body
