import unittest
from unittest.mock import Mock, patch

from gui_app import AudiobookGUIApp


class GuiGitHubMonitorTests(unittest.TestCase):
    def setUp(self):
        self.app = object.__new__(AudiobookGUIApp)

    @staticmethod
    def response(status, payload=None, headers=None, text=""):
        value = Mock()
        value.status_code = status
        value.headers = headers or {}
        value.text = text
        value.json.return_value = payload
        return value

    @patch("gui_app.requests.get")
    def test_success_preserves_raw_github_values(self, get):
        get.return_value = self.response(200, {
            "status": "waiting", "conclusion": None,
            "updated_at": "2026-08-19T04:00:00Z", "run_attempt": 2,
        })

        observation = self.app._observe_github_run("owner/repo", "token", 123, None)

        self.assertEqual(observation["kind"], "ok")
        self.assertEqual(observation["raw_status"], "waiting")
        self.assertIsNone(observation["raw_conclusion"])
        self.assertEqual(observation["run_attempt"], 2)

    @patch("gui_app.requests.get")
    def test_404_requires_two_checks_and_actions_access(self, get):
        get.side_effect = [
            self.response(404), self.response(200, {"workflow_runs": []}),
            self.response(404), self.response(200, {"workflow_runs": []}),
        ]

        first = self.app._observe_github_run("owner/repo", "token", 123, None)
        second = self.app._observe_github_run("owner/repo", "token", 123, first)

        self.assertFalse(first["confirmed_missing"])
        self.assertTrue(second["confirmed_missing"])
        self.assertEqual(get.call_count, 4)

    @patch("gui_app.requests.get")
    def test_404_with_forbidden_actions_access_is_not_called_missing(self, get):
        get.side_effect = [self.response(404), self.response(403, text="forbidden")]

        observation = self.app._observe_github_run("owner/repo", "token", 123, None)

        self.assertEqual(observation["kind"], "error")
        self.assertEqual(observation["error_code"], "forbidden")

    @patch("gui_app.requests.get")
    def test_rate_limit_is_not_reported_as_run_state(self, get):
        get.return_value = self.response(403, headers={"X-RateLimit-Remaining": "0"})

        observation = self.app._observe_github_run("owner/repo", "token", 123, None)

        self.assertEqual(observation["kind"], "error")
        self.assertEqual(observation["error_code"], "rate_limited")


if __name__ == "__main__":
    unittest.main()
