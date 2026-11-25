"""Live smoke checks; only record diagnostics, never persist chapter text."""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit, urljoin

import requests
from bs4 import BeautifulSoup

from src.catalog_parser import parse_catalog, parse_chapter_number
from src.crawler import fetch_chapter_text
from src.sources import resolve_source
from src.sources.registry import SOURCES
from src.sources.twkan import TwkanSource
from src.sources.sixk import SixKSource
from src.sources.xszj import XszjSource
from src.sources.metanovel import MetaNovelSource
from src.sources.moon17 import Moon17Source
from src.sources.book8 import Book8Source

# Test-only registration: candidates remain unavailable to production until validated.
CANDIDATE_ADAPTERS = {a.source_id: a for a in (TwkanSource(), SixKSource(), XszjSource(), MetaNovelSource(), Moon17Source(), Book8Source())}

SAMPLES = {
    "hjwzw": "https://tw.hjwzw.com/Book/1889",
    "shuba69": "https://www.69shuba.com/book/53782/",
    "czbooks": "https://czbooks.net/n/c37492",
    "twkan": "https://www.twkan.cc/book/2T9qchiJD4h31AQnZuI6gI.html",
    "sixk": "https://www.6ksu.app/novel/5050292.html",
    "xszj": "https://xszj.tw/book/491274",
    "metanovel": "https://m.metanovel.org/book/480384",
    "moon17": "https://www.17moon.com/book/8Kk/",
    "book8": "https://8book.com/novelbooks/168909/",
}
CANDIDATES = {
    "twkan": ("https://www.twkan.cc/book/2T9qchiJD4h31AQnZuI6gI.html", None),
    "moon17": ("https://www.17moon.com/book/8Kk/", "https://www.17moon.com/book/8Kk/eHQyA.html"),
    "book8": ("https://8book.com/novelbooks/168909/", None),
    "xszj": ("https://xszj.tw/book/491274", "https://xszj.tw/book/491274/chapter-1679186"),
    "sixk": ("https://www.6ksu.app/novel/5050292.html", None),
    "metanovel": ("https://m.metanovel.org/book/480384", "https://m.metanovel.org/book/480384/chapter/57083"),
}


def live_adapter(source_id):
    url = SAMPLES[source_id]
    if source_id in CANDIDATE_ADAPTERS:
        SOURCES.setdefault(source_id, CANDIDATE_ADAPTERS[source_id])
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
        if source_id == "hjwzw":
            numbers = [parse_chapter_number(t) for t in catalog["chapter_titles"]]
            if count != 120 or numbers != list(range(1, 121)):
                raise ValueError("Three Kingdoms catalog must contain chapters 1 through 120 in order")
        result["stage"] = "chapter"
        for index in sorted({1, count // 2, count}):
            target = catalog["chapters"][index - 1]
            detail = {"index": index, "url": target, "status": "FAIL"}
            result["chapters"].append(detail)
            try:
                title, body = fetch_chapter_text(target, source_id=source_id)
                size = len(re.findall(r"[\u3400-\u9fff]", body))
                if source_id == "hjwzw" and parse_chapter_number(title) != index:
                    raise ValueError("chapter title does not match expected chapter index")
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
    pending = [("book", book)]
    if chapter:
        pending.append(("chapter", chapter))
    visited = set()
    while pending and len(visited) < 5:
        role, url = pending.pop(0)
        if url in visited:
            continue
        visited.add(url)
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
            if response.status_code == 200:
                links = []
                for tag in soup.select("a[href]"):
                    target = urljoin(response.url, tag["href"])
                    label = tag.get_text(" ", strip=True)
                    if re.search(r"章|回|節|页|頁|目錄|目录|開始閱讀|開始看", label) and len(label) < 120:
                        links.append({"href": target, "label": label[:80], "parent": str(tag.parent.get("class", [])), "grandparent": str(tag.parent.parent.get("class", [])) if tag.parent.parent else ""})
                item["links"] = links[:14] + links[-5:]
                item["headings"] = [{"tag": tag.name, "id": tag.get("id"), "class": tag.get("class"), "label": tag.get_text(" ", strip=True)[:100]} for tag in soup.select("h1,h2")[:8]]
                item["containers"] = []
                for tag in soup.select("div,article,section,main,ul,ol"):
                    if not (tag.get("id") or tag.get("class")):
                        continue
                    count = len(re.findall(r"[\u3400-\u9fff]", tag.get_text("", strip=True)))
                    if count >= 100:
                        item["containers"].append({"tag": tag.name, "id": tag.get("id"), "class": tag.get("class"), "chars": count, "p": len(tag.select("p")), "links": len(tag.select("a")), "br": len(tag.select("br"))})
                item["containers"] = sorted(item["containers"], key=lambda c: c["chars"], reverse=True)[:25]
                if role == "book":
                    catalog = next((l["href"] for l in links if re.search(r"/catalog/|章節列表|章節目錄|全部章節", l["href"] + l["label"]) and l["href"] != url and urlsplit(l["href"]).hostname == urlsplit(url).hostname), None)
                    first = next((l["href"] for l in links if re.search(r"第一[章回]|第\s*1\s*[章回]|開始閱讀|開始看第1", l["label"]) and urlsplit(l["href"]).hostname == urlsplit(url).hostname), None)
                    if catalog: pending.append(("catalog", catalog))
                    if first and not chapter: pending.append(("chapter", first))
                if role == "catalog" and not chapter:
                    first = next((l["href"] for l in links if re.search(r"第一[章回]|第\s*1\s*[章回]", l["label"]) and urlsplit(l["href"]).hostname == urlsplit(url).hostname), None)
                    if first: pending.append(("chapter", first))
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
    parser.add_argument("source", choices=list(SAMPLES))
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
