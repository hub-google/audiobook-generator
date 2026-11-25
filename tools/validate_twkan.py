"""TWKan-specific diagnostics for publicly readable chapters; retain no novel text."""
import hashlib
import json
import re
from urllib.parse import urlsplit

from src.sources.twkan import TwkanSource
from src.sources.http_client import fetch_page
from src.sources.base import SourceAccessError, SourceParseError

# Retain the original failing sample, alongside separate public book pages.
BOOKS = [
    "https://www.twkan.cc/book/2T9qchiJD4h31AQnZuI6gI.html",
    "https://www.twkan.cc/book/5xqpGOcl46z9RzbASUJWgx.html",
    "https://www.twkan.cc/book/XdnDgELi9rQ0c8Vn7sTvD.html",
]

def inspect(url):
    src = TwkanSource()
    result = {"url": url, "status": "FAIL", "chapters": [], "catalog_pages": []}
    def fetch(target):
        response = fetch_page(target, src)
        if "/chapter/" in urlsplit(target).path and re.search(r"/(?:\d+|[A-Za-z0-9]+)\.html$", urlsplit(target).path):
            soup = src.soup(response.content)
            if soup.select_one("#allchapter"):
                result["catalog_pages"].append({"url": target, "links": len(soup.select("#allchapter a[href]"))})
        return response.content
    try:
        catalog = src.load_catalog(src.catalog_url(url), fetch)
        result["book_title"] = catalog["book_title"]
        records = catalog["chapter_records"]
        n = len(records)
        result["catalog_count"] = n
        if n < 3:
            raise SourceParseError("fewer than three catalog entries")
        indexes = sorted({0, n//2, max(0,n-2), n-1})
        for index in indexes:
            record = records[index]
            detail = {"index": index+1, "url": record["url"], "expected_title": record["title"], "status": "FAIL"}
            result["chapters"].append(detail)
            try:
                html = fetch(record["url"])
                soup = src.soup(html)
                body = soup.select_one("#content.readcontent")
                detail["raw_chinese_chars"] = len(re.findall(r"[\u3400-\u9fff]", body.get_text("", strip=True))) if body else None
                detail["raw_paragraphs"] = len(body.select("p")) if body else None
                title, text = src.parse_chapter(html, record["url"])
                if re.sub(r"\s+", "", title) != re.sub(r"\s+", "", record["title"]):
                    raise SourceParseError("chapter heading differs from catalog label")
                detail.update(status="PASS", title=title, chinese_chars=len(re.findall(r"[\u3400-\u9fff]", text)),
                              sha256=hashlib.sha256(text.encode()).hexdigest())
            except (SourceAccessError, SourceParseError) as exc:
                detail["error"] = str(exc)
                detail["category"] = type(exc).__name__
            except Exception as exc:
                detail["error"] = str(exc)
                detail["category"] = type(exc).__name__
        result["status"] = "PASS" if all(c["status"]=="PASS" for c in result["chapters"]) else "FAIL"
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result

if __name__ == "__main__":
    import sys
    results = [inspect(url) for url in BOOKS]
    print(json.dumps(results, ensure_ascii=False, indent=2), flush=True)
    sys.exit(0 if all(r["status"] == "PASS" for r in results) else 1)
