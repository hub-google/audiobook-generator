import ast
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

MODULE_PATH = Path(__file__).parents[1] / "merge_upload.py"
BUCKET_MODULE_PATH = Path(__file__).parents[1] / "bucket_pipeline.py"
GUI_MODULE_PATH = Path(__file__).parents[1] / "gui.py"
WORKFLOW_PATH = Path(__file__).parents[2] / ".github" / "workflows" / "merge-run-upload.yml"
SPEC = importlib.util.spec_from_file_location("merge_upload", MODULE_PATH)
merge_upload = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(merge_upload)
sys.modules["merge_upload"] = merge_upload

BUCKET_SPEC = importlib.util.spec_from_file_location("bucket_pipeline", BUCKET_MODULE_PATH)
bucket_pipeline = importlib.util.module_from_spec(BUCKET_SPEC)
BUCKET_SPEC.loader.exec_module(bucket_pipeline)

GUI_SPEC = importlib.util.spec_from_file_location("merge_upload_gui", GUI_MODULE_PATH)
merge_upload_gui = importlib.util.module_from_spec(GUI_SPEC)
GUI_SPEC.loader.exec_module(merge_upload_gui)

class GuiGitHubJsonTests(unittest.TestCase):
    def test_retries_empty_json_response(self):
        with patch.object(merge_upload_gui, "run_gh", side_effect=["", '[{"databaseId": 123}]']), \
             patch.object(merge_upload_gui.time, "sleep") as sleep:
            result = merge_upload_gui.run_gh_json("run", "list", attempts=2)
        self.assertEqual(result, [{"databaseId": 123}])
        sleep.assert_called_once_with(1.0)

    def test_reports_repeated_invalid_json_as_runtime_error(self):
        with patch.object(merge_upload_gui, "run_gh", return_value="not json"), \
             patch.object(merge_upload_gui.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "連續 2 次未回傳有效 JSON"):
                merge_upload_gui.run_gh_json("run", "view", attempts=2)

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

    def test_legacy_cover_reuses_exact_title_youtube_thumbnail(self):
        destination = Path("youtube_cover.jpg")
        search_call = Mock()
        search_call.execute.return_value = {"items": [{"snippet": {
            "title": "《仙逆》第 0001~0100 章【第 1 部】",
            "thumbnails": {"high": {"url": "https://img.example/original.jpg"}},
        }}]}
        youtube = Mock()
        youtube.search.return_value.list.return_value = search_call
        uploader = types.ModuleType("src.youtube_api_uploader")
        uploader.get_authenticated_service = Mock(return_value=youtube)
        image_response = Mock(content=b"original-cover")
        image_response.raise_for_status = Mock()
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, {"src.youtube_api_uploader": uploader}), patch.object(merge_upload.requests, "get", return_value=image_response):
            destination = Path(directory) / "youtube_cover.jpg"
            merge_upload.Pipeline.download_existing_youtube_cover("仙逆", destination)
            self.assertEqual(destination.read_bytes(), b"original-cover")
        youtube.search.return_value.list.assert_called_once_with(
            part="snippet", forMine=True, type="video", q="《仙逆》", maxResults=50
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

class BucketPipelineTests(unittest.TestCase):
    def test_legacy_run_prefers_restored_master_cover_over_youtube(self):
        artifacts = [{"name": "shared-config"}]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master = root / "Workspace" / "仙逆" / "Cover" / "master_cover.jpg"
            master.parent.mkdir(parents=True)
            from PIL import Image
            Image.new("RGB", (1280, 720), "navy").save(master)

            def download_config(_artifact, destination):
                destination.mkdir(parents=True, exist_ok=True)
                (destination / "config.yaml").write_text(
                    "book_title: 仙逆\nstart_chapter: 1\nend_chapter: 2025\n", encoding="utf-8"
                )

            previous = Path.cwd()
            try:
                os.chdir(root)
                with patch.object(bucket_pipeline, "download_artifact", side_effect=download_config), \
                     patch.object(merge_upload.Pipeline, "download_existing_youtube_cover") as youtube_cover:
                    title, cover, end_chapter, _ = bucket_pipeline.source_metadata_from_github(artifacts, root / "temp")
            finally:
                os.chdir(previous)

            self.assertEqual((title, end_chapter), ("仙逆", 2025))
            self.assertTrue(cover.is_file())
            self.assertEqual(cover.read_bytes(), master.read_bytes())
            youtube_cover.assert_not_called()

    def test_legacy_run_stops_when_original_cover_is_missing(self):
        artifacts = [{"name": "shared-config"}]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def download_config(_artifact, destination):
                destination.mkdir(parents=True, exist_ok=True)
                (destination / "config.yaml").write_text(
                    "book_title: 仙逆\nstart_chapter: 1\nend_chapter: 2025\n", encoding="utf-8"
                )

            previous = Path.cwd()
            try:
                os.chdir(root)
                with patch.object(bucket_pipeline, "download_artifact", side_effect=download_config), \
                     self.assertRaisesRegex(RuntimeError, "no original cover"):
                    bucket_pipeline.source_metadata_from_github(artifacts, root / "temp")
            finally:
                os.chdir(previous)

    def test_expected_worker_chapters_uses_the_same_config_slice_as_worker_pipeline(self):
        config = {"selected_indices": [1, 2, 4, 5, 8], "chapters_per_worker": 2}
        self.assertEqual(bucket_pipeline.expected_worker_chapters(config, 0), [1, 2])
        self.assertEqual(bucket_pipeline.expected_worker_chapters(config, 1), [4, 5])
        self.assertEqual(bucket_pipeline.expected_worker_chapters(config, 2), [8])

    def test_worker_inventory_accepts_only_proven_source_missing_chapters(self):
        result = bucket_pipeline.validate_worker_inventory(
            "mp4-worker-0", [1, 2, 3], [1, 3], {2}
        )
        self.assertEqual(result["mp4_chapters"], [1, 3])
        self.assertEqual(result["source_missing_chapters"], [2])

    def test_worker_inventory_rejects_unexplained_missing_chapter(self):
        with self.assertRaisesRegex(RuntimeError, r"unresolved_missing=\[2\]"):
            bucket_pipeline.validate_worker_inventory(
                "mp4-worker-0", [1, 2, 3], [1, 3], set()
            )

    def test_worker_inventory_rejects_duplicate_chapter_mp4(self):
        with self.assertRaisesRegex(RuntimeError, r"duplicates=\[2\]"):
            bucket_pipeline.validate_worker_inventory(
                "mp4-worker-0", [1, 2], [1, 2, 2], set()
            )

    def test_final_manifest_validation_requires_exact_worker_and_chapter_coverage(self):
        manifests = [
            {
                "worker_ids": [0], "merged_chapters": [1, 3],
                "worker_inventory": [{
                    "worker_id": 0, "expected": [1, 2, 3],
                    "mp4_chapters": [1, 3], "source_missing_chapters": [2],
                }],
            },
            {
                "worker_ids": [1], "merged_chapters": [4],
                "worker_inventory": [{
                    "worker_id": 1, "expected": [4],
                    "mp4_chapters": [4], "source_missing_chapters": [],
                }],
            },
        ]
        inventory, merged, missing = bucket_pipeline.validate_final_manifests(
            manifests, {"selected_indices": [1, 2, 3, 4]}, 2
        )
        self.assertEqual([item["worker_id"] for item in inventory], [0, 1])
        self.assertEqual(merged, [1, 3, 4])
        self.assertEqual(missing, [2])

    def test_final_manifest_validation_rejects_missing_shard_chapter(self):
        manifests = [{
            "worker_ids": [0], "merged_chapters": [1],
            "worker_inventory": [{
                "worker_id": 0, "expected": [1, 2],
                "mp4_chapters": [1], "source_missing_chapters": [],
            }],
        }]
        with self.assertRaisesRegex(RuntimeError, "final chapter coverage mismatch"):
            bucket_pipeline.validate_final_manifests(
                manifests, {"selected_indices": [1, 2]}, 1
            )

    def test_bucket_pipeline_adds_repository_root_for_src_imports(self):
        source = BUCKET_MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn("Path(__file__).resolve().parent.parent", source)
        self.assertIn("sys.path.insert(0, str(REPOSITORY_ROOT))", source)

    def test_bucket_pipeline_never_stages_worker_mp4s_in_hf(self):
        source = BUCKET_MODULE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("batch_bucket_files", source)
        self.assertNotIn("stage_worker", source)
        self.assertIn("ArtifactVideoProvider", source)
        self.assertIn("provider.cleanup()", source)

    def test_workflow_uses_parallel_shards_and_one_finalizer(self):
        source = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertNotIn("stage_workers:", source)
        self.assertNotIn("hf jobs run", source)
        self.assertIn("hf-mount", source)
        self.assertIn("--no-disk-cache", source)
        self.assertIn("merge_shards:", source)
        self.assertIn("max-parallel: 15", source)
        self.assertIn("--mode shard", source)
        self.assertIn("--mode finalize", source)
        self.assertIn("finalize_and_upload:", source)
        self.assertIn("actions/cache/restore@v4", source)
        self.assertIn("youtube-upload-state-source-${{ needs.discover.outputs.run_id }}-", source)

    def test_workflow_labels_inclusive_worker_range(self):
        source = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn('f"worker {start}" if end == start + 1', source)
        self.assertIn('f"workers {start}-{end - 1}"', source)
        self.assertIn("(${{ matrix.worker_label }})", source)
        self.assertNotIn("workers ${{ matrix.worker_start }}-${{ matrix.worker_end }}", source)

    def test_bucket_final_merge_uses_forward_only_fragmented_mp4(self):
        source = BUCKET_MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn("+frag_keyframe+empty_moov+default_base_moof", source)
        self.assertIn("output.unlink()", source)

if __name__ == "__main__": unittest.main()
