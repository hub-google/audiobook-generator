import importlib.util
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def module():
    path = ROOT / "合併上傳" / "merge_status.py"
    spec = importlib.util.spec_from_file_location("merge_status_under_test", path)
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value)
    return value


def test_phase_one_reports_matrix_completion_and_active_operation():
    status = module()
    run = {"status": "in_progress", "conclusion": ""}
    jobs = [
        {"name": "merge_and_pause (1)", "status": "completed", "conclusion": "success", "steps": []},
        {"name": "merge_and_pause (2)", "status": "in_progress", "conclusion": "", "steps": [{"name": "Phase 1 — upload to 98 percent", "status": "in_progress"}]},
    ]
    assert status.phase1_text(run, jobs) == "1/2 支完成；進行：上傳至 98%"


def test_phase_two_reports_earliest_resume_and_hourly_scan_in_taipei():
    status = module()
    phase, resume = status.phase2_summary([
        {"status": "paused_at_98", "target_resume_at": "2026-09-10T07:35:00+00:00"},
        {"status": "complete"},
    ])
    assert phase == "等待 24 小時（已完成 1/2）"
    assert resume == "09-10 15:35 可續做；排程約 09-10 16:17"


def test_run_title_extracts_book_name():
    assert module().run_book_title("【HF 合併】七零，我成了年代文里的惡毒女配｜全部合併") == "七零，我成了年代文里的惡毒女配"


def test_resume_run_title_extracts_book_name_and_rejects_legacy_unmapped_run():
    status = module()
    assert status.resume_run_book_title("【HF 續傳】七零，我成了年代文里的惡毒女配") == "七零，我成了年代文里的惡毒女配"
    assert status.resume_run_book_title("Resume HF audiobook YouTube upload") == ""


def test_phase_two_reports_scheduled_publish_when_complete():
    status = module()
    phase, resume = status.phase2_summary([
        {"status": "complete", "publish_at": "2026-09-14T10:00:00Z"},
    ])
    assert "已完成 1/1 支（預約 09-14 18:00 公開）" in phase

