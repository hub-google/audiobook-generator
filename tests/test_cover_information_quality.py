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

VISUAL_BRIEF = {
    "genre": "prehistoric oriental fantasy",
    "era_and_setting": "the Great Wilderness and ancient cultivation realms",
    "core_conflict": "Shi Hao rises from Stone Village against ancient clans",
    "main_character_identity": "young cultivator Shi Hao",
    "appearance": "long dark hair and a fierce youthful face",
    "clothing": "story-appropriate rugged ancient robes",
    "expression_and_action": "a determined gaze while confronting powerful enemies",
    "supporting_characters": [],
    "iconic_story_symbol": "the colossal primordial wilderness and Stone Village guardian willow",
    "iconic_prop_or_power": "verified ancient runes and cultivation power",
    "genre_color_palette": "deep black, brilliant gold and emerald highlights",
    "lighting_and_mood": "explosive backlight and an awe-inspiring heroic mood",
    "avoid_story_errors": ["do not mix in characters from unrelated adaptations"],
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
        "visual_brief": VISUAL_BRIEF,
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
        self.assertIn("prompt", result)

        sent_instruction = post.call_args.kwargs["json"]["contents"][0]["parts"][0]["text"]
        self.assertIn("只分析指定小說的原著版本", sent_instruction)
        self.assertIn("動畫、漫畫、遊戲、影視改編", sent_instruction)
        self.assertNotIn('"identity"', sent_instruction)

    @patch.dict(os.environ, {"GEMINI_API_KEY": "secret-test-key"})
    @patch("src.metadata_gen.requests.post")
    def test_internal_fallback_accepts_catalog_and_model_knowledge_together(self, post):
        payload = response_payload()
        result = json.loads(payload["candidates"][0]["content"]["parts"][0]["text"])
        result["story_facts"][0]["source_ids"] = ["S1", "MODEL_KNOWLEDGE"]
        payload["candidates"][0]["content"]["parts"][0]["text"] = json.dumps(result, ensure_ascii=False)
        post.return_value = Mock(status_code=200, json=lambda: payload, text="")

        research = {
            "mode": "internal_knowledge_fallback",
            "sources": [{"id": "S1", "title": "目錄", "url": "https://example.test", "text": self.synopsis}],
        }
        generated = generate_gemini_cover_information("完美世界", self.synopsis, research=research)

        self.assertEqual(generated["story_facts"][0]["source_ids"], ["S1", "MODEL_KNOWLEDGE"])

    @patch.dict(os.environ, {"GEMINI_API_KEY": "secret-test-key"})
    @patch("src.metadata_gen.requests.post")
    def test_internal_fallback_rejects_empty_or_unknown_source_ids(self, post):
        payload = response_payload()
        result = json.loads(payload["candidates"][0]["content"]["parts"][0]["text"])
        result["story_facts"][0]["source_ids"] = ["S99"]
        payload["candidates"][0]["content"]["parts"][0]["text"] = json.dumps(result, ensure_ascii=False)
        post.return_value = Mock(status_code=200, json=lambda: payload, text="")

        research = {
            "mode": "internal_knowledge_fallback",
            "sources": [{"id": "S1", "title": "目錄", "url": "https://example.test", "text": self.synopsis}],
        }
        with self.assertRaisesRegex(RuntimeError, "無效來源編號"):
            generate_gemini_cover_information("完美世界", self.synopsis, research=research)

    @patch.dict(os.environ, {"GEMINI_API_KEY": "secret-test-key"})
    @patch("src.metadata_gen.requests.post")
    def test_missing_story_facts_fails_instead_of_returning_prompt(self, post):
        payload = response_payload()
        payload["candidates"][0]["content"]["parts"][0]["text"] = json.dumps(
            {"status": "ok", "story_facts": [], "analysis": FIELDS, "visual_brief": VISUAL_BRIEF},
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
