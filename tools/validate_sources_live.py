"""Live validation of the nine production source adapters, without test-only registration."""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

from src.catalog_parser import parse_catalog, parse_chapter_number
from src.crawler import fetch_chapter_text
from src.sources import resolve_source, SourceAccessError
from src.sources.registry import SOURCES

SAMPLES = {
    "hjwzw": "https://tw.hjwzw.com/Book/1889",
    "shuba69": "https://www.69shuba.com/book/53782/",
    "czbooks": "https://czbooks.net/n/c37492",
    "twkan": "https://www.twkan.cc/book/5xqpGOcl46z9RzbASUJWgx.html",
    "sixk": "https://www.6ksu.app/novel/5050292.html",
    "xszj": "https://xszj.tw/book/491274",
    "metanovel": "https://m.metanovel.org/book/480384",
    "moon17": "https://www.17moon.com/book/8Kk/",
    "book8": "https://8book.com/novelbooks/168909/",
}
def live_adapter(source_id):
    # Production registration, not a test-only fallback, is authoritative.
    if set(SAMPLES) != set(SOURCES):
        raise ValueError("Live source samples and production registry have diverged")
    url = SAMPLES[source_id]
    adapter = resolve_source(url, source_id)
    result = {"source": source_id, "module": type(adapter).__name__, "url": url,
              "status": "FAIL", "chapters": [], "stage": "catalog"}
    try:
        catalog = parse_catalog(url)
        if catalog.get("source_id") != source_id:
            raise ValueError("parsed catalog source does not match selected adapter")
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
                detail["category"] = "ACCESS_RESTRICTED" if isinstance(exc, SourceAccessError) else "SOURCE_OR_PARSE_ERROR"
                detail["error"] = f"{type(exc).__name__}: {exc}"
        if all(c["status"] == "PASS" for c in result["chapters"]):
            unique_bodies = {c["sha256"] for c in result["chapters"]}
            if len(unique_bodies) != len(result["chapters"]):
                result["error"] = "different chapters returned identical body text"
            else:
                result["status"] = "PASS"
        else:
            result["error"] = "at least one chapter failed"
    except Exception as exc:
        result["category"] = "ACCESS_RESTRICTED" if isinstance(exc, SourceAccessError) else "SOURCE_OR_PARSE_ERROR"
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", choices=list(SAMPLES))
    args = parser.parse_args()
    result = live_adapter(args.source)
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
