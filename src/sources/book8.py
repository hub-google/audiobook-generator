"""Parse only legitimate same-site reader links; never rewrite third-party links."""
import re
from urllib.parse import urlsplit, urljoin
from .base import SourceAdapter, Chapter, SourceParseError
from .paging import read_body
from .book8_ajax import resolve_reader_ajax


class Book8Source(SourceAdapter):
    source_id = "book8"
    version = "5"
    hosts = ("8book.com", "www.8book.com", "v.8book.com", "m.8book.com", "i.8book.com")
    plain_http = True
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
            original = urljoin(url, link["href"])
            parts = urlsplit(original)
            # The site's own item.js publishes these two third-party jump hosts.
            # They are links, not content sources: reconstruct an internal reader
            # URL and fetch the real AJAX text only from the 8book host.
            if parts.hostname not in (*self.hosts, "sport.thepaperbooks.com", "finance.binaccount.com"):
                raise SourceParseError("無限小說目錄包含未受信任的外站閱讀網址")
            if not re.fullmatch(rf"/read/{self.book_id(url)}/?", parts.path):
                continue
            cid = parts.query
            if not re.fullmatch(r"\d+", cid):
                raise SourceParseError("無限小說章節識別不明")
            target = self.absolute_url(url, f"/read/{self.book_id(url)}/?{cid}")
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
        heading = soup.select_one("#chapter-title, .chapter-title") or soup.select_one("h1") or soup.select_one("h2")
        if body is None or heading is None:
            raise SourceParseError("無限小說尚未取得可辨識的完整閱讀頁")
        for node in body.select("script, style, iframe, nav, button, form, h1, h2"):
            node.decompose()
        for node in body.select("br"):
            node.replace_with("\n")
        for node in body.select("p, div, blockquote"):
            node.insert_after("\n")
        text = "\n".join(line.strip() for line in body.get_text().splitlines() if line.strip())
        if "\ufffd" in text or "\ufffc" in text or len(re.findall(r"[\u3400-\u9fff]", text)) < 300:
            raise SourceParseError("無限小說閱讀頁沒有完整正文")
        return heading.get_text(" ", strip=True), text


    def load_chapter(self, url, fetch):
        # Its initial HTML contains a JS-built AJAX URL, not the body.
        # Recover the URL without executing site JavaScript.
        title, endpoint = resolve_reader_ajax(self, fetch(url), url)
        soup = self.soup(fetch(endpoint))
        if soup.title and "404" in soup.title.get_text(strip=True):
            raise SourceParseError("無限小說 AJAX 正文返回 404 頁面")
        root = soup.select_one("body") or soup
        for tag in root.select("script, style, iframe, nav, button, form"):
            tag.decompose()
        for br in root.select("br"):
            br.replace_with("\n")
        for node in root.select("p, div, blockquote"):
            node.insert_after("\n")
        text = "\n".join(line.strip() for line in root.get_text().splitlines() if line.strip())
        if "\ufffd" in text or "\ufffc" in text or len(re.findall(r"[\u3400-\u9fff]", text)) < 300:
            raise SourceParseError("無限小說 AJAX 正文不完整")
        return title, text
