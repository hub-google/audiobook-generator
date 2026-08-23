import json
import os
import unittest
from unittest.mock import Mock, patch

from src.metadata_gen import generate_gemini_cover_information

FIELDS = {
    "故事類型與時代": "東方玄幻遠古時代，有明確的修行文明。",
    "世界觀": "大荒與上界並存，萬族和諸聖競逐。",
    "主角外觀與身分": "少年主角石昊，身分與成長線已由來源查證。",
    "代表性場景": "大荒石村與宏大的遠古山脈。",
    "法寶武器或關鍵物件": "查證資料記載的具體故事物件。",
    "色彩氣氛與構圖": "蒼茫大荒的暖金與青藍電影構圖。",
    "應避免畫錯的內容": "不得混入同名遊戲或其他作者作品。",
}


def response_payload(grounded=True):
    result = {
        "status": "ok",
        "verified_facts": [f"這是經搜尋查證且足夠具體的故事事實 {i}" for i in range(1, 6)],
        "analysis": FIELDS,
        "prompt": " ".join(["specific cinematic prehistoric oriental fantasy detail"] * 25),
    }
    candidate = {"content": {"parts": [{"text": json.dumps(result, ensure_ascii=False)}]}}
    if grounded:
        candidate["groundingMetadata"] = {"groundingChunks": [
            {"web": {"title": "source one", "uri": "https://one.example/book"}},
            {"web": {"title": "source two", "uri": "https://two.example/book"}},
        ]}
    return {"candidates": [candidate]}


class CoverInformationQualityTests(unittest.TestCase):
    def setUp(self):
        self.synopsis = "書名：《完美世界》；作者：辰東；類型：玄幻；原始簡介：一粒塵可填海，一根草斬盡日月星辰，群雄並起，萬族林立，一個少年從大荒中走出。"

    @patch.dict(os.environ, {"GEMINI_API_KEY": "secret-test-key"})
    @patch("src.metadata_gen.requests.post")
    def test_accepts_only_grounded_detailed_result(self, post):
        post.return_value = Mock(status_code=200, json=lambda: response_payload(), text="")
        result = generate_gemini_cover_information("完美世界", self.synopsis)
        self.assertEqual(len(result["grounding_sources"]), 2)
        self.assertGreaterEqual(len(result["verified_facts"]), 5)

    @patch.dict(os.environ, {"GEMINI_API_KEY": "secret-test-key"})
    @patch("src.metadata_gen.requests.post")
    def test_missing_grounding_fails_instead_of_returning_prompt(self, post):
        post.return_value = Mock(status_code=200, json=lambda: response_payload(False), text="")
        with self.assertRaisesRegex(RuntimeError, "沒有提供至少兩個"):
            generate_gemini_cover_information("完美世界", self.synopsis)

    @patch.dict(os.environ, {"GEMINI_API_KEY": "secret-test-key"})
    @patch("src.metadata_gen.requests.post")
    def test_api_error_does_not_expose_key(self, post):
        post.return_value = Mock(
            status_code=429,
            json=lambda: {"error": {"message": "quota exhausted"}},
            text="quota exhausted",
        )
        with self.assertRaises(RuntimeError) as caught:
            generate_gemini_cover_information("完美世界", self.synopsis)
        self.assertNotIn("secret-test-key", str(caught.exception))
        self.assertIn("429", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
