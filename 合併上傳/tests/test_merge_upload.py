import importlib.util
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).parents[1] / "merge_upload.py"
SPEC = importlib.util.spec_from_file_location("merge_upload", MODULE_PATH)
merge_upload = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(merge_upload)

class MergeUploadOrderingTests(unittest.TestCase):
    def test_orders_by_chapter_number_not_worker_or_lexical_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ["mp4-worker-9/x_chapter_100.mp4", "mp4-worker-0/x_chapter_2.mp4", "mp4-worker-0/x_chapter_11.mp4"]:
                path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b"mp4")
            ordered = merge_upload.ordered_chapter_videos(root)
            self.assertEqual([merge_upload.chapter_number(p) for p in ordered], [2, 11, 100])

    def test_ignores_non_chapter_mp4(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "preview.mp4").write_bytes(b"preview")
            chapter = root / "book_chapter_7.mp4"; chapter.write_bytes(b"chapter")
            self.assertEqual(merge_upload.ordered_chapter_videos(root), [chapter])

if __name__ == "__main__": unittest.main()
