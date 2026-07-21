import os
import sys
import time
import subprocess
import requests
import yaml
import logging

# 確保在 Spyder 或各種工作目錄下執行時，能正確 import 同目錄模組
sys.path.append(os.path.dirname(os.path.abspath(__file__)))


def load_config():
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def setup_logger(workspace_dir):
    if not os.path.exists(workspace_dir):
        os.makedirs(workspace_dir)

    log_file = os.path.join(workspace_dir, "system.log")

    # 防止 Spyder 重複執行時累積 handler
    root_logger = logging.getLogger()
    if root_logger.handlers:
        root_logger.handlers.clear()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler()
        ]
    )
    logging.info("Logger initialized.")


def main():
    config = load_config()
    book_title = config.get("book_title", "UnknownBook")
    workspace_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..",
        config['paths']['workspace_base'],
        book_title
    )

    setup_logger(workspace_dir)
    logging.info("=== Audiobook Automation Pipeline Started ===")
    logging.info(f"[Info] Book: {book_title}")

    # ---- 以下各步驟獨立 try/except，單步失敗不影響後續 ----

    from crawler import run_crawler
    from cleaner import run_cleaner
    from tts_ms import run_tts_ms
    from image_gen import run_image_gen
    from video_gen import run_video_gen

    try:
        logging.info("\n[Step 1/5] Crawling texts...")
        run_crawler()
        logging.info("[Step 1/5] ✓ Done.")
    except Exception as e:
        logging.error(f"[Step 1/5] ✗ Crawler failed: {e}", exc_info=True)

    try:
        logging.info("\n[Step 2/5] Cleaning texts...")
        run_cleaner()
        logging.info("[Step 2/5] ✓ Done.")
    except Exception as e:
        logging.error(f"[Step 2/5] ✗ Cleaner failed: {e}", exc_info=True)

    try:
        logging.info("\n[Step 3/5] Generating audio via Microsoft Edge TTS...")
        run_tts_ms()
        logging.info("[Step 3/5] ✓ Done.")
    except Exception as e:
        logging.error(f"[Step 3/5] ✗ TTS failed: {e}", exc_info=True)

    try:
        logging.info("\n[Step 4/5] Preparing images...")
        run_image_gen()
        logging.info("[Step 4/5] ✓ Done.")
    except Exception as e:
        logging.error(f"[Step 4/5] ✗ Image Gen failed: {e}", exc_info=True)

    try:
        logging.info("\n[Step 5/5] Generating video (FFmpeg)...")
        run_video_gen()
        logging.info("[Step 5/5] ✓ Done.")
    except Exception as e:
        logging.error(f"[Step 5/5] ✗ Video Gen failed: {e}", exc_info=True)

    output_dir = os.path.abspath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "Output", book_title
    ))
    logging.info("\n=== Pipeline Completed ===")
    logging.info(f"[Output] MP4 location: {output_dir}")
    # logging.info("[Note] The GPT-SoVITS API window is still open. Close it manually when done.")

if __name__ == "__main__":
    main()
