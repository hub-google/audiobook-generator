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
    版面設計（完美避開字幕與壓線）：
      - 深靛藍高雅漸層背景
      - 頂部：書名（金色，y=45）
      - 上中央：章節標題（白色大字 + 雙重陰影，y=95）
      - 頂部分割金線 (y=165，留足緩衝避免壓線)
      - 中央：【 本章劇情大綱 】標題 (y=195，移除 emoji 避免字型缺字豆腐塊)
      - 中下：50字內 AI 劇情摘要 (柔和白/銀色，y=235~375)
      - 底部分割金線 (y=395)
      - 底部 (y=400~720) 留出 320px 寬敞安全區，專供 CC 字幕與硬字幕內嵌
    """
    W, H = 1280, 720

    # ── 背景：深靛藍至暗藍漸層 ──
    img = Image.new("RGB", (W, H))
    draw = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        r = int(6  + t * 10)
        g = int(10 + t * 14)
        b = int(28 + t * 24)
        draw.line([(0, y), (W, y)], fill=(r, g, b))

    gold = (212, 175, 55)

    # ── 1. 書名 (y = 45) ──
    font_title = get_font(30)
    title_text = f"《{book_title}》"
    try:
        bbox = draw.textbbox((0, 0), title_text, font=font_title)
        tw = bbox[2] - bbox[0]
    except AttributeError:
        tw, _ = draw.textsize(title_text, font=font_title)
    draw.text(((W - tw) // 2, 45), title_text, font=font_title, fill=gold)

    # ── 2. 中央章節標題 (y = 95) ──
    font_chap = get_font(46)
    chap_text = chapter_title
    try:
        bbox = draw.textbbox((0, 0), chap_text, font=font_chap)
        cw = bbox[2] - bbox[0]
    except AttributeError:
        cw, _ = draw.textsize(chap_text, font=font_chap)

    if cw > W - 180:
        font_chap = get_font(36)
        try:
            bbox = draw.textbbox((0, 0), chap_text, font=font_chap)
            cw = bbox[2] - bbox[0]
        except AttributeError:
            cw, _ = draw.textsize(chap_text, font=font_chap)

    cx = (W - cw) // 2
    cy = 95
    # 立體深色陰影 + 主字
    draw.text((cx + 2, cy + 2), chap_text, font=font_chap, fill=(0, 0, 0, 220))
    draw.text((cx, cy), chap_text, font=font_chap, fill=(255, 255, 255))

    # 頂部分割金線 (y = 165，與標題下方保留 20px 空間，避免壓線)
    line_y_top = 165
    draw.line([(160, line_y_top), (W - 160, line_y_top)], fill=gold, width=2)

    # ── 3. 中央 AI 劇情摘要區 (y = 195 ~ 380) ──
    if not summary_text:
        summary_text = f"【本章大綱】《{book_title}》第 {chap_num} 章精彩故事劇情演繹。"

    # 摘要標頭 (移除 emoji 避免中文字型顯示豆腐塊 box)
    font_sum_header = get_font(22)
    sum_header_text = "【 本章劇情大綱 】"
    try:
        bbox = draw.textbbox((0, 0), sum_header_text, font=font_sum_header)
        hw = bbox[2] - bbox[0]
    except AttributeError:
        hw, _ = draw.textsize(sum_header_text, font=font_sum_header)
    draw.text(((W - hw) // 2, 195), sum_header_text, font=font_sum_header, fill=(220, 185, 90))

    # 摘要內容 (每行最多 32 字，行距 38px)
    font_sum = get_font(24)
    sum_color = (230, 235, 245)
    
    max_chars_per_line = 32
    lines = []
    for i in range(0, len(summary_text), max_chars_per_line):
        lines.append(summary_text[i:i + max_chars_per_line])
    lines = lines[:4]

    start_y = 235
    for idx, line in enumerate(lines):
        try:
            bbox = draw.textbbox((0, 0), line, font=font_sum)
            lw = bbox[2] - bbox[0]
        except AttributeError:
            lw, _ = draw.textsize(line, font=font_sum)
        
        lx = (W - lw) // 2
        ly = start_y + idx * 38
        # 四週暗黑陰影 + 本文
        for offset_x, offset_y in [(-1, -1), (1, -1), (-1, 1), (1, 1), (0, 2)]:
            draw.text((lx + offset_x, ly + offset_y), line, font=font_sum, fill=(0, 0, 0, 220))
        draw.text((lx, ly), line, font=font_sum, fill=sum_color)

    # 下方分割金線 (y = 395，動態緊貼摘要下方)
    line_y_bottom = start_y + len(lines) * 38 + 15
    if line_y_bottom < 390:
        line_y_bottom = 390
    draw.line([(160, line_y_bottom), (W - 160, line_y_bottom)], fill=gold, width=2)

    # ── 4. 底部字幕專屬保留區 (y = 400 ~ 720 保持淨空) ──

    img.save(output_path, "JPEG", quality=92)
    logging.info(f"[ImageGen] ✓ Generated title card: {os.path.basename(output_path)}")
    return True


def run_image_gen(target_indices=None):
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

    if target_indices is not None:
        wav_files = [w for w in wav_files if parse_chapter_num(os.path.basename(w)) in target_indices]

    if not wav_files:
        logging.info("[ImageGen] No matching chapters in target_indices. Skipping.")
        return

    # 快速評估：如果 target 節的 JPG 全數存在，立刻跳出
    all_jpgs_exist = all(
        os.path.exists(os.path.join(images_dir, f"{book_title}_chapter_{parse_chapter_num(os.path.basename(w))}.jpg"))
        and os.path.getsize(os.path.join(images_dir, f"{book_title}_chapter_{parse_chapter_num(os.path.basename(w))}.jpg")) > 100
        for w in wav_files
    )
    if all_jpgs_exist:
        logging.info(f"[ImageGen] ⚡ All {len(wav_files)} target chapter(s) already have title cards. Skipping.")
        return

    logging.info(f"[ImageGen] Found {len(wav_files)} chapter(s) to process. Generating title cards with AI summaries...")

    generated = 0
    skipped   = 0
    for wav_path in wav_files:
        chap_num = parse_chapter_num(os.path.basename(wav_path))
        out_path = os.path.join(images_dir, f"{book_title}_chapter_{chap_num}.jpg")

        if os.path.exists(out_path) and os.path.getsize(out_path) > 100:
            logging.info(f"[ImageGen] Skipping existing title card: {os.path.basename(out_path)}")
            skipped += 1
            continue

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
