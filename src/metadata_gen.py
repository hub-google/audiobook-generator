import os
import sys
import re
import urllib.parse
import requests
import logging
from datetime import datetime
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
    plot_focused = [s for s in sentences if any(k in s for k in ["主角", "故事", "講述", "歷經", "成仙", "冒險", "修行", "少年", "世界"])]
    if plot_focused:
        return "。".join(plot_focused) + "。"
    return text

def fetch_book_summary_online(book_title):
    """從網路搜尋該小說的「純劇情大綱與故事背景」"""
    logging.info(f"正在搜尋《{book_title}》的整體小說劇情大綱與簡介...")
    raw_summary = ""
    
    # 嘗試 1: 中文維基百科 REST API
    try:
        url = f"https://zh.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(book_title)}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
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
        pure_plot = f"講述《{book_title}》主角踏上充滿考驗與驚險的冒險旅程，展現壯麗的世界觀與英雄傳奇。"
    
    return pure_plot

def auto_generate_prompt_from_summary(book_title):
    pure_plot = fetch_book_summary_online(book_title)
    
    english_plot = ""
    try:
        text_to_translate = f"Plot of '{book_title}': {pure_plot[:180]}"
        res = requests.get(
            "https://api.mymemory.translated.net/get",
            params={"q": text_to_translate, "langpair": "zh-TW|en"},
            timeout=10
        )
        if res.status_code == 200:
            english_plot = res.json().get("responseData", {}).get("translatedText", "")
            logging.info(f"--> [自動翻譯純劇情英文]: {english_plot}")
    except Exception as e:
        logging.debug(f"[翻譯 API 略過]: {e}")

    if not english_plot:
        english_plot = f"Heroic fantasy storyline for novel '{book_title}'"

    final_prompt = (
        f"Masterpiece anime illustration depicting the story plot: {english_plot}. "
        "Heroic protagonist, epic atmospheric background matching the plot, cinematic lighting, dynamic pose, 8k 4k UHD resolution, ultra sharp focus, clean artwork, no text, no watermark"
    )
    
    return pure_plot, english_plot, final_prompt

def download_ai_image(prompt, width=2560, height=1440):
    logging.info(f"連線 AI 繪圖伺服器 (Flux 模型) 生成 2K 底圖 ({width}x{height})...")
    encoded_prompt = urllib.parse.quote(prompt)
    url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width={width}&height={height}&model=flux&nologo=true&enhance=true"
    
    headers = {"User-Agent": "Mozilla/5.0"}
    res = requests.get(url, headers=headers, timeout=90)
    
    if res.status_code == 200:
        logging.info("✅ 成功從 AI 繪圖 API 下載超高畫質底圖！")
        bg_path = "temp_ai_bg.jpg"
        with open(bg_path, "wb") as f:
            f.write(res.content)
            
        img = Image.open(bg_path).convert("RGB")
        
        w, h = img.size
        crop_h = int(h * 0.04)
        img = img.crop((0, 0, w, h - crop_h)).resize((width, height), Image.LANCZOS)
        
        img = ImageEnhance.Sharpness(img).enhance(1.2)
        img = ImageEnhance.Contrast(img).enhance(1.05)
        return img
    else:
        raise Exception(f"AI 生圖失敗，HTTP 狀態碼: {res.status_code}")

def create_youtube_cover(
    bg_img, 
    book_title, 
    start_chap, 
    end_chap, 
    is_completed=True, 
    output_filename="youtube_cover.jpg"
):
    logging.info("正在合成 2K 超高解析度封面（自動排版遮罩、字型與徽章）...")
    
    W, H = bg_img.size
    scale = W / 1920.0
    img = bg_img.copy()
    
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    
    # 底部漸暗遮罩
    mask_start = int(H * 0.55)
    solid_black_start = int(H * 0.93)
    
    for y in range(mask_start, H):
        if y >= solid_black_start:
            alpha = 255
        else:
            alpha = int(240 * ((y - mask_start) / (solid_black_start - mask_start)))
        overlay_draw.line([(0, y), (W, y)], fill=(0, 0, 0, alpha))
        
    # 頂部漸暗遮罩
    top_mask_end = int(H * 0.30)
    for y in range(0, top_mask_end):
        alpha = int(170 * (1 - y / top_mask_end))
        overlay_draw.line([(0, y), (W, y)], fill=(0, 0, 0, alpha))
        
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(img)
    
    def draw_text_with_stroke(draw_obj, position, text, font, fill_color, stroke_color=(0, 0, 0), stroke_width=8):
        x, y = position
        for dx in range(-stroke_width, stroke_width + 1):
            for dy in range(-stroke_width, stroke_width + 1):
                if dx*dx + dy*dy <= stroke_width*stroke_width:
                    draw_obj.text((x + dx, y + dy), text, font=font, fill=stroke_color)
        draw_obj.text((x, y), text, font=font, fill=fill_color)

    # (A) 左上角：章節範圍紅色徽章
    badge1_x, badge1_y = int(60 * scale), int(50 * scale)
    if isinstance(start_chap, int) and isinstance(end_chap, int):
        chap_text = f"第 {start_chap:03d} - {end_chap:03d} 集"
    else:
        chap_text = f"第 {start_chap} - {end_chap} 集"
    
    font_badge = get_font(int(52 * scale))
    bbox = draw.textbbox((0, 0), chap_text, font=font_badge)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    
    badge1_w, badge1_h = text_w + int(60 * scale), text_h + int(36 * scale)
    draw.rounded_rectangle(
        [badge1_x, badge1_y, badge1_x + badge1_w, badge1_y + badge1_h], 
        radius=int(20 * scale), 
        fill=(220, 38, 38)
    )
    draw.text((badge1_x + int(30 * scale), badge1_y + int(12 * scale)), chap_text, font=font_badge, fill=(255, 235, 59))
    
    # (B) 右上角：完結狀態徽章
    status_text = "【已完結】" if is_completed else "【連載中】"
    status_bg = (16, 185, 129) if is_completed else (245, 158, 11)
    
    font_status = get_font(int(48 * scale))
    s_bbox = draw.textbbox((0, 0), status_text, font=font_status)
    s_w = s_bbox[2] - s_bbox[0]
    s_h = s_bbox[3] - s_bbox[1]
    
    badge2_w, badge2_h = s_w + int(50 * scale), s_h + int(36 * scale)
    badge2_x = W - int(60 * scale) - badge2_w
    badge2_y = int(50 * scale)
    
    draw.rounded_rectangle(
        [badge2_x, badge2_y, badge2_x + badge2_w, badge2_y + badge2_h], 
        radius=int(20 * scale), 
        fill=status_bg
    )
    draw.text((badge2_x + int(25 * scale), badge2_y + int(12 * scale)), status_text, font=font_status, fill=(255, 255, 255))
    
    # (C) 左下角/底部：小說名稱 (金色大字 + 黑色粗描邊)
    base_font_size = 120 if len(book_title) <= 6 else int(120 * (6 / len(book_title)))
    font_book = get_font(int(base_font_size * scale))
    book_x, book_y = int(60 * scale), H - int(210 * scale)
    
    draw_text_with_stroke(
        draw, 
        (book_x, book_y), 
        book_title, 
        font_book, 
        fill_color=(255, 215, 0),
        stroke_color=(0, 0, 0), 
        stroke_width=int(10 * scale)
    )
    
    os.makedirs(os.path.dirname(os.path.abspath(output_filename)), exist_ok=True)
    img.save(output_filename, quality=100, subsampling=0)
    logging.info(f"✅ 2K 超高畫質封面已合成完成: {output_filename}")
    return output_filename

def save_process_log(output_dir, book_title, pure_plot, english_plot, final_prompt, img_width=2560, img_height=1440):
    log_filename = os.path.join(output_dir, f"{book_title}_process_log.txt")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    log_content = f"""======================================================================
AI 封面全自動生成過程記錄 Log
生成時間: {timestamp}
小說名稱: 《{book_title}》
輸出解析度: {img_width} x {img_height} (2K QHD 超高畫質)
======================================================================

1️⃣ Python 全自動從網路抓取的【純劇情大綱】
----------------------------------------------------------------------
{pure_plot}
(註: 已自動過濾作者名稱、出版年份、連載平台、字數等無關元數據雜訊)

2️⃣ Python 免費翻譯成的【英文劇情】
----------------------------------------------------------------------
"{english_plot}"

3️⃣ Python 最終組裝並發送給 AI 畫師的【完整 Prompt】
----------------------------------------------------------------------
{final_prompt}

4️⃣ 帶入 enhance=true 後，雲端 Flux AI 最終擴充渲染說明
----------------------------------------------------------------------
根據上述專注於《{book_title}》劇情大綱的英文 Prompt，Pollinations AI 的雲端 Flux 繪圖大模型帶入 `enhance=true` 後，自動擴充細節，繪製出符合該小說背景與主角氣質的 2K 超高畫質（{img_width}x{img_height}）動漫風格底圖，並由 Pillow 合成金色大字標題與【已完結】/【集數】徽章。
======================================================================
"""
    with open(log_filename, "w", encoding="utf-8") as f:
        f.write(log_content)
    logging.info(f"📄 已導出生成過程 TXT 記錄檔: {log_filename}")
    return log_filename

def generate_video_title(book_title, start_chap=1, end_chap=2400):
    return f"《{book_title}》| 已完結 | 第 {start_chap}~{end_chap} 章 (超長有聲小說全集)"

def generate_video_description(book_title, start_chap=1, end_chap=2400, pure_plot=None):
    if not pure_plot:
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

def save_book_metadata(book_title, start_chap=1, end_chap=2400, workspace_dir=None, is_completed=True):
    if not workspace_dir:
        SRC_DIR = os.path.dirname(os.path.abspath(__file__))
        workspace_dir = os.path.abspath(os.path.join(SRC_DIR, "..", "Workspace", book_title))

    os.makedirs(workspace_dir, exist_ok=True)

    pure_plot, english_plot, final_prompt = auto_generate_prompt_from_summary(book_title)

    title = generate_video_title(book_title, start_chap, end_chap)
    desc = generate_video_description(book_title, start_chap, end_chap, pure_plot=pure_plot)

    title_file = os.path.join(workspace_dir, "youtube_title.txt")
    desc_file = os.path.join(workspace_dir, "youtube_description.txt")
    cover_file = os.path.join(workspace_dir, "youtube_cover.jpg")

    with open(title_file, "w", encoding="utf-8") as f:
        f.write(title)

    with open(desc_file, "w", encoding="utf-8") as f:
        f.write(desc)

    # 下載 AI 高畫質底圖並合成封面
    bg_img = download_ai_image(final_prompt, width=2560, height=1440)
    create_youtube_cover(bg_img, book_title, start_chap, end_chap, is_completed=is_completed, output_filename=cover_file)

    # 記錄 log
    log_file = save_process_log(workspace_dir, book_title, pure_plot, english_plot, final_prompt)

    logging.info(f"📁 本地 Metadata 檔案已全數存入: {workspace_dir}")
    logging.info(f"   • 標題: {title_file}")
    logging.info(f"   • 簡介: {desc_file}")
    logging.info(f"   • 封面: {cover_file}")
    logging.info(f"   • 紀錄: {log_file}")

    return {
        "title": title,
        "description": desc,
        "title_file": title_file,
        "desc_file": desc_file,
        "cover_file": cover_file,
        "log_file": log_file
    }

if __name__ == "__main__":
    save_book_metadata("凡人修仙傳", 1, 2442)
