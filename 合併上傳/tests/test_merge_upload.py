import ast
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

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

class RunIdParsingTests(unittest.TestCase):
    def test_accepts_numeric_run_id(self):
        self.assertEqual(merge_upload.normalize_run_id("31962500241"), "31962500241")

    def test_extracts_id_from_copied_actions_url(self):
        self.assertEqual(
            merge_upload.normalize_run_id(
                "https://github.com/hub-google/audiobook-generator/actions/runs/31962500241"
            ),
            "31962500241",
        )

    def test_accepts_trailing_slash_and_query_string(self):
        self.assertEqual(
            merge_upload.normalize_run_id(
                "https://github.com/hub-google/audiobook-generator/actions/runs/31962500241/?check_suite_focus=true"
            ),
            "31962500241",
        )

    def test_rejects_unrecognized_value(self):
        with self.assertRaises(merge_upload.argparse.ArgumentTypeError):
            merge_upload.normalize_run_id("not-a-run")

    def test_normalizes_book_title_from_gui_style(self):
        self.assertEqual(merge_upload.normalize_book_title("仙逆(已完結)"), "仙逆")

    def test_preserves_plain_book_title(self):
        self.assertEqual(merge_upload.normalize_book_title("仙逆"), "仙逆")

class MetadataAndCoverTests(unittest.TestCase):
    def pipeline(self):
        pipeline = merge_upload.Pipeline.__new__(merge_upload.Pipeline)
        pipeline.args = types.SimpleNamespace(
            privacy="public", run_id="31962500241"
        )
        pipeline.work = Path("merge-upload-state")
        pipeline.state = {}
        pipeline.save = Mock()
        return pipeline

    def test_metadata_uses_run_title_and_run_cover_without_generating_a_cover(self):
        pipeline = self.pipeline()
        source_cover = pipeline.work / "metadata" / "youtube_cover.jpg"
        pipeline.source_metadata = Mock(return_value=("仙逆", source_cover))
        module = types.ModuleType("src.metadata_gen")
        module.generate_video_title = Mock(return_value="《仙逆》全集")
        module.generate_video_description = Mock(return_value="原本介紹格式")
        with patch.dict(sys.modules, {"src.metadata_gen": module}):
            result = pipeline.metadata([{"chapters": [1, 2]}, {"chapters": [2065, 2066]}])
        self.assertEqual(result, {
            "title": "《仙逆》全集", "description": "原本介紹格式",
            "cover_file": str(source_cover),
        })
        pipeline.source_metadata.assert_called_once_with()
        module.generate_video_title.assert_called_once_with("仙逆", 1, 2066)
        module.generate_video_description.assert_called_once_with("仙逆", 1, 2066)

    def test_upload_passes_generated_description_and_cover(self):
        pipeline = self.pipeline()
        metadata = {
            "title": "generated title", "description": "generated description",
            "cover_file": "youtube_cover.jpg",
        }
        pipeline.metadata = Mock(return_value=metadata)
        uploader = types.ModuleType("src.youtube_api_uploader")
        uploader.get_authenticated_service = Mock(return_value="youtube")
        uploader.upload_video_file = Mock(return_value="video-id")
        with patch.dict(sys.modules, {"src.youtube_api_uploader": uploader}):
            pipeline.upload(Path("merged.mp4"), [{"chapters": [1]}])
        uploader.upload_video_file.assert_called_once_with(
            "youtube", "merged.mp4", "generated title", "generated description",
            privacy_status="public", cover_path="youtube_cover.jpg",
        )

class HuggingFaceCompatibilityTests(unittest.TestCase):
    def test_all_upload_file_calls_use_keyword_arguments(self):
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "upload_file"
        ]
        self.assertGreater(len(calls), 0)
        for call in calls:
            self.assertEqual(call.args, [], f"upload_file call on line {call.lineno} has positional arguments")
            keyword_names = {keyword.arg for keyword in call.keywords}
            self.assertTrue(
                {"path_or_fileobj", "path_in_repo", "repo_id"}.issubset(keyword_names),
                f"upload_file call on line {call.lineno} is missing required keyword arguments",
            )

if __name__ == "__main__": unittest.main()
