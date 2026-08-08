"""
worker_pipeline.py — GitHub Actions Matrix Worker 統一入口

用法（由 audiobook.yml 的各 matrix job 呼叫）：
  python src/worker_pipeline.py \\
    --stage crawl \\
    --worker-id 0

各階段（stage）說明：
  crawl      — 爬取本 worker 負責的章節，輸出 RawText/
  clean      — 清洗 RawText/ → CleanText/
  tts        — Edge TTS，CleanText/ → Audio/ + Subtitles/
  image_gen  — 產生標題卡，Audio/ → Images/
  video_gen  — FFmpeg 合成，Audio/ + Images/ → Output/
"""

import os
import sys
import json
import glob
import re
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


def parse_chapter_num(filename):
    m = re.search(r'chapter_(\d+)', filename)
    if m:
        return int(m.group(1))
    return 9999


# ── 最終完整性驗收 ─────────────────────────────────────────

def validate_chapter_completeness(config, exact_indices, tts_failed_chapters=None):
    """
    在所有 Stage 完成後，逐章確認：
      ✅ Audio/{書名}_chapter_N.wav   — 聲音檔
      ✅ Images/{書名}_chapter_N.jpg  — 標題卡圖片
      ✅ Subtitles/{書名}_chapter_N.srt — 字幕檔
      ✅ Video/{書名}_chapter_N.mp4   — 單章影片

    四者缺一不可。
    - WAV 或 SRT 缺失：TTS 已用章節重試，到這裡仍缺表示完全失敗。
    - JPG 缺失：此處最多重試 3 次圖片生成。
    仍失敗的章節列入最終失敗清單並清除孤兒檔案。
    """
    from image_gen import generate_title_card, get_chapter_title
    book_title = config['book_title']
    workspace_dir = os.path.abspath(os.path.join(
        SRC_DIR, "..", config['paths']['workspace_base'], book_title
    ))
    audio_dir     = os.path.join(workspace_dir, "Audio")
    images_dir    = os.path.join(workspace_dir, "Images")
    subtitles_dir = os.path.join(workspace_dir, "Subtitles")
    video_dir     = os.path.join(workspace_dir, "Video")

    # Files are the source of truth. A chapter that failed TTS earlier may have
    # succeeded during a later recovery round, so historical failure markers
    # must not permanently poison final validation.
    final_failed = set()
    complete_chapters = []
    IMAGE_MAX_ATTEMPTS = 3

    for chap_num in sorted(exact_indices):
        wav_path = os.path.join(audio_dir,     f"{book_title}_chapter_{chap_num}.wav")
        jpg_path = os.path.join(images_dir,    f"{book_title}_chapter_{chap_num}.jpg")
        srt_path = os.path.join(subtitles_dir, f"{book_title}_chapter_{chap_num}.srt")
        mp4_path = os.path.join(video_dir,     f"{book_title}_chapter_{chap_num}.mp4")

        # ── 檢查 WAV / SRT（不可重試，TTS 已有章節重試機制）──
        wav_ok = os.path.exists(wav_path) and os.path.getsize(wav_path) > 100
        srt_ok = os.path.exists(srt_path) and os.path.getsize(srt_path) > 10
        mp4_ok = os.path.exists(mp4_path) and os.path.getsize(mp4_path) > 1000

        # ── 檢查 JPG，缺失時最多重試 3 次生成 ──
        jpg_ok = os.path.exists(jpg_path) and os.path.getsize(jpg_path) > 100
        if not jpg_ok and wav_ok:
            for img_attempt in range(1, IMAGE_MAX_ATTEMPTS + 1):
                logging.warning(
                    f"[Validate] 第 {chap_num} 章缺少圖片，嘗試重新生成 "
                    f"({img_attempt}/{IMAGE_MAX_ATTEMPTS})..."
                )
                try:
                    os.makedirs(images_dir, exist_ok=True)
                    chapter_title = get_chapter_title(workspace_dir, book_title, chap_num)
                    ok = generate_title_card(book_title, chap_num, chapter_title, jpg_path)
                    if ok and os.path.exists(jpg_path) and os.path.getsize(jpg_path) > 100:
                        jpg_ok = True
                        logging.info(f"[Validate] ✓ 第 {chap_num} 章圖片重新生成成功")
                        break
                except Exception as e:
                    logging.error(f"[Validate] 第 {chap_num} 章圖片生成嘗試 {img_attempt} 失敗: {e}")

        # ── 最終判決 ──
        missing = []
        if not wav_ok:
            missing.append("WAV聲音")
        if not jpg_ok:
            missing.append("JPG圖片")
        if not srt_ok:
            missing.append("SRT字幕")
        if not mp4_ok:
            missing.append("MP4影片")

        if missing:
            logging.error(
                f"[Validate] ✗ 第 {chap_num} 章不完整，缺少：{', '.join(missing)}。"
                f" 此章將不會被加入影片。"
            )
            # 刪除孤兒檔案
            for path in [wav_path, jpg_path, srt_path, mp4_path]:
                if os.path.exists(path):
                    try:
                        os.remove(path)
                        logging.warning(f"[Validate] 已刪除不完整產物: {os.path.basename(path)}")
                    except Exception:
                        pass
            final_failed.add(chap_num)
        else:
            logging.info(f"[Validate] ✓ 第 {chap_num} 章四件齊全 (WAV + JPG + SRT + MP4)")
            complete_chapters.append(chap_num)

    return complete_chapters, final_failed


def print_final_report(complete_chapters, failed_chapters, worker_id):
    """在 GitHub Actions 日誌中印出最終章節完成狀態。"""
    logging.info("")
    logging.info("=" * 60)
    logging.info(f"[Worker-{worker_id}] 📋 最終處理結果報告")
    logging.info("=" * 60)
    logging.info(f"  ✅ 成功完整章節：{len(complete_chapters)} 章  → {sorted(complete_chapters)}")
    if failed_chapters:
        logging.error(
            f"  ❌ 失敗章節 (共 {len(failed_chapters)} 章，已從輸出移除)：\n"
            f"     {sorted(failed_chapters)}"
        )
        logging.error(
            f"  ⚠️  失敗原因：抓取 / TTS / SRT / 圖片 / MP4 任一產物缺失 (多次重試後仍失敗)"
        )
    else:
        logging.info("  🎉 所有章節均成功，無任何失敗！")
    logging.info("=" * 60)
    logging.info("")


def require_complete_worker(failed_chapters, worker_id):
    """Prevent a partial worker artifact from being reported as successful."""
    if failed_chapters:
        failed = sorted(int(chapter) for chapter in failed_chapters)
        raise RuntimeError(
            f"Worker {worker_id} 章節驗收失敗，禁止發布不完整 artifact：{failed}"
        )


def recover_incomplete_chapters(config, chapters, exact_indices, failed_chapters,
                                worker_id, max_rounds=3):
    """Retry the complete pipeline only for chapters that failed validation."""
    pending = sorted(int(chapter) for chapter in failed_chapters)
    chapter_by_index = dict(zip(exact_indices, chapters))

    for recovery_round in range(1, max_rounds + 1):
        if not pending:
            break
        logging.warning(
            "[Worker-%s] 自動缺章修復 %s/%s：%s",
            worker_id, recovery_round, max_rounds, pending,
        )
        retry_urls = [chapter_by_index[index] for index in pending]
        stage_crawl(config, retry_urls, pending[0], pending)
        stage_clean(config, target_indices=pending)
        stage_tts(config, target_indices=pending)
        stage_image_gen(config, target_indices=pending)
        stage_video_gen(config, build_parts=False, target_indices=pending)
        _, still_failed = validate_chapter_completeness(config, pending)
        pending = sorted(still_failed)

    return set(pending)


# ── 各 Stage 處理函式 ──────────────────────────────────────

def stage_crawl(config, chapters, start_global_idx, exact_indices=None):
    from crawler import run_crawler_worker
    run_crawler_worker(config, chapters, start_global_idx, exact_indices)


def stage_clean(config, target_indices=None):
    from cleaner import run_cleaner
    run_cleaner(target_indices=target_indices)


def stage_tts(config, target_indices=None):
    from tts_ms import run_tts_ms
    succeeded, failed = run_tts_ms(target_indices=target_indices)
    return succeeded, failed


def stage_image_gen(config, target_indices=None):
    from image_gen import run_image_gen
    run_image_gen(target_indices=target_indices)


def stage_video_gen(config, build_parts=True, target_indices=None):
    from video_gen import run_video_gen
    run_video_gen(build_parts=build_parts, target_indices=target_indices)


# ── 主程式 ────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Audiobook Matrix Worker Pipeline")
    parser.add_argument("--stage",            required=True,
                        choices=["crawl", "clean", "tts", "image_gen", "video_gen", "validate", "pipeline"],
                        help="Pipeline stage to execute")
    parser.add_argument("--worker-id",        type=int, required=True,
                        help="Worker index (0-based)")
    parser.add_argument("--batch-size",       type=int, default=1,
                        help="Mini-batch size for end-to-end processing (default: 1 for per-chapter saving)")
    parser.add_argument("--force",            action="store_true",
                        help="Force re-rendering images and videos even if cached MP4 exists")
    parser.add_argument("--config",           type=str, default="",
                        help="Path to config.yaml (defaults to ../config.yaml relative to src/)")
    args = parser.parse_args()

    config_path = args.config if args.config else None
    config = load_config(config_path)

    # dynamically slice chapters from config
    chapters_per_worker = config.get("chapters_per_worker", 10)
    all_chapters = config.get("chapters", [])
    all_indices = config.get("selected_indices", [])

    start_idx = args.worker_id * chapters_per_worker
    end_idx = start_idx + chapters_per_worker

    chapters = all_chapters[start_idx:end_idx]
    exact_indices = all_indices[start_idx:end_idx]

    setup_logging(args.worker_id)
    if not exact_indices:
        logging.info(f"=== Worker {args.worker_id} has 0 chapters assigned. Exiting gracefully. ===")
        sys.exit(0)

    start_global_idx = exact_indices[0]
    logging.info(f"=== Worker {args.worker_id} | Stage: {args.stage} | 章節範圍: {exact_indices[0]}~{exact_indices[-1]} ===")
    logging.info(f"Assigned chapters: {len(chapters)} 章  (global idx: {exact_indices})")

    stage = args.stage
    tts_failed_chapters = set()

    if stage == "pipeline":
        batch_size = args.batch_size if args.batch_size > 0 else 1
        total_in_worker = len(exact_indices)
        book_title = config['book_title']
        workspace_dir = os.path.abspath(os.path.join(
            SRC_DIR, "..", config['paths']['workspace_base'], book_title
        ))
        video_dir = os.path.join(workspace_dir, "Video")

        logging.info(f"=== Worker {args.worker_id} 啟動逐章一條龍即時合成與存檔模式 (共 {total_in_worker} 章) ===")

        for i in range(0, total_in_worker, batch_size):
            sub_chapters = chapters[i:i + batch_size]
            sub_indices = exact_indices[i:i + batch_size]

            # ── 第一道防線：MP4 已存在 → 無條件跳過整個 batch，不跑任何 stage ──
            missing_mp4s = []
            for c_idx in sub_indices:
                mp4_file = os.path.join(video_dir, f"{book_title}_chapter_{c_idx}.mp4")
                if not (os.path.exists(mp4_file) and os.path.getsize(mp4_file) > 1000):
                    missing_mp4s.append(c_idx)

            if not missing_mp4s:
                logging.info(f"=== [Worker-{args.worker_id}] ⚡ 第 {sub_indices[0]}~{sub_indices[-1]} 章 MP4 已存在，跳過 ===")
                logging.info(f"[PROGRESS_MARKER] Worker-{args.worker_id} | Ch {sub_indices[0]}~{sub_indices[-1]} done ({i + len(sub_indices)}/{total_in_worker})")
                continue

            logging.info(f"=== [Worker-{args.worker_id}] ▶️ 開始執行批次：第 {sub_indices[0]}~{sub_indices[-1]} 章（缺 MP4: {missing_mp4s}）===")
            stage_crawl(config, sub_chapters, sub_indices[0], sub_indices)
            stage_clean(config, target_indices=sub_indices)
            _, failed_in_batch = stage_tts(config, target_indices=sub_indices)
            if failed_in_batch:
                tts_failed_chapters.update(failed_in_batch)
            stage_image_gen(config, target_indices=sub_indices)
            stage_video_gen(config, build_parts=False, target_indices=sub_indices)
            still_missing_mp4s = []
            for c_idx in sub_indices:
                mp4_file = os.path.join(video_dir, f"{book_title}_chapter_{c_idx}.mp4")
                if not (os.path.exists(mp4_file) and os.path.getsize(mp4_file) > 1000):
                    still_missing_mp4s.append(c_idx)
            if still_missing_mp4s:
                logging.error(
                    "[Worker-%s] 批次未產生必要 MP4：%s；將交由缺章修復流程重試",
                    args.worker_id, still_missing_mp4s,
                )
            else:
                logging.info(f"=== [Worker-{args.worker_id}] ✅ 批次完成：第 {sub_indices[0]}~{sub_indices[-1]} 章 MP4 已寫入 ===")
                logging.info(f"[PROGRESS_MARKER] Worker-{args.worker_id} | Ch {sub_indices[0]}~{sub_indices[-1]} done ({i + len(sub_indices)}/{total_in_worker})")

        # Validate before packaging. Any missing chapter is retried through the
        # complete pipeline, then validated again before artifacts may publish.
        logging.info(f"=== [Worker-{args.worker_id}] 執行最終完成度驗收 ===")
        complete_chapters, final_failed = validate_chapter_completeness(
            config, exact_indices, tts_failed_chapters
        )
        if final_failed:
            final_failed = recover_incomplete_chapters(
                config, chapters, exact_indices, final_failed, args.worker_id
            )
            complete_chapters, final_failed = validate_chapter_completeness(
                config, exact_indices
            )
        print_final_report(complete_chapters, final_failed, args.worker_id)
        require_complete_worker(final_failed, args.worker_id)

        # Only a complete worker may build aggregate outputs.
        logging.info(f"=== [Worker-{args.worker_id}] 驗收完成，開始執行 Part 大影片打包（force={args.force}）===")
        stage_video_gen(config, build_parts=True)

    elif stage == "crawl":
        stage_crawl(config, chapters, start_global_idx, exact_indices)

    elif stage == "clean":
        stage_clean(config)

    elif stage == "tts":
        _, tts_failed_chapters = stage_tts(config)

    elif stage == "image_gen":
        stage_image_gen(config)

    elif stage == "video_gen":
        stage_video_gen(config)

    elif stage == "validate":
        # 驗收：確認每章四件齊全（WAV + JPG + SRT + MP4）
        complete_chapters, final_failed = validate_chapter_completeness(
            config, exact_indices, tts_failed_chapters
        )
        print_final_report(complete_chapters, final_failed, args.worker_id)
        require_complete_worker(final_failed, args.worker_id)

    logging.info(f"=== Worker {args.worker_id} | Stage: {stage} DONE ===")


if __name__ == "__main__":
    main()
