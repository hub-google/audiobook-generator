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
    chapter_titles = []
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
                chapter_titles.append(a.text.strip() or f"第 {len(chapter_urls)} 章")

    return {
        "success": True,
        "book_title": book_title,
        "base_url": base_url,
        "chapters": chapter_urls,
        "chapter_titles": chapter_titles,
        "total_chapters": len(chapter_urls)
    }

def generate_config_yaml(catalog_url, start_chap=1, end_chap=10, output_path="config.yaml",
                          exclude_chapters=None, chapters_per_worker=5,
                          parsed_result=None):
    """
    根據解析結果生成 config.yaml 檔案。
    parsed_result: 可傳入已爬取的 parse_catalog() 結果，避免重複爬取。
    """
    if exclude_chapters is None:
        exclude_chapters = []

    res = parsed_result if parsed_result is not None else parse_catalog(catalog_url)
    if not res["success"] or res["total_chapters"] == 0:
        raise ValueError("無法解析章節或章節清單為空！")

    total = res["total_chapters"]
    start_chap = max(1, start_chap)
    end_chap   = min(total, end_chap)

    if start_chap > total:
        raise ValueError(f"開始章節({start_chap})超出全書總章節數({total})！")
    if start_chap > end_chap:
        raise ValueError(f"開始章節({start_chap})大於結束章節({end_chap})！")

    start_idx = start_chap - 1  # 轉為 0-based

    # 包含標題與 URL，並過濾排除的章節
    selected_chapters = []
    selected_indices  = []  # 記錄真實的 1-based 章節編號
    for i in range(start_idx, end_chap):
        if (i + 1) not in exclude_chapters:
            selected_chapters.append(res["chapters"][i])
            selected_indices.append(i + 1)

    config_data = {
        "book_title": res["book_title"],
        "base_url": res["base_url"],
        "catalog_url": catalog_url,
        "start_chapter": start_chap,
        "end_chapter": end_chap,
        "total_available_chapters": total,
        "chapters": selected_chapters,
        "selected_indices": selected_indices,  # 新增：明確記錄實際處理的章節編號
        "chapters_per_worker": chapters_per_worker,  # 新增：讓 Worker 知道每台機器的額度
        "tts": {
            "engine": "edge-tts",
            "edge_voice": "zh-CN-YunxiNeural"
        },
        "paths": {
            "workspace_base": "Workspace"
        },
        "gdrive_folder_id": os.environ.get("GDRIVE_FOLDER_ID", "")
    }

    with open(output_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(config_data, f, allow_unicode=True, sort_keys=False)

    print(f"[CatalogParser] 成功生成 {output_path}：書名【{res['book_title']}】，選取範圍 {start_chap} 至 {end_chap} 章（實際處理 {len(selected_chapters)} 章，全書 {total} 章）")
    return config_data


def generate_matrix(catalog_url, start_chap=1, end_chap=10, chapters_per_worker=5,
                    exclude_chapters=None, parsed_result=None):
    """
    解析目錄並計算每台 GitHub Actions worker 負責的章節子集。
    回傳符合 GitHub Actions matrix 格式的 dict：
      { "include": [ {"worker_id": 0}, ... ] }
    同時也回傳 (matrix, book_title, effective_chapters_per_worker)。
    parsed_result: 可傳入已爬取的 parse_catalog() 結果，避免重複爬取。
    """
    if exclude_chapters is None:
        exclude_chapters = []

    res = parsed_result if parsed_result is not None else parse_catalog(catalog_url)
    if not res["success"] or res["total_chapters"] == 0:
        raise ValueError("無法解析章節或章節清單為空！")

    total = res["total_chapters"]
    start_chap = max(1, start_chap)
    end_chap   = min(total, end_chap)

    if start_chap > total:
        raise ValueError(f"開始章節({start_chap})超出全書總章節數({total})！")
    if start_chap > end_chap:
        raise ValueError(f"開始章節({start_chap})大於結束章節({end_chap})！")

    start_idx = start_chap - 1  # 轉為 0-based

    # 建立過濾後的章節列表，保留真實的 1-based global_idx
    selected_with_idx = []
    for i in range(start_idx, end_chap):
        if (i + 1) not in exclude_chapters:
            selected_with_idx.append({"url": res["chapters"][i], "global_idx": i + 1})

    if not selected_with_idx:
        raise ValueError(f"設定範圍內沒有任何可處理的章節（可能全部被排除）！")

    # 自動調整 chapters_per_worker 避免超過 GitHub Actions 的 Matrix 256 上限
    MAX_WORKERS = 250
    total_selected = len(selected_with_idx)
    if total_selected > 0:
        required_workers = math.ceil(total_selected / chapters_per_worker)
        if required_workers > MAX_WORKERS:
            chapters_per_worker = math.ceil(total_selected / MAX_WORKERS)
            print(f"[CatalogParser] 警告：超過 GitHub Actions matrix 上限(256)，自動調整每台機器處理章節數為 {chapters_per_worker}")

    includes = []
    for i in range(0, len(selected_with_idx), chapters_per_worker):
        chunk = selected_with_idx[i:i + chapters_per_worker]
        start_c = chunk[0]["global_idx"]
        end_c = chunk[-1]["global_idx"]
        includes.append({
            "worker_id": len(includes),
            "book_title": res["book_title"],
            "start_chap": start_c,
            "end_chap": end_c
        })

    matrix = {"include": includes}
    print(f"[CatalogParser] Matrix: {len(includes)} workers，每台最多 {chapters_per_worker} 章，共 {total_selected} 章待處理")
    # 回傳 effective chapters_per_worker 以便呼叫端同步更新 config.yaml
    return matrix, res["book_title"], chapters_per_worker


if __name__ == "__main__":
    # 範例網址格式: https://tw.hjwzw.com/Book/Chapter/1644
    parser = argparse.ArgumentParser(description="Parse novel catalog and generate config.yaml + matrix.json")
    parser.add_argument("--url",            type=str, required=True, help="Catalog URL (e.g. https://tw.hjwzw.com/Book/Chapter/1644)")
    parser.add_argument("--start",          type=int, default=1,  help="Start chapter index (1-based)")
    parser.add_argument("--end",            type=int, default=10, help="End chapter index (1-based)")
    parser.add_argument("--output",         type=str, default="config.yaml", help="Output YAML config path")
    parser.add_argument("--workers",        type=int, default=0,  help="Chapters per worker (0 = single job mode)")
    parser.add_argument("--matrix-output",  type=str, default="", help="Path to write matrix JSON (for GitHub Actions)")
    parser.add_argument("--exclude-chapters", type=str, default="", help="Comma separated 1-based indices to exclude")
    args = parser.parse_args()

    exclude_list = []
    if args.exclude_chapters:
        try:
            exclude_list = [int(x.strip()) for x in args.exclude_chapters.split(",") if x.strip()]
        except ValueError:
            pass

    chapters_per_worker_input = args.workers if args.workers > 0 else 5

    # ── 只爬取一次目錄，共用於 config 與 matrix ──
    print(f"[CatalogParser] 正在解析目錄：{args.url}")
    parsed = parse_catalog(args.url)

    # ── 若需要 matrix，先計算以取得可能自動調整後的 chapters_per_worker ──
    effective_cpw = chapters_per_worker_input
    if args.workers > 0 and args.matrix_output:
        matrix, _, effective_cpw = generate_matrix(
            args.url, args.start, args.end,
            chapters_per_worker_input,
            exclude_chapters=exclude_list,
            parsed_result=parsed
        )
        with open(args.matrix_output, "w", encoding="utf-8") as f:
            json.dump(matrix, f, ensure_ascii=False)
        print(f"[CatalogParser] Matrix JSON 已寫入 {args.matrix_output} ({len(matrix['include'])} workers)")

    # ── 生成 config.yaml，使用調整後的 effective_cpw 確保兩者一致 ──
    generate_config_yaml(
        args.url, args.start, args.end, args.output,
        exclude_chapters=exclude_list,
        chapters_per_worker=effective_cpw,
        parsed_result=parsed
    )
