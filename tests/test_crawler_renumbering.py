import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.crawler import run_crawler_worker


class CrawlerRenumberingTests(unittest.TestCase):
    @patch("src.crawler.time.sleep", return_value=None)
    @patch("src.crawler.requests.get")
    def test_source_urls_are_saved_with_consecutive_output_numbers(self, get, _sleep):
        response = Mock()
        response.content = (
            b"<html><h1>Origin chapter</h1>"
            b"<div style='word-wrap: break-word; text-indent: 2em'>chapter body with enough meaningful novel content for strict validation</div></html>"
        )
        response.status_code = 200
        response.url = "https://tw.hjwzw.com/read/1"
        response.raise_for_status.return_value = None
        get.return_value = response

        with tempfile.TemporaryDirectory() as directory:
            config = {
                "book_title": "book", "base_url": "https://tw.hjwzw.com",
                "paths": {"workspace_base": directory},
            }
            run_crawler_worker(
                config, ["/read/1", "/read/3", "/read/5"],
                exact_indices=[1, 2, 3],
            )

            raw_dir = os.path.join(directory, "book", "RawText")
            self.assertEqual(sorted(os.listdir(raw_dir)), [
                "book_chapter_1_raw.txt",
                "book_chapter_2_raw.txt",
                "book_chapter_3_raw.txt",
            ])
            self.assertEqual(
                [call.args[0] for call in get.call_args_list],
                [
                    "https://tw.hjwzw.com/read/1",
                    "https://tw.hjwzw.com/read/3",
                    "https://tw.hjwzw.com/read/5",
                ],
            )


if __name__ == "__main__":
    unittest.main()
