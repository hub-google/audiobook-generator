import hashlib
import http.server
import importlib.util
from pathlib import Path
import shutil
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest


ROOT = Path(__file__).resolve().parents[1]


def cloud_pipeline_module():
    path = ROOT / "合併上傳" / "cloud_pipeline.py"
    spec = importlib.util.spec_from_file_location("cloud_pipeline_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hf_concat_attaches_authorization_to_each_remote_part_not_the_list_input():
    module = cloud_pipeline_module()
    content = module.ffconcat_text(["https://hf/one.mp4", "https://hf/two.mp4"], "secret")
    assert content.count("option headers 'Authorization: Bearer secret'") == 2
    source = (ROOT / "合併上傳" / "cloud_pipeline.py").read_text(encoding="utf-8")
    command = source.split('subprocess.run(["ffmpeg","-hide_banner"', 1)[1].split("check=True)", 1)[0]
    assert '"-headers"' not in command


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg is required")
def test_ffmpeg_really_reads_authenticated_concat_parts(tmp_path):
    module = cloud_pipeline_module()
    for number in (1, 2):
        subprocess.run([
            "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=32x32:r=10",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "0.4",
            "-c:v", "mpeg4", "-c:a", "aac", "-movflags", "+faststart", str(tmp_path / f"{number}.mp4"),
        ], check=True)

    seen_authorization = []
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(tmp_path), **kwargs)
        def log_message(self, *_args):
            pass
        def do_GET(self):
            seen_authorization.append(self.headers.get("Authorization"))
            if self.headers.get("Authorization") != "Bearer test-token":
                self.send_error(401); return
            super().do_GET()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        urls = [f"http://127.0.0.1:{server.server_port}/{number}.mp4" for number in (1, 2)]
        concat = tmp_path / "parts.ffconcat"
        concat.write_text(module.ffconcat_text(urls, "test-token"), encoding="utf-8")
        output = tmp_path / "merged.mp4"
        subprocess.run([
            "ffmpeg", "-v", "error", "-y", "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
            "-f", "concat", "-safe", "0", "-i", str(concat), "-map", "0:v:0", "-map", "0:a:0",
            "-c", "copy", str(output),
        ], check=True)
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
    assert output.stat().st_size > 0
    assert seen_authorization and set(seen_authorization) == {"Bearer test-token"}


def test_phase_one_refuses_bad_hf_checksum_before_contacting_youtube():
    module = cloud_pipeline_module()
    payload = b"complete merged video"
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.iter_content.return_value = [payload]
    response.raise_for_status.return_value = None
    manifest = {
        "status": "merge_complete", "bytes": len(payload),
        "sha256": "0" * 64, "video_path": "merged/audiobook.mp4",
        "youtube_title": "title", "youtube_description": "00:00 chapter",
    }
    args = SimpleNamespace(manifest="manifest.json", title="", privacy="public", state_path="state.json")
    with patch.object(module, "remote_json", return_value=manifest), \
         patch.object(module, "resolve_url", return_value="https://hf/video"), \
         patch.object(module, "repo_token", return_value=("repo", "token")), \
         patch.object(module.requests, "get", return_value=response), \
         patch.object(module, "credentials") as credentials, \
         patch.object(module.requests, "post") as youtube_post:
        with pytest.raises(RuntimeError, match="checksum mismatch"):
            module.phase1(args)
    credentials.assert_not_called()
    youtube_post.assert_not_called()


def test_hf_verification_accepts_only_exact_size_and_checksum():
    module = cloud_pipeline_module()
    payload = b"complete merged video"
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.iter_content.return_value = [payload[:5], payload[5:]]
    response.raise_for_status.return_value = None
    manifest = {"status": "merge_complete", "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(), "video_path": "merged/audiobook.mp4"}
    with patch.object(module, "resolve_url", return_value="https://hf/video"), \
         patch.object(module, "repo_token", return_value=("repo", "token")), \
         patch.object(module.requests, "get", return_value=response):
        assert module.verified_hf_source(manifest) == "https://hf/video"


def test_complete_video_validation_reads_all_packets_after_probe():
    module = cloud_pipeline_module()
    with patch.object(module, "validate_video", return_value={"bytes": 1, "sha256": "a" * 64, "duration_seconds": 3600}) as validate, \
         patch.object(module.subprocess, "run") as run:
        result = module.verify_complete_video("audiobook.mp4", 3600)
    validate.assert_called_once_with("audiobook.mp4", audio_duration=3600)
    command = run.call_args.args[0]
    assert command[:4] == ["ffmpeg", "-v", "error", "-xerror"]
    assert command[-3:] == ["-f", "null", "-"]
    assert result["sha256"] == "a" * 64


def test_complete_video_validation_rejects_materially_short_output_before_packet_scan():
    module = cloud_pipeline_module()
    validation = {"bytes": 1, "sha256": "a" * 64, "duration_seconds": 3500}
    with patch.object(module, "validate_video", return_value=validation), \
         patch.object(module.subprocess, "run") as run:
        with pytest.raises(RuntimeError, match="duration mismatch"):
            module.verify_complete_video("audiobook.mp4", 3600)
    run.assert_not_called()
