"""Stable viral-thumbnail prompt template shared by every novel genre."""

import re


COVER_TEMPLATE_VERSION = "viral-v1"

VISUAL_BRIEF_FIELDS = {
    "genre",
    "era_and_setting",
    "core_conflict",
    "main_character_identity",
    "appearance",
    "clothing",
    "expression_and_action",
    "supporting_characters",
    "iconic_story_symbol",
    "iconic_prop_or_power",
    "genre_color_palette",
    "lighting_and_mood",
    "avoid_story_errors",
}

FORBIDDEN_COMPOSITION_PHRASES = (
    "clean empty space",
    "open for text",
    "text safety",
    "text-safe",
    "wide establishing shot",
    "tiny distant protagonist",
    "small distant figure",
    "minimalist composition",
)

VIRAL_COVER_TEMPLATE = """Create a high-impact viral Chinese web-novel and micro-drama thumbnail, cinematic 16:9 widescreen, designed to remain instantly readable on a small mobile screen.

GENRE AND STORY:
Genre: {genre}.
Era and setting: {era_and_setting}.
Core dramatic conflict: {core_conflict}.

MAIN CHARACTER:
One visually striking {main_character_identity} dominates the foreground in a tight waist-up cinematic portrait. The protagonist has {appearance}, wears {clothing}, and displays {expression_and_action}. The face is large, sharp, attractive, emotionally intense, unobstructed, and occupies 35 to 45 percent of the image height.

SUPPORTING CHARACTERS:
{supporting_characters}. Use zero to two supporting characters only. They remain clearly secondary and must not compete with the protagonist's face.

STORY ICON:
A huge, instantly recognizable {iconic_story_symbol} rises behind the protagonist as the dominant background symbol. Include {iconic_prop_or_power} as a clearly visible story-specific element.

FIXED THUMBNAIL COMPOSITION:
Dense layered poster composition: foreground protagonist, middle-ground supporting characters, monumental story symbol in the background. Characters fill most of the frame. No wide establishing shot, no tiny or distant protagonist, no empty landscape, no large blank sky, and no minimalist composition. Keep every important face above the lower title zone. The lower 35 percent remains visually rich but slightly darker, lower-contrast and less detailed so a large two-line title can be overlaid without an empty text box. Strong separation between faces and background, bold silhouettes, dramatic rim lighting, radiant backlight, cinematic depth, particles, atmospheric energy, premium promotional-poster polish.

COLOR AND MOOD:
{genre_color_palette}. {lighting_and_mood}. Vivid colors, deep blacks, brilliant highlights, sharp facial detail, realistic premium Chinese drama promotional-poster aesthetic, sensational but coherent, highly clickable at thumbnail size.

STORY-SPECIFIC AVOIDANCE:
{avoid_story_errors}.

STRICT OUTPUT:
One coherent scene, not a collage or split panel. No text, no Chinese characters, no English letters, no numbers, no logo, no watermark, no signature, no frame, and no UI."""


def validate_visual_brief(brief):
    if not isinstance(brief, dict):
        raise ValueError("visual_brief 必須是 JSON 物件")
    missing = sorted(VISUAL_BRIEF_FIELDS - set(brief))
    if missing:
        raise ValueError("visual_brief 缺少欄位：" + ", ".join(missing))
    for field in VISUAL_BRIEF_FIELDS - {"supporting_characters", "avoid_story_errors"}:
        if not str(brief.get(field) or "").strip():
            raise ValueError(f"visual_brief 欄位不可空白：{field}")
    supporting = brief.get("supporting_characters")
    if not isinstance(supporting, list) or len(supporting) > 2 or any(not str(item).strip() for item in supporting):
        raise ValueError("supporting_characters 必須是最多兩項的陣列")
    avoidance = brief.get("avoid_story_errors")
    if not isinstance(avoidance, list) or not avoidance or any(not str(item).strip() for item in avoidance):
        raise ValueError("avoid_story_errors 必須是非空陣列")
    symbol = str(brief.get("iconic_story_symbol") or "").strip().lower()
    if symbol in {"mysterious power", "mystical energy", "dramatic background", "unknown symbol"}:
        raise ValueError("iconic_story_symbol 必須是具體可見的故事符號")
    flattened = " ".join(str(value) for value in brief.values()).lower()
    found = [phrase for phrase in FORBIDDEN_COMPOSITION_PHRASES if phrase in flattened]
    if found:
        raise ValueError("visual_brief 含有禁止構圖：" + ", ".join(found))
    return brief


def build_cover_prompt(brief):
    validated = validate_visual_brief(brief)
    values = dict(validated)
    values["supporting_characters"] = (
        "; ".join(str(item).strip() for item in validated["supporting_characters"])
        if validated["supporting_characters"] else
        "No supporting character is required; use story-specific environmental elements instead"
    )
    values["avoid_story_errors"] = "; ".join(str(item).strip() for item in validated["avoid_story_errors"])
    prompt = VIRAL_COVER_TEMPLATE.format(**values)
    if len(re.findall(r"[A-Za-z]+", prompt)) < 180:
        raise ValueError("組裝後的封面 Prompt 異常過短")
    return prompt
