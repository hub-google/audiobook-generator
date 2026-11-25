"""CZBooks source for authorized audiobook production."""
import re
from urllib.parse import urlsplit, parse_qs, urljoin
from .base import SourceAdapter, Chapter, SourceParseError


class CZBooksSource(SourceAdapter):
    source_id = "czbooks"
    version = "3"
    hosts = ("czbooks.net", "www.czbooks.net")
    min_interval = 3.0
    max_parallel = 1

    def book_id(self, url):
        match = re.fullmatch(r"/n/([A-Za-z0-9_-]+)(?:/[A-Za-z0-9_-]+)?/?", urlsplit(url).path)
        if not match:
            raise SourceParseError("無效的小說狂人書籍網址")
        return match[1]

    def catalog_url(self, url):
        parts = urlsplit(url)
        query = f"?{parts.query}" if parts.query and not re.match(r"/n/[^/]+/[^/]+", parts.path) else ""
        return self.absolute_url(url, f"/n/{self.book_id(url)}{query}")

    def metadata_url(self, url):
        return self.catalog_url(url)

    def parse_metadata(self, html, url):
        soup = self.soup(html)
        result = super().parse_metadata(html, url)
        for field, selector in (("title", ".novel-detail .title"),
                                ("author", ".novel-detail .author a"),
                                ("description", ".novel-detail .description")):
            node = soup.select_one(selector)
            if node:
                result[field] = node.get_text("\n" if field == "description" else " ", strip=True)
        heading = soup.find("h1")
        if not result["title"] and heading:
            result["title"] = heading.get_text(" ", strip=True)
        result["title"] = result["title"].strip().removeprefix("《").removesuffix("》")
        for row in soup.select(".novel-detail .state tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) >= 2 and cells[0].get_text(strip=True) == "分類":
                result["category"] = cells[1].get_text(strip=True)
        return result

    def parse_catalog(self, html, url):
        soup = self.soup(html)
        root = soup.select_one("#chapter-list") or soup.select_one(".chapter-list")
        if root is None:
            raise SourceParseError("小說狂人目錄容器不存在")
        pattern = re.compile(r"/n/" + re.escape(self.book_id(url)) + r"/([A-Za-z0-9_-]+)/?")
        entries = []
        seen = set()
        for link in root.select("a[href]"):
            href = link["href"]
            if not pattern.fullmatch(urlsplit(urljoin(url, href)).path):
                continue
            target = self.absolute_url(url, href)
            cid = pattern.fullmatch(urlsplit(target).path)[1]
            label = link.get_text(" ", strip=True)
            if not label:
                raise SourceParseError("小說狂人章節標題空白")
            if cid in seen:
                continue
            seen.add(cid)
            seq = parse_qs(urlsplit(target).query).get("chapterNumber", [""])[0]
            entries.append((cid, target, label, int(seq) if seq.isdigit() else None))
        # The site's chapterNumber is a zero-based order, not a chapter ID.
        if entries and all(item[3] is not None for item in entries):
            if len({item[3] for item in entries}) != len(entries):
                raise SourceParseError("小說狂人章節順序重複")
            entries.sort(key=lambda item: item[3])
            first = entries[0][3]
            if first not in (0, 1) or [item[3] for item in entries] != list(range(first, first + len(entries))):
                raise SourceParseError("小說狂人目錄順序不連續；拒絕不完整目錄")
        chapters = [Chapter(cid, target, label, label, i)
                    for i, (cid, target, label, _) in enumerate(entries, 1)]
        metadata = self.parse_metadata(html, url)
        return self.catalog_result(url, metadata["title"], chapters, metadata=metadata)

    def parse_chapter(self, html, url):
        soup = self.soup(html)
        body = soup.select_one("div.chapter-content .content") or soup.select_one("div.content")
        heading = soup.find("h1")
        if body is None or heading is None:
            raise SourceParseError("小說狂人正文結構不符")
        title = heading.get_text(" ", strip=True)
        # Keep the original chapter title, excluding only the site book prefix.
        title = re.sub(r"^[〖【《][^〗】》]+[〗】》]\s*", "", title)
        for node in body.select("script, style, iframe, h1, form, button, nav, .adsbygoogle, .chapter-navigation"):
            node.decompose()
        for node in body.select("br"):
            node.replace_with("\n")
        for node in body.select("p, div, blockquote"):
            node.insert_after("\n")
        text = "\n".join(line.strip() for line in body.get_text().splitlines() if line.strip())
        if not title or not text or "\ufffd" in text or "\ufffc" in text:
            raise SourceParseError("小說狂人標題或正文空白")
        return title, text
