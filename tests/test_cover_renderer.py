import os
import tempfile
import unittest

from PIL import Image

from src.cover_renderer import TITLE_FONT_PATH, render_viral_cover


class CoverRendererTests(unittest.TestCase):
    def test_uses_bundled_title_font_and_renders_long_title(self):
        self.assertTrue(TITLE_FONT_PATH.is_file())
        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, "cover.jpg")
            render_viral_cover(
                Image.new("RGB", (1280, 720), (70, 42, 18)),
                "十萬分身替我同時修煉", 1, 100, True, output, 1,
            )
            with Image.open(output) as rendered:
                self.assertEqual(rendered.size, (1280, 720))
                self.assertEqual(rendered.format, "JPEG")
            self.assertGreater(os.path.getsize(output), 10_000)


if __name__ == "__main__":
    unittest.main()
