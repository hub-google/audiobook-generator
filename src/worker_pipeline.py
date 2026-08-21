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
import shutil
import subprocess

try:
    from pipeline_checkpoint import PipelineCheckpoint, STAGES
    from source_status import SourceMissingError, SourceStatusStore
except ImportError:  # Allow importing as src.worker_pipeline in tests/tools.
    from src.pipeline_checkpoint import PipelineCheckpoint, STAGES
    from src.source_status import SourceMissingError, SourceStatusStore

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


def print_final_report(complete_chapters, failed_chapters, worker_id, source_missing=None):
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
    source_missing = sorted(int(chapter) for chapter in (source_missing or []))
    if source_missing:
        logging.warning(
            "⚠️ Origin website missing chapters (%s): %s",
            len(source_missing), source_missing,
        )
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
    return run_video_gen(build_parts=build_parts, target_indices=target_indices)


def part_output_paths(built_parts):
    """Return the merged video path from each part-builder result."""
    paths = []
    for position, part in enumerate(built_parts, start=1):
        if not isinstance(part, dict):
            raise TypeError(
                f"part builder result #{position} must be a mapping, "
                f"got {type(part).__name__}"
            )
        path = part.get("merged_video")
        if not isinstance(path, (str, os.PathLike)) or not os.fspath(path):
            raise RuntimeError(
                f"part builder result #{position} has no merged video path"
            )
        paths.append(os.fspath(path))
    if not paths:
        raise RuntimeError("part builder produced no merged videos")
    return paths


def run_resumable_chapter(config, checkpoint, chapter_url, chapter_num, worker_id):
    """Run one chapter from its first missing output and persist every result."""
    chapter_num = int(chapter_num)
    operations = (
        ("crawler", lambda: stage_crawl(config, [chapter_url], chapter_num, [chapter_num])),
        ("cleaner", lambda: stage_clean(config, target_indices=[chapter_num])),
        (("tts", "subtitle"), lambda: stage_tts(config, target_indices=[chapter_num])),
        ("image", lambda: stage_image_gen(config, target_indices=[chapter_num])),
        ("video", lambda: stage_video_gen(config, build_parts=False, target_indices=[chapter_num])),
    )

    for stage_names, operation in operations:
        stage_names = (stage_names,) if isinstance(stage_names, str) else stage_names
        missing = [stage for stage in stage_names if not checkpoint.is_completed(chapter_num, stage)]
        if not missing:
            logging.info("[CHECKPOINT] Worker-%s chapter %s %s already complete; skipping",
                         worker_id, chapter_num, ",".join(stage_names))
            continue

        first_stage_index = STAGES.index(stage_names[0])
        missing_upstream = [stage for stage in STAGES[:first_stage_index]
                            if not checkpoint.is_completed(chapter_num, stage)]
        if missing_upstream:
            raise RuntimeError(
                f"chapter {chapter_num} cannot run {stage_names[0]}; "
                f"missing upstream output(s): {missing_upstream}"
            )

        # Some operations (currently TTS + subtitle) regenerate all outputs in
        # their group, so record the attempt against every stage they execute.
        checkpoint.prepare_for_run(chapter_num, stage_names)
        for stage in stage_names:
            checkpoint.mark_running(chapter_num, stage)
        try:
            operation()
            for stage in stage_names:
                checkpoint.mark_completed(chapter_num, stage)
        except SourceMissingError as error:
            if stage_names[0] != "crawler":
                raise
            evidence = SourceStatusStore(checkpoint.workspace_dir).load(chapter_num)
            checkpoint.mark_source_missing(chapter_num, error, evidence=evidence)
            logging.warning(
                "::warning title=Origin website missing chapter::Chapter %s has no article "
                "after repeated successful HTTP responses and was skipped.", chapter_num,
            )
            return
        except Exception as error:
            for stage in stage_names:
                if checkpoint.is_completed(chapter_num, stage):
                    checkpoint.mark_completed(chapter_num, stage)
                else:
                    checkpoint.mark_failed(chapter_num, stage, error)
            raise



def _find_gh():
    gh_candidates = [
        r"C:\Program Files\GitHub CLI\gh.exe",
        shutil.which("gh"),
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "GitHub CLI", "gh.exe"),
    ]
    for p in gh_candidates:
        if p and os.path.exists(p):
            return p
    return "gh"


def _copy_artifact_files_to_workspace(src_dir, workspace_dir, book_title):
    """Safely map and copy chapter artifacts into the canonical Workspace structure."""
    subfolder_map = {
        ".mp4": "Video",
        ".srt": "Subtitles",
        ".wav": "Audio",
        ".jpg": "Images",
        ".jpeg": "Images",
        "_raw.txt": "RawText",
        "_clean.txt": "CleanText",
    }
    for root, _, files in os.walk(src_dir):
        for f in files:
            src_path = os.path.join(root, f)
            if not os.path.isfile(src_path) or os.path.getsize(src_path) == 0:
                continue
            dest_folder = None
            if f.endswith("_raw.txt"):
                dest_folder = "RawText"
            elif f.endswith("_clean.txt"):
                dest_folder = "CleanText"
            elif "manifest-worker" in f and f.endswith(".json"):
                dest_folder = "Manifests"
            elif "source_missing" in f and f.endswith(".json"):
                dest_folder = "SourceStatus"
            else:
                ext = os.path.splitext(f)[1].lower()
                dest_folder = subfolder_map.get(ext)

            if dest_folder and "chapter_" in f:
                target_dir = os.path.join(workspace_dir, dest_folder)
                os.makedirs(target_dir, exist_ok=True)
                dest_path = os.path.join(target_dir, f)
                if not os.path.exists(dest_path) or os.path.getsize(dest_path) < os.path.getsize(src_path):
                    shutil.copy2(src_path, dest_path)


def inherit_historical_artifacts(config, checkpoint, worker_id, exact_indices):
    """Scan and download completed chapter artifacts from previous runs of the same novel."""
    incomplete = set(checkpoint.incomplete_chapters())
    if not incomplete:
        logging.info("[Inherit] Worker %s 所有章節已由本機快取命中，無需拉取歷史 Artifacts。", worker_id)
        return

    book_title = config.get("book_title", "")
    task_id = config.get("queue_task_id") or os.environ.get("QUEUE_TASK_ID", "")
    current_run_id = os.environ.get("GITHUB_RUN_ID", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "hub-google/audiobook-generator")
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")

    logging.info("🔍 [Inherit] 正在為 Worker %s 搜尋同本小說（%s）的歷史 Run Artifacts...", worker_id, book_title)

    # 1. 取得歷史 Run ID 列表 (倒序，從最新到最舊)
    candidate_runs = []

    # 嘗試從 automation-state 的 audiobook-queue.json 讀取 run_history
    try:
        from .cloud_queue import GitHubQueueStore
    except ImportError:
        try:
            from cloud_queue import GitHubQueueStore
        except ImportError:
            GitHubQueueStore = None

    if GitHubQueueStore and token:
        try:
            store = GitHubQueueStore(repo, token, branch=os.environ.get("QUEUE_STATE_BRANCH", "automation-state"))
            queue, _ = store.load()
            for t in queue.get("tasks", []):
                if (task_id and t.get("task_id") == task_id) or (book_title and t.get("book_title") == book_title):
                    history = t.get("run_history") or []
                    for item in reversed(history):
                        rid = item.get("run_id")
                        if rid and str(rid) != str(current_run_id) and rid not in candidate_runs:
                            candidate_runs.append(rid)
        except Exception as queue_err:
            logging.warning("[Inherit] 讀取雲端隊列歷史失敗: %s", queue_err)

    # 補充：如果從隊列沒拿到足夠的候選，或者手動執行沒有 task_id，用 gh api 查詢該 workflow 的歷史 runs
    gh_bin = _find_gh()
    if gh_bin or token:
        try:
            cmd = [
                gh_bin, "api",
                f"repos/{repo}/actions/workflows/audiobook.yml/runs?per_page=30",
                "--jq", ".workflow_runs[] | {id: .id, title: (.display_title // .name)}"
            ]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if res.returncode == 0 and res.stdout.strip():
                for line in res.stdout.strip().split("\n"):
                    if not line.strip():
                        continue
                    item = json.loads(line)
                    rid = item.get("id")
                    rtitle = item.get("title") or ""
                    # 匹配 task_id 或 book_title
                    match = False
                    if task_id and task_id in rtitle:
                        match = True
                    elif book_title and book_title in rtitle:
                        match = True
                    if match and rid and str(rid) != str(current_run_id) and rid not in candidate_runs:
                        candidate_runs.append(rid)
        except Exception as api_err:
            logging.warning("[Inherit] API 查詢歷史 Runs 失敗: %s", api_err)

    if not candidate_runs:
        logging.info("[Inherit] 未找到同本小說的歷史 Run，將依序檢查本地 Cache 或執行全新運算。")
        return

    logging.info("📋 [Inherit] 找到 %d 個歷史 Run 候選：%s", len(candidate_runs), candidate_runs)

    # 2. 依序倒序遍歷所有歷史 Run
    target_artifacts = [
        f"mp4-worker-{worker_id}",
        f"video-worker-{worker_id}",
    ]

    try:
        from .youtube_api_uploader import download_artifact_task
    except ImportError:
        try:
            from youtube_api_uploader import download_artifact_task
        except ImportError:
            download_artifact_task = None

    if not download_artifact_task:
        logging.warning("[Inherit] download_artifact_task 不可用，略過歷史 Artifact 下載")
        return

    import tempfile
    with tempfile.TemporaryDirectory() as temp_dir:
        for past_run_id in candidate_runs:
            if not incomplete:
                break
            logging.info("🔎 [Inherit] 正在檢查歷史 Run %s 的產物...", past_run_id)
            downloaded = False
            for art_name in target_artifacts:
                art_dest = os.path.join(temp_dir, f"run_{past_run_id}_{art_name}")
                try:
                    success = download_artifact_task(str(past_run_id), repo, art_name, art_dest)
                except Exception as dl_err:
                    logging.warning("[Inherit] 下載 Run %s 產物 %s 失敗（可能已被手動刪除）: %s", past_run_id, art_name, dl_err)
                    continue

                if success and os.path.exists(art_dest):
                    downloaded = True
                    _copy_artifact_files_to_workspace(art_dest, checkpoint.workspace_dir, book_title)

            if downloaded:
                checkpoint.reconcile()
                incomplete = set(checkpoint.incomplete_chapters())
                recovered_count = len(exact_indices) - len(incomplete)
                logging.info("📥 [Inherit] 從歷史 Run %s 成功還原！目前 Worker %s 進度：%d/%d 章",
                             past_run_id, worker_id, recovered_count, len(exact_indices))

    if not incomplete:
        logging.info("🎉 [Inherit] Worker %s 所有章節（%d 章）已全數從歷史 Run 還原完畢，100%% 齊全！",
                     worker_id, len(exact_indices))
    else:
        logging.info("⚠️ [Inherit] 經過歷史回溯，Worker %s 仍有 %d 章缺失，將由 TTS 引擎自動補齊。",
                     worker_id, len(incomplete))

def run_pipeline(config, worker_id=0, chapters=None, exact_indices=None,
                 build_parts=True, force=False):
    """Run the strict resumable pipeline for local and Actions callers alike."""
    chapters = list(config.get("chapters", []) if chapters is None else chapters)
    exact_indices = list(
        config.get("selected_indices", []) if exact_indices is None else exact_indices
    )
    if not chapters or not exact_indices or len(chapters) != len(exact_indices):
        raise ValueError("chapters and selected_indices must be non-empty and have equal length")

    book_title = config["book_title"]
    workspace_dir = os.path.abspath(os.path.join(
        SRC_DIR, "..", config["paths"]["workspace_base"], book_title
    ))
    checkpoint = PipelineCheckpoint(
        workspace_dir, book_title, worker_id, exact_indices
    )
    # ── 跨 Run 歷史 Artifacts 優先繼承 ───────────────────────────
    inherit_historical_artifacts(config, checkpoint, worker_id, exact_indices)
    logging.info("=== Worker %s resumable per-stage pipeline (%s chapters) ===",
                 worker_id, len(exact_indices))

    for position, (chapter_url, chapter_num) in enumerate(
        zip(chapters, exact_indices), start=1
    ):
        try:
            run_resumable_chapter(
                config, checkpoint, chapter_url, chapter_num, worker_id
            )
            logging.info("[PROGRESS_MARKER] Worker-%s | Ch %s complete (%s/%s)",
                         worker_id, chapter_num, position, len(exact_indices))
        except Exception as error:
            # Independent chapters may continue, but the worker cannot succeed
            # while any chapter remains incomplete.
            logging.exception("[CHECKPOINT] Worker-%s chapter %s stopped: %s",
                              worker_id, chapter_num, error)

    final_failed = set(checkpoint.incomplete_chapters())
    missing_chapters = set(checkpoint.source_missing_chapters())
    complete_chapters = [
        chapter for chapter in exact_indices if chapter not in final_failed
        and chapter not in missing_chapters
    ]
    print_final_report(complete_chapters, final_failed, worker_id, missing_chapters)
    require_complete_worker(final_failed, worker_id)
    manifest = checkpoint.export_manifest()
    m_val = checkpoint.validate_manifest()
    chapter_count = m_val.get("chapter_count", len(complete_chapters)) if isinstance(m_val, dict) else len(complete_chapters)
    total_duration = m_val.get("total_duration_seconds", 0.0) if isinstance(m_val, dict) else 0.0
    missing_count = m_val.get("missing_count", len(missing_chapters)) if isinstance(m_val, dict) else len(missing_chapters)
    logging.info("[Worker-%s] ✅ 輕量時長清單 (Manifest) 嚴格驗證通過：共 %s 章時長，總計 %.1fs，來源缺章 %s",
                 worker_id, chapter_count, total_duration, missing_count)

    if build_parts:
        logging.info(
            "=== [Worker-%s] All stage outputs valid; building Parts (force=%s) ===",
            worker_id, force,
        )
        checkpoint.mark_worker_stage_running("part_build")
        try:
            built_parts = stage_video_gen(config, build_parts=True)
            checkpoint.mark_worker_stage_completed(
                "part_build", part_output_paths(built_parts)
            )
        except Exception as error:
            checkpoint.mark_worker_stage_failed("part_build", error)
            raise
    return checkpoint


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
        run_pipeline(
            config, worker_id=args.worker_id, chapters=chapters,
            # Matrix workers only publish per-chapter artifacts. The uploader
            # downloads every worker artifact and builds globally contiguous
            # 10-11 hour Parts once, after all workers have succeeded.
            exact_indices=exact_indices, build_parts=False, force=args.force,
        )

    elif stage == "crawl":
        stage_crawl(config, chapters, start_global_idx, exact_indices)

    elif stage == "clean":
        stage_clean(config, target_indices=exact_indices)

    elif stage == "tts":
        _, tts_failed_chapters = stage_tts(config, target_indices=exact_indices)

    elif stage == "image_gen":
        stage_image_gen(config, target_indices=exact_indices)

    elif stage == "video_gen":
        stage_video_gen(config, build_parts=False, target_indices=exact_indices)

    elif stage == "validate":
        # 驗收：確認每章四件齊全（WAV + JPG + SRT + MP4）
        complete_chapters, final_failed = validate_chapter_completeness(
            config, exact_indices, tts_failed_chapters
        )
        print_final_report(complete_chapters, final_failed, args.worker_id)
        require_complete_worker(final_failed, args.worker_id)
        book_title = config["book_title"]
        workspace_dir = os.path.abspath(os.path.join(
            SRC_DIR, "..", config["paths"]["workspace_base"], book_title
        ))
        checkpoint = PipelineCheckpoint(workspace_dir, book_title, args.worker_id, exact_indices)
        checkpoint.export_manifest()
        m_val = checkpoint.validate_manifest()
        chapter_count = m_val.get("chapter_count", len(complete_chapters)) if isinstance(m_val, dict) else len(complete_chapters)
        total_duration = m_val.get("total_duration_seconds", 0.0) if isinstance(m_val, dict) else 0.0
        missing_count = m_val.get("missing_count", 0) if isinstance(m_val, dict) else 0
        logging.info("[Worker-%s] ✅ 輕量時長清單 (Manifest) 嚴格驗證通過：共 %s 章時長，總計 %.1fs，來源缺章 %s",
                     args.worker_id, chapter_count, total_duration, missing_count)

    logging.info(f"=== Worker {args.worker_id} | Stage: {stage} DONE ===")


if __name__ == "__main__":
    main()
