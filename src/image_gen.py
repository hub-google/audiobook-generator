try:
    from .source_identity import workspace_name
except ImportError:
    from source_identity import workspace_name

import glob
import logging
import os
import re
import sys

import yaml
from PIL import Image, ImageDraw, ImageFont
try:
    from .artifact_validation import ArtifactValidationError, validate_image
except ImportError:
    from artifact_validation import ArtifactValidationError, validate_image

SRC_DIR = os.path.dirname(os.path.abspath(__file__))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

FONT_PATHS = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msjhbd.ttc",
    r"C:\Windows\Fonts\msjh.ttc",
]


def load_config():
    config_path = os.path.join(SRC_DIR, "..", "config.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def parse_chapter_num(filename):
    match = re.search(r"chapter_(\d+)", filename)
    return int(match.group(1)) if match else 9999


def get_chapter_title(workspace_dir, book_title, chap_num):
    raw_path = os.path.join(workspace_dir, "RawText", f"{book_title}_chapter_{chap_num}_raw.txt")
    if os.path.exists(raw_path):
        try:
            with open(raw_path, "r", encoding="utf-8") as f:
                first_line = f.readline().strip()
            if first_line:
                return first_line
        except OSError:
            pass
    return f"第{chap_num}章"


def get_font(size):
    for font_path in FONT_PATHS:
        if os.path.exists(font_path):
            try:
                return ImageFont.truetype(font_path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _chinese_number(number):
    digits = "零一二三四五六七八九"
    if number < 10:
        return digits[number]
    if number < 20:
        return "十" + (digits[number % 10] if number % 10 else "")
    if number < 100:
        return digits[number // 10] + "十" + (digits[number % 10] if number % 10 else "")
    return str(number)


def _chapter_label(chap_num):
    return f"第{_chinese_number(int(chap_num))}章"


def _clean_chapter_title(chapter_title, chap_num):
    title = (chapter_title or "").strip()
    title = re.sub(r"^第\s*[零〇一二三四五六七八九十百千兩0-9]+\s*[章回集]\s*[:：、.．\-—]?\s*", "", title)
    return title or _chapter_label(chap_num)


def _text_width(draw, text, font):
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def _fit_title(draw, text, max_width, max_lines=2):
    for size in range(70, 37, -2):
        font = get_font(size)
        if _text_width(draw, text, font) <= max_width:
            return font, [text]
        for split in range(1, len(text)):
            lines = [text[:split].strip(), text[split:].strip()]
            if all(lines) and max(_text_width(draw, line, font) for line in lines) <= max_width:
                balance = abs(_text_width(draw, lines[0], font) - _text_width(draw, lines[1], font))
                if split == min(range(1, len(text)), key=lambda i: abs(i - len(text) / 2)) or balance < max_width * .18:
                    return font, lines[:max_lines]
    return get_font(38), [text[: max(1, len(text) // 2)], text[max(1, len(text) // 2):]]


def generate_title_card(book_title, chap_num, chapter_title, output_path):
    """沿用舊版深靛藍標題卡，只重排移除摘要後的上半部。"""
    width, height = 1280, 720
    image = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(image)
    for y in range(height):
        t = y / height
        draw.line([(0, y), (width, y)], fill=(int(6 + 10*t), int(10 + 14*t), int(28 + 24*t)))

    gold = (212, 175, 55)
    white = (248, 249, 252)
    book_text = f"《{book_title.strip('《》')}》"
    book_font = get_font(30)
    draw.text(((width - _text_width(draw, book_text, book_font)) / 2, 48), book_text, font=book_font, fill=gold)

    title = _clean_chapter_title(chapter_title, chap_num)
    title_font, lines = _fit_title(draw, title, width - 220)
    line_height = title_font.size + 12 if hasattr(title_font, "size") else 66
    block_height = len(lines) * line_height
    start_y = 112 + max(0, (135 - block_height) / 2)
    for index, line in enumerate(lines):
        x = (width - _text_width(draw, line, title_font)) / 2
        y = start_y + index * line_height
        draw.text((x + 3, y + 4), line, font=title_font, fill=(0, 0, 0))
        draw.text((x, y), line, font=title_font, fill=white)

    # 恢復舊版的長金色分隔線語彙，但只保留一條，不再形成摘要框。
    separator_y = 265
    draw.line([(190, separator_y), (1090, separator_y)], fill=(132, 105, 38), width=1)
    draw.line([(255, separator_y + 3), (1025, separator_y + 3)], fill=gold, width=2)

    chapter_text = _chapter_label(chap_num)
    chapter_font = get_font(44)
    chapter_x = (width - _text_width(draw, chapter_text, chapter_font)) / 2
    draw.text((chapter_x + 2, 307), chapter_text, font=chapter_font, fill=(0, 0, 0))
    draw.text((chapter_x, 305), chapter_text, font=chapter_font, fill=gold)

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    partial_path = output_path + ".tmp"
    image.save(partial_path, "JPEG", quality=92, optimize=True)
    validate_image(partial_path, expected_size=(width, height))
    os.replace(partial_path, output_path)
    logging.info("[ImageGen] 已生成無摘要章節標題卡: %s", os.path.basename(output_path))
    return True


def run_image_gen(target_indices=None):
    """依 Audio 章節檔生成標題卡，不讀取或生成章節摘要。"""
    config = load_config()
    book_title = config.get("book_title", "UnknownBook")
    workspace_dir = os.path.abspath(os.path.join(SRC_DIR, "..", config["paths"]["workspace_base"], workspace_name(config)))
    audio_dir = os.path.join(workspace_dir, "Audio")
    images_dir = os.path.join(workspace_dir, "Images")
    os.makedirs(images_dir, exist_ok=True)

    wav_files = [p for p in sorted(glob.glob(os.path.join(audio_dir, "*.wav")), key=lambda p: parse_chapter_num(os.path.basename(p))) if "_tmp_part_" not in os.path.basename(p)]
    if target_indices is not None:
        targets = {int(value) for value in target_indices}
        wav_files = [p for p in wav_files if parse_chapter_num(os.path.basename(p)) in targets]
    if not wav_files:
        logging.warning("[ImageGen] 沒有符合條件的章節 WAV，略過圖片生成。")
        return

    generated = skipped = 0
    for wav_path in wav_files:
        chap_num = parse_chapter_num(os.path.basename(wav_path))
        output_path = os.path.join(images_dir, f"{book_title}_chapter_{chap_num}.jpg")
        if os.path.exists(output_path):
            try:
                validate_image(output_path)
                skipped += 1
                continue
            except (ArtifactValidationError, OSError, ValueError):
                logging.warning("[ImageGen] Existing image is invalid; rebuilding: %s", output_path)
        generate_title_card(book_title, chap_num, get_chapter_title(workspace_dir, book_title, chap_num), output_path)
        generated += 1
    logging.info("[ImageGen] 完成。生成=%d，沿用=%d。", generated, skipped)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_image_gen()
