import os
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from src.cover_renderer import TITLE_BOTTOM_SAFE_MARGIN, TITLE_FONT_PATH, render_viral_cover


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

    def test_only_draws_book_title_and_completed_status(self):
        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, "cover.jpg")
            drawn_text = []
            drawn_positions = []

            from PIL import ImageDraw
            original_text = ImageDraw.ImageDraw.text

            def record_text(draw, xy, text, *args, **kwargs):
                drawn_text.append(text)
                drawn_positions.append(xy)
                return original_text(draw, xy, text, *args, **kwargs)

            with patch.object(ImageDraw.ImageDraw, "text", record_text):
                render_viral_cover(
                    Image.new("RGB", (1280, 720), (70, 42, 18)),
                    "大主宰", 1, 100, True, output, 1,
                )

            self.assertTrue(drawn_text)
            self.assertEqual(set(drawn_text), {"大主宰", "已完結"})
            title_y_positions = [xy[1] for xy, text in zip(drawn_positions, drawn_text) if text == "大主宰"]
            self.assertTrue(title_y_positions)
            self.assertLessEqual(max(title_y_positions), 720 - TITLE_BOTTOM_SAFE_MARGIN)

    def test_ongoing_cover_does_not_draw_status(self):
        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, "cover.jpg")
            drawn_text = []
            from PIL import ImageDraw
            original_text = ImageDraw.ImageDraw.text

            def record_text(draw, xy, text, *args, **kwargs):
                drawn_text.append(text)
                return original_text(draw, xy, text, *args, **kwargs)

            with patch.object(ImageDraw.ImageDraw, "text", record_text):
                render_viral_cover(
                    Image.new("RGB", (1280, 720), (70, 42, 18)),
                    "大主宰", 1, 100, False, output, 1,
                )
            self.assertEqual(set(drawn_text), {"大主宰"})

    def test_simplified_title_converts_to_traditional_and_renders_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, "cover_shengxu.jpg")
            drawn_text = []
            from PIL import ImageDraw
            original_text = ImageDraw.ImageDraw.text

            def record_text(draw, xy, text, *args, **kwargs):
                drawn_text.append(text)
                return original_text(draw, xy, text, *args, **kwargs)

            with patch.object(ImageDraw.ImageDraw, "text", record_text):
                render_viral_cover(
                    Image.new("RGB", (1280, 720), (70, 42, 18)),
                    "圣墟", 1, 100, True, output, 1,
                )
            self.assertEqual(set(drawn_text), {"聖墟", "已完結"})
            self.assertTrue(os.path.isfile(output))
            self.assertGreater(os.path.getsize(output), 10_000)

    def test_missing_glyph_falls_back_entire_cover_to_mashanzheng(self):
        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, "cover_fallback.jpg")
            drawn_fonts = []
            from PIL import ImageDraw
            original_text = ImageDraw.ImageDraw.text

            def record_text(draw, xy, text, font=None, *args, **kwargs):
                if font:
                    drawn_fonts.append((text, getattr(font, "path", str(font))))
                return original_text(draw, xy, text, font=font, *args, **kwargs)

            with patch.object(ImageDraw.ImageDraw, "text", record_text):
                render_viral_cover(
                    Image.new("RGB", (1280, 720), (70, 42, 18)),
                    "甭鬧了", 1, 100, True, output, 1,
                )
            self.assertTrue(drawn_fonts)
            for text, font_path in drawn_fonts:
                self.assertTrue(str(font_path).endswith("MaShanZheng.ttf"))


if __name__ == "__main__":
    unittest.main()
