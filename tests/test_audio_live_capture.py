"""Live Worker journal correctness without hitting cloud APIs."""
from pathlib import Path
import os
import sys

from tools.audio_live_capture import LiveJournal, main, sanitize, severity

ROOT = Path(__file__).resolve().parents[1]


def test_live_journal_redacts_bearer_and_private_provider_tokens():
    raw = "Authorization: Bearer ghp_ABCDEFGHIJKLMNO1234567890; HF_TOKEN=hf_ABCDEFGHIJKLMNO1234567890"
    clean = sanitize(raw)
    assert "ghp_ABCDEFGHIJKLMNO1234567890" not in clean
    assert "hf_ABCDEFGHIJKLMNO1234567890" not in clean
    assert "[REDACTED]" in clean
    assert len(clean)<=800
    url=sanitize("https://generativelanguage.googleapis.com/v1beta/models/x:generateContent?key=AIzaSECRET12345678901234567890&format=json")
    assert "AIzaSECRET12345678901234567890" not in url
    assert "?key=[REDACTED]" in url


def test_log_severity():
    assert severity("ERROR: unexpected provider 503") == "error"
    assert severity("retrying after WARNING") == "warning"
    assert severity("INFO chapter 1 done") == "info"


def test_wrapper_preserves_console_and_exit_code(capsys,monkeypatch):
    monkeypatch.delenv("ACTIONS_ID_TOKEN_REQUEST_URL",raising=False)
    command=[sys.executable,"-c","print('chapter 1 ok'); raise SystemExit(7)"]
    code=main(["--task-id","book-test-live-journal","--job","test","--",*command])
    captured=capsys.readouterr()
    assert code==7
    assert "chapter 1 ok" in captured.out


def test_unconfigured_workflow_does_not_call_backend(monkeypatch):
    monkeypatch.delenv("ACTIONS_ID_TOKEN_REQUEST_URL",raising=False)
    journal=LiveJournal("book-test-live-journal","test")
    assert not journal.available
    journal.start()
    journal.write("DATA")
    journal.close()


def test_workflows_attach_live_console_without_replacing_primary_process():
    audiobook=(ROOT/".github/workflows/audiobook.yml").read_text(encoding="utf-8")
    cover=(ROOT/".github/workflows/cover-preflight.yml").read_text(encoding="utf-8")
    for source in (audiobook,cover):
        assert "id-token: write" in source
        assert "tools.audio_live_capture" in source
        assert "queue_task_id" in source
    assert "python -u src/worker_pipeline.py --stage crawl" in audiobook
    assert "python -u src/worker_pipeline.py --stage pipeline" in audiobook
    assert "python -u src/youtube_api_uploader.py" in audiobook
    for stage in ("introduction","cover-information","review","cover"):
        assert '"cover-'+stage+'"' in cover
