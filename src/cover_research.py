"""Research the selected book's plot and sources for AI cover generation.

The selected novel adapter is the authoritative identity source. Wikipedia,
DuckDuckGo and Bing are strictly fallbacks, never fabricated plot details.
"""
import base64
import json
import logging
import os
import re
import urllib.parse

import requests
from bs4 import BeautifulSoup

from .cover_ai import _validate_plot_source


def _catalog_book_url(catalog_url):
    if not catalog_url:
        return ""
    try:
        from .sources import resolve_source
    except ImportError:
        from sources import resolve_source
    return resolve_source(catalog_url).metadata_url(catalog_url)


def fetch_book_summary_details(book_title, catalog_url=None):
    """取得小說簡介及來源；找不到時明確回報，不製造替代劇情。"""
    logging.info(f"正在搜尋《{book_title}》的整體小說劇情大綱與簡介...")
    raw_summary = ""
    source = ""

    def validated_identity(details, source_url):
        page_title = details['title']
        description = details['description']
        author = details['author']
        category = details['category']
        if page_title and book_title not in page_title and page_title not in book_title:
            raise ValueError(f"目錄書名是「{page_title}」，不是「{book_title}」")
        identity = f"書名：《{page_title or book_title}》；作者：{author or '未標示'}；類型：{category or '未標示'}；原始簡介：{description}"
        return _validate_plot_source(book_title, identity, source_url), source_url

    # The catalog selected by the user is the identity source for this exact
    # book. Prefer its own OpenGraph description over title-only web lookups.
    configured_catalog_url = catalog_url or os.getenv("BOOK_CATALOG_URL", "")
    book_url = _catalog_book_url(configured_catalog_url)
    if book_url:
        try:
            try:
                from .sources import resolve_source
                from .sources.http_client import fetch_page
            except ImportError:
                from sources import resolve_source
                from sources.http_client import fetch_page
            adapter = resolve_source(book_url)
            fallback_catalog_url = adapter.catalog_url(configured_catalog_url or book_url)
            source_urls = list(dict.fromkeys((book_url, fallback_catalog_url)))
            for source_url in source_urls:
                try:
                    details = adapter.parse_metadata(
                        fetch_page(source_url, adapter).content, source_url
                    )
                    return validated_identity(details, source_url)
                except (requests.RequestException, ValueError, RuntimeError) as exc:
                    logging.warning("小說來源簡介取得失敗 (%s): %s", source_url, exc)

            # catalog_parser has already fetched this exact page successfully.
            # Reuse its small metadata snapshot if a second request is rate-limited.
            cached_metadata = os.getenv("BOOK_CATALOG_METADATA", "")
            if cached_metadata:
                try:
                    return validated_identity(json.loads(cached_metadata), fallback_catalog_url)
                except (KeyError, TypeError, ValueError, RuntimeError) as exc:
                    logging.warning("目錄簡介快照無法使用: %s", exc)
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            # Source setup itself can fail. Continue to the existing Wikipedia
            # and search fallbacks instead of aborting cover preflight.
            logging.warning("無法建立小說來源簡介查詢: %s", exc)
    
    # 嘗試 1: 中文維基百科 REST API
    try:
        url = f"https://zh.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(book_title)}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        res = requests.get(url, headers=headers, timeout=8)
        if res.status_code == 200:
            extract = res.json().get("extract", "")
            if extract:
                try:
                    raw_summary = _validate_plot_source(book_title, extract, "zh.wikipedia.org")
                    source = "zh.wikipedia.org"
                except ValueError:
                    raw_summary = ""
    except Exception as e:
        logging.debug(f"[維基百科略過]: {e}")

    # 嘗試 2: DuckDuckGo 搜尋
    if not raw_summary:
        try:
            ddg_url = f"https://api.duckduckgo.com/?q={urllib.parse.quote(book_title + ' 小說 劇情簡介')}&format=json&no_html=1"
            res = requests.get(ddg_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
            if res.status_code == 200:
                extract = res.json().get("AbstractText", "")
                if extract:
                    raw_summary = _validate_plot_source(book_title, extract, "api.duckduckgo.com")
                    source = "api.duckduckgo.com"
        except Exception:
            pass

    pure_plot = _validate_plot_source(book_title, raw_summary, source) if raw_summary else ""
    if not pure_plot:
        raise RuntimeError(f"查不到《{book_title}》的可靠小說簡介；已停止，不會產生通用封面 Prompt")
    return pure_plot, source


def fetch_book_summary_online(book_title):
    return fetch_book_summary_details(book_title)[0]


def _decode_bing_result_url(url):
    try:
        encoded = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("u", [""])[0]
        if encoded.startswith("a1"):
            payload = encoded[2:] + "=" * (-len(encoded[2:]) % 4)
            return base64.urlsafe_b64decode(payload).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        pass
    return url


def collect_cover_research(book_title, synopsis, catalog_source):
    """Collect web evidence first; explicitly label model-knowledge fallback."""
    evidence = [{"id": "S1", "title": "目前小說目錄頁", "url": catalog_source, "text": synopsis}]
    author_match = re.search(r"作者：([^；]+)", synopsis)
    author = author_match.group(1).strip() if author_match else ""
    author_variants = {author, author.replace("東", "东")}
    queries = [
        f'"{book_title}" "{author}" 小說 主角 世界觀',
        f'"{book_title}" "{author}" 小說 法寶 場景',
    ]
    seen = {catalog_source}
    for query in queries:
        try:
            response = requests.get(
                "https://www.bing.com/search?q=" + urllib.parse.quote(query),
                headers={"User-Agent": "Mozilla/5.0"}, timeout=15,
            )
            response.raise_for_status()
            soup = BeautifulSoup(response.content, "html.parser")
            for result in soup.select("li.b_algo"):
                link, snippet_tag = result.select_one("h2 a"), result.select_one(".b_caption p")
                if not link or not snippet_tag:
                    continue
                title = link.get_text(" ", strip=True)
                snippet = snippet_tag.get_text(" ", strip=True)
                combined = title + " " + snippet
                if book_title not in combined or (author and not any(value and value in combined for value in author_variants)):
                    continue
                url = _decode_bing_result_url(str(link.get("href") or ""))
                if not url.startswith("http") or url in seen or len(snippet) < 35:
                    continue
                detail = snippet
                try:
                    page = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
                    page.raise_for_status()
                    page_soup = BeautifulSoup(page.content, "html.parser")
                    summary_tag = page_soup.select_one(".J-summary")
                    description_tag = page_soup.select_one('meta[name="description"]')
                    expanded = (
                        summary_tag.get_text(" ", strip=True) if summary_tag else
                        str(description_tag.get("content") or "").strip() if description_tag else ""
                    )
                    if book_title in expanded and any(value and value in expanded for value in author_variants):
                        detail = expanded[:6000]
                except requests.RequestException:
                    pass
                seen.add(url)
                evidence.append({"id": f"S{len(evidence)+1}", "title": title, "url": url, "text": detail})
                if len(evidence) >= 5:
                    break
        except requests.RequestException:
            continue
        if len(evidence) >= 5:
            break
    external_chars = sum(len(item["text"]) for item in evidence[1:])
    mode = (
        "web_evidence" if external_chars >= 800 else
        "hybrid_web_and_model_knowledge" if len(evidence) >= 2 else
        "internal_knowledge_fallback"
    )
    return {"mode": mode, "sources": evidence}

