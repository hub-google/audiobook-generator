import re
from urllib.parse import urlsplit
from .base import SourceAdapter, SourceParseError
from .paging import catalog_entries, read_body, next_page
from .moon17_reader import render_article


class Moon17Source(SourceAdapter):
    source_id = "moon17"
    version = "5"
    hosts = ("17moon.com", "www.17moon.com")
    plain_http = False
    min_timeout = 30.0
    transient_retries = 1
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

    def _page(self, html, url):
        soup = self.soup(html)
        # Recreate the site's published public reader.js paragraph order and
        # the visible Unicode characters rendered by its CSS icon font.
        article = soup.select_one("article#article")
        if article:
            render_article(article)
        title, body = read_body(self, soup, "article#article", "h1.reader-header__title")
        if "\ufffd" in body or "\ufffc" in body:
            raise SourceParseError("月亮小說網正文有未解出的字元")
        title = re.sub(r"^《[^》]+》", "", title).strip()
        match = re.search(r"\s*[（(](\d+)[）)]\s*$", title)
        page = int(match[1]) if match else 1
        title = title[:match.start()].strip() if match else title
        target = next_page(self, soup, url, ".reader-header__nav a[href]")
        if target:
            base_path = re.sub(r"_\d+\.html$", ".html", urlsplit(url).path)
            if urlsplit(target).path != base_path[:-5] + f"_{page + 1}.html" or urlsplit(target).query != urlsplit(url).query:
                raise SourceParseError("unexpected chapter pagination URL")
        return title, body, page, target

    def _check_text(self, body):
        # Reject visibly corrupt text rather than treating a word count as success.
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        if sum(line.startswith(("。", "，", "？", "！", "”", "」", "：")) for line in lines) >= 3:
            raise SourceParseError("疑似文字順序異常；須使用正常可讀且授權的正文")

    def parse_chapter(self, html, url):
        title, body, page, target = self._page(html, url)
        if target or page != 1:
            raise SourceParseError("saved HTML contains only part of chapter")
        self._check_text(body)
        return title, body

    def load_chapter(self, url, fetch):
        title, body, page, target = self._page(fetch(url), url)
        if page != 1:
            raise SourceParseError("chapter must start at page 1")
        parts = [body]
        visited = {url}
        while target:
            if target in visited or len(visited) >= 100:
                raise SourceParseError("chapter pagination loop or limit")
            visited.add(target)
            other, body, page, target = self._page(fetch(target), target)
            if other != title or page != len(parts) + 1 or body in parts:
                raise SourceParseError("inconsistent chapter pagination")
            parts.append(body)
        text = "\n".join(parts)
        self._check_text(text)
        return title, text
