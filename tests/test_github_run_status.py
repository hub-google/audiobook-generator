import unittest
from datetime import datetime, timedelta, timezone

from src.github_run_status import (
    RUN_CONCLUSION_LABELS,
    RUN_STATUS_LABELS,
    error_observation,
    missing_observation,
    observation_text,
    successful_observation,
)


NOW = datetime(2026, 8, 19, 4, 0, 0, tzinfo=timezone.utc)
CHECKED = NOW.isoformat()


class GitHubRunStatusTests(unittest.TestCase):
    def test_every_official_incomplete_status_has_an_explicit_label(self):
        expected = {"requested", "queued", "pending", "waiting", "in_progress"}
        self.assertEqual(set(RUN_STATUS_LABELS), expected)
        for status in expected:
            observation = successful_observation({"status": status, "conclusion": None}, CHECKED)
            self.assertEqual(observation_text(observation, now=NOW), RUN_STATUS_LABELS[status])

    def test_every_official_conclusion_has_an_explicit_label(self):
        expected = {
            "success", "cancelled", "failure", "timed_out", "action_required",
            "stale", "neutral", "skipped", "startup_failure",
        }
        self.assertEqual(set(RUN_CONCLUSION_LABELS), expected)
        for conclusion in expected:
            observation = successful_observation(
                {"status": "completed", "conclusion": conclusion}, CHECKED,
            )
            self.assertEqual(observation_text(observation, now=NOW), RUN_CONCLUSION_LABELS[conclusion])

    def test_unknown_values_are_never_guessed(self):
        status = successful_observation({"status": "future_status", "conclusion": None}, CHECKED)
        conclusion = successful_observation(
            {"status": "completed", "conclusion": "future_conclusion"}, CHECKED,
        )
        self.assertEqual(observation_text(status, now=NOW), "未知狀態｜future_status")
        self.assertEqual(observation_text(conclusion, now=NOW), "未知結果｜future_conclusion")

    def test_missing_run_requires_two_confirmed_observations(self):
        first = missing_observation(None, CHECKED, confirmed=True)
        second = missing_observation(first, CHECKED, confirmed=True)
        self.assertEqual(observation_text(first, now=NOW), "API 查無 Run｜複查中")
        self.assertEqual(observation_text(second, now=NOW), "Run 已不存在")

    def test_api_errors_replace_running_with_unconfirmed(self):
        for code in (
            "unauthorized", "forbidden", "rate_limited", "github_error",
            "network_error", "invalid_response",
        ):
            text = observation_text(error_observation(code, checked_at=CHECKED), now=NOW)
            self.assertTrue(text.startswith("無法確認｜"), text)

    def test_observation_becomes_unconfirmed_after_ttl(self):
        observation = successful_observation({"status": "in_progress", "conclusion": None}, CHECKED)
        self.assertEqual(observation_text(observation, now=NOW), "執行中")
        self.assertEqual(
            observation_text(observation, now=NOW + timedelta(seconds=31)),
            "無法確認｜查證資料已過期",
        )


if __name__ == "__main__":
    unittest.main()
