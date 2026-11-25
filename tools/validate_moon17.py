"""Validate the public reader's normal rendered DOM; no challenge solving."""
import hashlib
import json
import re
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright
from src.sources.moon17 import Moon17Source
from src.sources.http_client import fetch_page
from src.sources.base import SourceAccessError, SourceParseError

URL = "https://www.17moon.com/book/8Kk/"
src = Moon17Source()


def main():
    report = {"source": src.source_id, "status": "FAIL", "chapters": []}
    try:
        catalog = src.load_catalog(URL, lambda url: fetch_page(url, src).content)
        report["catalog_count"] = catalog["total_chapters"]
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            def fetch_rendered(url):
                response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
                if response is None or response.status in (401,403,429):
                    raise SourceAccessError(f"正常瀏覽器存取受限 HTTP {response.status if response else 'unknown'}")
                src.absolute_url(url, page.url)
                page.locator("article#article").wait_for(timeout=10000)
                page.wait_for_timeout(1200)
                return page.content()
            n = catalog["total_chapters"]
            for i in (1,n//2,n):
                url = catalog["chapters"][i-1]
                item = {"index": i,"url":url,"status":"FAIL"}
                report["chapters"].append(item)
                try:
                    title, body = src.load_chapter(url, fetch_rendered)
                    count = len(re.findall(r"[\u3400-\u9fff]",body))
                    if count < 300:
                        raise SourceParseError("正文不完整")
                    item.update(status="PASS",title=title,chinese_chars=count,sha256=hashlib.sha256(body.encode()).hexdigest())
                except Exception as e:
                    item["error"] = f"{type(e).__name__}: {e}"
                    if isinstance(e,SourceAccessError):
                        break
            browser.close()
        report["status"] = "PASS" if len(report["chapters"])==3 and all(x["status"]=="PASS" for x in report["chapters"]) else "FAIL"
    except Exception as e:
        report["error"] = f"{type(e).__name__}: {e}"
    print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
    return report["status"] == "PASS"

if __name__ == "__main__":
    import sys
    sys.exit(0 if main() else 1)
