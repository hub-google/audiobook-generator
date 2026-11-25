from datetime import datetime, timedelta, timezone
from src.dispatch_retry_policy import retry_state


def test_rate_limit_produces_bounded_retry_without_queue_mutation():
    now = datetime(2026, 11, 25, 6, 0, tzinfo=timezone.utc)
    expected = now + timedelta(hours=2)
    assert retry_state("rateLimitExceeded", now=now) == {
        "status": "waiting_retry",
        "reason": "rateLimitExceeded",
        "retry_at": expected.isoformat(),
    }


def test_existing_retry_deadline_wins_over_new_log_marker():
    now = datetime(2026, 11, 25, 6, 0, tzinfo=timezone.utc)
    existing = now + timedelta(minutes=42)
    assert retry_state("backendError", now+timedelta(hours=5), existing_at=existing)["retry_at"] == existing.isoformat()


def test_permanent_failure_is_not_scheduled_indefinitely():
    assert retry_state("invalidCredentials") == {
        "status": "needs_attention", "reason": "invalidCredentials", "retry_at": None,
    }
    assert retry_state("")["reason"] == "permanentFailure"
