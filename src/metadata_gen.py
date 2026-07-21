import os
import sys
import re
import urllib.parse
import requests
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

def clean_pure_plot_summary(text):
    if not text:
        return ""
    sentences = re.split(r'[。！!？?\n]', text)
    meta_keywords = ["連載", "出版", "出版社", "字數", "改編", "動畫", "影視", "起點", "年", "月", "日", "英譯", "Wuxiaworld", "作者", "繁體", "簡體"]
    clean_sentences = []
    for s in sentences:
        s = s.strip()
        if len(s) < 5:
            continue
        if not any(k in s for k in meta_keywords):
            clean_sentences.append(s)
    if clean_sentences:
        return "。".join(clean_sentences) + "。"
    return text

def fetch_book_summary_online(book_title):
    """依照 test_ai_cover.py 邏輯，從網路搜尋該小說的「整體完整劇情大綱」"""
    logging.info(f"正在搜尋《{book_title}》的整體小說劇情大綱與簡介...")
    raw_summary = ""
    
    # 嘗試 1: 中文維基百科 REST API
    try:
        url = f"https://zh.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(book_title)}"
        headers = {"User-Agent": "Mozilla/5.0"}
        res = requests.get(url, headers=headers, timeout=8)
        if res.status_code == 200:
            extract = res.json().get("extract", "")
            if extract:
                raw_summary = extract
    except Exception as e:
        logging.debug(f"[維基百科略過]: {e}")

    # 嘗試 2: DuckDuckGo 搜尋
    if not raw_summary:
        try:
            ddg_url = f"https://api.duckduckgo.com/?q={urllib.parse.quote(book_title + ' 小說 劇情簡介')}&format=json&no_html=1"
            res = requests.get(ddg_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
            if res.status_code == 200:
                extract = res.json().get("AbstractText", "")
                if extract:
                    raw_summary = extract
        except Exception:
            pass

    pure_plot = clean_pure_plot_summary(raw_summary)
    if not pure_plot:
        pure_plot = f"講述《{book_title}》主角踏上充滿考驗與驚險的冒險旅程，精彩劇情高潮疊起、扣人心弦。"
    
    return pure_plot

def generate_video_title(book_title, start_chap=1, end_chap=2400):
    return f"《{book_title}》| 已完結 | 第 {start_chap}~{end_chap} 章 (超長有聲小說全集)"

def generate_video_description(book_title, start_chap=1, end_chap=2400):
    # 使用 test_ai_cover.py 的整體小說劇情簡介獲取邏輯
    pure_plot = fetch_book_summary_online(book_title)

    desc = f"""【超長有聲小說大合集】《{book_title}》全集收聽

📖 小說名稱：《{book_title}》
📌 包含章節：第 {start_chap} 章 至 第 {end_chap} 章 (全集完結)
🎧 播放長度：完整連續播放無中斷

【故事整體大綱簡介】：
{pure_plot}

💡 提示：本影片由全自動 AI 有聲書系統自動生成與排版，歡迎訂閱、點讚與分享！
"""
    return desc.strip()

def generate_youtube_cover(book_title, start_chap=1, end_chap=2400, output_path="youtube_cover.jpg"):
    width, height = 1280, 720
    img = Image.new("RGB", (width, height), color=(18, 24, 38))
    draw = ImageDraw.Draw(img)

    draw.rectangle([30, 30, width - 30, height - 30], outline=(212, 175, 55), width=4)
    draw.rectangle([45, 45, width - 45, height - 45], outline=(100, 120, 160), width=2)

    title_font = get_font(72)
    sub_font = get_font(42)
    tag_font = get_font(32)

    draw.text((width // 2, 220), f"《{book_title}》", font=title_font, fill=(255, 235, 170), anchor="mm")

    tag_text = "【 已完結 ‧ 超長有聲書全集 】"
    draw.text((width // 2, 340), tag_text, font=sub_font, fill=(212, 175, 55), anchor="mm")

    chap_text = f"收錄範圍：第 {start_chap} 章 ～ 第 {end_chap} 章"
    draw.text((width // 2, 450), chap_text, font=sub_font, fill=(220, 230, 245), anchor="mm")

    draw.text((width // 2, 600), "高清音質 ‧ 無縫連播 ‧ 免費收聽", font=tag_font, fill=(160, 180, 200), anchor="mm")

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    img.save(output_path, quality=95)
    logging.info(f"✅ 已成功生成 YouTube 高清封面: {output_path}")
    return output_path

def save_book_metadata(book_title, start_chap=1, end_chap=2400, workspace_dir=None):
    if not workspace_dir:
        SRC_DIR = os.path.dirname(os.path.abspath(__file__))
        workspace_dir = os.path.abspath(os.path.join(SRC_DIR, "..", "Workspace", book_title))

    os.makedirs(workspace_dir, exist_ok=True)

    title = generate_video_title(book_title, start_chap, end_chap)
    desc = generate_video_description(book_title, start_chap, end_chap)

    title_file = os.path.join(workspace_dir, "youtube_title.txt")
    desc_file = os.path.join(workspace_dir, "youtube_description.txt")
    cover_file = os.path.join(workspace_dir, "youtube_cover.jpg")

    with open(title_file, "w", encoding="utf-8") as f:
        f.write(title)

    with open(desc_file, "w", encoding="utf-8") as f:
        f.write(desc)

    generate_youtube_cover(book_title, start_chap, end_chap, cover_file)

    logging.info(f"📁 本地 Metadata 檔案已全數存入: {workspace_dir}")
    logging.info(f"   • 標題: {title_file}")
    logging.info(f"   • 簡介: {desc_file}")
    logging.info(f"   • 封面: {cover_file}")

    return {
        "title": title,
        "description": desc,
        "title_file": title_file,
        "desc_file": desc_file,
        "cover_file": cover_file
    }

if __name__ == "__main__":
    save_book_metadata("凡人修仙傳", 1, 2442)
