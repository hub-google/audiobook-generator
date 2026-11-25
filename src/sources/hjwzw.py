import re
from urllib.parse import urlsplit
from .base import SourceAdapter, Chapter, SourceParseError


class HJWZWSource(SourceAdapter):
    source_id = "hjwzw"
    max_parallel = 17  # Preserve the established HJWZW production concurrency.
    hosts = ("tw.hjwzw.com", "www.hjwzw.com", "hjwzw.com")
    opening_labels = ("黃金屋", "黄金屋")

    def book_id(self, url):
        match = re.search(r"/Book/(?:Chapter/)?(\d+)", urlsplit(url).path, re.I)
        if not match:
            raise SourceParseError("無效的黃金屋書籍網址")
        return match[1]

    def catalog_url(self, url):
        query = f'?{urlsplit(url).query}' if urlsplit(url).query else ''
        return self.absolute_url(url, f"/Book/Chapter/{self.book_id(url)}{query}")

    def metadata_url(self, url):
        return self.absolute_url(url, f"/Book/{self.book_id(url)}")

    def parse_catalog(self, html, url):
        soup = self.soup(html)
        title = soup.find("h1")
        metadata = self.parse_metadata(html, url)
        title = title.get_text(strip=True) if title else metadata['title']
        if not title and soup.title:
            title = soup.title.get_text().split('-')[0].split('_')[0].strip()
        chapters = []
        for a in soup.find_all('a', href=True):
            if '/book/read/' not in a['href'].lower() and '/read/' not in a['href'].lower():
                continue
            href = self.absolute_url(url, a['href'])
            label = a.get_text(strip=True)
            chapters.append(Chapter(urlsplit(href).path + ('?' + urlsplit(href).query if urlsplit(href).query else ''), href, label, label, len(chapters)+1))
        return self.catalog_result(url, title, chapters)

    def parse_chapter(self, html, url):
        soup = self.soup(html)
        title = soup.find('h1')
        body = soup.find('div', style=lambda v: v and 'word-wrap: break-word' in v and 'text-indent: 2em' in v)
        if not title or not body or not body.get_text(strip=True):
            raise SourceParseError('黃金屋章節結構不符或正文空白')
        for node in body.select('script, style, iframe'):
            node.decompose()
        return title.get_text(strip=True), body.get_text(separator='\n', strip=True)
