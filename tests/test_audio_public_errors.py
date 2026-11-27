"""Regression checks for actionable, privacy-safe AUDIO site failures."""
import unittest
import requests

from src.sources import SourceAccessError, SourceParseError
from tools.audio_web_command_runner import public_command_error


class AudioPublicErrorTests(unittest.TestCase):
    def test_source_403_explains_block_and_never_leaks_url_query(self):
        error = SourceAccessError(
            "來源存取受限 HTTP 403: https://69shuba.com/txt/1/1?token=private-secret"
        )
        label = public_command_error(error)
        self.assertIn("403", label)
        self.assertIn("拒絕存取", label)
        self.assertNotIn("private-secret", label)
        self.assertNotIn("69shuba.com", label)

    def test_source_429_explains_rate_limit(self):
        label = public_command_error(SourceAccessError("來源存取受限 HTTP 429"))
        self.assertIn("429", label)
        self.assertIn("拒絕存取", label)

    def test_gemini_overloaded_503_not_just_exception_type(self):
        label = public_command_error(RuntimeError("Gemini HTTP 503: high demand. key=private-secret"))
        self.assertIn("Gemini", label)
        self.assertIn("503", label)
        self.assertNotIn("private-secret", label)

    def test_insufficient_story_facts_is_not_called_a_success(self):
        label = public_command_error(RuntimeError("Gemini 判定來源不足，拒絕產生 Prompt"))
        self.assertIn("故事資料不足", label)

    def test_parser_error_is_actionable(self):
        label = public_command_error(SourceParseError("目錄不存在"))
        self.assertIn("格式無法解析", label)

    def test_validation_error_never_returns_secret(self):
        label = public_command_error(ValueError("api_key=private-secret"))
        self.assertIn("請檢查輸入設定", label)
        self.assertNotIn("private-secret", label)

    def test_unknown_errors_are_not_dumped_publicly(self):
        label = public_command_error(RuntimeError("service token=private-secret"))
        self.assertIn("後端操作失敗", label)
        self.assertNotIn("private-secret", label)


if __name__ == "__main__":
    unittest.main()
