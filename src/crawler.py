import os
import time
import json
import random
import logging
import requests
from bs4 import BeautifulSoup
import yaml

def load_config():
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def run_crawler():
    config = load_config()
    book_title = config['book_title']
    base_url = config['base_url']
    chapters = config['chapters']
    
    workspace_dir = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", config['paths']['workspace_base'], book_title))
    raw_text_dir = os.path.join(workspace_dir, "RawText")
    if not os.path.exists(raw_text_dir):
        os.makedirs(raw_text_dir)
        
    progress_file = os.path.join(workspace_dir, "progress.json")
    scraped_chapters = []
    if os.path.exists(progress_file):
        with open(progress_file, "r", encoding="utf-8") as f:
            scraped_chapters = json.load(f).get("scraped_chapters", [])
            
    for i, chap_url in enumerate(chapters):
        if chap_url in scraped_chapters:
            logging.info(f"[Crawler] Skipping already scraped chapter: {chap_url}")
            continue
            
        url = base_url + chap_url
        logging.info(f"[Crawler] Scraping {url}...")
        
        user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.1.1 Safari/605.1.15",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0"
        ]
        
        max_retries = 3
        for attempt in range(max_retries):
            try:
                headers = {"User-Agent": random.choice(user_agents)}
                
                # Random delay to prevent ban
                delay = random.uniform(2, 5)
                time.sleep(delay)
                
                resp = requests.get(url, headers=headers, timeout=10)
                resp.raise_for_status()
                resp.encoding = 'utf-8'
                soup = BeautifulSoup(resp.text, 'html.parser')
                
                title_h1 = soup.find('h1')
                title = title_h1.text.strip() if title_h1 else f"Unknown_Chapter_{i+1}"
                
                content_div = soup.find('div', style=lambda value: value and 'word-wrap: break-word' in value and 'text-indent: 2em' in value)
                raw_text = content_div.get_text(separator='\n') if content_div else ""
                
                if not raw_text:
                    logging.warning(f"[Crawler] Warning: No content found for {title}")
                    break
                    
                raw_filename = f"{book_title}_chapter_{i+1}_raw.txt"
                raw_path = os.path.join(raw_text_dir, raw_filename)
                
                with open(raw_path, "w", encoding="utf-8") as f:
                    f.write(title + "\n\n" + raw_text)
                logging.info(f"[Crawler] Saved raw text for {title} to {raw_path}")
                
                # Update progress
                scraped_chapters.append(chap_url)
                with open(progress_file, "w", encoding="utf-8") as f:
                    json.dump({"scraped_chapters": scraped_chapters}, f)
                    
                break # Success, exit retry loop
            except Exception as e:
                logging.error(f"[Crawler] Attempt {attempt+1}/{max_retries} failed for {url}: {e}")
                if attempt < max_retries - 1:
                    backoff = 2 ** attempt
                    logging.info(f"[Crawler] Retrying in {backoff} seconds...")
                    time.sleep(backoff)
                else:
                    logging.error(f"[Crawler] Max retries reached for {url}. Skipping.")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_crawler()


def run_crawler_worker(config, chapters, start_global_idx=1, exact_indices=None):
    """
    Matrix worker 專用版本：接收明確的章節 URL 列表與全域起始索引。
    檔名格式: {book_title}_chapter_{global_idx}_raw.txt
    """
    book_title = config['book_title']
    base_url   = config['base_url']

    workspace_dir = os.path.abspath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..",
        config['paths']['workspace_base'], book_title
    ))
    raw_text_dir = os.path.join(workspace_dir, "RawText")
    os.makedirs(raw_text_dir, exist_ok=True)

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

        if chap_url in scraped_chapters:
            logging.info(f"[Crawler Worker] Skipping already scraped chapter {global_idx}: {chap_url}")
            continue

        url = base_url + chap_url
        logging.info(f"[Crawler Worker] Scraping chapter {global_idx}: {url}")

        max_retries = 3
        for attempt in range(max_retries):
            try:
                headers = {"User-Agent": random.choice(user_agents)}
                time.sleep(random.uniform(2, 5))

                resp = requests.get(url, headers=headers, timeout=10)
                resp.raise_for_status()
                soup = BeautifulSoup(resp.content, 'html.parser', from_encoding='utf-8')

                title_h1  = soup.find('h1')
                title     = title_h1.text.strip() if title_h1 else f"Unknown_Chapter_{global_idx}"

                content_div = soup.find('div', style=lambda v: v and 'word-wrap: break-word' in v and 'text-indent: 2em' in v)
                raw_text    = content_div.get_text(separator='\n') if content_div else ""

                if not raw_text:
                    logging.warning(f"[Crawler Worker] No content for chapter {global_idx}: {title}")
                    break

                raw_filename = f"{book_title}_chapter_{global_idx}_raw.txt"
                raw_path     = os.path.join(raw_text_dir, raw_filename)
                with open(raw_path, "w", encoding="utf-8") as f:
                    f.write(title + "\n\n" + raw_text)
                logging.info(f"[Crawler Worker] Saved: {raw_filename}")

                scraped_chapters.append(chap_url)
                with open(progress_file, "w", encoding="utf-8") as f:
                    json.dump({"scraped_chapters": scraped_chapters}, f)
                break

            except Exception as e:
                logging.error(f"[Crawler Worker] Attempt {attempt+1}/{max_retries} failed for chapter {global_idx}: {e}")
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                else:
                    logging.error(f"[Crawler Worker] Max retries reached for chapter {global_idx}. Skipping.")
