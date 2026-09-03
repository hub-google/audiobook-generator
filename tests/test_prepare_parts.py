import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from src.prepare_parts import (fetch_parts_from_hf, merge_assigned_parts, plan_parts, restore_locked_merge_result, restore_locked_plan)
from src.youtube_api_uploader import scan_artifact_chapters
from src.part_builder import get_media_duration


class PreparePartsTests(unittest.TestCase):
    def test_restores_only_fingerprint_matching_locked_plan(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root); source=root/'source'; source.mkdir(); output=root/'output'
            config={'book_profile_id':'fp-1','selected_indices':[1,2],'cleaner':{'fingerprint':'clean-1'}}
            (source/'config.yaml').write_text(yaml.safe_dump(config),encoding='utf-8')
            (root/'current.yaml').write_text(yaml.safe_dump(config),encoding='utf-8')
            plan={'source_run_id':'123','selected_indices':[1,2],'parts':[{'part_num':1}], 'matrix':{'include':[{'merge_worker_id':0,'part_numbers':'1'}]}}
            (source/'parts-plan.json').write_text(json.dumps(plan),encoding='utf-8')
            self.assertEqual(restore_locked_plan(source,root/'current.yaml',output)['source_run_id'],'123')
            self.assertTrue((output/'parts-plan.json').is_file())
            config['book_profile_id']='wrong'; (root/'current.yaml').write_text(yaml.safe_dump(config),encoding='utf-8')
            self.assertIsNone(restore_locked_plan(source,root/'current.yaml',root/'wrong-output'))

    def test_valid_locked_merge_result_skips_remerge_and_wrong_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root); source=root/'source'; source.mkdir(); output=root/'output'
            plan={'source_run_id':'123','parts':[{'part_num':1,'duration':1.0}]}
            (root/'plan.json').write_text(json.dumps(plan),encoding='utf-8')
            item={'part_num':1,'duration':1.0,'subtitle':'part-1.srt'}
            (source/'part-1.srt').write_text('subtitle',encoding='utf-8')
            manifest=source/'shard-manifest-1.json'
            manifest.write_text(json.dumps({'source_run_id':'123','parts':[item]}),encoding='utf-8')
            with patch('src.prepare_parts.validate_srt',return_value={'valid':True}):
                self.assertTrue(restore_locked_merge_result(root/'plan.json',[1],source,output))
            self.assertTrue((output/'shard-manifest-1.json').is_file())
            manifest.write_text(json.dumps({'source_run_id':'999','parts':[item]}),encoding='utf-8')
            self.assertFalse(restore_locked_merge_result(root/'plan.json',[1],source,root/'wrong'))

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

    def test_scan_artifact_chapters_manifest_first(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            video_dir = root / 'Video'
            video_dir.mkdir(parents=True, exist_ok=True)
            srt_dir = root / 'Subtitles'
            srt_dir.mkdir(parents=True, exist_ok=True)
            manifest_dir = root / 'Manifests'
            manifest_dir.mkdir(parents=True, exist_ok=True)

            video_file = video_dir / '測試書_chapter_15.mp4'
            video_file.write_bytes(b'video-bytes')
            srt_file = srt_dir / '測試書_chapter_15.srt'
            srt_file.write_text('1\n00:00:00,000 --> 00:06:53,000\n字幕內容\n', encoding='utf-8')

            manifest_data = {
                'worker_id': 0,
                'chapters': [{
                    'chap_num': 15,
                    'dur': 413.0,
                    'artifact': 'mp4-worker-0',
                    'video_relpath': 'Video/測試書_chapter_15.mp4',
                    'srt_relpath': 'Subtitles/測試書_chapter_15.srt',
                }],
            }
            (manifest_dir / 'manifest-worker-0.json').write_text(
                json.dumps(manifest_data, ensure_ascii=False), encoding='utf-8'
            )

            # Probing should not be needed because duration is taken directly from manifest
            with patch('src.youtube_api_uploader.get_media_duration') as mock_probe:
                items = scan_artifact_chapters(str(root), 'mp4-worker-0')

            mock_probe.assert_not_called()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]['chap_num'], 15)
            self.assertEqual(items[0]['dur'], 413.0)
            self.assertEqual(Path(items[0]['path']), video_file.resolve())
            self.assertEqual(Path(items[0]['srt_path']), srt_file.resolve())

    def test_get_media_duration_handles_na_and_srt_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            video_file = root / 'test_chapter_1.mp4'
            video_file.write_bytes(b'fake-video')
            srt_file = root / 'test_chapter_1.srt'
            srt_file.write_text('1\n00:00:00,000 --> 00:05:30,500\nHello\n', encoding='utf-8')

            # Mock ffprobe returning N/A for format but 330.5 for stream
            mock_res_stream = MagicMock(returncode=0, stdout='N/A\n330.5\n')
            with patch('subprocess.run', return_value=mock_res_stream):
                dur = get_media_duration(str(video_file))
            self.assertAlmostEqual(dur, 330.5)

            # Mock ffprobe and ffmpeg failing completely -> fallback to SRT
            mock_res_fail = MagicMock(returncode=1, stdout='', stderr='')
            with patch('subprocess.run', return_value=mock_res_fail):
                dur_srt = get_media_duration(str(video_file))
            self.assertAlmostEqual(dur_srt, 330.5)

    def test_merge_recovers_zero_duration_from_srt(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            plan = root / 'parts-plan.json'
            plan.write_text(json.dumps({
                'source_run_id': '123', 'book_title': '測試書',
                'source_missing_chapters': [],
                'chapter_artifacts': {'1': 'mp4-worker-0'},
                'parts': [{'part_num': 1, 'start_chap': 1, 'end_chap': 1,
                           'chapters': [1], 'duration': 300.0}],
            }), encoding='utf-8')
            chapter_video = root / 'chapter_1.mp4'
            chapter_srt = root / 'chapter_1.srt'
            chapter_video.write_bytes(b'chapter-video')
            chapter_srt.write_text('1\n00:00:00,000 --> 00:05:00,000\n字幕\n', encoding='utf-8')
            # scanned has dur: 0.0 (simulating ffprobe glitch)
            scanned = [{'artifact': 'mp4-worker-0', 'chap_num': 1, 'dur': 0.0,
                        'path': str(chapter_video), 'srt_path': str(chapter_srt)}]
            api = MagicMock()

            def make_srt(_items, destination):
                Path(destination).write_text('1\n00:00:00,000 --> 00:05:00,000\n字幕\n', encoding='utf-8')
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
                 patch('src.prepare_parts._media_info', return_value={'format': {'duration': '300'}}):
                merge_assigned_parts(plan, [1], 'owner/repo', root / 'out', root / 'work')

            api.create_commit.assert_called_once()

    def test_fetch_parts_from_hf_retries_corrupt_download_and_succeeds(self):
        import hashlib
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            plan = root / 'parts-plan.json'
            plan.write_text(json.dumps({
                'source_run_id': '123', 'book_title': '測試書',
                'parts': [{'part_num': 1, 'start_chap': 1, 'end_chap': 1}],
            }), encoding='utf-8')
            sidecars = root / 'sidecars'
            sidecars.mkdir()
            valid_bytes = b'valid video content'
            valid_sha = hashlib.sha256(valid_bytes).hexdigest()
            part_info = {
                'part_num': 1, 'start_chap': 1, 'end_chap': 1,
                'video': '測試書_Part_01_Ch0001_to_Ch0001.mp4',
                'subtitle': '測試書_Part_01_Ch0001_to_Ch0001.srt',
                'hf_video_path': 'remote/video.mp4',
                'video_bytes': len(valid_bytes),
                'video_sha256': valid_sha,
            }
            (sidecars / 'shard-manifest-1.json').write_text(json.dumps({
                'source_run_id': '123', 'parts': [part_info]
            }), encoding='utf-8')
            (sidecars / '測試書_Part_01_Ch0001_to_Ch0001.srt').write_text('subtitle content', encoding='utf-8')

            corrupt_file = root / 'corrupt.mp4'
            corrupt_file.write_bytes(b'truncated')
            valid_file = root / 'valid.mp4'
            valid_file.write_bytes(valid_bytes)

            download_calls = []
            def mock_download(_repo, _path, **kwargs):
                download_calls.append(kwargs)
                if len(download_calls) < 3:
                    return str(corrupt_file)
                return str(valid_file)

            with patch.dict('os.environ', {'HF_TOKEN': 'token', 'HF_ARCHIVE_REPO': 'owner/archive'}), \
                 patch('huggingface_hub.HfApi'), \
                 patch('huggingface_hub.hf_hub_download', side_effect=mock_download), \
                 patch('time.sleep'):
                fetch_parts_from_hf(str(plan), str(root / 'out'), sidecar_dir=str(sidecars))

            self.assertEqual(len(download_calls), 3)
            target_video = root / 'out' / '測試書_Part_01_Ch0001_to_Ch0001.mp4'
            self.assertTrue(target_video.is_file())
            self.assertEqual(target_video.stat().st_size, len(valid_bytes))

if __name__ == '__main__':
    unittest.main()

