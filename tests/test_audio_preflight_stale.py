"""AUDIO cannot claim TXT/cover is running when GitHub ended a day ago."""
from datetime import datetime, timedelta, timezone

from src.queue_dispatcher import finished_preflight_is_stale

NOW = datetime(2026, 11, 27, 8, 0, tzinfo=timezone.utc)


def run(status, result, age_hours):
    return {
        "status": status,
        "conclusion": result,
        "updated_at": (NOW - timedelta(hours=age_hours)).isoformat(),
    }


def test_two_terminal_failures_after_one_day_need_attention():
    assert finished_preflight_is_stale(run("completed", "failure", 41),
                                      run("completed", "failure", 44), now=NOW)


def test_terminal_failure_during_retry_window_is_not_stale():
    assert not finished_preflight_is_stale(run("completed", "failure", 1),
                                          run("completed", "failure", 1), now=NOW)


def test_never_interrupt_active_retry():
    assert not finished_preflight_is_stale(run("completed", "failure", 40),
                                          run("in_progress", None, 0), now=NOW)


def test_successful_preflight_remains_successful():
    assert not finished_preflight_is_stale(run("completed", "success", 50),
                                          run("completed", "success", 50), now=NOW)


def test_refuse_staleness_without_known_run():
    assert not finished_preflight_is_stale(run("completed", "failure", 50), None, now=NOW)
