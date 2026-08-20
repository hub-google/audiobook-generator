import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from src.prepare_parts import merge_assigned_parts, plan_parts
from src.youtube_api_uploader import scan_artifact_chapters


class PreparePartsTests(unittest.TestCase):
    def test_merge_uploads_complete_part_tree_in_one_hf_commit(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            plan = root / 'parts-plan.json'
            plan.write_text(json.dumps({
                'source_run_id': '123', 'book_title': '測試書',
                'source_missing_chapters': [],
                'chapter_artifacts': {'1': 'mp4-worker-0'},
                'parts': [{'part_num': 1, 'start_chap': 1, 'end_chap': 1,
                           'chapters': [1], 'duration': 1.0}],
            }), encoding='utf-8')
            chapter_video, chapter_srt = root / 'chapter_1.mp4', root / 'chapter_1.srt'
            chapter_video.write_bytes(b'chapter-video')
            chapter_srt.write_text('subtitle', encoding='utf-8')
            scanned = [{'artifact': 'mp4-worker-0', 'chap_num': 1, 'dur': 1.0,
                        'path': str(chapter_video), 'srt_path': str(chapter_srt)}]
            api = MagicMock()

            def make_srt(_items, destination):
                Path(destination).write_text('merged subtitle', encoding='utf-8')
                return True

            def make_video(_part, destination):
                Path(destination).write_bytes(b'merged video')
                return True

            with patch.dict('os.environ', {'HF_TOKEN': 'token', 'HF_ARCHIVE_REPO': 'owner/archive'}), \
                 patch('huggingface_hub.HfApi', return_value=api), \
                 patch('src.prepare_parts.download_artifact_task', return_value=True), \
                 patch('src.prepare_parts.scan_artifact_chapters', return_value=scanned), \
                 patch('src.prepare_parts.generate_part_srt', side_effect=make_srt), \
                 patch('src.prepare_parts.merge_part_videos', side_effect=make_video), \
                 patch('src.prepare_parts.validate_video', return_value={'valid': True}), \
                 patch('src.prepare_parts.validate_srt', return_value={'valid': True}), \
                 patch('src.prepare_parts._media_info', return_value={'format': {'duration': '1'}}):
                merge_assigned_parts(plan, [1], 'owner/repo', root / 'out', root / 'work')

            api.create_commit.assert_called_once()
            paths = {operation.path_in_repo for operation in api.create_commit.call_args.kwargs['operations']}
            self.assertEqual({Path(path).name for path in paths}, {
                '測試書_Part_01_Ch0001_to_Ch0001.mp4',
                '測試書_Part_01_Ch0001_to_Ch0001.srt',
                'merge_manifest.json', 'part_manifest.json', 'media_info.json',
            })

    def test_scanned_inventory_exposes_local_merge_paths(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            video = root / 'Video' / '測試書_chapter_1.mp4'
            subtitle = root / 'Subtitles' / '測試書_chapter_1.srt'
            video.parent.mkdir()
            subtitle.parent.mkdir()
            video.write_bytes(b'video')
            subtitle.write_text('1\n00:00:00,000 --> 00:00:01,000\n字幕\n', encoding='utf-8')
            with patch('src.youtube_api_uploader.get_media_duration', return_value=1.0):
                item = scan_artifact_chapters(str(root), 'mp4-worker-0')[0]
            self.assertEqual(Path(item['path']), video.resolve())
            self.assertEqual(Path(item['srt_path']), subtitle.resolve())

    def test_merge_rejects_missing_subtitles_with_chapter_numbers(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            plan = root / 'parts-plan.json'
            plan.write_text(json.dumps({
                'source_run_id': '123', 'book_title': '測試書',
                'chapter_artifacts': {'1': 'mp4-worker-0'},
                'parts': [{'part_num': 1, 'start_chap': 1, 'end_chap': 1,
                           'chapters': [1], 'duration': 1.0}],
            }), encoding='utf-8')
            video = root / 'chapter_1.mp4'
            video.write_bytes(b'video')
            scanned = [{'artifact': 'mp4-worker-0', 'chap_num': 1, 'dur': 1.0,
                        'path': str(video), 'srt_path': None}]
            with patch('src.prepare_parts.download_artifact_task', return_value=True), \
                 patch('src.prepare_parts.scan_artifact_chapters', return_value=scanned):
                with self.assertRaisesRegex(RuntimeError, r'missing_subtitles=\[1\]'):
                    merge_assigned_parts(plan, [1], 'owner/repo', root / 'out', root / 'work')

    def test_locks_all_parts_and_builds_bounded_merge_matrix(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            config = root / 'config.yaml'
            config.write_text(yaml.safe_dump({
                'book_title': '測試書',
                'selected_indices': [1, 2],
            }, allow_unicode=True), encoding='utf-8')

            def download(_run, _repo, name, destination):
                destination = Path(destination)
                (destination / 'Video').mkdir(parents=True)
                (destination / 'Subtitles').mkdir(parents=True)
                number = int(name.rsplit('-', 1)[1]) + 1
                (destination / 'Video' / f'測試書_chapter_{number}.mp4').write_bytes(b'video')
                (destination / 'Subtitles' / f'測試書_chapter_{number}.srt').write_text('srt', encoding='utf-8')
                return True

            def scan(directory, artifact):
                number = int(artifact.rsplit('-', 1)[1]) + 1
                base = Path(directory)
                return [{
                    'artifact': artifact,
                    'chap_num': number,
                    'dur': 20_000.0,
                    'path': str(base / 'Video' / f'測試書_chapter_{number}.mp4'),
                    'srt_path': str(base / 'Subtitles' / f'測試書_chapter_{number}.srt'),
                }]

            with patch('src.prepare_parts.get_run_manifest_artifact_names', return_value=[]), \
                 patch('src.prepare_parts.get_run_artifact_names', return_value=['mp4-worker-0', 'mp4-worker-1']), \
                 patch('src.prepare_parts.download_artifact_task', side_effect=download), \
                 patch('src.prepare_parts.scan_artifact_chapters', side_effect=scan), \
                 patch('src.prepare_parts.confirmed_missing_from_directory', return_value=set()), \
                 patch('src.prepare_parts.validate_chapter_inventory'):
                manifest = plan_parts(
                    '123', 'owner/repo', config, root / 'out', root / 'work'
                )

            self.assertEqual([part['part_num'] for part in manifest['parts']], [1, 2])
            saved = json.loads((root / 'out' / 'parts-plan.json').read_text(encoding='utf-8'))
            self.assertEqual(saved['source_run_id'], '123')
            self.assertEqual(saved['chapter_artifacts'], {'1': 'mp4-worker-0', '2': 'mp4-worker-1'})
            self.assertEqual(len(saved['matrix']['include']), 2)
            self.assertLessEqual(len(saved['matrix']['include']), 17)
            self.assertTrue((root / 'out' / 'config.yaml').is_file())

    def test_plan_parts_fast_path_with_manifest_artifacts(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            config = root / 'config.yaml'
            config.write_text(yaml.safe_dump({
                'book_title': '測試書',
                'selected_indices': [1, 2],
            }, allow_unicode=True), encoding='utf-8')

            def download(_run, _repo, name, destination):
                destination = Path(destination)
                destination.mkdir(parents=True, exist_ok=True)
                worker_id = int(name.rsplit('-', 1)[1])
                chapter_num = worker_id + 1
                manifest_data = {
                    'schema_version': 1,
                    'worker_id': worker_id,
                    'artifact': f'mp4-worker-{worker_id}',
                    'book_title': '測試書',
                    'chapters': [{
                        'chap_num': chapter_num,
                        'dur': 20_000.0,
                        'artifact': f'mp4-worker-{worker_id}',
                    }],
                    'source_missing': [],
                }
                (destination / f'manifest-worker-{worker_id}.json').write_text(
                    json.dumps(manifest_data, ensure_ascii=False), encoding='utf-8'
                )
                return True

            with patch('src.prepare_parts.get_run_manifest_artifact_names', return_value=['manifest-worker-0', 'manifest-worker-1']), \
                 patch('src.prepare_parts.get_run_artifact_names', return_value=['mp4-worker-0', 'mp4-worker-1']), \
                 patch('src.prepare_parts.download_artifact_task', side_effect=download), \
                 patch('src.prepare_parts.scan_artifact_chapters') as mock_scan, \
                 patch('src.prepare_parts.validate_chapter_inventory'):
                manifest = plan_parts(
                    '123', 'owner/repo', config, root / 'out', root / 'work'
                )

            mock_scan.assert_not_called()
            self.assertEqual([part['part_num'] for part in manifest['parts']], [1, 2])
            saved = json.loads((root / 'out' / 'parts-plan.json').read_text(encoding='utf-8'))
            self.assertEqual(saved['source_run_id'], '123')
            self.assertEqual(saved['chapter_artifacts'], {'1': 'mp4-worker-0', '2': 'mp4-worker-1'})


if __name__ == '__main__':
    unittest.main()
