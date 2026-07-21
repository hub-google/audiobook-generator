import os
import sys
import urllib.parse
import re
import requests
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont, ImageEnhance

# 設定 UTF-8 輸出
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')

# ---------------------------------------------------------
# 1. 字型載入函數 (Windows / Linux 通用)
# ---------------------------------------------------------
FONT_PATHS = [
    r"C:\Windows\Fonts\msjhbd.ttc",   # 微軟正黑體 粗體
    r"C:\Windows\Fonts\msjh.ttc",     # 微軟正黑體
    r"C:\Windows\Fonts\msyhbd.ttc",   # 微軟雅黑 粗體
    r"C:\Windows\Fonts\msyh.ttc",     # 微軟雅黑
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", # Linux
]

def get_font(size):
    for font_path in FONT_PATHS:
        if os.path.exists(font_path):
            try:
                return ImageFont.truetype(font_path, size)
            except Exception:
                continue
    return ImageFont.load_default()

# ---------------------------------------------------------
# 2. 純劇情過濾器：過濾出版/作者/年份等元數據
# ---------------------------------------------------------
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

# ---------------------------------------------------------
# 3. 免費搜尋小說真實劇情簡介 (不需 API KEY)
# ---------------------------------------------------------
def fetch_book_summary_online(book_title):
    print(f"[1/4] 正在連線網路搜尋《{book_title}》的「純劇情大綱與故事背景」...")
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
        print(f"--> [維基百科搜尋略過]: {e}")
        
    # 嘗試 2: DuckDuckGo 搜尋「小說 劇情簡介」
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
        pure_plot = f"講述《{book_title}》主角踏上充滿考驗與驚險的奇幻冒險旅程，展現壯麗的世界觀與英雄傳奇。"
        
    print(f"--> [已精準提取純劇情簡介]:\n    {pure_plot}\n")
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
            print(f"--> [自動翻譯純劇情英文]:\n    {english_plot}\n")
    except Exception as e:
        print(f"--> [翻譯 API 略過]: {e}")

    if not english_plot:
        english_plot = f"Heroic fantasy storyline for novel '{book_title}'"

    final_prompt = (
        f"Masterpiece anime illustration depicting the story plot: {english_plot}. "
        "Heroic protagonist, epic atmospheric background matching the plot, cinematic lighting, dynamic pose, 8k 4k UHD resolution, ultra sharp focus, clean artwork, no text, no watermark"
    )
    
    return pure_plot, english_plot, final_prompt

# ---------------------------------------------------------
# 4. 連線 Pollinations.ai 下載 2K 超高畫質底圖
# ---------------------------------------------------------
def download_ai_image(prompt, width=2560, height=1440):
    print(f"[2/4] 連線 AI 繪圖伺服器 (Flux 模型) 依劇情生成 2K 底圖 ({width}x{height})...")
    
    encoded_prompt = urllib.parse.quote(prompt)
    url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width={width}&height={height}&model=flux&nologo=true&enhance=true"
    
    headers = {"User-Agent": "Mozilla/5.0"}
    res = requests.get(url, headers=headers, timeout=90)
    
    if res.status_code == 200:
        print("[OK] 成功從 AI 繪圖 API 下載超高畫質底圖！")
        bg_path = "temp_ai_bg.jpg"
        with open(bg_path, "wb") as f:
            f.write(res.content)
            
        img = Image.open(bg_path).convert("RGB")
        
        # 裁切掉底部 4% 雜訊區域
        w, h = img.size
        crop_h = int(h * 0.04)
        img = img.crop((0, 0, w, h - crop_h)).resize((width, height), Image.LANCZOS)
        
        # 銳利化與對比度微調
        img = ImageEnhance.Sharpness(img).enhance(1.2)
        img = ImageEnhance.Contrast(img).enhance(1.05)
        return img
    else:
        raise Exception(f"AI 生圖失敗，HTTP 狀態碼: {res.status_code}")

# ---------------------------------------------------------
# 5. Pillow 自動合成高解析度大字體、章節範圍與完結徽章
# ---------------------------------------------------------
def create_youtube_cover(
    bg_img, 
    book_title, 
    start_chap, 
    end_chap, 
    is_completed=True, 
    output_filename="cover_output.jpg"
):
    print("[3/4] 正在合成超高解析度封面（自動等比例縮放字型與繪製遮罩）...")
    
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
    
    # Helper: 繪製含粗描邊的文字
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
    
    # 儲存最終圖片
    img.save(output_filename, quality=100, subsampling=0)
    print(f"[3/4] 2K 超高畫質封面合成完畢！已儲存至：{os.path.abspath(output_filename)}")
    return output_filename

# ---------------------------------------------------------
# 6. 自動輸出生成過程 TXT 紀錄檔 (存至 Cover/ 資料夾)
# ---------------------------------------------------------
def save_process_log(output_dir, book_title, pure_plot, english_plot, final_prompt, img_width, img_height):
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
        
    print(f"[4/4] 成功導出生成過程 TXT 記錄檔：{os.path.abspath(log_filename)}")
    return log_filename

# ---------------------------------------------------------
# 主執行區塊 (產出至 Workspace/Cover 資料夾)
# ---------------------------------------------------------
if __name__ == "__main__":
    print("=" * 60)
    print("100% Python 全自動網路搜尋劇情簡介 & Cover 資料夾產出腳本")
    print("=" * 60)
    
    # =========================================================
    # 小說參數設定
    # =========================================================
    BOOK_TITLE = "凡人修仙傳"       # 小說名稱
    START_CHAP = 1                # 起始章節
    END_CHAP = 50                 # 結束章節
    IS_COMPLETED = True           # 是否已完結 (True: 已完結, False: 連載中)
    
    # 解析度設定 (2560x1440 2K QHD 超高畫質)
    IMG_WIDTH = 2560
    IMG_HEIGHT = 1440
    
    # 建立 Workspace 下的 Cover 資料夾
    COVER_DIR = os.path.join(os.getcwd(), "Cover")
    os.makedirs(COVER_DIR, exist_ok=True)
    
    COVER_IMAGE_FILE = os.path.join(COVER_DIR, f"{BOOK_TITLE}_cover.jpg")
    # =========================================================
    
    try:
        # 1. 自動抓取與過濾劇情，組裝 Prompt
        pure_plot, english_plot, final_prompt = auto_generate_prompt_from_summary(BOOK_TITLE)
        
        # 2. 下載 AI 高畫質底圖
        bg_img = download_ai_image(final_prompt, width=IMG_WIDTH, height=IMG_HEIGHT)
        
        # 3. 疊加高解析度文字與徽章，存入 Cover/ 資料夾
        out_image = create_youtube_cover(
            bg_img=bg_img,
            book_title=BOOK_TITLE,
            start_chap=START_CHAP,
            end_chap=END_CHAP,
            is_completed=IS_COMPLETED,
            output_filename=COVER_IMAGE_FILE
        )
        
        # 4. 自動導出 4 個步驟的過程 TXT 檔至 Cover/ 資料夾
        out_log = save_process_log(
            output_dir=COVER_DIR,
            book_title=BOOK_TITLE,
            pure_plot=pure_plot,
            english_plot=english_plot,
            final_prompt=final_prompt,
            img_width=IMG_WIDTH,
            img_height=IMG_HEIGHT
        )
        
        print("\n" + "=" * 60)
        print(f"[完成] 所有產出已成功存入 Cover 資料夾：")
        print(f" 1. 封面圖片: {os.path.abspath(out_image)}")
        print(f" 2. 過程紀錄: {os.path.abspath(out_log)}")
        print("=" * 60)
        
    except Exception as e:
        print(f"\n[ERROR] 發生錯誤: {e}")
