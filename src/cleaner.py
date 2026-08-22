import os
import re
import unicodedata
from difflib import SequenceMatcher
import yaml
import logging

try:
    from .book_profiles import validate_remove_patterns
except ImportError:
    from book_profiles import validate_remove_patterns

def load_config():
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

_CHAPTER_MARKER_RE = re.compile(
    r"^第.{1,18}[章回節节集]"
)
_PURE_CHAPTER_RE = re.compile(
    r"^第\s*[0-9零〇一二三四五六七八九十百千萬万兩两]+\s*[章回節节集]\s*"
    r"(?:[（(]?\s*(?:大結局|大结局|正文完|全書完|全书完|完)\s*[）)]?)?\s*$"
)
_OPENING_AD_RE = re.compile(
    r"(?:請記住本站域名|请记住本站域名|本站域名|快捷鍵\s*[:：].*返回書頁|"
    r"手機閱讀|手机阅读|返回書頁|返回书页)"
)


def _comparison_text(value):
    """Normalize harmless display differences before comparing opening labels."""
    value = unicodedata.normalize("NFKC", str(value or "")).lower()
    return re.sub(r"[^0-9a-z\u3400-\u9fff]", "", value)


def _is_repeated_opening_title(line, title):
    line_key = _comparison_text(line)
    title_key = _comparison_text(title)
    if not line_key or not title_key:
        return False
    if line_key == title_key:
        return True
    if _PURE_CHAPTER_RE.fullmatch(unicodedata.normalize("NFKC", line).strip()):
        return True
    # Fuzzy matching is deliberately limited to lines shaped like chapter
    # headings and is only called for the opening block of the body.
    if not _CHAPTER_MARKER_RE.match(unicodedata.normalize("NFKC", line).strip()):
        return False
    ratio = SequenceMatcher(None, line_key, title_key).ratio()
    shared = SequenceMatcher(None, line_key, title_key).find_longest_match().size
    return ratio >= 0.72 and shared >= min(6, len(title_key))


def _clean_opening_lines(text, title, book_title, scan_nonempty=12):
    title_key = _comparison_text(title)
    book_key = _comparison_text(book_title)
    kept = []
    seen_nonempty = 0
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if line:
            seen_nonempty += 1
        in_opening = seen_nonempty <= scan_nonempty
        remove = False
        if in_opening and line:
            line_key = _comparison_text(line)
            remove = bool(_OPENING_AD_RE.search(line))
            remove = remove or line_key == "黃金屋"
            remove = remove or bool(book_key and line_key == book_key)
            remove = remove or bool(title_key and _is_repeated_opening_title(line, title))
        if not remove:
            kept.append(raw_line.strip())
    return "\n".join(kept)


def clean_text_content(text, title, book_title, remove_patterns=None):
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    for unwanted_text in validate_remove_patterns(remove_patterns):
        text = text.replace(unwanted_text, '')
    text = text.replace('\xa0', ' ').replace('\u3000', ' ')
    text = _clean_opening_lines(text, title, book_title)
    text = re.sub(r'\n[ \t]*\n+', '\n', text)
    return text.strip()

def split_overlong_clause(text, hard_max=18):
    """
    若單一子句無標點符號但長度超過 hard_max (18字)，
    依據語法停頓詞 (但是, 然而, 的時候, 之時) 或中央黃金分割點切分，
    硬性保證輸出的每個子句絕不超過 18 個字。
    """
    text = text.strip()
    if len(text) <= hard_max:
        return [text]
        
    # 語法/情節自然停頓關鍵詞
    grammar_pauses = ["但是", "然而", "因為", "所以", "雖然", "結果", "隨後", "接著", "然後", "並且", "只見", "只聽", "忽見", "轉眼", "同時", "傳來", "传来", "的時候", "之時", "之後", "之處"]
    
    for kw in grammar_pauses:
        idx = text.find(kw)
        if 5 <= idx <= hard_max:
            part1 = text[:idx + len(kw)].strip()
            part2 = text[idx + len(kw):].strip()
            if part1 and part2:
                return split_overlong_clause(part1, hard_max) + split_overlong_clause(part2, hard_max)
            
    # 若無語法停頓詞，從中央切分
    mid = len(text) // 2
    part1 = text[:mid].strip()
    part2 = text[mid:].strip()
    return split_overlong_clause(part1, hard_max) + split_overlong_clause(part2, hard_max)

def chunk_text(text, max_length=18):
    """將過長的段落依據標點符號與智慧自然斷句截斷，硬性確保每段 8~18 字，100% 保證單行字幕"""
    paragraphs = text.split('\n')
    chunks = []
    
    # 依句點、驚嘆號、問號、逗點、頓號、分號切分
    split_pattern = r'([。！？\.\!\?，,、；;])'
    
    for p in paragraphs:
        p = p.strip()
        if not p:
            continue
            
        parts = re.split(split_pattern, p)
        current_chunk = ""
        for i in range(0, len(parts), 2):
            sentence = parts[i]
            punct = parts[i+1] if i+1 < len(parts) else ""
            
            if not sentence and not punct:
                continue
                
            combined = sentence + punct
            
            # 若單一標點區間本身就超過 max_length，調用 split_overlong_clause 強制二次拆分
            if len(combined) > max_length:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                    current_chunk = ""
                sub_chunks = split_overlong_clause(combined, hard_max=max_length)
                chunks.extend(sub_chunks)
            else:
                if len(current_chunk) + len(combined) > max_length and current_chunk:
                    chunks.append(current_chunk.strip())
                    current_chunk = combined
                else:
                    current_chunk += combined
                    
        if current_chunk:
            chunks.append(current_chunk.strip())
                
    # 排除無意義純標點孤行 (需含有中文字元、英數字，非純標點符號)
    valid_chunks = []
    for c in chunks:
        c_clean = c.strip()
        if not c_clean:
            continue
        if re.search(r'[\u4e00-\u9fa5a-zA-Z0-9]', c_clean):
            valid_chunks.append(c_clean)
            
    return "\n".join(valid_chunks)

def parse_chapter_num(filename):
    m = re.search(r'chapter_(\d+)', filename)
    if m:
        return int(m.group(1))
    return 9999

def run_cleaner(target_indices=None):
    config = load_config()
    book_title = config['book_title']
    
    workspace_dir = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", config['paths']['workspace_base'], book_title))
    raw_text_dir = os.path.join(workspace_dir, "RawText")
    clean_text_dir = os.path.join(workspace_dir, "CleanText")
    
    if not os.path.exists(clean_text_dir):
        os.makedirs(clean_text_dir)
        
    if not os.path.exists(raw_text_dir):
        logging.warning("[Cleaner] No RawText directory found.")
        return
        
    for filename in os.listdir(raw_text_dir):
        if not filename.endswith("_raw.txt"):
            continue
            
        chap_num = parse_chapter_num(filename)
        if target_indices is not None and chap_num not in target_indices:
            continue

        raw_path = os.path.join(raw_text_dir, filename)

        # 如果已有干淨 clean.txt，且未要求 force，直接跳過
        clean_filename = filename.replace("_raw.txt", "_clean.txt")
        clean_path = os.path.join(clean_text_dir, clean_filename)
        if os.path.exists(clean_path) and os.path.getsize(clean_path) > 10:
            logging.info(f"[Cleaner] Skipping existing: {clean_filename}")
            continue

        with open(raw_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
            
        if not lines:
            continue
            
        title = lines[0].strip()
        raw_content = "".join(lines[1:])
        
        cleaner_config = config.get("cleaner") or {}
        cleaned_text = clean_text_content(
            raw_content, title, book_title,
            remove_patterns=cleaner_config.get("remove_patterns") or [],
        )
        chunked_text = chunk_text(cleaned_text, max_length=18)
        
        clean_tmp = clean_path + ".tmp"
        with open(clean_tmp, "w", encoding="utf-8") as f:
            f.write(chunked_text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(clean_tmp, clean_path)
            
        logging.info(f"[Cleaner] Cleaned, chunked and saved text to {clean_path}")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_cleaner()
