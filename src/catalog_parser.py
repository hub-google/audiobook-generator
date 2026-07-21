import os
import re
import math
import yaml
import json
import argparse
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Safari/605.1.15",
]

def parse_catalog(catalog_url):
    """
    抓取小說目錄頁面，解析書名與章節 URL 列表。
    """
    parsed_uri = urlparse(catalog_url)
    base_url = f"{parsed_uri.scheme}://{parsed_uri.netloc}"

    headers = {"User-Agent": USER_AGENTS[0]}
    response = requests.get(catalog_url, headers=headers, timeout=15)
    response.raise_for_status()

    # 使用 response.content (bytes) 配合 from_encoding 讓 BeautifulSoup 自行處理編碼
    # 避免 requests 自動偵測編碼錯誤導致中文亂碼
    soup = BeautifulSoup(response.content, 'html.parser', from_encoding='utf-8')

    # 1. 解析書名
    book_title = "未知小說"
    h1_tag = soup.find('h1')
    if h1_tag and h1_tag.text.strip():
        book_title = h1_tag.text.strip()
    else:
        og_title = soup.find('meta', property='og:title')
        if og_title and og_title.get('content'):
            book_title = og_title['content'].strip()
        elif soup.title:
            book_title = soup.title.text.split('-')[0].split('_')[0].strip()

    # 清理書名中不合法的檔名字元
    book_title = re.sub(r'[\\/:*?"<>|]', '', book_title)

    # 2. 解析章節連結
    chapter_urls = []
    seen = set()

    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        # 匹配 hjwzw.com 的 /Book/Read/ 格式，或其他通用格式
        if '/Book/Read/' in href or '/read/' in href.lower():
            if href.startswith('http'):
                # 絕對網址：取出路徑部分
                from urllib.parse import urlparse as _up
                full_href = _up(href).path
            elif href.startswith('/'):
                full_href = href
            else:
                full_href = '/' + href.lstrip('/')

            if full_href not in seen:
                seen.add(full_href)
                chapter_urls.append(full_href)

    return {
        "success": True,
        "book_title": book_title,
        "base_url": base_url,
        "chapters": chapter_urls,
        "total_chapters": len(chapter_urls)
    }

def generate_config_yaml(catalog_url, start_chap=1, end_chap=10, output_path="config.yaml"):
    """
    根據解析結果生成 config.yaml 檔案
    """
    res = parse_catalog(catalog_url)
    if not res["success"] or res["total_chapters"] == 0:
        raise ValueError("無法解析章節或章節清單為空！")

    total = res["total_chapters"]
    start_idx = max(1, start_chap) - 1
    end_idx = min(total, end_chap)

    selected_chapters = res["chapters"][start_idx:end_idx]

    config_data = {
        "book_title": res["book_title"],
        "base_url": res["base_url"],
        "catalog_url": catalog_url,
        "start_chapter": start_chap,
        "end_chapter": min(total, end_chap),
        "total_available_chapters": total,
        "chapters": selected_chapters,
        "tts": {
            "engine": "edge-tts",
            "edge_voice": "zh-CN-YunxiNeural"
        },
        "paths": {
            "workspace_base": "Workspace"
        }
    }

    with open(output_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(config_data, f, allow_unicode=True, sort_keys=False)

    print(f"[CatalogParser] 成功生成 {output_path}：書名【{res['book_title']}】，選取第 {start_chap} 至 {min(total, end_chap)} 章（共 {len(selected_chapters)} 章，全書 {total} 章）")
    return config_data


def generate_matrix(catalog_url, start_chap=1, end_chap=10, chapters_per_worker=5):
    """
    解析目錄並計算每台 GitHub Actions worker 負責的章節子集。
    回傳符合 GitHub Actions matrix 格式的 dict：
      { "include": [ {"worker_id": 0, "start_global_idx": 1, "chapters_json": "[...]"}, ... ] }
    同時也回傳 total_workers。
    """
    res = parse_catalog(catalog_url)
    if not res["success"] or res["total_chapters"] == 0:
        raise ValueError("無法解析章節或章節清單為空！")

    total = res["total_chapters"]
    start_idx = max(1, start_chap) - 1          # 轉為 0-based
    end_idx   = min(total, end_chap)             # 0-based exclusive
    selected  = res["chapters"][start_idx:end_idx]

    includes = []
    for i in range(0, len(selected), chapters_per_worker):
        chunk = selected[i : i + chapters_per_worker]
        includes.append({
            "worker_id":       len(includes),
            "start_global_idx": start_idx + i + 1,   # 1-based 全域索引
            "chapters_json":   json.dumps(chunk, ensure_ascii=False)
        })

    matrix = {"include": includes}
    print(f"[CatalogParser] Matrix: {len(includes)} workers，每台最多 {chapters_per_worker} 章")
    return matrix, res["book_title"]


if __name__ == "__main__":
    # 範例網址格式: https://tw.hjwzw.com/Book/Chapter/1644
    parser = argparse.ArgumentParser(description="Parse novel catalog and generate config.yaml + matrix.json")
    parser.add_argument("--url",            type=str, required=True, help="Catalog URL (e.g. https://tw.hjwzw.com/Book/Chapter/1644)")
    parser.add_argument("--start",          type=int, default=1,  help="Start chapter index (1-based)")
    parser.add_argument("--end",            type=int, default=10, help="End chapter index (1-based)")
    parser.add_argument("--output",         type=str, default="config.yaml", help="Output YAML config path")
    parser.add_argument("--workers",        type=int, default=0,  help="Chapters per worker (0 = single job mode)")
    parser.add_argument("--matrix-output",  type=str, default="", help="Path to write matrix JSON (for GitHub Actions)")
    args = parser.parse_args()

    generate_config_yaml(args.url, args.start, args.end, args.output)

    if args.workers > 0 and args.matrix_output:
        matrix, _ = generate_matrix(args.url, args.start, args.end, args.workers)
        with open(args.matrix_output, "w", encoding="utf-8") as f:
            json.dump(matrix, f, ensure_ascii=False)
        print(f"[CatalogParser] Matrix JSON 已寫入 {args.matrix_output} ({len(matrix['include'])} workers)")
