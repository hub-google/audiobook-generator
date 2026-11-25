import sys
import types
import unittest
from unittest.mock import Mock, patch

import requests
from PIL import Image

from src.metadata_gen import download_ai_image


class MetadataHfRetryTests(unittest.TestCase):
    @patch.dict("os.environ", {"HF_TOKEN": "hf_test-token"})
    @patch("src.metadata_gen.time.sleep")
    @patch("src.metadata_gen.requests.post")
    def test_sdk_connection_failure_retries_then_succeeds(self, mock_post, mock_sleep):
        fake_hf = types.ModuleType("huggingface_hub")
        client = Mock()
        fake_hf.InferenceClient = Mock(return_value=client)

        client.text_to_image.side_effect = [
            requests.ConnectionError("connection reset by peer"),
            Image.new("RGB", (1280, 720)),
        ]
        with patch.dict(sys.modules, {"huggingface_hub": fake_hf}):
            image = download_ai_image("cover", max_attempts=3, retry_base_seconds=1)

        self.assertEqual(image.size, (1280, 720))
        self.assertEqual(client.text_to_image.call_count, 2)
        mock_post.assert_not_called()
        mock_sleep.assert_called_once_with(1)

    @patch.dict("os.environ", {"HF_TOKEN": "hf_test-token"})
    @patch("src.metadata_gen.time.sleep")
    @patch("src.metadata_gen.requests.post")
    def test_auth_failure_does_not_retry(self, mock_post, mock_sleep):
        fake_hf = types.ModuleType("huggingface_hub")
        client = Mock()
        fake_hf.InferenceClient = Mock(return_value=client)
        error = RuntimeError("unauthorized")
        error.response = Mock(status_code=401)
        client.text_to_image.side_effect = error

        with patch.dict(sys.modules, {"huggingface_hub": fake_hf}):
            with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
                download_ai_image("cover", max_attempts=5, retry_base_seconds=1)

        client.text_to_image.assert_called_once()
        mock_post.assert_not_called()
        mock_sleep.assert_not_called()

if __name__ == "__main__":
    unittest.main()
