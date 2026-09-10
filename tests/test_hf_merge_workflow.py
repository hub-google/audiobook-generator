from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def workflow(name):
    return yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))


def test_gui_dispatches_hf_identity_not_source_run_artifacts():
    text = (ROOT / "合併上傳" / "gui.py").read_text(encoding="utf-8")
    assert '"merge-hf-book.yml"' in text
    assert '"book_key"' in text
    assert '("book_title",p["book_title"])' in text
    assert "source_run_id" not in text
    assert "mp4-worker" not in text


def test_merge_run_name_is_human_readable_and_describes_grouping():
    text = (ROOT / ".github" / "workflows" / "merge-hf-book.yml").read_text(encoding="utf-8")
    parsed = workflow("merge-hf-book.yml")
    inputs = parsed[True]["workflow_dispatch"]["inputs"]
    assert inputs["book_title"]["required"] is True
    assert "inputs.book_title" in text.splitlines()[1]
    assert "inputs.book_key" not in text.splitlines()[1]
    assert "全部合併" in text.splitlines()[1]
    assert "每支最多 {0} 小時" in text.splitlines()[1]


def test_cloud_workflow_only_artifacts_the_small_plan():
    text = (ROOT / ".github" / "workflows" / "merge-hf-book.yml").read_text(encoding="utf-8")
    assert "hf-merge-plan" in text
    assert "audiobook.mp4" not in text
    assert "mp4-worker" not in text
    assert "cloud_pipeline.py\" merge" in text


def test_merge_downloads_pinned_parts_before_local_ffmpeg_concat():
    text = (ROOT / "合併上傳" / "cloud_pipeline.py").read_text(encoding="utf-8")
    merge = text.split("def merge_output", 1)[1].split("def credentials", 1)[0]
    assert "download_pinned_part" in merge
    assert "local_ffconcat_text" in merge
    assert "https://huggingface.co" not in merge
    assert "-protocol_whitelist" not in merge


def test_full_book_merges_are_globally_serialized_including_split_outputs():
    parsed = workflow("merge-hf-book.yml")
    assert parsed["concurrency"] == {
        "group": "hf-book-merge-global",
        "cancel-in-progress": False,
    }
    strategy = parsed["jobs"]["merge_and_pause"]["strategy"]
    assert strategy["fail-fast"] is False
    assert strategy["max-parallel"] == 1


def test_phase_one_records_24_hours_and_98_percent():
    text = (ROOT / "合併上傳" / "cloud_pipeline.py").read_text(encoding="utf-8")
    assert "int(total*.98)" in text
    assert "timedelta(hours=24)" in text
    assert 'response.status_code!=308' in text


def test_gui_previews_exact_youtube_title_and_removes_old_heading():
    text = (ROOT / "合併上傳" / "gui.py").read_text(encoding="utf-8")
    assert '"youtube_title","YouTube 影片名稱"' in text
    assert 'text="HF 有聲小說"' not in text
    assert 'self.title("HF 有聲小說' not in text


def test_gui_exposes_clickable_links_for_both_action_phases():
    text = (ROOT / "合併上傳" / "gui.py").read_text(encoding="utf-8")
    assert '"🔗 開啟合併 Run"' in text
    assert '"🔗 開啟續傳 Run"' in text
    assert 'column not in {"#2", "#4"}' in text
    assert 'cursor="hand2" if linked else ""' in text


def test_merge_upload_uses_manifest_title_and_chapter_timeline():
    text = (ROOT / "合併上傳" / "cloud_pipeline.py").read_text(encoding="utf-8")
    assert '"youtube_title":item["youtube_title"]' in text
    assert '"youtube_description":output_chapter_timeline(item)' in text
    assert 'manifest.get("youtube_title")' in text
    assert 'manifest.get("youtube_description")' in text


def test_scheduler_and_resume_workflows_parse():
    assert workflow("hf-upload-resume-scheduler.yml")
    assert workflow("resume-hf-upload.yml")


def test_resume_run_is_identifiable_by_book_and_scheduler_passes_title():
    resume = (ROOT / ".github" / "workflows" / "resume-hf-upload.yml").read_text(encoding="utf-8")
    pipeline = (ROOT / "合併上傳" / "cloud_pipeline.py").read_text(encoding="utf-8")
    assert 'run-name: "【HF 續傳】${{ inputs.book_title || inputs.state_path }}"' in resume
    assert 'book_title: {description: "Human-readable audiobook title for progress tracking"' in resume
    assert '"book_title":str(state.get("book_title") or "")' in pipeline


def test_resume_scheduler_runs_hourly_and_keeps_only_latest_run_record():
    text = (ROOT / ".github" / "workflows" / "hf-upload-resume-scheduler.yml").read_text(encoding="utf-8")
    parsed = workflow("hf-upload-resume-scheduler.yml")
    assert parsed[True]["schedule"] == [{"cron": "17 * * * *"}]
    assert "Delete older resume scheduler run records" in text
    assert "actions/workflows/hf-upload-resume-scheduler.yml/runs?per_page=100" in text
    assert 'select(.id != ($CURRENT_RUN_ID | tonumber))' in text
    assert 'actions/runs/$old_run_id/cancel' in text
    assert '--method DELETE "repos/$REPOSITORY/actions/runs/$old_run_id"' in text


def test_scan_due_falls_back_to_manifest_title_and_defaults_master():
    pipeline = (ROOT / "合併上傳" / "cloud_pipeline.py").read_text(encoding="utf-8")
    assert 'manifest_path' in pipeline
    assert 'remote_json(state["manifest_path"]).get("book_title")' in pipeline
    assert 'os.environ.get("GITHUB_REF_NAME","master")' in pipeline

