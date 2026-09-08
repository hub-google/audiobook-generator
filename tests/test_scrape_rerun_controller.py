import unittest
from unittest.mock import Mock

from src.scrape_rerun_controller import (
    MAX_NATIVE_SCRAPE_RERUNS,
    decide_rerun,
    failed_scrape_job_ids,
    run_controller,
)


class ScrapeRerunControllerTests(unittest.TestCase):
    def test_completed_failed_scrape_reruns_only_failed_scrape_jobs(self):
        run = {
            "status": "completed",
            "conclusion": "failure",
            "name": "【TXT抓取】有聲小說製作｜book",
            "run_attempt": 1,
        }
        jobs = [
            {"id": 101, "name": "🕷️ Scrape — Worker 0", "conclusion": "failure"},
            {"id": 102, "name": "🕷️ Scrape — Worker 1", "conclusion": "success"},
            {"id": 103, "name": "Strict final success gate", "conclusion": "failure"},
        ]

        decision = decide_rerun(run, jobs)

        self.assertTrue(decision.should_rerun)
        self.assertEqual(decision.failed_job_ids, (101,))

    def test_controller_reruns_immediately_after_each_completed_attempt(self):
        client = Mock()
        client.get_run.side_effect = [
            {"status": "completed", "conclusion": "failure", "name": "【TXT抓取】book", "run_attempt": 1},
            {"status": "in_progress", "conclusion": None, "name": "【TXT抓取】book", "run_attempt": 2},
            {"status": "completed", "conclusion": "success", "name": "【TXT抓取】book", "run_attempt": 2},
        ]
        client.get_jobs.side_effect = [
            [{"id": 101, "name": "🕷️ Scrape — Worker 0", "conclusion": "failure"}],
            [],
        ]

        with unittest.mock.patch("src.scrape_rerun_controller.time.sleep"):
            outcome = run_controller(client, "123")

        self.assertEqual(outcome, "source_run_not_failed")
        client.rerun_failed_jobs.assert_called_once_with("123")

    def test_attempt_21_stops_without_another_native_rerun(self):
        run = {
            "status": "completed",
            "conclusion": "failure",
            "name": "【TXT抓取】book",
            "run_attempt": MAX_NATIVE_SCRAPE_RERUNS + 1,
        }
        jobs = [{"id": 101, "name": "🕷️ Scrape — Worker 0", "conclusion": "failure"}]

        decision = decide_rerun(run, jobs)

        self.assertFalse(decision.should_rerun)
        self.assertTrue(decision.exhausted)
        self.assertEqual(decision.reason, "scrape_rerun_limit_reached")

    def test_non_scrape_failure_is_not_rerun(self):
        run = {
            "status": "completed",
            "conclusion": "failure",
            "name": "【後製上傳】book",
            "run_attempt": 1,
        }
        jobs = [{"id": 101, "name": "📤 Ordered publication", "conclusion": "failure"}]

        decision = decide_rerun(run, jobs)

        self.assertFalse(decision.should_rerun)
        self.assertEqual(decision.reason, "not_a_txt_scrape_run")


if __name__ == "__main__":
    unittest.main()
