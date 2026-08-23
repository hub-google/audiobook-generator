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
        "story_facts": [
            {"fact": "主角石昊從大荒石村成長", "source_ids": ["MODEL_KNOWLEDGE"]},
            *[
                {"fact": f"這是足夠具體的大荒故事事實 {i}", "source_ids": ["MODEL_KNOWLEDGE"]}
                for i in range(2, 6)
            ],
        ],
        "analysis": FIELDS,
        "prompt": " ".join(["specific cinematic prehistoric oriental fantasy detail"] * 25),
    }
    candidate = {"content": {"parts": [{"text": json.dumps(result, ensure_ascii=False)}]}}
    return {"candidates": [candidate]}


class CoverInformationQualityTests(unittest.TestCase):
    def setUp(self):
        self.synopsis = "書名：《完美世界》；作者：辰東；類型：玄幻；原始簡介：一粒塵可填海，一根草斬盡日月星辰，群雄並起，萬族林立，一個少年從大荒中走出。"

    @patch.dict(os.environ, {"GEMINI_API_KEY": "secret-test-key"})
    @patch("src.metadata_gen.requests.post")
    def test_accepts_detailed_story_specific_result(self, post):
        post.return_value = Mock(status_code=200, json=lambda: response_payload(), text="")
        result = generate_gemini_cover_information("完美世界", self.synopsis)
        self.assertGreaterEqual(len(result["story_facts"]), 5)

    @patch.dict(os.environ, {"GEMINI_API_KEY": "secret-test-key"})
    @patch("src.metadata_gen.requests.post")
    def test_missing_story_facts_fails_instead_of_returning_prompt(self, post):
        payload = response_payload()
        payload["candidates"][0]["content"]["parts"][0]["text"] = json.dumps(
            {"status": "ok", "story_facts": [], "analysis": FIELDS, "prompt": "specific " * 150},
            ensure_ascii=False,
        )
        post.return_value = Mock(status_code=200, json=lambda: payload, text="")
        with self.assertRaisesRegex(RuntimeError, "五條具體"):
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
