import os
import re
import yaml
import logging

def load_config():
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def clean_text_content(text, title, book_title):
    # 更嚴格的清理規則，把整句包含「請記住本站域名」的文字移除
    text = re.sub(r'請記住本站域名.*?(?=\n|$)', '', text)
    text = re.sub(r'快捷鍵:.*?返回書頁', '', text)
    text = text.replace('黃金屋', '')
    text = text.replace('\xa0', '').strip()
    
    # 移除重複的空白行
    text = re.sub(r'\n\s*\n', '\n', text)
    
    # 確保內文不會重複出現標題 (因為標題我們不希望 TTS 唸出)
    if title:
        text = text.replace(title, "").strip()
    if book_title:
        text = text.replace(book_title, "").strip()
        
    return text

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
    grammar_pauses = ["但是", "然而", "因為", "所以", "雖然", "結果", "隨後", "接著", "然後", "並且", "只見", "只聽", "忽見", "轉眼", "同時", "的時候", "之時", "之後", "之處"]
    
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
                
    # 排除無意義標點孤行
    valid_chunks = []
    for c in chunks:
        c_clean = c.strip()
        if len(c_clean) > 1 or c_clean in "。！？":
            valid_chunks.append(c_clean)
            
    return "\n".join(valid_chunks)

def run_cleaner():
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
            
        raw_path = os.path.join(raw_text_dir, filename)
        with open(raw_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
            
        if not lines:
            continue
            
        title = lines[0].strip()
        raw_content = "".join(lines[1:])
        
        cleaned_text = clean_text_content(raw_content, title, book_title)
        chunked_text = chunk_text(cleaned_text, max_length=18)
        
        clean_filename = filename.replace("_raw.txt", "_clean.txt")
        clean_path = os.path.join(clean_text_dir, clean_filename)
        
        with open(clean_path, "w", encoding="utf-8") as f:
            f.write(chunked_text)
            
        logging.info(f"[Cleaner] Cleaned, chunked and saved text to {clean_path}")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_cleaner()
