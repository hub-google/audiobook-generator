"""Check multiple real public catalogs and their advertised reader links."""
import hashlib
import json
import re
from urllib.parse import urlsplit, urljoin

from src.sources.book8 import Book8Source
from src.sources.http_client import fetch_page
from src.sources.base import SourceParseError

src = Book8Source()
LISTING = "https://www.8book.com/booklists/update/1.html"
ORIGINAL = "https://8book.com/novelbooks/168909/"


def inspect(url):
    item = {"url":url,"status":"FAIL","chapters":[]}
    try:
        html = fetch_page(src.catalog_url(url),src).content
        soup = src.soup(html)
        item["catalog_links"] = len(soup.select("#chapters a[href]"))
        item["reader_hosts"] = sorted({urlsplit(urljoin(url,a["href"])).hostname or "" for a in soup.select("#chapters a[href]")})
        catalog = src.parse_catalog(html,src.catalog_url(url))
        n = catalog["total_chapters"]
        item["catalog_count"] = n
        if n < 3:
            raise SourceParseError("目錄少於三筆")
        for idx in sorted({0,n//2,n-1}):
            record = catalog["chapter_records"][idx]
            row = {"index":idx+1,"url":record["url"],"status":"FAIL"}
            item["chapters"].append(row)
            try:
                title, body = src.load_chapter(record["url"],lambda target:fetch_page(target,src).content)
                count = len(re.findall(r"[\u3400-\u9fff]",body))
                if count < 300:
                    raise SourceParseError("正文不完整")
                row.update(status="PASS",title=title,chinese_chars=count,sha256=hashlib.sha256(body.encode()).hexdigest())
            except Exception as e:
                row["error"] = f"{type(e).__name__}: {e}"
        item["status"] = "PASS" if all(x["status"]=="PASS" for x in item["chapters"]) else "FAIL"
    except Exception as e:
        item["error"] = f"{type(e).__name__}: {e}"
    return item

def main():
    urls = [ORIGINAL]
    report = {"source":"book8","status":"FAIL","books":[]}
    try:
        soup = src.soup(fetch_page(LISTING,src).content)
        for a in soup.select("a[href]"):
            url = urljoin(LISTING,a["href"])
            if src.matches(url) and re.fullmatch(r"/novelbooks/\d+/?",urlsplit(url).path):
                if src.book_id(url) not in {src.book_id(u) for u in urls}:
                    urls.append(url)
            if len(urls) >= 4:
                break
    except Exception as e:
        report["listing_error"] = f"{type(e).__name__}: {e}"
    report["books"] = [inspect(url) for url in urls]
    report["status"] = "PASS" if len(urls)>=3 and all(x["status"]=="PASS" for x in report["books"]) else "FAIL"
    print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
    return report["status"] == "PASS"

if __name__ == "__main__":
    import sys
    sys.exit(0 if main() else 1)
