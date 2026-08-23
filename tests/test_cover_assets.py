import tempfile
import unittest
from pathlib import Path

from PIL import Image

from src.cover_assets import normalize_manual_cover, validate_cached_cover


class ManualCoverTests(unittest.TestCase):
    def test_normalizes_crop_format_size_and_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "portrait.png"
            output = Path(directory) / "master_cover.jpg"
            Image.new("RGBA", (1000, 1400), (30, 80, 160, 180)).save(source)
            result = normalize_manual_cover(source, output)
            self.assertEqual((result["width"], result["height"]), (1280, 720))
            self.assertLess(result["bytes"], 2 * 1024 * 1024)
            self.assertEqual(validate_cached_cover(output, result["sha256"]), result["sha256"])

    def test_rejects_wrong_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.jpg"
            output = Path(directory) / "master_cover.jpg"
            Image.new("RGB", (1280, 720), "navy").save(source)
            normalize_manual_cover(source, output)
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                validate_cached_cover(output, "0" * 64)


if __name__ == "__main__":
    unittest.main()
