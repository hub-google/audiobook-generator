"""Deterministic YouTube thumbnail typography for viral novel covers."""

import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from io import BytesIO
import yaml


YOUTUBE_COVER_SIZE = (1280, 720)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
TITLE_FONT_PATH = PROJECT_ROOT / "fonts" / "YujiBoku.ttf"
INFO_FONT_PATHS = (
    Path(r"C:\Windows\Fonts\msjhbd.ttc"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
)


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


def _info_font(size):
    for path in INFO_FONT_PATHS:
        if path.is_file():
            return ImageFont.truetype(str(path), size)
    raise RuntimeError("找不到封面資訊用粗體中文字型")


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


def _draw_layer(draw, xy, text, font, fill, stroke_width=0, stroke_fill=None):
    draw.text(xy, text, font=font, fill=fill, stroke_width=stroke_width, stroke_fill=stroke_fill)


def render_viral_cover(bg_img, book_title, start_chap, end_chap, is_completed=True,
                       output_filename="youtube_cover.png", part_num=None):
    width, height = YOUTUBE_COVER_SIZE
    typography = _settings()
    configured_font = PROJECT_ROOT / str(typography.get("title_font") or "fonts/YujiBoku.ttf")
    image = bg_img.convert("RGB").resize((width, height), Image.Resampling.LANCZOS).convert("RGBA")

    # Preserve a rich background while locally calming the title zone.
    shade = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    shade_draw = ImageDraw.Draw(shade)
    start = int(height * .60)
    for y in range(start, height):
        progress = (y - start) / max(1, height - start)
        shade_draw.line((0, y, width, y), fill=(8, 5, 4, int(52 + 92 * progress)))
    image = Image.alpha_composite(image, shade)

    clean_title = book_title.replace("《", "").replace("》", "").strip()
    draw = ImageDraw.Draw(image)
    title_font, lines = _fit_title(draw, clean_title, configured_font)
    line_step = int(title_font.size * .86)
    block_height = line_step * len(lines)
    start_y = height - block_height - 60

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
        # Outer near-black edge, warm-gold inner edge, ivory face.
        outer = _rgb(typography.get("title_outer_stroke"), (12, 8, 6)) + (255,)
        inner = _rgb(typography.get("title_inner_stroke"), (190, 128, 43)) + (255,)
        face = _rgb(typography.get("title_face"), (255, 244, 208)) + (255,)
        _draw_layer(draw, (x, y), line, title_font, inner, 14, outer)
        _draw_layer(draw, (x, y), line, title_font, face, 5, inner)

    badge_text = "全集" if not part_num else f"第{part_num}部"
    badge_font = _info_font(52)
    badge_box = draw.textbbox((0, 0), badge_text, font=badge_font, stroke_width=5)
    badge_w, badge_h = badge_box[2] - badge_box[0], badge_box[3] - badge_box[1]
    bx, by = width - badge_w - 54, 30
    draw.text((bx + 4, by + 5), badge_text, font=badge_font, fill=(0, 0, 0), stroke_width=8, stroke_fill=(255, 255, 255))
    badge_fill = _rgb(typography.get("badge_fill"), (190, 15, 25))
    badge_face = _rgb(typography.get("badge_face"), (255, 255, 255))
    draw.text((bx, by), badge_text, font=badge_font, fill=badge_fill, stroke_width=4, stroke_fill=badge_face)

    range_text = f"第{start_chap}–{end_chap}章 · {'已完結' if is_completed else '連載中'}"
    info_font = _info_font(25)
    draw.text((28, 24), range_text, font=info_font, fill=(255, 255, 255), stroke_width=3, stroke_fill=(8, 8, 8))

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
