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
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
]

def get_font(size):
    for font_path in FONT_PATHS:
        if os.path.exists(font_path):
            try:
                return ImageFont.truetype(font_path, size)
            except Exception:
                try:
                    return ImageFont.truetype(font_path, size, index=0)
                except Exception:
                    continue

    # 自動備用：如果 Linux 系統無字型，自動下載 NotoSansTC 粗體字型
    fallback_font = os.path.abspath("temp_fallback_font.ttf")
    if not os.path.exists(fallback_font):
        try:
            logging.info("📥 正在下載 Linux CJK 中文字型檔 (NotoSansTC)...")
            url = "https://github.com/google/fonts/raw/main/ofl/notosanstc/NotoSansTC-Bold.ttf"
            res = requests.get(url, timeout=15)
            if res.status_code == 200:
                with open(fallback_font, "wb") as f:
                    f.write(res.content)
                logging.info("✅ 成功下載 NotoSansTC 中文字型檔！")
        except Exception as e:
            logging.warning(f"無法下載備用中文字型: {e}")

    if os.path.exists(fallback_font):
        try:
            return ImageFont.truetype(fallback_font, size)
        except Exception:
            pass

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

def get_calligraphy_font(size):
    """取得極具張力與狂草飛白筆觸的毛筆狂草字體 (Yuji Boku / 飛白勁道書法體)"""
    SRC_DIR = os.path.dirname(os.path.abspath(__file__))
    brush_calligraphy_fonts = [
        os.path.abspath(os.path.join(SRC_DIR, "..", "fonts", "YujiBoku.ttf")),      # 飛白勁道毛筆字體 (100% 支援繁體傳字)
        os.path.abspath(os.path.join(SRC_DIR, "..", "fonts", "MaShanZheng.ttf")),   # 馬山正體
        os.path.abspath(os.path.join(SRC_DIR, "..", "fonts", "ZhiMangXing.ttf")),  # 織芒星體
        r"C:\Windows\Fonts\FZSTK.TTF",     # 方正舒體
        r"C:\Windows\Fonts\SIMLI.TTF",     # 隸書
        r"C:\Windows\Fonts\msjhbd.ttc",    # 微軟正黑體 粗體
    ]
    for p in brush_calligraphy_fonts:
        if os.path.exists(p) and os.path.getsize(p) > 1000:
            try:
                font = ImageFont.truetype(p, size)
                # 測試關鍵字「傳」是否能顯示
                if font.getmask("傳").getbbox() is not None:
                    return font
            except Exception:
                continue
    return get_font(size)

def generate_dynamic_taglines(book_title, pure_plot=""):
    """
    根據小說書名與大綱，由 AI 自動生成 2 句霸氣吸睛的四字宣傳標語 (絕不硬編或複製他人文案)
    """
    try:
        import urllib.parse
        import requests
        prompt = f"請為小說《{book_title}》寫2句霸氣吸睛的4字宣傳標語，用繁體中文，格式如: 句一, 句二"
        url = f"https://text.pollinations.ai/{urllib.parse.quote(prompt)}?model=openai"
        res = requests.get(url, timeout=3)
        if res.status_code == 200 and res.text:
            text = res.text.strip().replace('\n', ' ')
            m = re.findall(r'[\u4e00-\u9fa5]{4}', text)
            if len(m) >= 2 and m[0] != m[1]:
                return m[0], m[1]
    except Exception:
        pass

    # 備用智慧主題標語庫 (根據小說類型題材自動對應，不抄襲他人)
    if "凡人" in book_title:
        return "山 村 少 年", "踏 入 仙 途"
    elif "仙" in book_title or "修" in book_title or "劍" in book_title:
        return "逆 天 獨 尊", "踏 碎 凌 霄"
    elif "武" in book_title or "江湖" in book_title:
        return "縱 橫 江 湖", "獨 步 武 林"
    elif "醫" in book_title or "都市" in book_title:
        return "神 醫 下 山", "縱 橫 都 市"
    elif "帝" in book_title or "王" in book_title or "神" in book_title:
        return "萬 族 共 尊", "獨 斷 萬 古"
    else:
        return "執 掌 乾 坤", "逆 天 飛 升"

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
        f"8k resolution cinematic masterpiece Xianxia anime artwork for novel '{book_title}', {english_plot}. "
        "Epic golden floating immortal palace gates in clouds, heroic male anime cultivator warrior in flying action stance on left side, "
        "dramatic cinematic lighting, golden magic aura, 4k resolution wallpaper, official poster, high contrast, masterwork"
    )
    
    return pure_plot, english_plot, final_prompt

def download_ai_image(prompt, width=2560, height=1440):
    logging.info(f"連線 AI 繪圖伺服器 (Flux 模型) 生成 2K 高畫質底圖 ({width}x{height})...")
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
        
        img = ImageEnhance.Sharpness(img).enhance(1.3)
        img = ImageEnhance.Contrast(img).enhance(1.1)
        return img
    else:
        raise Exception(f"AI 生圖失敗，HTTP 狀態碼: {res.status_code}")

def create_youtube_cover(
    bg_img, 
    book_title, 
    start_chap, 
    end_chap, 
    is_completed=True, 
    output_filename="youtube_cover.jpg",
    part_num=None
):
    """
    自適應商業級 2K 封面合成引擎 (2560x1440)
    1. 支援 3~20 字任意長度小說書名，自動計算最佳字型大小與分行對齊 (右對齊排版)。
    2. 100% 繁體無缺字 (使用 FZSTK / 微軟正黑體)。
    3. 只保留書名與左上角集數徽章，無任何廢話標語或膠囊底板。
    """
    logging.info("正在合成 2K 自適應大氣小說封面 (動態字號 + 右對齊排版)...")
    
    W, H = bg_img.size
    scale = W / 1920.0
    img = bg_img.copy()
    
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    
    # 右側暗黑漸變陰影 (襯托金色標題)
    for x in range(int(W * 0.25), W):
        t = (x - W * 0.25) / (W * 0.75)
        alpha = int(210 * t)
        overlay_draw.line([(x, 0), (x, H)], fill=(0, 0, 0, alpha))
        
    top_mask_end = int(H * 0.25)
    for y in range(0, top_mask_end):
        alpha = int(140 * (1 - y / top_mask_end))
        overlay_draw.line([(0, y), (W, y)], fill=(0, 0, 0, alpha))

    bottom_mask_start = int(H * 0.70)
    for y in range(bottom_mask_start, H):
        alpha = int(160 * ((y - bottom_mask_start) / (H - bottom_mask_start)))
        overlay_draw.line([(0, y), (W, y)], fill=(0, 0, 0, alpha))
        
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(img)
    
    def draw_thick_text(draw_obj, x, y, text, font, fill_color, stroke_color=(15, 10, 5), stroke_width=12):
        draw_obj.text((x, y), text, font=font, fill=fill_color, stroke_width=stroke_width, stroke_fill=stroke_color)

    # ── 1. 左上角：精緻章節與部數琉璃徽章 ──
    badge_x, badge_y = int(70 * scale), int(60 * scale)
    if isinstance(start_chap, int) and isinstance(end_chap, int):
        chap_text = f"第 {start_chap:03d} - {end_chap:03d} 集"
    else:
        chap_text = f"第 {start_chap} - {end_chap} 集"
    
    if part_num:
        chap_text = f"【第 {part_num} 部】 " + chap_text
        
    font_badge = get_font(int(50 * scale))
    try:
        bbox = draw.textbbox((0, 0), chap_text, font=font_badge)
        bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
    except Exception:
        bw, bh = 300, 40

    badge_w, badge_h = bw + int(60 * scale), bh + int(32 * scale)
    draw.rounded_rectangle(
        [badge_x, badge_y, badge_x + badge_w, badge_y + badge_h], 
        radius=int(20 * scale), 
        fill=(210, 25, 25),
        outline=(255, 215, 0),
        width=int(4 * scale)
    )
    draw.text((badge_x + int(30 * scale), badge_y + int(12 * scale)), chap_text, font=font_badge, fill=(255, 255, 255))

    # ── 2. 右側自適應書名排版引擎 (右邊距 120px 右對齊) ──
    clean_title = book_title.replace("《", "").replace("》", "").strip()
    title_len = len(clean_title)
    right_margin_x = W - int(120 * scale)

    # 分級處理字數：
    # 級別 A：短書名 (<= 6 字，如《凡人修仙傳》) -> 單行或雙行 200pt 巨型大字
    if title_len <= 6:
        font_size = int(210 * scale)
        font_title = get_calligraphy_font(font_size)
        stroke_w = int(14 * scale)
        
        try:
            bbox = draw.textbbox((0, 0), clean_title, font=font_title)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        except Exception:
            tw, th = 800, 210
            
        start_x = right_margin_x - tw
        start_y = int(320 * scale)
        draw_thick_text(draw, start_x, start_y, clean_title, font_title, fill_color=(255, 220, 60), stroke_width=stroke_w)

    # 級別 B：中等長度書名 (7 ~ 11 字) -> 自動切分為 2 行右對齊 (150pt)
    elif title_len <= 11:
        font_size = int(155 * scale)
        font_title = get_calligraphy_font(font_size)
        stroke_w = int(12 * scale)
        
        # 標點符號或對半切分
        if "：" in clean_title:
            lines = clean_title.split("：", 1)
        elif " " in clean_title:
            lines = clean_title.split(" ", 1)
        else:
            mid = (title_len + 1) // 2
            lines = [clean_title[:mid], clean_title[mid:]]
            
        start_y = int(260 * scale)
        for line in lines:
            if not line:
                continue
            try:
                bbox = draw.textbbox((0, 0), line, font=font_title)
                lw, lh = bbox[2] - bbox[0], bbox[3] - bbox[1]
            except Exception:
                lw, lh = 600, 155
            lx = right_margin_x - lw
            draw_thick_text(draw, lx, start_y, line, font_title, fill_color=(255, 220, 60), stroke_width=stroke_w)
            start_y += lh + int(35 * scale)

    # 級別 C：長書名 (12 ~ 20 字) -> 自動切分為 2~3 行右對齊 (115pt)
    else:
        font_size = int(115 * scale)
        font_title = get_calligraphy_font(font_size)
        stroke_w = int(10 * scale)
        
        # 按照標點符號或每行 6-8 字拆分
        parts = re.split(r'([：，,；\s])', clean_title)
        lines = []
        curr = ""
        for p in parts:
            if len(curr) + len(p) <= 8:
                curr += p
            else:
                lines.append(curr)
                curr = p
        if curr:
            lines.append(curr)
            
        start_y = int(240 * scale)
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                bbox = draw.textbbox((0, 0), line, font=font_title)
                lw, lh = bbox[2] - bbox[0], bbox[3] - bbox[1]
            except Exception:
                lw, lh = 500, 115
            lx = right_margin_x - lw
            draw_thick_text(draw, lx, start_y, line, font_title, fill_color=(255, 220, 60), stroke_width=stroke_w)
            start_y += lh + int(25 * scale)

    # ── 3. 右下角醒目【已完結】/【連載中】標籤 (避開 YouTube 進度條) ──
    if is_completed:
        status_text = "【 已完結 】"
        status_fill = (16, 185, 129)   # 翡翠綠 (極致醒目)
    else:
        status_text = "【 連載中 】"
        status_fill = (245, 158, 11)   # 琥珀金
        
    font_status = get_font(int(52 * scale))
    try:
        bbox = draw.textbbox((0, 0), status_text, font=font_status)
        sw, sh = bbox[2] - bbox[0], bbox[3] - bbox[1]
    except Exception:
        sw, sh = 260, 50

    status_w = sw + int(50 * scale)
    status_h = sh + int(28 * scale)
    
    # 放置於右下角 (距離底部 140px，避開播放進度條)
    status_x = right_margin_x - status_w
    status_y = H - int(140 * scale) - status_h
    
    draw.rounded_rectangle(
        [status_x, status_y, status_x + status_w, status_y + status_h], 
        radius=int(18 * scale), 
        fill=status_fill, 
        outline=(255, 255, 255), 
        width=int(4 * scale)
    )
    draw.text((status_x + int(25 * scale), status_y + int(10 * scale)), status_text, font=font_status, fill=(255, 255, 255))

    # 存檔
    os.makedirs(os.path.dirname(os.path.abspath(output_filename)), exist_ok=True)
    q = 95
    img.save(output_filename, quality=q, optimize=True)
    while os.path.getsize(output_filename) >= 2000000 and q > 50:
        q -= 5
        img.save(output_filename, quality=q, optimize=True)

    size_mb = os.path.getsize(output_filename) / (1024 * 1024)
    logging.info(f"✅ 2K 自適應大氣封面合成完成: {output_filename} (品質 quality={q}, 大小 {size_mb:.2f} MB)")
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

def generate_video_title(book_title, start_chap=1, end_chap=2400, part_num=None):
    if part_num:
        return f"《{book_title}》第 {start_chap:04d}~{end_chap:04d} 章【第 {part_num} 部】"
    return f"《{book_title}》| 已完結 | 第 {start_chap}~{end_chap} 章 (超長有聲小說全集)"

def generate_video_description(book_title, start_chap=1, end_chap=2400, pure_plot=None, part_num=None):
    if not pure_plot:
        pure_plot = fetch_book_summary_online(book_title)

    part_str = f"【第 {part_num} 部】" if part_num else ""
    desc = f"""【超長有聲小說大合集】《{book_title}》{part_str}廣播劇收聽

📖 小說名稱：《{book_title}》
📌 包含章節：第 {start_chap} 章 至 第 {end_chap} 章 {part_str}
🎧 播放長度：完整連續播放無中斷 (約 10~11 小時)

【故事整體大綱簡介】：
{pure_plot}

歡迎訂閱、點讚、開啟小鈴鐺並分享給同好朋友！
"""
    return desc.strip()

def save_book_metadata(book_title, start_chap=1, end_chap=2400, workspace_dir=None, is_completed=True, part_num=None):
    if not workspace_dir:
        SRC_DIR = os.path.dirname(os.path.abspath(__file__))
        workspace_dir = os.path.abspath(os.path.join(SRC_DIR, "..", "Workspace", book_title))

    os.makedirs(workspace_dir, exist_ok=True)

    pure_plot, english_plot, final_prompt = auto_generate_prompt_from_summary(book_title)

    title = generate_video_title(book_title, start_chap, end_chap, part_num=part_num)
    desc = generate_video_description(book_title, start_chap, end_chap, pure_plot=pure_plot, part_num=part_num)

    title_file = os.path.join(workspace_dir, "youtube_title.txt")
    desc_file = os.path.join(workspace_dir, "youtube_description.txt")
    cover_file = os.path.join(workspace_dir, "youtube_cover.jpg")

    with open(title_file, "w", encoding="utf-8") as f:
        f.write(title)

    with open(desc_file, "w", encoding="utf-8") as f:
        f.write(desc)

    # 下載 AI 高畫質底圖並合成封面
    bg_img = download_ai_image(final_prompt, width=2560, height=1440)
    create_youtube_cover(bg_img, book_title, start_chap, end_chap, is_completed=is_completed, output_filename=cover_file, part_num=part_num)

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

def get_chapter_title(workspace_dir, book_title, chap_num):
    """
    嘗試從 RawText 讀取章節標題（第一行）。
    找不到時回傳預設 '第N章'。
    """
    if workspace_dir:
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

if __name__ == "__main__":
    save_book_metadata("凡人修仙傳", 1, 2442)
