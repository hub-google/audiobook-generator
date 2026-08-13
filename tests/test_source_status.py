import os
import tempfile
import unittest

from src.source_status import SourceStatusStore, looks_like_anti_bot_page


class SourceStatusTests(unittest.TestCase):
    def test_three_matching_http_200_empty_pages_confirm_source_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SourceStatusStore(directory)
            for _ in range(3):
                result = store.record_empty_page(
                    721,
                    "https://example.test/read/721",
                    200,
                    "https://example.test/read/721",
                    "Unknown_Chapter_721",
                    b"<html><h1>missing</h1></html>",
                )
        self.assertEqual(result["status"], "source_missing")
        self.assertEqual(result["confirmation_count"], 3)

    def test_different_empty_pages_remain_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SourceStatusStore(directory)
            for payload in (b"one", b"two", b"three"):
                result = store.record_empty_page(
                    721, "https://example.test/read/721", 200,
                    "https://example.test/read/721", "Unknown", payload,
                )
        self.assertEqual(result["status"], "source_missing_candidate")

    def test_antibot_page_is_detected(self):
        self.assertTrue(looks_like_anti_bot_page("Cloudflare Just a moment"))
        self.assertFalse(looks_like_anti_bot_page("ordinary empty chapter page"))
