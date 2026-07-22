import os
import sys
import glob
import re
import yaml
import logging
from PIL import Image, ImageDraw, ImageFont

SRC_DIR = os.path.dirname(os.path.abspath(__file__))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from summary_gen import get_or_generate_chapter_summary

# 字型路徑（支援 Windows 與 Linux / GitHub Actions）
FONT_PATHS = [
    # Linux (Ubuntu / GitHub Actions)
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    # Windows 內建
    r"C:\Windows\Fonts\msyh.ttc",      # Microsoft YaHei
    r"C:\Windows\Fonts\msjh.ttc",      # Microsoft JhengHei（繁體）
    r"C:\Windows\Fonts\simsun.ttc",    # SimSun fallback
    r"C:\Windows\Fonts\mingliu.ttc",   # 細明體
]


def load_config():
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def parse_chapter_num(filename):
    """從檔名解析章節號碼，回傳整數。"""
    m = re.search(r'chapter_(\d+)', filename)
    if m:
        return int(m.group(1))
    return 9999


def get_chapter_title(workspace_dir, book_title, chap_num):
    """
    嘗試從 RawText 讀取章節標題（第一行）。
    找不到時回傳預設 '第N章'。
    """
    raw_path = os.path.join(workspace_dir, "RawText", f"{book_title}_chapter_{chap_num}_raw.txt")
    if os.path.exists(raw_path):
        try:
            with open(raw_path, "r", encoding="utf-8") as f:
                first_line = f.readline().strip()
            if first_line:
                return first_line
        except Exception:
            pass
    return f"第{chap_num}章"


def get_font(size):
    """取得可用的中文字型，回傳 PIL ImageFont 物件。"""
    for font_path in FONT_PATHS:
        if os.path.exists(font_path):
            try:
                return ImageFont.truetype(font_path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def generate_title_card(book_title, chap_num, chapter_title, output_path, summary_text=""):
    """
    用 Pillow 產生 1280×720 的章節標題卡圖片。
    版面設計：
      - 深色高雅漸層背景
      - 頂部裝飾金線 (y=140)
      - 上方：書名（金色，y=165）
      - 中央：章節標題（白色大字 + 陰影，y=245）
      - 中間分割金線 (y=370)
      - 中下：50字內 AI 劇情摘要 (柔和白/銀色，y=405~515)
      - 底部安全區 (y=530~720) 留空，避免被 YouTube CC 字幕遮擋
    """
    W, H = 1280, 720

    # ── 背景：深靛藍至近黑漸層 ──
    img = Image.new("RGB", (W, H))
    draw = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        r = int(5  + t * 10)
        g = int(8  + t * 12)
        b = int(30 + t * 20)
        draw.line([(0, y), (W, y)], fill=(r, g, b))

    gold = (212, 175, 55)

    # ── 1. 頂部金線與書名 ──
    line_y_top = 140
    draw.line([(120, line_y_top), (W - 120, line_y_top)], fill=gold, width=1)

    font_title = get_font(48)
    title_text = book_title
    try:
        bbox = draw.textbbox((0, 0), title_text, font=font_title)
        tw = bbox[2] - bbox[0]
    except AttributeError:
        tw, _ = draw.textsize(title_text, font=font_title)
    draw.text(((W - tw) // 2, line_y_top + 20), title_text, font=font_title, fill=gold)

    # ── 2. 中央章節標題 ──
    font_chap = get_font(68)
    chap_text = chapter_title
    try:
        bbox = draw.textbbox((0, 0), chap_text, font=font_chap)
        cw = bbox[2] - bbox[0]
        ch = bbox[3] - bbox[1]
    except AttributeError:
        cw, ch = draw.textsize(chap_text, font=font_chap)

    if cw > W - 200:
        font_chap = get_font(48)
        try:
            bbox = draw.textbbox((0, 0), chap_text, font=font_chap)
            cw = bbox[2] - bbox[0]
            ch = bbox[3] - bbox[1]
        except AttributeError:
            cw, ch = draw.textsize(chap_text, font=font_chap)

    cx = (W - cw) // 2
    cy = 245
    draw.text((cx + 3, cy + 3), chap_text, font=font_chap, fill=(0, 0, 0, 180))
    draw.text((cx, cy), chap_text, font=font_chap, fill=(255, 255, 255))

    # ── 3. 中間分隔金線 ──
    line_y_middle = 370
    draw.line([(140, line_y_middle), (W - 140, line_y_middle)], fill=gold, width=1)

    # ── 4. AI 劇情摘要 (避開底部字幕區，擺放於 y=405~515) ──
    if summary_text:
        font_sum = get_font(28)
        sum_color = (220, 225, 235)
        
        # 進行自動分行 (每行最多約 34 字)
        max_chars_per_line = 34
        lines = []
        for i in range(0, len(summary_text), max_chars_per_line):
            lines.append(summary_text[i:i + max_chars_per_line])
        
        # 最多顯示 2 行
        lines = lines[:2]
        
        start_y = 405
        for idx, line in enumerate(lines):
            try:
                bbox = draw.textbbox((0, 0), line, font=font_sum)
                lw = bbox[2] - bbox[0]
            except AttributeError:
                lw, _ = draw.textsize(line, font=font_sum)
            
            lx = (W - lw) // 2
            ly = start_y + idx * 40
            # 陰影 + 本文
            draw.text((lx + 2, ly + 2), line, font=font_sum, fill=(0, 0, 0, 150))
            draw.text((lx, ly), line, font=font_sum, fill=sum_color)

    img.save(output_path, "JPEG", quality=92)
    logging.info(f"[ImageGen] ✓ Generated title card: {os.path.basename(output_path)}")
    return True


def run_image_gen():
    """
    掃描 Audio/ 目錄中所有章節 WAV，為每一章產生對應的 title_card_chapter_N.jpg。
    包含自動生成 50 字 AI 劇情摘要並排版至圖片中。
    """
    config = load_config()
    book_title = config.get("book_title", "UnknownBook")

    workspace_dir = os.path.abspath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..",
        config['paths']['workspace_base'], book_title
    ))
    audio_dir  = os.path.join(workspace_dir, "Audio")
    images_dir = os.path.join(workspace_dir, "Images")
    os.makedirs(images_dir, exist_ok=True)

    # 找所有章節 WAV（排除 _tmp_part_ 暫存檔）
    all_wavs = sorted(
        glob.glob(os.path.join(audio_dir, "*.wav")),
        key=lambda p: parse_chapter_num(os.path.basename(p))
    )
    wav_files = [w for w in all_wavs if "_tmp_part_" not in os.path.basename(w)]

    if not wav_files:
        logging.warning("[ImageGen] No chapter WAV files found in Audio/. Skipping title card generation.")
        return

    logging.info(f"[ImageGen] Found {len(wav_files)} chapter(s). Generating title cards with AI summaries...")

    generated = 0
    skipped   = 0
    for wav_path in wav_files:
        chap_num = parse_chapter_num(os.path.basename(wav_path))
        out_path = os.path.join(images_dir, f"{book_title}_chapter_{chap_num}.jpg")

        # 取得或自動生成章節 AI 摘要
        summary_text = get_or_generate_chapter_summary(workspace_dir, book_title, chap_num)

        chapter_title = get_chapter_title(workspace_dir, book_title, chap_num)
        ok = generate_title_card(book_title, chap_num, chapter_title, out_path, summary_text=summary_text)
        if ok:
            generated += 1

    logging.info(f"[ImageGen] ✓ Done. Generated={generated}, Skipped={skipped}.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_image_gen()
