import os
import sys
import re
import json
import random
import urllib.parse
import requests
import logging
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont, ImageEnhance
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MetadataGen] %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()]
)


LIGHT_PAINTING_STYLE = (
    "Spectacular light painting art style, with luminous energy trails, "
    "flowing long-exposure light ribbons, radiant contours, and controlled "
    "glowing accents integrated naturally into the story scene."
)

COVER_OUTPUT_RULES = "No text, no letters, no logo, no watermark, no signature."


def finalize_cover_prompt(prompt):
    """套用所有封面來源都不可繞過的全域生圖風格與輸出限制。"""
    return f"{prompt.strip()} {LIGHT_PAINTING_STYLE} {COVER_OUTPUT_RULES}"


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

def fetch_book_summary_details(book_title):
    """取得小說簡介及來源；找不到時明確回報，不製造替代劇情。"""
    logging.info(f"正在搜尋《{book_title}》的整體小說劇情大綱與簡介...")
    raw_summary = ""
    source = ""
    
    # 嘗試 1: 中文維基百科 REST API
    try:
        url = f"https://zh.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(book_title)}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        res = requests.get(url, headers=headers, timeout=8)
        if res.status_code == 200:
            extract = res.json().get("extract", "")
            if extract:
                raw_summary = extract
                source = "zh.wikipedia.org"
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
                    source = "api.duckduckgo.com"
        except Exception:
            pass

    pure_plot = clean_pure_plot_summary(raw_summary)
    if not pure_plot:
        logging.warning("查不到《%s》的可靠小說簡介；封面將使用題材中性備用方案。", book_title)
    return pure_plot, source


def fetch_book_summary_online(book_title):
    return fetch_book_summary_details(book_title)[0]

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

def _neutral_cover_prompt(book_title):
    return (
        f"Premium cinematic book-cover artwork inspired only by the title '{book_title}', "
        "genre-neutral atmospheric environment, no specific character appearance or invented story event, "
        "16:9 YouTube thumbnail composition, clear focal subject separated from a clean text-safe area, "
        "high detail, crisp focus, strong silhouette and tonal separation, readable at small thumbnail size, "
        "no text, no letters, no logo, no watermark"
    )


def analyze_cover_brief(book_title, pure_plot, source="", workspace_dir=None, analyzer=None):
    """可替換的封面分析層：手動 JSON > 注入分析器 > 保守本地分析。"""
    manual_path = os.path.join(workspace_dir, "Cover", "cover_brief.json") if workspace_dir else ""
    if manual_path and os.path.exists(manual_path):
        try:
            with open(manual_path, "r", encoding="utf-8") as f:
                brief = json.load(f)
            brief["analysis_method"] = "manual"
            return brief
        except (OSError, ValueError) as exc:
            logging.warning("手動封面分析檔無法讀取：%s", exc)
    if analyzer:
        brief = analyzer(book_title=book_title, synopsis=pure_plot, source=source)
        brief["analysis_method"] = "external"
        return brief
    if not pure_plot:
        return {"analysis_method": "neutral_fallback", "prompt": _neutral_cover_prompt(book_title)}

    return {
        "analysis_method": "local_evidence_only",
        "source": source,
        "synopsis": pure_plot,
    }

def generate_gemini_art_prompt(book_title, pure_plot):
    """
    使用 Google AI Studio Gemini 藝術總監引擎，分析全本小說名稱與完整劇情簡介，
    產出高質感、鮮豔震撼、具備核心主角與靈魂法寶/特徵的 8K 影視級英文 AI 生圖提示詞 (Art Prompt)。
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("❌ [CRITICAL] 缺少 GEMINI_API_KEY！無法啟動 Gemini LLM 藝術總監引擎，流程中斷。")

    logging.info(f"🎨 啟動 Gemini 藝術總監引擎分析《{book_title}》劇情與視覺風格...")
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key={api_key}"
    
    sys_instruction = (
        "You are a world-class Hollywood concept artist and master book-cover art director. "
        "Carefully analyze the given book title and synopsis to create a breathtaking, ultra-vibrant, 16:9 cinematic 8K concept art prompt in English.\n"
        "IMPORTANT RULES:\n"
        "1. DYNAMIC STORY ANALYSIS: Fully analyze the unique setting, era, genre, core characters, iconic artifacts/objects, atmosphere, and major plot points from the provided title and synopsis. Customize every visual detail specifically for this book.\n"
        "2. HIGH-AESTHETIC CHARACTER DESIGN: If the scene features a character, they MUST be exceptionally attractive and stylish according to mainstream visual standards (e.g. extremely handsome/stunningly gorgeous face, sharp refined facial features, idol-level facial structure, photogenic, high-end fashionable attire appropriate for the story's setting). NEVER generate plain, ordinary, or generic characters.\n"
        "3. DYNAMIC FOCAL POINT & ENVIRONMENT: Feature the central hero or primary story conflict in a compelling 16:9 focal composition with atmospheric cinematic lighting.\n"
        "4. VIBRANT COLORS & LIGHTING: Use vibrant, vivid color contrasts and dynamic lighting matching the story mood instead of dull, dark, or muddy colors.\n"
        "5. QUALITY & BEAUTY KEYWORDS: MUST include: 'extremely handsome facial features, high aesthetic appealing face, hyperrealistic 8k resolution, crisp focus, epic cinematic lighting, masterpiece composition, highly detailed, trending on ArtStation'.\n"
        "6. DO NOT include any text, title letters, logos, watermarks, or signatures in the visual.\n"
        "7. Output ONLY the raw English prompt text, without any intro/outro, quotes, or markdown backticks."
    )
    
    user_prompt = f"Book Title: 《{book_title}》\nSynopsis:\n{pure_plot}\n\nPlease generate a breathtaking cinematic masterpiece concept art prompt in English."
    
    payload = {
        "contents": [{
            "parts": [{"text": sys_instruction + "\n\n" + user_prompt}]
        }]
    }
    
    res = requests.post(url, json=payload, timeout=30)
    if res.status_code == 200:
        candidates = res.json().get("candidates", [])
        if candidates and "content" in candidates[0]:
            prompt_text = candidates[0]["content"]["parts"][0]["text"].strip()
            prompt_text = re.sub(r"^```[a-zA-Z]*\n?", "", prompt_text).rstrip("`").strip()
            prompt_text = prompt_text.strip('"').strip("'")
            logging.info(f"✨ Gemini 藝術總監產出提示詞成功: {prompt_text[:120]}...")
            return prompt_text
    
    raise Exception(f"❌ Gemini 藝術總監 Prompt 生成失敗 (HTTP {res.status_code}): {res.text[:300]}")


def auto_generate_prompt_from_summary(book_title, workspace_dir=None, analyzer=None):
    pure_plot, source = fetch_book_summary_details(book_title)
    brief = analyze_cover_brief(book_title, pure_plot, source=source, workspace_dir=workspace_dir, analyzer=analyzer)
    if brief.get("prompt"):
        final_prompt = finalize_cover_prompt(brief["prompt"])
        brief["prompt"] = final_prompt
        return pure_plot, "", final_prompt, brief
    
    # 透過 Gemini LLM 藝術總監產生大師級 Prompt
    final_prompt = finalize_cover_prompt(generate_gemini_art_prompt(book_title, pure_plot))
    brief["prompt"] = final_prompt
    return pure_plot, pure_plot, final_prompt, brief


def download_ai_image(prompt, width=1280, height=720):
    import time
    import io
    logging.info(f"🖼️ 連線 Hugging Face AI 繪圖伺服器 (FLUX.1-schnell) 生成 2K 高畫質封面底圖 ({width}x{height})...")
    
    hf_token = os.getenv("HF_TOKEN")
    if not hf_token or not hf_token.startswith("hf_"):
        raise ValueError("❌ [CRITICAL] 缺少有效 HF_TOKEN！無法啟動 Hugging Face 生圖，流程直接終止。")

    img = None
    try:
        try:
            from huggingface_hub import InferenceClient
            client = InferenceClient(model="black-forest-labs/FLUX.1-schnell", token=hf_token)
            img = client.text_to_image(
                prompt,
                width=width,
                height=height,
                num_inference_steps=4,
                guidance_scale=0.0,
            )
        except Exception as hf_err:
            logging.warning(f"⚠️ huggingface_hub 呼叫失敗，改用 REST API 重試: {hf_err}")
            api_url = "https://api-inference.huggingface.co/models/black-forest-labs/FLUX.1-schnell"
            headers = {"Authorization": f"Bearer {hf_token}"}
            res = requests.post(
                api_url,
                headers=headers,
                json={
                    "inputs": prompt,
                    "parameters": {
                        "width": width,
                        "height": height,
                        "num_inference_steps": 4,
                        "guidance_scale": 0.0,
                    },
                },
                timeout=60,
            )
            if res.status_code == 200:
                img = Image.open(io.BytesIO(res.content))
            else:
                raise Exception(f"HTTP {res.status_code}: {res.text[:200]}")

        if img:
            img = img.convert("RGB")
            if img.size != (width, height):
                raise RuntimeError(
                    f"Hugging Face returned {img.size[0]}x{img.size[1]}; "
                    f"expected {width}x{height}. Refusing to stretch the image."
                )
            logging.info("✅ 成功從 Hugging Face FLUX.1 生成超高畫質底圖！")
            return img
    except Exception as e:
        raise RuntimeError(f"❌ [CRITICAL] Hugging Face 生圖失敗: {e}！流程直接終止，嚴禁降級使用低品質備用圖。")



def _create_youtube_cover_legacy(
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

    # ── 1. 左上角：精緻章節與部數琉璃徽章 (加大高寬與呼吸感) ──
    badge_x, badge_y = int(70 * scale), int(60 * scale)
    if isinstance(start_chap, int) and isinstance(end_chap, int):
        chap_text = f"第 {start_chap:03d} - {end_chap:03d} 集"
    else:
        chap_text = f"第 {start_chap} - {end_chap} 集"
    
    if part_num:
        chap_text = f"【第 {part_num} 部】 " + chap_text
        
    font_badge = get_font(int(48 * scale))
    try:
        bbox = draw.textbbox((0, 0), chap_text, font=font_badge)
        bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
    except Exception:
        bw, bh = 300, 48

    pad_x1 = int(45 * scale)
    pad_y1 = int(22 * scale)
    badge_w = bw + pad_x1 * 2
    badge_h = bh + pad_y1 * 2

    draw.rounded_rectangle(
        [badge_x, badge_y, badge_x + badge_w, badge_y + badge_h], 
        radius=int(22 * scale), 
        fill=(210, 25, 25),
        outline=(255, 215, 0),
        width=int(4 * scale)
    )
    draw.text((badge_x + pad_x1, badge_y + pad_y1 - int(4 * scale)), chap_text, font=font_badge, fill=(255, 255, 255))

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

    # ── 3. 右下角醒目【已完結】/【連載中】標籤 (加大高寬防壓字，距離底部 140px 避開進度條) ──
    if is_completed:
        status_text = "【 已完結 】"
        status_fill = (16, 185, 129)   # 翡翠綠 (極致醒目)
    else:
        status_text = "【 連載中 】"
        status_fill = (245, 158, 11)   # 琥珀金
        
    font_status = get_font(int(48 * scale))
    try:
        bbox = draw.textbbox((0, 0), status_text, font=font_status)
        sw, sh = bbox[2] - bbox[0], bbox[3] - bbox[1]
    except Exception:
        sw, sh = 260, 48

    pad_x2 = int(45 * scale)
    pad_y2 = int(22 * scale)
    status_w = sw + pad_x2 * 2
    status_h = sh + pad_y2 * 2
    
    # 放置於右下角 (距離底部 140px，避開播放進度條)
    status_x = right_margin_x - status_w
    status_y = H - int(140 * scale) - status_h
    
    draw.rounded_rectangle(
        [status_x, status_y, status_x + status_w, status_y + status_h], 
        radius=int(22 * scale), 
        fill=status_fill, 
        outline=(255, 255, 255), 
        width=int(4 * scale)
    )
    draw.text((status_x + pad_x2, status_y + pad_y2 - int(4 * scale)), status_text, font=font_status, fill=(255, 255, 255))

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


def _measure(draw, text, font):
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0], box[3] - box[1]


def _fit_font(draw, text, max_width, start_size, min_size=54):
    for size in range(start_size, min_size - 1, -4):
        font = get_calligraphy_font(size)
        if _measure(draw, text, font)[0] <= max_width:
            return font
    return get_calligraphy_font(min_size)


def _create_youtube_cover_redesign(
    bg_img,
    book_title,
    start_chap,
    end_chap,
    is_completed=True,
    output_filename="youtube_cover.jpg",
    part_num=None,
):
    """在固定無文字主視覺上疊加一致的 Part 資訊版型。"""
    width, height = 2560, 1440
    img = bg_img.convert("RGB").resize((width, height), Image.LANCZOS)
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    shade = ImageDraw.Draw(overlay)

    # 僅使用局部漸層，不以大面積黑色矩形遮蔽主視覺。
    for x in range(0, 1450):
        alpha = int(178 * (1 - x / 1450) ** 1.7)
        shade.line([(x, 0), (x, height)], fill=(4, 8, 22, alpha))
    for y in range(920, height):
        alpha = int(92 * (y - 920) / (height - 920))
        shade.line([(0, y), (width, y)], fill=(3, 6, 16, alpha))
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(img)

    clean_title = book_title.replace("《", "").replace("》", "").strip()
    title_font = _fit_font(draw, clean_title, 1340, 220, 92)
    title_w, title_h = _measure(draw, clean_title, title_font)
    title_x, title_y = 105, 245
    # 輕量陰影取代粗黑描邊。
    draw.text((title_x + 7, title_y + 9), clean_title, font=title_font, fill=(0, 0, 0))
    draw.text((title_x, title_y), clean_title, font=title_font, fill=(255, 224, 118), stroke_width=2, stroke_fill=(102, 67, 16))
    draw.line([(title_x, title_y + title_h + 45), (title_x + min(title_w, 760), title_y + title_h + 45)], fill=(222, 184, 76), width=5)

    part_text = f"第 {part_num} 部" if part_num else "完整合集"
    range_text = f"第 {int(start_chap):03d}–{int(end_chap):03d} 章" if str(start_chap).isdigit() and str(end_chap).isdigit() else f"第 {start_chap}–{end_chap} 章"
    info_font = get_font(65)
    info_text = f"{part_text}  ·  {range_text}"
    info_w, info_h = _measure(draw, info_text, info_font)
    info_x, info_y = 110, 690
    draw.rounded_rectangle((info_x - 24, info_y - 18, info_x + info_w + 28, info_y + info_h + 30), radius=18, fill=(15, 24, 48), outline=(224, 187, 82), width=3)
    draw.text((info_x, info_y), info_text, font=info_font, fill=(255, 255, 255))

    status_text = "已完結" if is_completed else "連載中"
    status_fill = (5, 143, 105) if is_completed else (185, 50, 38)
    status_font = get_font(92)
    status_w, status_h = _measure(draw, status_text, status_font)
    pad_x, pad_y = 58, 34
    box_w, box_h = status_w + pad_x * 2, status_h + pad_y * 2
    status_x = width - box_w - 105
    status_y = 92
    draw.rounded_rectangle((status_x, status_y, status_x + box_w, status_y + box_h), radius=28, fill=status_fill, outline=(255, 232, 157), width=7)
    draw.text((status_x + pad_x, status_y + pad_y - 9), status_text, font=status_font, fill=(255, 255, 255), stroke_width=2, stroke_fill=(0, 55, 42))

    os.makedirs(os.path.dirname(os.path.abspath(output_filename)), exist_ok=True)
    quality = 94
    img.save(output_filename, "JPEG", quality=quality, optimize=True)
    while os.path.getsize(output_filename) >= 2_000_000 and quality > 55:
        quality -= 4
        img.save(output_filename, "JPEG", quality=quality, optimize=True)
    logging.info("✅ Part 封面完成（固定主視覺版型）: %s", output_filename)
    return output_filename


def create_youtube_cover(bg_img, book_title, start_chap, end_chap, is_completed=True, output_filename="youtube_cover.jpg", part_num=None):
    """使用專案原有封面文字排版；底圖仍採每本小說唯一 master cover。"""
    return _create_youtube_cover_legacy(
        bg_img,
        book_title,
        start_chap,
        end_chap,
        is_completed=is_completed,
        output_filename=output_filename,
        part_num=part_num,
    )

def save_process_log(output_dir, book_title, pure_plot, english_plot, final_prompt, img_width=2560, img_height=1440):
    log_filename = os.path.join(output_dir, f"{book_title}_process_log.txt")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    log_content = f"""======================================================================
AI 封面全自動生成過程記錄 Log
生成時間: {timestamp}
小說名稱: 《{book_title}》
輸出解析度: {img_width} x {img_height} (2K QHD 超高畫質)
======================================================================

1️⃣ 從可靠來源取得的【小說簡介】
----------------------------------------------------------------------
{pure_plot}
(若此處為空，代表查無可靠簡介，提示詞已改用中性備用方案。)

2️⃣ Python 免費翻譯成的【英文劇情】
----------------------------------------------------------------------
"{english_plot}"

3️⃣ Python 最終組裝並發送給 AI 畫師的【完整 Prompt】
----------------------------------------------------------------------
{final_prompt}

4️⃣ 主視覺生成與快取說明
----------------------------------------------------------------------
上述提示詞只使用查得的小說資訊，不固定指定仙俠、人物性別、宮殿或法術。無文字主視覺固定快取於 Cover/master_cover.jpg；各 Part 只在其複本上疊加書名、部數、章節範圍與狀態。
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

def _valid_master_cover(path):
    if not os.path.exists(path) or os.path.getsize(path) < 10_000:
        return False
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            return image.width >= 1280 and image.height >= 720
    except (OSError, ValueError):
        return False


def _neutral_master_image(width=2560, height=1440):
    """生圖服務不可用時的無文字、中性高品質本地備援。"""
    image = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(image)
    for y in range(height):
        t = y / height
        draw.line([(0, y), (width, y)], fill=(int(12 + 20*t), int(22 + 24*t), int(50 + 42*t)))
    for radius, alpha in [(650, 32), (430, 42), (260, 58)]:
        glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        gd.ellipse((width-900-radius, 380-radius, width-900+radius, 380+radius), fill=(224, 177, 76, alpha))
        image = Image.alpha_composite(image.convert("RGBA"), glow).convert("RGB")
    return image


def ensure_master_cover(book_title, book_workspace_dir, force_regenerate=False, analyzer=None):
    """取得每本小說唯一無文字主視覺；正常執行只生成一次。"""
    cover_dir = os.path.join(book_workspace_dir, "Cover")
    os.makedirs(cover_dir, exist_ok=True)
    master_path = os.path.join(cover_dir, "master_cover.jpg")
    # Only an explicit caller request may spend money regenerating this image.
    force = bool(force_regenerate)
    if _valid_master_cover(master_path) and not force:
        logging.info("♻️ 重用小說主視覺快取: %s", master_path)
        return master_path, None

    if force:
        logging.warning("已要求強制重生《%s》主視覺。", book_title)
    pure_plot, english_plot, final_prompt, brief = auto_generate_prompt_from_summary(
        book_title, workspace_dir=book_workspace_dir, analyzer=analyzer
    )
    master = download_ai_image(final_prompt, width=1280, height=720)

    master.convert("RGB").save(master_path, "JPEG", quality=94, optimize=True)
    with open(os.path.join(cover_dir, "master_cover_prompt.json"), "w", encoding="utf-8") as f:
        json.dump({"book_title": book_title, "brief": brief, "prompt": final_prompt}, f, ensure_ascii=False, indent=2)
    save_process_log(cover_dir, book_title, pure_plot, english_plot, final_prompt)
    logging.info("✅ 已建立小說唯一無文字主視覺: %s", master_path)
    return master_path, (pure_plot, english_plot, final_prompt)


def save_book_metadata(book_title, start_chap=1, end_chap=2400, workspace_dir=None, is_completed=True, part_num=None, force_regenerate_master=False, cover_analyzer=None):
    src_dir = os.path.dirname(os.path.abspath(__file__))
    book_workspace_dir = os.path.abspath(os.path.join(src_dir, "..", "Workspace", book_title))
    if not workspace_dir:
        # 上傳器可能先建立所有 Part 再逐一上傳；每部必須有獨立成品路徑，
        # 才不會全部指向最後一次覆寫的 youtube_cover.jpg。
        workspace_dir = os.path.join(book_workspace_dir, f"Part_{int(part_num):02d}") if part_num else book_workspace_dir
    workspace_dir = os.path.abspath(workspace_dir)
    # part_builder 傳入 Workspace/{書名}/Part_XX；主視覺仍固定回到書籍根目錄。
    if re.fullmatch(r"Part_\d+", os.path.basename(workspace_dir), re.IGNORECASE):
        book_workspace_dir = os.path.dirname(workspace_dir)

    os.makedirs(workspace_dir, exist_ok=True)
    master_cover, generated_info = ensure_master_cover(
        book_title, book_workspace_dir, force_regenerate=force_regenerate_master, analyzer=cover_analyzer
    )
    if generated_info:
        pure_plot, english_plot, final_prompt = generated_info
    else:
        pure_plot = ""
        prompt_record = os.path.join(book_workspace_dir, "Cover", "master_cover_prompt.json")
        try:
            with open(prompt_record, "r", encoding="utf-8") as f:
                pure_plot = json.load(f).get("brief", {}).get("synopsis", "")
        except (OSError, ValueError):
            pass
        english_plot, final_prompt = "", "(reused cached master cover)"

    title = generate_video_title(book_title, start_chap, end_chap, part_num=part_num)
    desc = generate_video_description(book_title, start_chap, end_chap, pure_plot=pure_plot, part_num=part_num)

    title_file = os.path.join(workspace_dir, "youtube_title.txt")
    desc_file = os.path.join(workspace_dir, "youtube_description.txt")
    cover_file = os.path.join(workspace_dir, "youtube_cover.jpg")

    with open(title_file, "w", encoding="utf-8") as f:
        f.write(title)

    with open(desc_file, "w", encoding="utf-8") as f:
        f.write(desc)

    # 每一部只在同一張無文字 master cover 上疊加可變資訊。
    with Image.open(master_cover) as cached_master:
        bg_img = cached_master.convert("RGB").copy()
    create_youtube_cover(bg_img, book_title, start_chap, end_chap, is_completed=is_completed, output_filename=cover_file, part_num=part_num)

    log_file = os.path.join(book_workspace_dir, "Cover", f"{book_title}_process_log.txt")

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
        "master_cover_file": master_cover,
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
