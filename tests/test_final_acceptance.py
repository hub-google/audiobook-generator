"""Offline acceptance tests: real media, large inventory, and fault recovery."""
import json
import math
import shutil
import struct
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock
import pytest
import yaml
from PIL import Image
from src.artifact_validation import ArtifactValidationError, validate_video, validate_worker_manifest
from src.catalog_parser import generate_matrix_from_config
from src.cleaner import clean_text_content, chunk_text
from src.pipeline_checkpoint import PipelineCheckpoint
from src.prepare_parts import plan_parts
from src.resume_planner import validate_worker
from src.subtitle_gen import generate_chapter_srt
from src.video_gen import generate_chapter_video
from src.part_builder import merge_part_videos
from src.youtube_upload.planning import validate_chapter_inventory, build_part_plan_from_inventory
from src.queue_dispatcher import Dispatcher
from src.scrape_rerun_controller import run_controller


def test_1700_chapter_inventory_with_exclusions_and_17_workers():
    selected = [n for n in range(1, 1701) if n % 23]
    matrix, _, _ = generate_matrix_from_config({"selected_indices": selected, "chapters_per_worker": 10, "book_title": "Acceptance"})
    workers = matrix["include"]
    assert len(workers) <= 17
    assigned = [n for w in workers for n in selected if w["start_chap"] <= n <= w["end_chap"]]
    assert assigned == selected
    missing = {selected[30], selected[1000]}
    inventory = [{"chap_num": n, "dur": 600, "artifact": "video-worker-0"} for n in selected if n not in missing]
    validate_chapter_inventory(inventory, 1, 1700, missing, expected_indices=selected)
    parts = build_part_plan_from_inventory(inventory, confirmed_missing=missing, expected_indices=selected)
    assert [n for p in parts for n in p["chapters"]] == [n for n in selected if n not in missing]
    assert all(p["duration"] <= 11*3600 for p in parts)
    assert {n for p in parts for n in p["source_missing_chapters"]} == missing
    with pytest.raises(RuntimeError):
        validate_chapter_inventory(inventory[:-1], 1, 1700, missing, expected_indices=selected)


def write_chapter(checkpoint, chapter):
    text = "這是一段自行創作的測試文字，用於確認字幕與影片製作流程正確。"
    title = f"第{chapter}章 測試"
    for stage in ("crawler", "cleaner", "tts", "subtitle", "image", "video"):
        Path(checkpoint.output_path(chapter, stage)).parent.mkdir(parents=True, exist_ok=True)
    Path(checkpoint.output_path(chapter, "crawler")).write_text(title + "\n\n" + text, encoding="utf-8")
    # The source and TTS service are fixture inputs; media generation and validators are real.
    cleaned = chunk_text(clean_text_content(text, title, checkpoint.book_title))
    Path(checkpoint.output_path(chapter, "cleaner")).write_text(cleaned, encoding="utf-8")
    wav = checkpoint.output_path(chapter, "tts")
    lines = [line for line in cleaned.splitlines() if line.strip()]
    segments = []
    for index, line in enumerate(lines):
        path = Path(wav).parent / f"fixture-{chapter}-{index}.wav"
        with wave.open(str(path), "wb") as audio:
            audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(24000)
            audio.writeframes(b"".join(struct.pack("<h", int(2500*math.sin(2*math.pi*440*i/24000))) for i in range(48000)))
        segments.append(str(path))
    with wave.open(wav, "wb") as audio:
        audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(24000)
        for path in segments:
            with wave.open(path, "rb") as part:
                audio.writeframes(part.readframes(part.getnframes()))
    generate_chapter_srt(segments, lines, checkpoint.output_path(chapter, "subtitle"))
    Image.new("RGB", (1280,720), "navy").save(checkpoint.output_path(chapter, "image"), "JPEG")
    video, duration = generate_chapter_video(checkpoint.book_title, wav, checkpoint.workspace_dir, str(Path(checkpoint.output_path(chapter,"video")).parent), [])
    assert video and duration > 0
    checkpoint.reconcile()


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="requires FFmpeg")
def test_real_media_partial_worker_restore_and_merge(tmp_path):
    root = tmp_path / "artifacts" / "video-worker-0" / "Acceptance"
    settings = {stage: {"version": "acceptance"} for stage in ("crawler","cleaner","tts","subtitle","image","video")}
    checkpoint = PipelineCheckpoint(root, "Acceptance", 0, [1, 3], stage_settings=settings)
    write_chapter(checkpoint, 1)
    write_chapter(checkpoint, 3)
    good = Path(checkpoint.output_path(1, "video")).read_bytes()
    Path(checkpoint.output_path(3, "subtitle")).write_text("corrupt", encoding="utf-8")
    checkpoint.reconcile()
    partial = checkpoint.export_manifest()
    assert partial["status"] == "partial"
    assert partial["incomplete_chapters"] == [3]
    assert [row["chap_num"] for row in partial["chapters"]] == [1]
    config = {"selected_indices": [1,3], "book_title": "Acceptance"}
    worker = {"worker_id": 0, "start_chap": 1, "end_chap": 3}
    assert validate_worker(root, worker, config)["completed_chapters"] == [1]
    # Restart, rebuild the damaged chapter, and keep the already-validated chapter.
    restored = PipelineCheckpoint(root, "Acceptance", 0, [1,3], stage_settings=settings)
    assert restored.incomplete_chapters() == [3]
    restored.prepare_for_run(3, ("subtitle","video"))
    write_chapter(restored, 3)
    assert restored.incomplete_chapters() == []
    assert Path(restored.output_path(1, "video")).read_bytes() == good
    restored.export_manifest()
    assert validate_worker(root, worker, config, require_complete=True)["complete"]
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump(config), encoding="utf-8")
    plan = plan_parts("1", "", cfg, tmp_path/"plan", tmp_path/"work", artifact_dir=tmp_path/"artifacts")
    assert plan["parts"][0]["chapters"] == [1,3]
    assert plan["source_missing_chapters"] == []
    files = [restored.output_path(n,"video") for n in (1,3)]
    target = tmp_path/"merged.mp4"
    assert merge_part_videos({"part_num":1, "files": files, "duration": plan["parts"][0]["duration"]}, str(target))
    assert validate_video(target, plan["parts"][0]["duration"])["duration_seconds"] > 0


def test_duplicate_and_nonfinite_manifest_rejected(tmp_path):
    path = tmp_path/"manifest.json"
    for rows in ([{"chap_num":1,"dur":1},{"chap_num":1,"dur":1}], [{"chap_num":1,"dur":float("nan")}]):
        path.write_text(json.dumps({"worker_id":0,"chapters":rows,"source_missing":[]}))
        with pytest.raises(ArtifactValidationError):
            validate_worker_manifest(path, 0, [1])


def test_native_retry_exact_twenty_then_stop():
    client = Mock()
    client.get_run.side_effect = [{"status":"completed","conclusion":"failure","display_title":"【TXT抓取】Acceptance","run_attempt":i} for i in range(1,22)]
    client.get_jobs.return_value = [{"id":1,"name":"🕷️ Scrape — Worker 1","conclusion":"failure"},{"id":2,"name":"🕷️ Scrape — Worker 2","conclusion":"success"}]
    assert run_controller(client, "100") == "needs_attention"
    assert client.rerun_failed_jobs.call_count == 20


def test_abandoned_controller_recovered_only_when_no_live_controller():
    dispatcher = Dispatcher("owner/repo","token")
    past = (datetime.now(timezone.utc)-timedelta(minutes=10)).isoformat()
    dispatcher.run_by_id = Mock(return_value={"id":100,"status":"completed","conclusion":"failure","run_attempt":2,"display_title":"【TXT抓取】Acceptance","updated_at":past})
    dispatcher.run_jobs = Mock(return_value=[{"id":1,"name":"🕷️ Scrape — Worker 1","conclusion":"failure"}])
    task = {"workflow_phase":"preflight","status":"preparing_assets","stages":{"scrape":{"status":"failed","reason":"failure","run_id":100,"run_attempt":2}}}
    dispatcher.request = Mock(return_value=Mock(json=Mock(return_value={"workflow_runs":[{"display_title":"控制｜來源 Run #100","status":"in_progress"}]})))
    assert not dispatcher.recover_preflight_controller(task)
    assert dispatcher.request.call_count == 1
    dispatcher.request.reset_mock()
    dispatcher.request.return_value.json.return_value = {"workflow_runs":[{"display_title":"控制｜來源 Run #100","status":"completed","updated_at":past}]}
    assert dispatcher.recover_preflight_controller(task)
    assert dispatcher.request.call_args.args == ("POST", "/actions/workflows/scrape-rerun.yml/dispatches")
