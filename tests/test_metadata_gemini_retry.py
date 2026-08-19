import unittest
from unittest.mock import Mock, patch

from src.metadata_gen import generate_gemini_art_prompt


def _response(status, text="", prompt=None):
    response = Mock(status_code=status, text=text)
    response.json.return_value = {
        "candidates": [{"content": {"parts": [{"text": prompt}]}}]
    } if prompt is not None else {}
    return response


class MetadataGeminiRetryTests(unittest.TestCase):
    @patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"})
    @patch("src.metadata_gen.time.sleep")
    @patch("src.metadata_gen.requests.post")
    def test_gemini_503_retries_without_skipping_cover(self, mock_post, mock_sleep):
        mock_post.side_effect = [
            _response(503, "high demand"),
            _response(503, "high demand"),
            _response(200, prompt="cinematic cover"),
        ]

        result = generate_gemini_art_prompt("測試小說", "劇情", max_attempts=4, retry_base_seconds=1)

        self.assertEqual(result, "cinematic cover")
        self.assertEqual(mock_post.call_count, 3)
        self.assertEqual([call.args[0] for call in mock_sleep.call_args_list], [1, 2])

    @patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"})
    @patch("src.metadata_gen.time.sleep")
    @patch("src.metadata_gen.requests.post")
    def test_gemini_503_exhaustion_fails_the_cover_prerequisite(self, mock_post, mock_sleep):
        mock_post.return_value = _response(503, "high demand")

        with self.assertRaisesRegex(RuntimeError, "封面前置未完成，流程中止"):
            generate_gemini_art_prompt("測試小說", "劇情", max_attempts=3, retry_base_seconds=1)

        self.assertEqual(mock_post.call_count, 3)
        self.assertEqual([call.args[0] for call in mock_sleep.call_args_list], [1, 2])


if __name__ == "__main__":
    unittest.main()
