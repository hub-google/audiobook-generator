import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from src.cover_assets import normalize_manual_cover, upload_cover, validate_cached_cover


class ManualCoverTests(unittest.TestCase):
    def test_normalizes_crop_format_size_and_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "portrait.png"
            output = Path(directory) / "master_cover.jpg"
            Image.new("RGBA", (1000, 1400), (30, 80, 160, 180)).save(source)
            result = normalize_manual_cover(source, output)
            self.assertEqual((result["width"], result["height"]), (1280, 720))
            self.assertGreater(result["bytes"], 10_000)
            self.assertEqual(validate_cached_cover(output, result["sha256"]), result["sha256"])
            self.assertTrue(output.with_suffix(".manual.json").is_file())

    def test_rejects_wrong_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.jpg"
            output = Path(directory) / "master_cover.jpg"
            Image.new("RGB", (1280, 720), "navy").save(source)
            normalize_manual_cover(source, output)
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                validate_cached_cover(output, "0" * 64)

    def test_upload_explains_missing_write_permission(self):
        from huggingface_hub.errors import HfHubHTTPError

        response = Mock(status_code=403)
        error = HfHubHTTPError("Forbidden", response=response)
        api = Mock()
        api.upload_file.side_effect = error
        with patch("huggingface_hub.HfApi", return_value=api):
            with self.assertRaisesRegex(RuntimeError, "沒有寫入權限"):
                upload_cover("cover.jpg", "profile", "hf_read_only", "owner/archive")


if __name__ == "__main__":
    unittest.main()
