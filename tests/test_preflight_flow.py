from unittest.mock import Mock
from datetime import datetime, timezone, timedelta
import copy
import pytest
from src.cloud_queue import new_task, add_tasks, empty_queue, approve_preflight
from src.queue_dispatcher import Dispatcher


def ready_task():
    task = new_task("https://example.com/book", "Book", 1, 2)
    task.update(workflow_phase="preflight", status="waiting_review", scrape_run_id=100, cover_run_id=200)
    for name, rid in (("scrape", 100), ("cover", 200)):
        task["stages"][name].update(status="completed", run_id=rid)
    return task


def test_approval_requires_both_assets_and_current_run_identity():
    task = ready_task()
    queue = add_tasks(empty_queue(), [task])
    with pytest.raises(ValueError):
        approve_preflight(queue, task["task_id"], 99, 200, {})
    incomplete = copy.deepcopy(queue)
    incomplete["queue"][0]["stages"]["cover"]["status"] = "running"
    with pytest.raises(ValueError):
        approve_preflight(incomplete, task["task_id"], 100, 200, {})
    approved = approve_preflight(queue, task["task_id"], 100, 200, {"status": "approved", "remove_patterns": []})
    assert approved["queue"][0]["workflow_phase"] == "processing"


def fake_dispatcher(queue):
    dispatcher = Dispatcher("owner/repo", "token")
    dispatcher.dispatch_discovery_delays = (0,)
    dispatcher.store = Mock()
    dispatcher.store.load.return_value = (queue, "sha")
    dispatcher.profile_store.load = Mock(return_value=({"books": {}}, None))
    dispatcher.request = Mock()
    dispatcher.cover_runs = Mock(return_value=[])
    return dispatcher


def test_processing_dispatch_preserves_phase_and_never_selects_scrape_resume():
    task = ready_task()
    queue = approve_preflight(add_tasks(empty_queue(), [task]), task["task_id"], 100, 200,
                              {"status": "approved", "remove_patterns": []})
    dispatcher = fake_dispatcher(queue)
    dispatcher.select_artifact_source_run_id = Mock(side_effect=AssertionError("must not scan unrelated runs"))
    dispatcher.runs = Mock(side_effect=[[], [{"id": 300, "display_title": task["task_id"],
        "created_at": (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()}]])
    dispatcher.dispatch_next(queue)
    writes = [call.args[0] for call in dispatcher.store.save.call_args_list]
    assert all(q["queue"][0]["workflow_phase"] == "processing" for q in writes)
    payload = dispatcher.request.call_args_list[0].kwargs["json"]["inputs"]
    assert payload["resume_source_run_id"] == ""
    assert payload["scrape_source_run_id"] == "100"
    assert payload["cover_source_run_id"] == "200"
    assert payload["execution_phase"] == "processing"


def test_cover_retry_dispatches_only_cover():
    task = ready_task()
    task.update(status="queued")
    task["stages"]["cover"].update(status="failed")
    queue = add_tasks(empty_queue(), [task])
    dispatcher = fake_dispatcher(queue)
    dispatcher.runs = Mock(return_value=[])
    dispatcher.dispatch_next(queue)
    posts = [c.args[1] for c in dispatcher.request.call_args_list if c.args[0] == "POST"]
    assert posts == ["/actions/workflows/cover-preflight.yml/dispatches"]


def test_processing_reconcile_ignores_scraper_success():
    task = ready_task()
    task.update(workflow_phase="processing", status="queued", run_id=None)
    queue = add_tasks(empty_queue(), [task])
    dispatcher = fake_dispatcher(queue)
    dispatcher.runs = Mock(return_value=[{"id": 100, "display_title": "【TXT抓取】" + task["task_id"],
                                         "status": "completed", "conclusion": "success"}])
    result, _ = dispatcher.reconcile(queue)
    assert result["queue"][0]["status"] == "queued"
    assert not result["completed"]


def test_processing_retry_uses_only_previous_processing_run_as_resume_source():
    task = ready_task()
    task.update(
        workflow_phase="processing", status="queued", run_id=None,
        processing_run_id=333,
        ad_review={"status": "approved", "remove_patterns": []},
    )
    queue = add_tasks(empty_queue(), [task])
    dispatcher = fake_dispatcher(queue)
    dispatcher.runs = Mock(return_value=[])
    dispatcher.cover_runs = Mock(return_value=[])
    dispatcher.dispatch_next(queue)
    payload = dispatcher.request.call_args_list[0].kwargs["json"]["inputs"]
    assert payload["resume_source_run_id"] == "333"
    assert payload["execution_phase"] == "resume_processing"
    assert payload["scrape_source_run_id"] == "100"


def test_dispatch_without_indexed_run_keeps_task_reserved():
    task = new_task("https://example.com/book", "Book", 1, 2)
    queue = add_tasks(empty_queue(), [task])
    dispatcher = fake_dispatcher(queue)
    dispatcher.runs = Mock(return_value=[])
    dispatcher.cover_runs = Mock(return_value=[])
    result, _ = dispatcher.dispatch_next(queue)
    assert result["queue"][0]["status"] == "preparing_assets"
    assert result["queue"][0]["workflow_phase"] == "preflight"


def test_dispatch_polls_until_both_preflight_run_ids_are_attached():
    task = new_task("https://example.com/book", "Book", 1, 2)
    queue = add_tasks(empty_queue(), [task])
    dispatcher = fake_dispatcher(queue)
    dispatcher.dispatch_discovery_delays = (0, 0)
    created = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
    dispatcher.runs = Mock(side_effect=[[], [{
        "id": 100, "display_title": f"【TXT抓取】Book｜{task['task_id']}", "created_at": created,
    }]])
    dispatcher.cover_runs = Mock(side_effect=[[], [{
        "id": 200, "display_title": f"封面｜Book｜{task['task_id']}", "created_at": created,
    }]])

    dispatcher.dispatch_next(queue)

    attached = dispatcher.store.save.call_args_list[-1].args[0]["queue"][0]
    assert attached["scrape_run_id"] == 100
    assert attached["cover_run_id"] == 200
    assert attached["stages"]["scrape"]["run_id"] == 100
    assert attached["stages"]["cover"]["run_id"] == 200
