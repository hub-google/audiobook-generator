from unittest.mock import Mock
from datetime import datetime, timezone, timedelta
import copy
import pytest
from src.cloud_queue import (
    new_task, add_tasks, empty_queue, approve_preflight,
    confirm_preflight_review, retry_failed_preflight_stages, start_reviewed_processing,
)
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


def test_confirm_review_does_not_start_processing_until_explicit_action():
    task = ready_task()
    queue = add_tasks(empty_queue(), [task])
    review = {"status": "approved", "remove_patterns": ["不是廣告也要排除"]}

    confirmed = confirm_preflight_review(queue, task["task_id"], 100, 200, review)
    waiting = confirmed["queue"][0]
    assert waiting["status"] == "waiting_review"
    assert waiting["workflow_phase"] == "preflight"
    assert waiting["ad_review"] == review

    started = start_reviewed_processing(confirmed, task["task_id"])
    assert started["queue"][0]["status"] == "queued"
    assert started["queue"][0]["workflow_phase"] == "processing"


@pytest.mark.parametrize("status", ["queued", "running", "interrupted", "processing", "waiting_review"])
def test_confirm_review_is_allowed_regardless_of_task_status(status):
    task = ready_task()
    task["status"] = status
    queue = add_tasks(empty_queue(), [task])
    review = {"status": "approved", "remove_patterns": ["更新後的廣告詞"]}

    confirmed = confirm_preflight_review(queue, task["task_id"], 100, 200, review)

    assert confirmed["queue"][0]["status"] == status
    assert confirmed["queue"][0]["ad_review"] == review


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


def test_preflight_retry_detaches_failed_runs_and_preserves_successful_stage():
    task = ready_task()
    task.update(status="needs_attention", run_id=100, reason="preflight_failed:TXT")
    task["stages"]["scrape"].update(status="failed", reason="failure")
    queue = retry_failed_preflight_stages(
        add_tasks(empty_queue(), [task]), task["task_id"],
    )

    retried = queue["queue"][0]
    assert retried["status"] == "queued"
    assert retried["run_id"] is None
    assert retried["scrape_run_id"] is None
    assert retried["stages"]["scrape"]["status"] == "pending"
    assert retried["stages"]["scrape"]["run_id"] is None
    assert retried["cover_run_id"] == 200
    assert retried["stages"]["cover"]["status"] == "completed"

    dispatcher = fake_dispatcher(queue)
    dispatcher.runs = Mock(return_value=[])
    dispatcher.dispatch_next(queue)
    posts = [c.args[1] for c in dispatcher.request.call_args_list if c.args[0] == "POST"]
    assert posts == ["/actions/workflows/audiobook.yml/dispatches"]
    payload = dispatcher.request.call_args_list[0].kwargs["json"]["inputs"]
    assert payload["execution_phase"] == "scrape_only"
    assert payload["scrape_source_run_id"] == ""
    assert payload["cover_source_run_id"] == "200"


def test_preflight_scrape_retry_never_reuses_stale_top_level_run_id():
    task = ready_task()
    task.update(status="queued", scrape_run_id=100)
    task["stages"]["scrape"].update(status="pending", run_id=None)
    queue = add_tasks(empty_queue(), [task])
    dispatcher = fake_dispatcher(queue)
    dispatcher.runs = Mock(return_value=[])

    dispatcher.dispatch_next(queue)

    payload = dispatcher.request.call_args_list[0].kwargs["json"]["inputs"]
    assert payload["execution_phase"] == "scrape_only"
    assert payload["scrape_source_run_id"] == ""


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


def test_completed_book_event_releases_only_the_next_books_preflight_workflows():
    first = new_task("https://example.com/first", "First", 1, 2)
    first.update(workflow_phase="processing", status="running", run_id=300,
                 processing_run_id=300)
    second = new_task("https://example.com/second", "Second", 1, 2)
    queue = add_tasks(empty_queue(), [first, second])
    dispatcher = Dispatcher(
        "owner/repo", "token", trigger_run_id=300,
        trigger_workflow_name="Audiobook Automation Pipeline (Parallel)",
        trigger_conclusion="success",
    )
    dispatcher.dispatch_discovery_delays = (0,)
    dispatcher.store = Mock()
    dispatcher.store.load.return_value = (queue, "sha")
    dispatcher.store.save.return_value = "next-sha"
    dispatcher.profile_store.load = Mock(return_value=({"books": {}}, None))
    stale = {"id": 300, "status": "in_progress", "conclusion": None,
             "display_title": f"First｜{first['task_id']}"}
    dispatcher.runs = Mock(return_value=[stale])
    dispatcher.cover_runs = Mock(return_value=[])
    dispatcher.request = Mock()

    reconciled, _ = dispatcher.reconcile(queue)
    dispatcher.store.load.return_value = (reconciled, "next-sha")
    dispatcher.dispatch_next(reconciled)

    posts = [call.args[1] for call in dispatcher.request.call_args_list
             if call.args[0] == "POST"]
    assert posts == [
        "/actions/workflows/audiobook.yml/dispatches",
        "/actions/workflows/cover-preflight.yml/dispatches",
    ]
