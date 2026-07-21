"""
worker_pipeline.py — GitHub Actions Matrix Worker 統一入口

用法（由 audiobook.yml 的各 matrix job 呼叫）：
  python src/worker_pipeline.py \\
    --stage crawl \\
    --worker-id 0 \\
    --start-global-idx 1 \\
    --chapters-json '["/Book/Read/1644,409280", "/Book/Read/1644,409281"]'

各階段（stage）說明：
  crawl      — 爬取本 worker 負責的章節，輸出 RawText/
  clean      — 清洗 RawText/ → CleanText/
  tts        — Edge TTS，CleanText/ → Audio/
  image_gen  — 產生標題卡，Audio/ → Images/
  video_gen  — FFmpeg 合成，Audio/ + Images/ → Output/
"""

import os
import sys
import json
import yaml
import logging
import argparse

# 確保 src/ 下的模組可被 import
SRC_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SRC_DIR)


# ── 工具函式 ──────────────────────────────────────────────

def load_config(config_path=None):
    if config_path is None:
        config_path = os.path.join(SRC_DIR, "..", "config.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def setup_logging(worker_id):
    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s [Worker-{worker_id}] %(levelname)s %(message)s",
        handlers=[logging.StreamHandler()]
    )


# ── 各 Stage 處理函式 ──────────────────────────────────────

def stage_crawl(config, chapters, start_global_idx):
    from crawler import run_crawler_worker
    run_crawler_worker(config, chapters, start_global_idx)


def stage_clean(config):
    from cleaner import run_cleaner
    # cleaner 讀取 config.yaml 並處理目前工作目錄的 RawText/，
    # 由於每個 matrix job 的 runner 是獨立機器，只會看到自己 crawl 的檔案。
    run_cleaner()


def stage_tts(config):
    from tts_ms import run_tts_ms
    run_tts_ms()


def stage_image_gen(config):
    from image_gen import run_image_gen
    run_image_gen()


def stage_video_gen(config):
    from video_gen import run_video_gen
    run_video_gen()


# ── 主程式 ────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Audiobook Matrix Worker Pipeline")
    parser.add_argument("--stage",            required=True,
                        choices=["crawl", "clean", "tts", "image_gen", "video_gen"],
                        help="Pipeline stage to execute")
    parser.add_argument("--worker-id",        type=int, required=True,
                        help="Worker index (0-based)")
    parser.add_argument("--start-global-idx", type=int, default=1,
                        help="1-based global chapter index for the first chapter in this worker's batch")
    parser.add_argument("--chapters-json",    type=str, default="[]",
                        help="JSON list of chapter URL paths (e.g. [\"/Book/Read/1644,409280\", ...])")
    parser.add_argument("--config",           type=str, default="",
                        help="Path to config.yaml (defaults to ../config.yaml relative to src/)")
    args = parser.parse_args()

    setup_logging(args.worker_id)
    logging.info(f"=== Worker {args.worker_id} | Stage: {args.stage} | Start global idx: {args.start_global_idx} ===")

    config_path = args.config if args.config else None
    config = load_config(config_path)

    chapters = json.loads(args.chapters_json)
    logging.info(f"Assigned chapters: {len(chapters)} 章  (global idx {args.start_global_idx} ~ {args.start_global_idx + len(chapters) - 1})")

    stage = args.stage

    if stage == "crawl":
        stage_crawl(config, chapters, args.start_global_idx)

    elif stage == "clean":
        stage_clean(config)

    elif stage == "tts":
        stage_tts(config)

    elif stage == "image_gen":
        stage_image_gen(config)

    elif stage == "video_gen":
        stage_video_gen(config)

    logging.info(f"=== Worker {args.worker_id} | Stage: {stage} DONE ===")


if __name__ == "__main__":
    main()
