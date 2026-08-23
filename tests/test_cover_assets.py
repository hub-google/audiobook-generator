import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from src.cover_assets import (
    normalize_manual_cover, restore_cover, restore_from_config, upload_cover, upload_github_cover,
    validate_cached_cover,
)


class ManualCoverTests(unittest.TestCase):
    def test_normalizes_crop_format_size_and_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "portrait.png"
            output = Path(directory) / "master_cover.jpg"
            Image.new("RGBA", (1000, 1400), (30, 80, 160, 180)).save(source)
            result = normalize_manual_cover(source, output)
            self.assertEqual((result["width"], result["height"]), (1280, 720))
            self.assertGreater(result["bytes"], 10_000)
            self.assertEqual(validate_cached_cover(output, result["sha256"]), result["sha256"])
            self.assertTrue(output.with_suffix(".manual.json").is_file())

    def test_rejects_wrong_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.jpg"
            output = Path(directory) / "master_cover.jpg"
            Image.new("RGB", (1280, 720), "navy").save(source)
            normalize_manual_cover(source, output)
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                validate_cached_cover(output, "0" * 64)

    def test_upload_explains_missing_write_permission(self):
        from huggingface_hub.errors import HfHubHTTPError

        response = Mock(status_code=403)
        error = HfHubHTTPError("Forbidden", response=response)
        api = Mock()
        api.upload_file.side_effect = error
        with patch("huggingface_hub.HfApi", return_value=api):
            with self.assertRaisesRegex(RuntimeError, "沒有寫入權限"):
                upload_cover("cover.jpg", "profile", "hf_read_only", "owner/archive")

    def test_github_upload_creates_durable_cover_record(self):
        with tempfile.TemporaryDirectory() as directory:
            cover = Path(directory) / "master_cover.jpg"
            Image.new("RGB", (1280, 720), "navy").save(cover, quality=98)
            missing = Mock(status_code=404)
            uploaded = Mock(status_code=201)
            uploaded.json.return_value = {"content": {"sha": "blob-sha"}}
            with patch("src.cover_assets.requests.get", return_value=missing), \
                    patch("src.cover_assets.requests.put", return_value=uploaded):
                record = upload_github_cover(cover, "profile", "owner/repo", "token")
            self.assertEqual(record["provider"], "github")
            self.assertEqual(record["branch"], "automation-state")
            self.assertEqual(record["blob_sha"], "blob-sha")

    def test_restore_github_cover_verifies_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.jpg"
            normalized = Path(directory) / "normalized.jpg"
            destination = Path(directory) / "restored.jpg"
            Image.new("RGB", (1280, 720), "navy").save(source)
            details = normalize_manual_cover(source, normalized)
            response = Mock(status_code=200, content=normalized.read_bytes())
            record = {
                "provider": "github", "repo": "owner/repo", "branch": "automation-state",
                "remote_path": "manual-covers/profile/master_cover.jpg", "sha256": details["sha256"],
            }
            with patch("src.cover_assets.requests.get", return_value=response):
                restore_cover(record, destination, "token")
            self.assertEqual(validate_cached_cover(destination), details["sha256"])

    def test_retry_recovers_manual_cover_from_durable_book_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.yaml"
            config.write_text(
                "book_title: 完美世界\n"
                "catalog_url: https://example.com/book/1\n",
                encoding="utf-8",
            )
            record = {"provider": "github", "sha256": "a" * 64}
            store = Mock()
            store.load.return_value = ({"books": {}}, None)
            with patch.dict("os.environ", {
                "GH_TOKEN": "token", "GITHUB_REPOSITORY": "owner/repo",
            }), patch("src.book_profiles.GitHubBookProfileStore", return_value=store), \
                    patch("src.book_profiles.get_book_profile", return_value=("profile-1", {"manual_cover": record})), \
                    patch("src.cover_assets.restore_cover") as restore:
                restored = restore_from_config(config, root / "Workspace")

            self.assertTrue(restored)
            destination = root / "Workspace" / "完美世界" / "Cover" / "master_cover.jpg"
            restore.assert_called_once_with(record, destination, "token")


if __name__ == "__main__":
    unittest.main()
