"""Small shared primitives. Site selectors and URL rules stay in adapters."""
import re
from urllib.parse import urlsplit
from .base import Chapter, SourceParseError


def catalog_entries(source, soup, url, selector, pattern):
    root = soup.select_one(selector)
    if root is None:
        raise SourceParseError(f"{source.source_id}: missing catalog")
    records = []
    seen = set()
    for link in root.select("a[href]"):
        href = link["href"]
        match = re.fullmatch(pattern, urlsplit(href).path)
        if not match:
            continue
        target = source.absolute_url(url, href)
        cid = match[1]
        label = link.get_text(" ", strip=True)
        if not label:
            raise SourceParseError("empty chapter title")
        if cid not in seen:
            records.append(Chapter(cid, target, label, label, len(records) + 1))
            seen.add(cid)
    if not records:
        raise SourceParseError(f"{source.source_id}: empty catalog")
    return records


def next_page(source, soup, url, selector, label=r"下一[頁页]"):
    targets = set()
    for link in soup.select(selector):
        if re.search(label, link.get_text(" ", strip=True)) and link.get("href"):
            targets.add(source.absolute_url(url, link["href"]))
    if len(targets) > 1:
        raise SourceParseError("conflicting pagination links")
    return next(iter(targets), None)


def read_body(source, soup, selector, heading):
    body = soup.select_one(selector)
    title = soup.select_one(heading)
    if body is None or title is None:
        raise SourceParseError(f"{source.source_id}: missing chapter body or title")
    for node in body.select("script,style,iframe,form,button,nav,[hidden],.adsbygoogle"):
        node.decompose()
    text = body.get_text("\n", strip=True)
    if not text.strip():
        raise SourceParseError(f"{source.source_id}: empty chapter body")
    return title.get_text(" ", strip=True), text


def merge_catalog(source, url, fetch, selector, pattern, title_from, next_from):
    seen_pages = set()
    seen_ids = set()
    chapters = []
    current = url
    title = None
    while current:
        if current in seen_pages or len(seen_pages) >= 1000:
            raise SourceParseError("catalog pagination loop or limit")
        seen_pages.add(current)
        soup = source.soup(fetch(current))
        current_title = title_from(soup)
        if title is None:
            title = current_title
        elif title != current_title:
            raise SourceParseError("catalog title changed during pagination")
        records = catalog_entries(source, soup, current, selector, pattern)
        for record in records:
            if record.source_chapter_id in seen_ids:
                raise SourceParseError("duplicate chapter across catalog pages")
            seen_ids.add(record.source_chapter_id)
            record.source_order = len(chapters) + 1
            chapters.append(record)
        current = next_from(soup, current)
    return source.catalog_result(url, title, chapters)


def single_catalog(source, html, url, selector, pattern, title_from, next_from):
    soup = source.soup(html)
    if next_from(soup, url):
        raise SourceParseError("saved HTML contains only part of catalog; full authorized export required")
    return source.catalog_result(url, title_from(soup), catalog_entries(source, soup, url, selector, pattern))
