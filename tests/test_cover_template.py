import unittest

from src.cover_template import build_cover_prompt, validate_visual_brief


def brief(genre="urban suspense"):
    return {
        "genre": genre,
        "era_and_setting": "a modern Asian metropolis at night",
        "core_conflict": "a framed protagonist uncovers a corporate conspiracy",
        "main_character_identity": "a determined young investigator",
        "appearance": "a sharp realistic face and dark hair",
        "clothing": "a fitted charcoal coat appropriate to the story",
        "expression_and_action": "an intense gaze while holding verified evidence",
        "supporting_characters": ["one secondary rival behind the protagonist"],
        "iconic_story_symbol": "a colossal illuminated corporate tower",
        "iconic_prop_or_power": "the verified encrypted evidence device",
        "genre_color_palette": "deep blue, black, cinematic orange and gold",
        "lighting_and_mood": "hard rim light and tense nocturnal atmosphere",
        "avoid_story_errors": ["no ancient clothing", "no supernatural weapon"],
    }


class CoverTemplateTests(unittest.TestCase):
    def test_every_genre_receives_the_same_mobile_thumbnail_composition(self):
        for genre in ("fantasy", "romance", "science fiction", "historical drama", "apocalypse"):
            prompt = build_cover_prompt(brief(genre))
            self.assertIn("tight waist-up cinematic portrait", prompt)
            self.assertIn("lower 35 percent", prompt)
            self.assertIn("No text", prompt)

    def test_more_than_two_supporting_characters_is_rejected(self):
        value = brief()
        value["supporting_characters"] = ["a", "b", "c"]
        with self.assertRaisesRegex(ValueError, "最多兩項"):
            validate_visual_brief(value)

    def test_text_safe_empty_space_instruction_is_rejected(self):
        value = brief()
        value["lighting_and_mood"] = "clean empty space for title"
        with self.assertRaisesRegex(ValueError, "禁止構圖"):
            validate_visual_brief(value)

    def test_abstract_story_symbol_is_rejected(self):
        value = brief()
        value["iconic_story_symbol"] = "mysterious power"
        with self.assertRaisesRegex(ValueError, "具體可見"):
            validate_visual_brief(value)


if __name__ == "__main__":
    unittest.main()
