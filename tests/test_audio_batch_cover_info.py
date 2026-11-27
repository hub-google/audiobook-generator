"""Desktop cover information generation must remain identical in Actions."""
from unittest.mock import patch
import pytest
from tools.audio_batch_cover_info import parse_task_ids, run_batch

def test_batch_generates_each_selected_book_and_preserves_order():
    tasks=[
        {"task_id":"book-20261125-11111111","book_title":"甲","catalog_url":"https://czbooks.net/n/a"},
        {"task_id":"book-20261125-22222222","book_title":"乙","catalog_url":"https://czbooks.net/n/b"},
    ]
    called=[]
    def generator(title,url):
        called.append((title,url))
        return {"prompt":f"{title} prompt","analysis": {"世界觀":"測試"}}
    rows=run_batch(tasks,generator)
    assert [r["task_id"] for r in rows]==[x["task_id"] for x in tasks]
    assert len(called)==2
    assert [r["data"]["prompt"] for r in rows]==["甲 prompt","乙 prompt"]

def test_invalid_selected_task_ids_are_rejected():
    with pytest.raises(ValueError): parse_task_ids("book-other")
    assert parse_task_ids("book-20261125-11111111")==["book-20261125-11111111"]

def test_missing_gemini_response_returns_failure_not_placeholder():
    task={"task_id":"book-20261125-11111111","book_title":"甲","catalog_url":"https://czbooks.net/n/a"}
    results=run_batch([task],lambda title,url: {"prompt":""})
    assert results[0]["status"]=="failed"
    assert "data" not in results[0]


def test_batch_cover_automatic_results_never_require_browser_credentials():
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]
    js=(root/"docs/site/batch-cover.js").read_text(encoding="utf-8")
    workflow=(root/".github/workflows/audio-batch-cover-info.yml").read_text(encoding="utf-8")
    assert '"batch_cover_info",{task_ids:ids,request_id}' in js
    assert "web-cover-info/" in js
    assert "localStorage.setItem(REQUEST_STORE" in js
    assert "Authorization" not in js and "HF_TOKEN" not in js and "GEMINI_API_KEY" not in js
    assert "web-cover-info/$REQUEST_ID.json" in workflow
    assert "--input batch-cover-request.json" in workflow
