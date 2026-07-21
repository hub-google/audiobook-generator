import os
import sys
import re
import logging
from PIL import Image, ImageDraw, ImageFont, ImageEnhance

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MetadataGen] %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()]
)

FONT_PATHS = [
    r"C:\Windows\Fonts\msjhbd.ttc",   # 微軟正黑體 粗體
    r"C:\Windows\Fonts\msjh.ttc",     # 微軟正黑體
    r"C:\Windows\Fonts\msyhbd.ttc",   # 微軟雅黑 粗體
    r"C:\Windows\Fonts\msyh.ttc",     # 微軟雅黑
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
]

def get_font(size):
    for font_path in FONT_PATHS:
        if os.path.exists(font_path):
            try:
                return ImageFont.truetype(font_path, size)
            except Exception:
                continue
    return ImageFont.load_default()

def generate_video_title(book_title, start_chap=1, end_chap=2400):
    return f"《{book_title}》| 已完結 | 第 {start_chap}~{end_chap} 章 (超長有聲小說全集)"

def generate_video_description(book_title, start_chap=1, end_chap=2400, sample_text=""):
    plot_summary = ""
    if sample_text:
        sentences = [s.strip() for s in re.split(r'[。！!？?\n]', sample_text) if len(s.strip()) > 10]
        if sentences:
            plot_summary = "。".join(sentences[:3]) + "。"

    if not plot_summary:
        plot_summary = f"《{book_title}》講述了一段波瀾壯闊的傳奇故事，精彩章節連播不間斷，帶您沉浸式體驗有聲小說的無限魅力。"

    desc = f"""【超長有聲小說大合集】《{book_title}》全集收聽

📖 小說名稱：《{book_title}》
📌 包含章節：第 {start_chap} 章 至 第 {end_chap} 章 (全集完結)
🎧 播放長度：完整連續播放無中斷

【故事簡介與劇情大綱】：
{plot_summary}

💡 提示：本影片由全自動 AI 有聲書系統自動生成與排版，歡迎訂閱、點讚與分享！
"""
    return desc.strip()

def generate_youtube_cover(book_title, start_chap=1, end_chap=2400, output_path="youtube_cover.jpg"):
    width, height = 1280, 720
    # Create dark gradient background
    img = Image.new("RGB", (width, height), color=(18, 24, 38))
    draw = ImageDraw.Draw(img)

    # Decorative borders & inner glow box
    draw.rectangle([30, 30, width - 30, height - 30], outline=(212, 175, 55), width=4)
    draw.rectangle([45, 45, width - 45, height - 45], outline=(100, 120, 160), width=2)

    # Title font
    title_font = get_font(72)
    sub_font = get_font(42)
    tag_font = get_font(32)

    # Main Book Title
    draw.text((width // 2, 220), f"《{book_title}》", font=title_font, fill=(255, 235, 170), anchor="mm")

    # Status Tag Box
    tag_text = "【 已完結 ‧ 超長有聲書全集 】"
    draw.text((width // 2, 340), tag_text, font=sub_font, fill=(212, 175, 55), anchor="mm")

    # Chapter range text
    chap_text = f"收錄範圍：第 {start_chap} 章 ～ 第 {end_chap} 章"
    draw.text((width // 2, 450), chap_text, font=sub_font, fill=(220, 230, 245), anchor="mm")

    # Footer note
    draw.text((width // 2, 600), "高清音質 ‧ 無縫連播 ‧ 免費收聽", font=tag_font, fill=(160, 180, 200), anchor="mm")

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    img.save(output_path, quality=95)
    logging.info(f"✅ 已成功生成 YouTube 高清封面: {output_path}")
    return output_path

if __name__ == "__main__":
    t = generate_video_title("凡人修仙傳", 1, 2442)
    d = generate_video_description("凡人修仙傳", 1, 2442)
    c = generate_youtube_cover("凡人修仙傳", 1, 2442, "test_cover.jpg")
    print("Title:", t)
    print("Description:\n", d)
    print("Cover generated at:", c)
