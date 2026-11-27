"""Keyword discovery for AUDIO. Search links only; never download chapters.

Public search indexes discover candidate URLs. Every candidate is checked
against the existing source registry; only recognized book URLs are returned.
"""
from __future__ import annotations
import argparse
import concurrent.futures
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

import requests
from bs4 import BeautifulSoup

from src.sources.registry import SOURCES
from src.sources.http_client import fetch_page

SOURCE_NAMES = {
    "hjwzw": "黃金屋", "shuba69": "69 書吧", "czbooks": "小說狂人",
    "sixk": "6K 小說", "xszj": "小說之家", "metanovel": "MetaNovel",
    "twkan": "天天看", "moon17": "月亮小說網", "book8": "無限小說",
}
USER_AGENT = "Mozilla/5.0 (compatible; AUDIO-Search/1.0; +https://github.com/hub-google/audiobook-generator)"
TIMEOUT = 12


def candidate_url(value, source):
    """Never return a search page, reader chapter, redirect or foreign host."""
    raw = str(value or "").strip()
    if raw.startswith("//"):
        raw = "https:" + raw
    parts = urlsplit(raw)
    if parts.hostname in ("www.google.com", "www.bing.com", "duckduckgo.com"):
        raw = parse_qs(parts.query).get("uddg", [""])[0]
        parts = urlsplit(raw)
    if not source.matches(raw) or parts.port not in (None, 80, 443):
        return None
    try:
        # The adapter is the sole authority on what constitutes a book URL.
        canonical = source.catalog_url(raw)
        source.book_id(canonical)
        if not source.matches(canonical):
            return None
        return canonical
    except (ValueError, AttributeError):
        return None


def indexed_results(keyword, source):
    """Public Bing RSS + DuckDuckGo HTML fallback; no API key in Pages."""
    host = source.hosts[0].removeprefix("www.")
    query = f"site:{host} {keyword}"
    headers = {"User-Agent": USER_AGENT, "Accept": "application/rss+xml,text/html"}
    links = []
    errors = []
    try:
        response = requests.get("https://www.bing.com/search", params={"format": "rss", "q": query},
                                headers=headers, timeout=TIMEOUT)
        response.raise_for_status()
        root = ET.fromstring(response.content)
        for item in root.findall(".//item"):
            links.append((item.findtext("link") or "", item.findtext("title") or ""))
    except (requests.RequestException, ET.ParseError) as exc:
        errors.append(type(exc).__name__)
    if len(links) < 2:
        try:
            response = requests.get("https://html.duckduckgo.com/html/",
                                    params={"q": query}, headers=headers, timeout=TIMEOUT)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, "html.parser")
            for a in soup.select("a.result__a[href]"):
                links.append((a.get("href", ""), a.get_text(" ", strip=True)))
        except requests.RequestException as exc:
            errors.append(type(exc).__name__)
    return links, errors


def search_source(keyword, source_id):
    source = SOURCES[source_id]
    links, errors = indexed_results(keyword, source)
    results = []
    seen = set()
    for url, title in links:
        canonical = candidate_url(url, source)
        if not canonical or canonical in seen:
            continue
        seen.add(canonical)
        display = re.sub(r"\s*[-–—|_｜]\s*(?:69书吧|69書吧|小說狂人|小说狂人|月亮小說網|天天看|黃金屋|无弹窗|免費小說).*$", "", title, flags=re.I).strip()
        if not display:
            display = canonical.rsplit("/", 2)[-2]
        results.append({
            "source_id": source_id, "source_name": SOURCE_NAMES[source_id],
            "title": display[:160], "author": "", "catalog_url": canonical,
            "metadata_verified": False,
        })
        if len(results) >= 8:
            break
    # Enrich a few leading matches from the source's own book metadata, not chapters.
    for item in results[:3]:
        try:
            metadata_url = source.metadata_url(item["catalog_url"])
            page = fetch_page(metadata_url, source, timeout=9)
            metadata = source.parse_metadata(page.content, metadata_url)
            if metadata.get("title"):
                item["title"] = str(metadata["title"]).strip()[:160]
            if metadata.get("author"):
                item["author"] = str(metadata["author"]).strip()[:120]
            item["metadata_verified"] = bool(metadata.get("title"))
        except Exception:
            # A blocked details page must not discard an otherwise valid link.
            pass
    return {"source_id": source_id, "source_name": SOURCE_NAMES[source_id],
            "results": results,
            "status": "ok" if results else ("unavailable" if errors else "no_results")}


def search_novels(keyword, source_ids=None):
    keyword = re.sub(r"\s+", " ", str(keyword or "")).strip()
    if not 1 <= len(keyword) <= 80:
        raise ValueError("Keyword must be 1–80 characters")
    ids = list(source_ids or SOURCES)
    if not ids or len(ids) != len(set(ids)) or any(s not in SOURCES for s in ids):
        raise ValueError("Unknown source ID")
    # Search sources concurrently, but bound external traffic to a few requests.
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        jobs = {sid: pool.submit(search_source, keyword, sid) for sid in ids}
        by_source = []
        for sid in ids:
            try:
                by_source.append(jobs[sid].result())
            except Exception:
                by_source.append({"source_id": sid, "source_name": SOURCE_NAMES[sid],
                                  "results": [], "status": "unavailable"})
    results = [novel for group in by_source for novel in group["results"]]
    return {"keyword": keyword, "sources": by_source, "results": results,
            "updated_at": datetime.now(timezone.utc).isoformat()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--keyword", required=True)
    parser.add_argument("--sources", default="")
    parser.add_argument("--request-id", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-f0-9]{32}", args.request_id):
        parser.error("Invalid request ID")
    selected = args.sources.split(",") if args.sources else None
    data = search_novels(args.keyword, selected)
    data["request_id"] = args.request_id
    Path("novel-search.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    print("AUDIO source search:", len(data["results"]), "valid books;", len(data["sources"]), "sources")


if __name__ == "__main__":
    main()
