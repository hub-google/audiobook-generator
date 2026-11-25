"""Live smoke checks; only record diagnostics, never persist chapter text."""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

from src.catalog_parser import parse_catalog
from src.crawler import fetch_chapter_text
from src.sources import resolve_source

SAMPLES = {
    "hjwzw": "https://tw.hjwzw.com/Book/1889",
    "shuba69": "https://www.69shuba.com/book/53782/",
    "czbooks": "https://czbooks.net/n/c37492",
}
CANDIDATES = {
    "twkan": ("https://www.twkan.cc/", None),
    "moon17": ("https://www.17moon.com/book/8Kk/", "https://www.17moon.com/book/8Kk/eHQyA.html"),
    "book8": ("https://8book.com/novelbooks/168909/", "https://sport.thepaperbooks.com/read/168909/?39876="),
    "xszj": ("https://xszj.tw/book/491274", "https://xszj.tw/book/491274/chapter-1679186"),
    "sixk": ("https://www.6ksu.app/", None),
    "metanovel": ("https://m.metanovel.org/book/480384", "https://m.metanovel.org/book/480384/chapter/57083"),
}


def live_adapter(source_id):
    url = SAMPLES[source_id]
    adapter = resolve_source(url, source_id)
    result = {"source": source_id, "module": type(adapter).__name__, "url": url,
              "status": "FAIL", "chapters": [], "stage": "catalog"}
    try:
        catalog = parse_catalog(url)
        count = catalog["total_chapters"]
        result["catalog_count"] = count
        result["book_title"] = catalog["book_title"]
        if count < 3:
            raise ValueError("catalog has fewer than 3 chapters")
        result["stage"] = "chapter"
        for index in sorted({1, count // 2, count}):
            target = catalog["chapters"][index - 1]
            detail = {"index": index, "url": target, "status": "FAIL"}
            result["chapters"].append(detail)
            try:
                title, body = fetch_chapter_text(target, source_id=source_id)
                size = len(re.findall(r"[\u3400-\u9fff]", body))
                if not title.strip() or size < 300:
                    raise ValueError(f"empty title or too few Chinese characters: {size}")
                detail.update(status="PASS", title=title, chinese_chars=size,
                              sha256=hashlib.sha256(body.encode("utf-8")).hexdigest())
            except Exception as exc:
                detail["error"] = f"{type(exc).__name__}: {exc}"
        if all(c["status"] == "PASS" for c in result["chapters"]):
            result["status"] = "PASS"
        else:
            result["error"] = "at least one chapter failed"
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def probe_candidate(source_id):
    book, chapter = CANDIDATES[source_id]
    result = {"source": source_id, "status": "NOT_IMPLEMENTED", "pages": []}
    for role, url in (("book", book), ("chapter", chapter)):
        if not url:
            continue
        item = {"role": role, "url": url}
        result["pages"].append(item)
        try:
            response = requests.get(url, timeout=15, headers={"User-Agent": "AUDIO-source-validation/1.0"})
            soup = BeautifulSoup(response.content, "html.parser")
            item.update(http=response.status_code, final_url=response.url,
                        bytes=len(response.content),
                        title=soup.title.get_text(" ", strip=True) if soup.title else "")
            if role == "chapter":
                # Diagnostics only; generic visible text is not verified chapter body.
                item["cross_domain"] = urlsplit(book).hostname != urlsplit(response.url).hostname
                item["pagination_labels"] = [label for label in ("下一頁", "下一页", "下一章")
                                             if label in soup.get_text(" ", strip=True)]
        except Exception as exc:
            item["error"] = f"{type(exc).__name__}: {exc}"
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", choices=[*SAMPLES, *CANDIDATES])
    args = parser.parse_args()
    result = live_adapter(args.source) if args.source in SAMPLES else probe_candidate(args.source)
    report = json.dumps(result, ensure_ascii=False, indent=2)
    print(report, flush=True)
    path = Path("source-report.json")
    path.write_text(report + "\n", encoding="utf-8")
    import os
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as out:
            out.write(f"## {args.source}: {result['status']}\n\n")
            out.write("~~~json\n" + report + "\n~~~\n")
    return 1 if result["status"] == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())
