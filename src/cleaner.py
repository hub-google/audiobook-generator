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

def chunk_text(text, max_length=100):
    """將過長的段落依據標點符號截斷，確保每段不超過 max_length，方便 TTS 處理"""
    paragraphs = text.split('\n')
    chunks = []
    
    # 用來切割的標點符號 (全形與半形)
    split_chars = r'([。！？\.\!\?])'
    
    for p in paragraphs:
        p = p.strip()
        if not p:
            continue
            
        if len(p) <= max_length:
            chunks.append(p)
        else:
            # 依據標點符號切割
            parts = re.split(split_chars, p)
            current_chunk = ""
            for i in range(0, len(parts), 2):
                sentence = parts[i]
                punct = parts[i+1] if i+1 < len(parts) else ""
                
                if not sentence and not punct:
                    continue
                    
                combined = sentence + punct
                
                # 如果單句加上標點還是太長 (例如沒有合適標點的長句)，強行依賴逗號切割
                if len(combined) > max_length:
                    sub_parts = re.split(r'([，,、])', combined)
                    sub_chunk = ""
                    for j in range(0, len(sub_parts), 2):
                        sub_sentence = sub_parts[j]
                        sub_punct = sub_parts[j+1] if j+1 < len(sub_parts) else ""
                        sub_combined = sub_sentence + sub_punct
                        
                        if len(sub_chunk) + len(sub_combined) > max_length and sub_chunk:
                            chunks.append(sub_chunk.strip())
                            sub_chunk = sub_combined
                        else:
                            sub_chunk += sub_combined
                    if sub_chunk:
                        chunks.append(sub_chunk.strip())
                else:
                    if len(current_chunk) + len(combined) > max_length and current_chunk:
                        chunks.append(current_chunk.strip())
                        current_chunk = combined
                    else:
                        current_chunk += combined
                        
            if current_chunk:
                chunks.append(current_chunk.strip())
                
    # 確保最後沒有空行
    return "\n".join([c for c in chunks if c])

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
            
        # 假設第一行是標題，後面是內文
        title = lines[0].strip()
        raw_content = "".join(lines[1:])
        
        cleaned_text = clean_text_content(raw_content, title, book_title)
        chunked_text = chunk_text(cleaned_text, max_length=100)
        
        clean_filename = filename.replace("_raw.txt", "_clean.txt")
        clean_path = os.path.join(clean_text_dir, clean_filename)
        
        with open(clean_path, "w", encoding="utf-8") as f:
            f.write(chunked_text)
            
        logging.info(f"[Cleaner] Cleaned, chunked and saved text to {clean_path}")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_cleaner()
