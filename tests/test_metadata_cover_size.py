import os
import tempfile
import unittest

from PIL import Image

from src.metadata_gen import (
    YOUTUBE_COVER_MAX_BYTES,
    YOUTUBE_COVER_SIZE,
    _valid_youtube_cover,
    create_youtube_cover,
)


class MetadataCoverSizeTests(unittest.TestCase):
    def test_generated_youtube_cover_uses_single_hd_spec(self):
        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, "youtube_cover.jpg")
            master = Image.new("RGB", YOUTUBE_COVER_SIZE, "navy")

            create_youtube_cover(master, "測試小說", 1, 10, output_filename=output, part_num=1)

            with Image.open(output) as image:
                self.assertEqual(image.size, YOUTUBE_COVER_SIZE)
            self.assertLess(os.path.getsize(output), YOUTUBE_COVER_MAX_BYTES)
            self.assertTrue(_valid_youtube_cover(output))

    def test_rejects_old_2k_part_cover(self):
        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, "youtube_cover.jpg")
            Image.new("RGB", (2560, 1440), "navy").save(output, "JPEG")

            self.assertFalse(_valid_youtube_cover(output))


if __name__ == "__main__":
    unittest.main()
