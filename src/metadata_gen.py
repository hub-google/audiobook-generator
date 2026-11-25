import os
import re
import json
import urllib.parse
import base64
import requests
import logging
import time
import yaml
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont
from bs4 import BeautifulSoup
from dotenv import load_dotenv

try:
    from .cover_renderer import render_viral_cover
    from .cover_template import COVER_TEMPLATE_VERSION, build_cover_prompt, validate_visual_brief
except ImportError:
    from cover_renderer import render_viral_cover
    from cover_template import COVER_TEMPLATE_VERSION, build_cover_prompt, validate_visual_brief

load_dotenv()
from .cover_inference import _hf_error_is_retryable, download_ai_image
from .cover_ai import (
    _validate_plot_source, finalize_cover_prompt,
    generate_gemini_art_prompt, generate_gemini_book_introduction,
    generate_gemini_cover_information, review_cover_information,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MetadataGen] %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()]
)


COVER_OUTPUT_RULES = "No text, no letters, no logo, no watermark, no signature."
YOUTUBE_COVER_SIZE = (1280, 720)
YOUTUBE_COVER_MAX_BYTES = 2 * 1024 * 1024


def _cover_config():
    config_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "config.yaml"))
    if not os.path.isfile(config_path):
        return {}
    try:
        with open(config_path, "r", encoding="utf-8") as handle:
            root = yaml.safe_load(handle) or {}
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise RuntimeError(f"無法讀取封面設定：{exc}") from exc
    cover = root.get("cover") or {}
    if not isinstance(cover, dict):
        raise RuntimeError("config.yaml 的 cover 必須是物件")
    return cover


def _write_json_atomic(path, payload):
    path = os.path.abspath(path)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(temporary, path)


def _validate_cover_config(config):
    template = str(config.get("template") or COVER_TEMPLATE_VERSION)
    if template != COVER_TEMPLATE_VERSION:
        raise ValueError(f"不支援的封面模板版本：{template}")
    width = int(config.get("width") or YOUTUBE_COVER_SIZE[0])
    height = int(config.get("height") or YOUTUBE_COVER_SIZE[1])
    if (width, height) != YOUTUBE_COVER_SIZE:
        raise ValueError("viral-v1 封面尺寸必須固定為 1280×720")
    steps = int(config.get("inference_steps") or 4)
    if steps < 1 or steps > 100:
        raise ValueError("HF inference_steps 必須介於 1 到 100")
    guidance = float(config.get("guidance_scale") if config.get("guidance_scale") is not None else 0.0)
    if guidance < 0 or guidance > 30:
        raise ValueError("HF guidance_scale 必須介於 0 到 30")
    composition = config.get("composition") or {}
    if int(composition.get("max_supporting_characters", 2)) != 2:
        raise ValueError("viral-v1 的 max_supporting_characters 必須固定為 2")
    if float(composition.get("title_zone_bottom_ratio", 0.35)) != 0.35:
        raise ValueError("viral-v1 的 title_zone_bottom_ratio 必須固定為 0.35")
    typography = config.get("typography") or {}
    if int(typography.get("title_max_lines", 2)) != 2:
        raise ValueError("viral-v1 的 title_max_lines 必須固定為 2")
    return width, height, steps, guidance


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

    raise RuntimeError("找不到可正確顯示中文的封面字型；流程停止，不使用低品質預設字型")

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

def _catalog_book_url(catalog_url):
    if not catalog_url:
        return ""
    try:
        from .sources import resolve_source
    except ImportError:
        from sources import resolve_source
    return resolve_source(catalog_url).metadata_url(catalog_url)


def fetch_book_summary_details(book_title, catalog_url=None):
    """取得小說簡介及來源；找不到時明確回報，不製造替代劇情。"""
    logging.info(f"正在搜尋《{book_title}》的整體小說劇情大綱與簡介...")
    raw_summary = ""
    source = ""

    def validated_identity(details, source_url):
        page_title = details['title']
        description = details['description']
        author = details['author']
        category = details['category']
        if page_title and book_title not in page_title and page_title not in book_title:
            raise ValueError(f"目錄書名是「{page_title}」，不是「{book_title}」")
        identity = f"書名：《{page_title or book_title}》；作者：{author or '未標示'}；類型：{category or '未標示'}；原始簡介：{description}"
        return _validate_plot_source(book_title, identity, source_url), source_url

    # The catalog selected by the user is the identity source for this exact
    # book. Prefer its own OpenGraph description over title-only web lookups.
    configured_catalog_url = catalog_url or os.getenv("BOOK_CATALOG_URL", "")
    book_url = _catalog_book_url(configured_catalog_url)
    if book_url:
        try:
            try:
                from .sources import resolve_source
                from .sources.http_client import fetch_page
            except ImportError:
                from sources import resolve_source
                from sources.http_client import fetch_page
            adapter = resolve_source(book_url)
            fallback_catalog_url = adapter.catalog_url(configured_catalog_url or book_url)
            source_urls = list(dict.fromkeys((book_url, fallback_catalog_url)))
            for source_url in source_urls:
                try:
                    details = adapter.parse_metadata(
                        fetch_page(source_url, adapter).content, source_url
                    )
                    return validated_identity(details, source_url)
                except (requests.RequestException, ValueError, RuntimeError) as exc:
                    logging.warning("小說來源簡介取得失敗 (%s): %s", source_url, exc)

            # catalog_parser has already fetched this exact page successfully.
            # Reuse its small metadata snapshot if a second request is rate-limited.
            cached_metadata = os.getenv("BOOK_CATALOG_METADATA", "")
            if cached_metadata:
                try:
                    return validated_identity(json.loads(cached_metadata), fallback_catalog_url)
                except (KeyError, TypeError, ValueError, RuntimeError) as exc:
                    logging.warning("目錄簡介快照無法使用: %s", exc)
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            # Source setup itself can fail. Continue to the existing Wikipedia
            # and search fallbacks instead of aborting cover preflight.
            logging.warning("無法建立小說來源簡介查詢: %s", exc)
    
    # 嘗試 1: 中文維基百科 REST API
    try:
        url = f"https://zh.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(book_title)}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        res = requests.get(url, headers=headers, timeout=8)
        if res.status_code == 200:
            extract = res.json().get("extract", "")
            if extract:
                try:
                    raw_summary = _validate_plot_source(book_title, extract, "zh.wikipedia.org")
                    source = "zh.wikipedia.org"
                except ValueError:
                    raw_summary = ""
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
                    raw_summary = _validate_plot_source(book_title, extract, "api.duckduckgo.com")
                    source = "api.duckduckgo.com"
        except Exception:
            pass

    pure_plot = _validate_plot_source(book_title, raw_summary, source) if raw_summary else ""
    if not pure_plot:
        raise RuntimeError(f"查不到《{book_title}》的可靠小說簡介；已停止，不會產生通用封面 Prompt")
    return pure_plot, source


def fetch_book_summary_online(book_title):
    return fetch_book_summary_details(book_title)[0]


def _decode_bing_result_url(url):
    try:
        encoded = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("u", [""])[0]
        if encoded.startswith("a1"):
            payload = encoded[2:] + "=" * (-len(encoded[2:]) % 4)
            return base64.urlsafe_b64decode(payload).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        pass
    return url


def collect_cover_research(book_title, synopsis, catalog_source):
    """Collect web evidence first; explicitly label model-knowledge fallback."""
    evidence = [{"id": "S1", "title": "目前小說目錄頁", "url": catalog_source, "text": synopsis}]
    author_match = re.search(r"作者：([^；]+)", synopsis)
    author = author_match.group(1).strip() if author_match else ""
    author_variants = {author, author.replace("東", "东")}
    queries = [
        f'"{book_title}" "{author}" 小說 主角 世界觀',
        f'"{book_title}" "{author}" 小說 法寶 場景',
    ]
    seen = {catalog_source}
    for query in queries:
        try:
            response = requests.get(
                "https://www.bing.com/search?q=" + urllib.parse.quote(query),
                headers={"User-Agent": "Mozilla/5.0"}, timeout=15,
            )
            response.raise_for_status()
            soup = BeautifulSoup(response.content, "html.parser")
            for result in soup.select("li.b_algo"):
                link, snippet_tag = result.select_one("h2 a"), result.select_one(".b_caption p")
                if not link or not snippet_tag:
                    continue
                title = link.get_text(" ", strip=True)
                snippet = snippet_tag.get_text(" ", strip=True)
                combined = title + " " + snippet
                if book_title not in combined or (author and not any(value and value in combined for value in author_variants)):
                    continue
                url = _decode_bing_result_url(str(link.get("href") or ""))
                if not url.startswith("http") or url in seen or len(snippet) < 35:
                    continue
                detail = snippet
                try:
                    page = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
                    page.raise_for_status()
                    page_soup = BeautifulSoup(page.content, "html.parser")
                    summary_tag = page_soup.select_one(".J-summary")
                    description_tag = page_soup.select_one('meta[name="description"]')
                    expanded = (
                        summary_tag.get_text(" ", strip=True) if summary_tag else
                        str(description_tag.get("content") or "").strip() if description_tag else ""
                    )
                    if book_title in expanded and any(value and value in expanded for value in author_variants):
                        detail = expanded[:6000]
                except requests.RequestException:
                    pass
                seen.add(url)
                evidence.append({"id": f"S{len(evidence)+1}", "title": title, "url": url, "text": detail})
                if len(evidence) >= 5:
                    break
        except requests.RequestException:
            continue
        if len(evidence) >= 5:
            break
    external_chars = sum(len(item["text"]) for item in evidence[1:])
    mode = (
        "web_evidence" if external_chars >= 800 else
        "hybrid_web_and_model_knowledge" if len(evidence) >= 2 else
        "internal_knowledge_fallback"
    )
    return {"mode": mode, "sources": evidence}

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
    根據小說書名與大綱，由 AI 自動生成 2 句霸氣吸睛的四字宣傳標語。
    失敗或超時直接報錯中止，嚴禁使用低品質硬編碼備用語句。
    """
    try:
        import urllib.parse
        import requests
        prompt = f"請為小說《{book_title}》寫2句霸氣吸睛的4字宣傳標語，用繁體中文，格式如: 句一, 句二"
        url = f"https://text.pollinations.ai/{urllib.parse.quote(prompt)}?model=openai"
        res = requests.get(url, timeout=10)
        if res.status_code == 200 and res.text:
            text = res.text.strip().replace('\n', ' ')
            m = re.findall(r'[\u4e00-\u9fa5]{4}', text)
            if len(m) >= 2 and m[0] != m[1]:
                return m[0], m[1]
            raise ValueError(f"AI 回傳內容無法提取標準4字標語: {text[:100]}")
        else:
            raise RuntimeError(f"HTTP {res.status_code}: {res.text[:100]}")
    except Exception as exc:
        raise RuntimeError(f"❌ AI 宣傳標語生成失敗或超時 ({exc})！嚴禁使用寫死備用方案，流程中止。")

def analyze_cover_brief(book_title, pure_plot, source="", workspace_dir=None, analyzer=None):
    """可替換的封面分析層；任何無效來源都 fail closed。"""
    manual_path = os.path.join(workspace_dir, "Cover", "cover_brief.json") if workspace_dir else ""
    if manual_path and os.path.exists(manual_path):
        try:
            with open(manual_path, "r", encoding="utf-8") as f:
                brief = json.load(f)
            brief["analysis_method"] = "manual"
            return brief
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"手動封面分析檔存在但無法讀取：{exc}") from exc
    if analyzer:
        brief = analyzer(book_title=book_title, synopsis=pure_plot, source=source)
        brief["analysis_method"] = "external"
        return brief
    if not pure_plot:
        raise RuntimeError(f"《{book_title}》沒有可靠劇情簡介；停止封面流程")

    return {
        "analysis_method": "local_evidence_only",
        "source": source,
        "synopsis": pure_plot,
    }

def build_cover_information(book_title, catalog_url=None, progress_callback=None):
    notify = progress_callback or (lambda _stage: None)
    notify("取得小說資料")
    pure_plot, source = fetch_book_summary_details(book_title, catalog_url=catalog_url)
    notify("整理考據來源")
    research = collect_cover_research(book_title, pure_plot, source)
    notify("Gemini 分析中")
    draft = generate_gemini_cover_information(book_title, pure_plot, research=research)
    notify("Gemini 二次審核中")
    result = review_cover_information(book_title, pure_plot, research, draft)
    result.update({"book_title": book_title, "synopsis": pure_plot, "source": source})
    notify("完成")
    return result


def auto_generate_prompt_from_summary(book_title, workspace_dir=None, analyzer=None):
    if os.getenv("COVER_GEMINI_TITLE_ONLY") == "1":
        pure_plot = generate_gemini_book_introduction(book_title)
        source = "Gemini book introduction"
    else:
        pure_plot, source = fetch_book_summary_details(book_title)
    brief = analyze_cover_brief(book_title, pure_plot, source=source, workspace_dir=workspace_dir, analyzer=analyzer)
    if brief.get("visual_brief"):
        final_prompt = finalize_cover_prompt(brief["visual_brief"])
        brief["prompt"] = final_prompt
        return pure_plot, "", final_prompt, brief
    
    # GUI and production share the same strict Gemini response and validation.
    research = (
        {"mode": "internal_knowledge_fallback", "sources": []}
        if os.getenv("COVER_GEMINI_TITLE_ONLY") == "1"
        else collect_cover_research(book_title, pure_plot, source)
    )
    draft = generate_gemini_cover_information(book_title, pure_plot, research=research)
    information = review_cover_information(book_title, pure_plot, research, draft)
    final_prompt = information["prompt"]
    brief["analysis"] = information["analysis"]
    brief["story_facts"] = information["story_facts"]
    brief["visual_brief"] = information["visual_brief"]
    brief["template_version"] = information["template_version"]
    brief["prompt"] = final_prompt
    return pure_plot, pure_plot, final_prompt, brief


def create_youtube_cover(bg_img, book_title, start_chap, end_chap, is_completed=True, output_filename="youtube_cover.jpg", part_num=None):
    """使用固定熱門縮圖版型；不因檔案大小降低畫質。"""
    return render_viral_cover(
        bg_img, book_title, start_chap, end_chap,
        is_completed=is_completed, output_filename=output_filename, part_num=part_num,
    )

def save_process_log(output_dir, book_title, pure_plot, english_plot, final_prompt, img_width=1280, img_height=720):
    log_filename = os.path.join(output_dir, f"{book_title}_process_log.txt")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    log_content = f"""======================================================================
AI 封面全自動生成過程記錄 Log
生成時間: {timestamp}
小說名稱: 《{book_title}》
輸出解析度: {img_width} x {img_height} (HD)
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
上述提示詞只使用查得的小說資訊，不固定指定仙俠、人物性別、宮殿或法術。無文字主視覺固定快取於 Cover/master_cover.jpg；各 Part 只在其複本上疊加書名，作品完結時另於右上角顯示「已完結」；不顯示部數或章節範圍。
======================================================================
"""
    with open(log_filename, "w", encoding="utf-8") as f:
        f.write(log_content)
    logging.info(f"📄 已導出生成過程 TXT 記錄檔: {log_filename}")
    return log_filename

def generate_video_title(book_title, start_chap=1, end_chap=2400, part_num=None):
    part_str = f"【第 {part_num} 部】" if part_num else ""
    return f"[已完結]《{book_title}》第 {int(start_chap)}~{int(end_chap)} 章{part_str}"

def generate_video_description(book_title, start_chap=1, end_chap=2400, pure_plot=None, part_num=None):
    """The uploader adds the playlist and verified chapter timeline.

    Keep the base text empty so titles, ranges and promotional copy are not
    duplicated in every video's description.
    """
    return ""

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


def _master_template_version(cover_dir):
    record = os.path.join(cover_dir, "master_cover_prompt.json")
    try:
        with open(record, "r", encoding="utf-8") as handle:
            return str((json.load(handle) or {}).get("template_version") or "")
    except (OSError, ValueError):
        return ""


def _part_cover_signature(book_title, start_chap, end_chap, is_completed, part_num):
    typography = (_cover_config().get("typography") or {})
    return {
        "template_version": COVER_TEMPLATE_VERSION,
        "book_title": book_title,
        "start_chap": str(start_chap),
        "end_chap": str(end_chap),
        "is_completed": bool(is_completed),
        "part_num": int(part_num) if part_num is not None else None,
        "typography": typography,
    }


def _part_cover_cache_current(cover_file, signature):
    if not _valid_youtube_cover(cover_file):
        return False
    metadata_path = cover_file + ".meta.json"
    try:
        with open(metadata_path, "r", encoding="utf-8") as handle:
            return json.load(handle) == signature
    except (OSError, ValueError):
        return False


def _valid_youtube_cover(path):
    if not os.path.exists(path) or not 10_000 <= os.path.getsize(path) < YOUTUBE_COVER_MAX_BYTES:
        return False
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            return tuple(image.size) == YOUTUBE_COVER_SIZE and image.format in {"JPEG", "PNG"}
    except (OSError, ValueError):
        return False


def ensure_master_cover(book_title, book_workspace_dir, force_regenerate=False, analyzer=None):
    """取得每本小說唯一無文字主視覺；正常執行只生成一次。"""
    cover_dir = os.path.join(book_workspace_dir, "Cover")
    os.makedirs(cover_dir, exist_ok=True)
    master_path = os.path.join(cover_dir, "master_cover.jpg")
    manual_marker = os.path.splitext(master_path)[0] + ".manual.json"
    # Only an explicit caller request may spend money regenerating this image.
    force = bool(force_regenerate)
    if _valid_master_cover(master_path) and not force:
        if os.path.isfile(manual_marker):
            logging.info("♻️ 重用使用者指定的小說主視覺: %s", master_path)
            return master_path, None
        cached_version = _master_template_version(cover_dir)
        if cached_version == COVER_TEMPLATE_VERSION:
            logging.info("♻️ 重用小說主視覺快取: %s", master_path)
            return master_path, None
        logging.warning("小說主視覺使用舊模板 %s，將依 viral-v1 重新生成。", cached_version or "unknown")

    if force:
        logging.warning("已要求強制重生《%s》主視覺。", book_title)
    pure_plot, english_plot, final_prompt, brief = auto_generate_prompt_from_summary(
        book_title, workspace_dir=book_workspace_dir, analyzer=analyzer
    )
    master = download_ai_image(final_prompt, width=1280, height=720)

    temporary_master = master_path + ".tmp"
    master.convert("RGB").save(temporary_master, "JPEG", quality=98, subsampling=0, optimize=False)
    os.replace(temporary_master, master_path)
    if os.path.isfile(manual_marker):
        os.unlink(manual_marker)
    _write_json_atomic(
        os.path.join(cover_dir, "master_cover_prompt.json"),
        {"book_title": book_title, "template_version": COVER_TEMPLATE_VERSION, "brief": brief, "prompt": final_prompt},
    )
    save_process_log(cover_dir, book_title, pure_plot, english_plot, final_prompt)
    logging.info("✅ 已建立小說唯一無文字主視覺: %s", master_path)
    return master_path, (pure_plot, english_plot, final_prompt)


def save_book_metadata(book_title, start_chap=1, end_chap=2400, workspace_dir=None, is_completed=True, part_num=None, force_regenerate_master=False, cover_analyzer=None):
    src_dir = os.path.dirname(os.path.abspath(__file__))
    storage_name = os.environ.get('BOOK_SOURCE_FINGERPRINT') or book_title
    book_workspace_dir = os.path.abspath(os.path.join(src_dir, "..", "Workspace", storage_name))
    if not workspace_dir:
        # 上傳器可能先建立所有 Part 再逐一上傳；每部必須有獨立成品路徑，
        # 才不會全部指向最後一次覆寫的 youtube_cover.jpg。
        workspace_dir = os.path.join(book_workspace_dir, f"Part_{int(part_num):02d}") if part_num else book_workspace_dir
    workspace_dir = os.path.abspath(workspace_dir)
    # part_builder 傳入 Workspace/{書名}/Part_XX；主視覺仍固定回到書籍根目錄。
    if re.fullmatch(r"Part_\d+", os.path.basename(workspace_dir), re.IGNORECASE):
        book_workspace_dir = os.path.dirname(workspace_dir)
    else:
        book_workspace_dir = workspace_dir

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

    # Reuse a completed Part cover. The unique AI master is also reused unless
    # the caller explicitly requests regeneration.
    cover_signature = _part_cover_signature(book_title, start_chap, end_chap, is_completed, part_num)
    if _part_cover_cache_current(cover_file, cover_signature):
        logging.info("♻️ 重用已完成的 1280x720 Part 封面: %s", cover_file)
    else:
        with Image.open(master_cover) as cached_master:
            bg_img = cached_master.convert("RGB").copy()
        create_youtube_cover(
            bg_img, book_title, start_chap, end_chap,
            is_completed=is_completed, output_filename=cover_file, part_num=part_num,
        )
        _write_json_atomic(cover_file + ".meta.json", cover_signature)

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
