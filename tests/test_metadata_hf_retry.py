import sys
import types
import unittest
from unittest.mock import Mock, patch

import requests

from src.metadata_gen import download_ai_image


class MetadataHfRetryTests(unittest.TestCase):
    @patch.dict("os.environ", {"HF_TOKEN": "hf_test-token"})
    @patch("src.metadata_gen.time.sleep")
    @patch("src.metadata_gen.requests.post")
    def test_dns_failure_retries_then_succeeds(self, mock_post, mock_sleep):
        fake_hf = types.ModuleType("huggingface_hub")
        client = Mock()
        client.text_to_image.side_effect = RuntimeError("SDK unavailable")
        fake_hf.InferenceClient = Mock(return_value=client)

        image_response = Mock(status_code=200, content=self._jpeg_bytes(), text="")
        mock_post.side_effect = [
            requests.ConnectionError("Failed to resolve api-inference.huggingface.co"),
            image_response,
        ]
        with patch.dict(sys.modules, {"huggingface_hub": fake_hf}):
            image = download_ai_image("cover", max_attempts=3, retry_base_seconds=1)

        self.assertEqual(image.size, (1280, 720))
        self.assertEqual(mock_post.call_count, 2)
        mock_sleep.assert_called_once_with(1)

    @patch.dict("os.environ", {"HF_TOKEN": "hf_test-token"})
    @patch("src.metadata_gen.time.sleep")
    @patch("src.metadata_gen.requests.post")
    def test_auth_failure_does_not_retry(self, mock_post, mock_sleep):
        fake_hf = types.ModuleType("huggingface_hub")
        client = Mock()
        client.text_to_image.side_effect = RuntimeError("SDK unavailable")
        fake_hf.InferenceClient = Mock(return_value=client)
        mock_post.return_value = Mock(status_code=401, text="unauthorized")

        with patch.dict(sys.modules, {"huggingface_hub": fake_hf}):
            with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
                download_ai_image("cover", max_attempts=5, retry_base_seconds=1)

        self.assertEqual(mock_post.call_count, 1)
        mock_sleep.assert_not_called()

    @staticmethod
    def _jpeg_bytes():
        import io
        from PIL import Image

        output = io.BytesIO()
        Image.new("RGB", (1280, 720)).save(output, format="JPEG")
        return output.getvalue()


if __name__ == "__main__":
    unittest.main()
