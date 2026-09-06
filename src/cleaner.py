try:
    from .source_identity import workspace_name
except ImportError:
    from source_identity import workspace_name

import os
import re
import unicodedata
from difflib import SequenceMatcher
import yaml
import logging
try:
    from .artifact_validation import ArtifactValidationError, validate_text
except ImportError:
    from artifact_validation import ArtifactValidationError, validate_text

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


def _clean_opening_lines(text, title, book_title, scan_nonempty=12, source_labels=None):
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
            if source_labels is None:
                try:
                    from .sources.hjwzw import HJWZWSource
                except ImportError:
                    from sources.hjwzw import HJWZWSource
                source_labels = HJWZWSource.opening_labels
            remove = remove or line_key in {_comparison_text(x) for x in source_labels}
            remove = remove or bool(book_key and line_key == book_key)
            remove = remove or bool(title_key and _is_repeated_opening_title(line, title))
        if not remove:
            kept.append(raw_line.strip())
    return "\n".join(kept)


def clean_text_content(text, title, book_title, remove_patterns=None, source_labels=None):
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    for unwanted_text in validate_remove_patterns(remove_patterns):
        text = text.replace(unwanted_text, '')
    text = text.replace('\xa0', ' ').replace('\u3000', ' ')
    text = _clean_opening_lines(text, title, book_title, source_labels=source_labels)
    text = re.sub(r'\n[ \t]*\n+', '\n', text)
    return text.strip()

_CLAUSE_RE = re.compile(r".*?(?:……|\.\.\.|[，,；;：:。！？!?…]+)[”’」』）》）)]*|.+$", re.S)
_COMMA_END_RE = re.compile(r"[，,][”’」』）》）)]*$")
_SEMANTIC_TURN_RE = re.compile(r"卻|但|然而|不過|只是|所以|因此")


def _split_semantic_turn(clause):
    """Split a long clause only at a late semantic turn, never at a midpoint."""
    if len(clause) < 20:
        return [clause]
    for match in _SEMANTIC_TURN_RE.finditer(clause):
        index = match.start()
        if index >= 10 and len(clause) - index >= 5:
            return [clause[:index].rstrip("，,") + "，", clause[index:].lstrip()]
    return [clause]


def chunk_text(text, max_length=None):
    """Create punctuation/meaning-aware TTS segments without fixed-width cuts.

    ``max_length`` remains accepted for callers from older GUI code but is no
    longer used. Blank lines preserve source paragraph boundaries for graded
    audio pauses. Subtitle wrapping is handled later by subtitle_gen.py.
    """
    output_paragraphs = []
    for paragraph in (line.strip() for line in text.splitlines()):
        if not paragraph:
            continue
        raw_clauses = []
        for match in _CLAUSE_RE.finditer(paragraph):
            raw_clauses.extend(_split_semantic_turn(match.group(0).strip()))

        chunks = []
        pending = ""
        for clause in raw_clauses:
            if not clause:
                continue
            pending += clause
            visible_len = len(re.sub(r"[^0-9A-Za-z\u3400-\u9fff]", "", pending))
            if _COMMA_END_RE.search(pending) and visible_len <= 6:
                continue
            if re.search(r"[\u3400-\u9fffa-zA-Z0-9]", pending):
                chunks.append(pending.strip())
            pending = ""
        if pending:
            if chunks:
                chunks[-1] += pending
            elif re.search(r"[\u3400-\u9fffa-zA-Z0-9]", pending):
                chunks.append(pending.strip())
        if chunks:
            output_paragraphs.append("\n".join(chunks))
    return "\n\n".join(output_paragraphs)

def parse_chapter_num(filename):
    m = re.search(r'chapter_(\d+)', filename)
    if m:
        return int(m.group(1))
    return 9999

def run_cleaner(target_indices=None):
    config = load_config()
    book_title = config['book_title']
    
    workspace_dir = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", config['paths']['workspace_base'], workspace_name(config)))
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
        if os.path.exists(clean_path):
            try:
                validate_text(clean_path, clean=True)
                logging.info(f"[Cleaner] Skipping validated: {clean_filename}")
                continue
            except (ArtifactValidationError, OSError, ValueError):
                logging.warning("[Cleaner] Existing output is invalid; rebuilding: %s", clean_filename)

        with open(raw_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
            
        if not lines:
            continue
            
        title = lines[0].strip()
        raw_content = "".join(lines[1:])
        
        cleaner_config = config.get("cleaner") or {}
        source_labels = None
        if config.get('source_id'):
            try:
                from .sources import resolve_source
            except ImportError:
                from sources import resolve_source
            source_labels = resolve_source(config['catalog_url'], config['source_id']).opening_labels
        cleaned_text = clean_text_content(
            raw_content, title, book_title,
            remove_patterns=cleaner_config.get("remove_patterns") or [],
            source_labels=source_labels,
        )
        chunked_text = chunk_text(cleaned_text)
        
        clean_tmp = clean_path + ".tmp"
        with open(clean_tmp, "w", encoding="utf-8") as f:
            f.write(chunked_text)
            f.flush()
            os.fsync(f.fileno())
        validate_text(clean_tmp, clean=True)
        os.replace(clean_tmp, clean_path)
            
        logging.info(f"[Cleaner] Cleaned, chunked and saved text to {clean_path}")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_cleaner()
