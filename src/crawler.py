try:
    from .source_identity import workspace_name
except ImportError:
    from source_identity import workspace_name

import os
import time
import json
import random
import logging
import requests
from bs4 import BeautifulSoup
import yaml
try:
    from .artifact_validation import ArtifactValidationError, validate_text
except ImportError:
    from artifact_validation import ArtifactValidationError, validate_text

try:
    from .source_status import (
        SourceMissingError, SourceStatusStore, looks_like_anti_bot_page,
    )
except ImportError:
    from source_status import (
        SourceMissingError, SourceStatusStore, looks_like_anti_bot_page,
    )


USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Safari/605.1.15",
]


def fetch_chapter_text(url, timeout=15, source_id=None, html=None):
    try:
        from .sources import resolve_source
        from .sources.http_client import fetch_page
    except ImportError:
        from sources import resolve_source
        from sources.http_client import fetch_page
    source = resolve_source(url, source_id)
    if html is None:
        html = fetch_page(url, source, timeout).content
    return source.parse_chapter(html, url)

def load_config():
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def run_crawler():
    config = load_config()
    run_crawler_worker(config, config['chapters'], exact_indices=config.get('selected_indices'))


def run_crawler_worker(config, chapters, start_global_idx=1, exact_indices=None):
    """
    Matrix worker 專用版本：接收明確的章節 URL 列表與全域起始索引。
    檔名格式: {book_title}_chapter_{global_idx}_raw.txt
    """
    book_title = config['book_title']
    base_url   = config['base_url']
    from urllib.parse import urljoin
    try:
        from .sources import resolve_source, SourceParseError, SourceAccessError
    except ImportError:
        from sources import resolve_source, SourceParseError, SourceAccessError
    source = resolve_source(config.get('catalog_url') or base_url, config.get('source_id'))

    workspace_dir = os.path.abspath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..",
        config['paths']['workspace_base'], workspace_name(config)
    ))
    raw_text_dir = os.path.join(workspace_dir, "RawText")
    os.makedirs(raw_text_dir, exist_ok=True)
    source_status = SourceStatusStore(workspace_dir)

    progress_file = os.path.join(workspace_dir, "progress.json")
    scraped_chapters = []
    if os.path.exists(progress_file):
        with open(progress_file, "r", encoding="utf-8") as f:
            scraped_chapters = json.load(f).get("scraped_chapters", [])

    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.1.1 Safari/605.1.15",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0"
    ]

    for local_i, chap_url in enumerate(chapters):
        if exact_indices and local_i < len(exact_indices):
            global_idx = exact_indices[local_i]
        else:
            global_idx = start_global_idx + local_i   # 退回原本的推算方式

        raw_filename = f"{book_title}_chapter_{global_idx}_raw.txt"
        raw_path     = os.path.join(raw_text_dir, raw_filename)
        provenance_path = raw_path + '.source.json'
        expected_provenance = {
            'source_fingerprint': config.get('source_fingerprint'),
            'url': urljoin(base_url, chap_url), 'parser_version': source.version,
        }
        provenance_matches = not config.get('source_schema_version')
        if not provenance_matches:
            try:
                with open(provenance_path, encoding='utf-8') as handle:
                    provenance_matches = json.load(handle) == expected_provenance
            except (OSError, ValueError):
                pass

        # progress.json is only an index. The actual output file is the source of
        # truth, otherwise a stale progress entry can permanently skip a chapter.
        if os.path.exists(raw_path) and provenance_matches:
            try:
                validate_text(raw_path, clean=False)
                source_status.mark_available(global_idx)
                logging.info(f"[Crawler Worker] Skipping validated chapter {global_idx}: {chap_url}")
                continue
            except (ArtifactValidationError, OSError, ValueError):
                logging.warning("[Crawler Worker] RawText %s is corrupt; rebuilding", global_idx)

        if not config.get("source_schema_version") and source_status.is_confirmed_missing(global_idx):
            raise SourceMissingError(
                f"chapter {global_idx} is confirmed missing from the origin website: {urljoin(base_url, chap_url)}"
            )

        url = urljoin(base_url, chap_url)
        logging.info(f"[Crawler Worker] Scraping chapter {global_idx}: {url}")

        max_retries = 3
        for attempt in range(max_retries):
            try:
                title, raw_text = fetch_chapter_text(url, source_id=source.source_id)
                edited_titles = config.get("chapter_title_by_index") or {}
                title = str(edited_titles.get(str(global_idx), edited_titles.get(global_idx, title)))

                raw_filename = f"{book_title}_chapter_{global_idx}_raw.txt"
                raw_path     = os.path.join(raw_text_dir, raw_filename)
                raw_tmp = raw_path + ".tmp"
                with open(raw_tmp, "w", encoding="utf-8") as f:
                    f.write(title + "\n\n" + raw_text)
                    f.flush()
                    os.fsync(f.fileno())
                validate_text(raw_tmp, clean=False)
                os.replace(raw_tmp, raw_path)
                if config.get('source_schema_version'):
                    with open(provenance_path + '.tmp', 'w', encoding='utf-8') as handle:
                        json.dump(expected_provenance, handle, ensure_ascii=False)
                    os.replace(provenance_path + '.tmp', provenance_path)
                source_status.mark_available(global_idx)
                logging.info(f"[Crawler Worker] Saved: {raw_filename}")

                scraped_chapters.append(chap_url)
                progress_tmp = progress_file + ".tmp"
                with open(progress_tmp, "w", encoding="utf-8") as f:
                    json.dump({"scraped_chapters": scraped_chapters}, f)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(progress_tmp, progress_file)
                break

            except (SourceMissingError, SourceParseError, SourceAccessError):
                raise
            except Exception as e:
                logging.error(f"[Crawler Worker] Attempt {attempt+1}/{max_retries} failed for chapter {global_idx}: {e}")
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                else:
                    logging.error(f"[Crawler Worker] Max retries reached for chapter {global_idx}. Raising.")
                    raise RuntimeError(
                        f"[Crawler Worker] 章節 {global_idx} 爬取失敗，已重試 {max_retries} 次: {e}"
                    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_crawler()
