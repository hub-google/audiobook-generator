import re
from urllib.parse import urlsplit
from .base import SourceAdapter, Chapter, SourceParseError


class Shuba69Source(SourceAdapter):
    source_id = "shuba69"
    hosts = ("www.69shuba.com", "69shuba.com")
    encoding = "gb18030"
    min_interval = 1.0

    def book_id(self, url):
        match = re.search(r'/(?:book|txt)/(\d+)', urlsplit(url).path, re.I)
        if not match:
            raise SourceParseError('無效的 69 書吧書籍網址')
        return match[1]

    def get_referer(self, url):
        try:
            bid = self.book_id(url)
            return self.absolute_url(url, f'/book/{bid}/')
        except Exception:
            return f"{urlsplit(url).scheme}://{urlsplit(url).netloc}/"

    def metadata_url(self, url):
        return self.absolute_url(url, f'/book/{self.book_id(url)}.htm')

    def clean_title(self, title):
        return re.sub(r'^\s*\d+\s*[.．]\s*', '', title).strip()

    def parse_metadata(self, html, url):
        result = super().parse_metadata(html, url)
        body = self.soup(html).select_one('.navtxt')
        if body:
            result['description'] = body.get_text('\n', strip=True)
        return result

    def parse_catalog(self, html, url):
        soup = self.soup(html)
        root = soup.select_one('#catalog')
        if root is None:
            raise SourceParseError('69 書吧目錄容器不存在')
        heading = root.find('h1') or soup.find('h1')
        title = re.sub(r'(最新章[節节]|章[節节]列表).*$', '', heading.get_text(strip=True)) if heading else ''
        entries = []
        book_id = self.book_id(url)
        for a in root.select('a[href]'):
            href = self.absolute_url(url, a['href'])
            match = re.fullmatch(r'/txt/' + re.escape(book_id) + r'/(\d+)/?', urlsplit(href).path)
            if match:
                label = a.get_text(strip=True)
                entries.append((match[1], href, label))
        # DOM may be descending. IDs are site publication IDs, not title numbers.
        if len(entries) > 1 and int(entries[0][0]) > int(entries[-1][0]):
            entries.reverse()
        chapters = [Chapter(cid, href, label, self.clean_title(label), i)
                    for i, (cid, href, label) in enumerate(entries, 1)]
        return self.catalog_result(url, title, chapters)

    def parse_chapter(self, html, url):
        soup = self.soup(html)
        body = soup.select_one('.txtnav')
        heading = soup.find('h1')
        if body is None or heading is None:
            raise SourceParseError('69 書吧正文結構不符')
        title = self.clean_title(heading.get_text(strip=True))
        for node in body.select('h1, .txtinfo, .bottom-ad, .contentadv, script, style, iframe, .page1'):
            node.decompose()
        text = body.get_text('\n', strip=True)
        text = re.sub(r'(?:\n|^)[（(]本章完[）)]\s*$', '', text).strip()
        if not text:
            raise SourceParseError('69 書吧正文空白')
        return title, text
