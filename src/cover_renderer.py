"""Deterministic YouTube thumbnail typography for viral novel covers."""

import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from io import BytesIO
import yaml


YOUTUBE_COVER_SIZE = (1280, 720)
# YujiBoku's glyphs extend roughly 60 px below Pillow's nominal line block at
# the largest title size. A 190 px layout margin preserves about 120 px of
# visible bottom clearance, matching the approved reference thumbnail.
TITLE_BOTTOM_SAFE_MARGIN = 190
PROJECT_ROOT = Path(__file__).resolve().parent.parent
TITLE_FONT_PATH = PROJECT_ROOT / "fonts" / "YujiBoku.ttf"


def _settings():
    path = PROJECT_ROOT / "config.yaml"
    if not path.is_file():
        return {}
    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise RuntimeError(f"無法讀取封面文字設定：{exc}") from exc
    typography = (config.get("cover") or {}).get("typography") or {}
    if not isinstance(typography, dict):
        raise RuntimeError("config.yaml 的 cover.typography 必須是物件")
    if int(typography.get("title_max_lines", 2)) != 2:
        raise RuntimeError("viral-v1 的 title_max_lines 必須固定為 2")
    return typography


def _rgb(value, fallback):
    if value in (None, ""):
        return fallback
    text = str(value or "").lstrip("#")
    if len(text) == 6:
        try:
            return tuple(int(text[index:index + 2], 16) for index in (0, 2, 4))
        except ValueError:
            pass
    raise RuntimeError(f"封面顏色必須是 #RRGGBB：{value}")


def _font(path, size):
    if not Path(path).is_file():
        raise RuntimeError(f"封面指定字型不存在：{path}")
    return ImageFont.truetype(str(path), size)


def _width(draw, text, font, stroke=0):
    box = draw.textbbox((0, 0), text, font=font, stroke_width=stroke)
    return box[2] - box[0]


def _balanced_lines(draw, title, font, max_width):
    if _width(draw, title, font, 12) <= max_width:
        return [title]
    candidates = []
    for split in range(1, len(title)):
        left, right = title[:split].strip(), title[split:].strip()
        if left and right:
            widths = [_width(draw, left, font, 12), _width(draw, right, font, 12)]
            if max(widths) <= max_width:
                candidates.append((abs(widths[0] - widths[1]), [left, right]))
    return min(candidates, default=(0, []), key=lambda item: item[0])[1]


def _fit_title(draw, title, font_path, max_width=1130):
    for size in range(150, 67, -2):
        font = _font(font_path, size)
        lines = _balanced_lines(draw, title, font, max_width)
        if lines:
            return font, lines
    raise RuntimeError(f"書名過長，無法放入兩行封面標題：{title}")


def to_traditional(text):
    if not text:
        return ""
    try:
        import opencc
        return opencc.OpenCC("s2tw").convert(str(text))
    except Exception:
        return str(text)


def to_simplified(text):
    if not text:
        return ""
    try:
        import opencc
        return opencc.OpenCC("t2s").convert(str(text))
    except Exception:
        return str(text)


def font_has_glyphs(font_path, text):
    if not Path(font_path).is_file():
        return False
    try:
        from fontTools.ttLib import TTFont
        font = TTFont(str(font_path))
        cmap = font.getBestCmap()
        if not cmap:
            return False
        for char in text:
            if ord(char) not in cmap:
                return False
        return True
    except Exception:
        return False


def _draw_layer(draw, xy, text, font, fill, stroke_width=0, stroke_fill=None):
    draw.text(xy, text, font=font, fill=fill, stroke_width=stroke_width, stroke_fill=stroke_fill)


def render_viral_cover(bg_img, book_title, start_chap, end_chap, is_completed=True,
                       output_filename="youtube_cover.png", part_num=None):
    width, height = YOUTUBE_COVER_SIZE
    typography = _settings()
    configured_font = PROJECT_ROOT / str(typography.get("title_font") or "fonts/YujiBoku.ttf")
    image = bg_img.convert("RGB").resize((width, height), Image.Resampling.LANCZOS).convert("RGBA")

    clean_title = book_title.replace("《", "").replace("》", "").strip()
    trad_title = to_traditional(clean_title)

    status_text = "已完結"
    selected_title = trad_title
    selected_font = configured_font

    check_text = trad_title + (status_text if is_completed else "")
    if not font_has_glyphs(configured_font, check_text):
        # 只要首選字型缺字，整張封面全套統一改用 MaShanZheng.ttf
        selected_font = PROJECT_ROOT / "fonts" / "MaShanZheng.ttf"
        if not font_has_glyphs(selected_font, selected_title):
            selected_title = to_simplified(clean_title)
        if is_completed:
            status_text = "已完结" if not font_has_glyphs(selected_font, "已完結") else "已完結"

    draw = ImageDraw.Draw(image)
    title_font, lines = _fit_title(draw, selected_title, selected_font)
    line_step = int(title_font.size * .86)
    block_height = line_step * len(lines)
    # Match the approved reference: the title sits in the lower half, with a
    # substantial clean margin below it instead of touching the thumbnail edge.
    start_y = height - block_height - TITLE_BOTTOM_SAFE_MARGIN

    # One blurred black shadow behind the deterministic multi-stroke title.
    shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    positioned = []
    for index, line in enumerate(lines):
        line_width = _width(draw, line, title_font, 14)
        x = (width - line_width) // 2
        y = start_y + index * line_step
        positioned.append((x, y, line))
        shadow_color = _rgb(typography.get("title_shadow"), (0, 0, 0)) + (235,)
        shadow_draw.text((x + 8, y + 10), line, font=title_font, fill=shadow_color, stroke_width=17, stroke_fill=shadow_color)
    shadow = shadow.filter(ImageFilter.GaussianBlur(5))
    image = Image.alpha_composite(image, shadow)
    draw = ImageDraw.Draw(image)
    for x, y, line in positioned:
        # Approved title style: pale cream-gold face, warm-gold inner edge,
        # near-black outer edge and black blurred shadow.
        outer = _rgb(typography.get("title_outer_stroke"), (12, 8, 6)) + (255,)
        inner = _rgb(typography.get("title_inner_stroke"), (190, 128, 43)) + (255,)
        face = _rgb(typography.get("title_face"), (255, 244, 208)) + (255,)
        _draw_layer(draw, (x, y), line, title_font, inner, 14, outer)
        _draw_layer(draw, (x, y), line, title_font, face, 5, inner)

    if is_completed:
        status_font = _font(selected_font, max(68, title_font.size - 70))
        status_box = draw.textbbox((0, 0), status_text, font=status_font, stroke_width=10)
        status_width = status_box[2] - status_box[0]
        status_x = width - status_width - 30
        status_y = 24
        status_shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
        status_shadow_draw = ImageDraw.Draw(status_shadow)
        shadow_color = _rgb(typography.get("title_shadow"), (0, 0, 0)) + (235,)
        status_shadow_draw.text(
            (status_x + 5, status_y + 6), status_text, font=status_font,
            fill=shadow_color, stroke_width=11, stroke_fill=shadow_color,
        )
        image = Image.alpha_composite(image, status_shadow.filter(ImageFilter.GaussianBlur(4)))
        draw = ImageDraw.Draw(image)
        # Apply the reference's top-right badge verbatim: red face, thick white
        # border and a final black edge/shadow, without a filled badge panel.
        _draw_layer(draw, (status_x, status_y), status_text, status_font, (255, 255, 255, 255), 11, (5, 5, 5, 255))
        _draw_layer(draw, (status_x, status_y), status_text, status_font, (205, 24, 32, 255), 5, (255, 255, 255, 255))

    output = os.path.abspath(output_filename)
    os.makedirs(os.path.dirname(output), exist_ok=True)
    extension = Path(output).suffix.lower()
    temporary = output + ".tmp"
    if extension == ".png":
        image.convert("RGB").save(temporary, "PNG", optimize=False)
    else:
        # Preserve the best quality that still satisfies YouTube's thumbnail API limit.
        rgb = image.convert("RGB")
        encoded = None
        for quality in (98, 96, 94, 92, 90, 88, 85, 82, 78, 74, 70, 65, 60):
            buffer = BytesIO()
            rgb.save(buffer, "JPEG", quality=quality, subsampling=0, optimize=True)
            if buffer.tell() < 2 * 1024 * 1024:
                encoded = buffer.getvalue()
                break
        if encoded is None:
            raise RuntimeError("封面無法在維持 1280×720 的前提下壓縮至 YouTube 2 MB 限制內")
        with open(temporary, "wb") as handle:
            handle.write(encoded)
    os.replace(temporary, output)
    return output
