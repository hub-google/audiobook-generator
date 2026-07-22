import os
import sys
import glob
import re
import json
import time
import logging

def generate_ai_chapter_summary(chapter_num, content, max_chars=50):
    """
    對章節內文進行 50 字內的大綱總結 (優先使用 g4f 免費 LLM，異常時啟用本地句型抽取備用)
    """
    sample_text = content[:2500]
    prompt = (
        f"請閱讀以下《凡人修仙傳》第 {chapter_num} 章的內文，用繁體中文總結這一章的故事大綱與核心劇情發展。\n"
        f"【硬性要求】：\n"
        f"1. 必須使用繁體中文。\n"
        f"2. 內容精準，切中核心人物與關鍵事件。\n"
        f"3. 總字數嚴格控制在 {max_chars} 字以內。\n\n"
        f"章節內文摘要：\n{sample_text}"
    )

    try:
        from g4f.client import Client
        models_to_try = ["gpt-4o-mini", "qwen-2.5-72b", "llama-3.3-70b"]
        client = Client()
        for model in models_to_try:
            try:
                response = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": "你是一位專業的小說劇情總結專家，善於提煉故事核心，請精準控制字數在50字以內。"},
                        {"role": "user", "content": prompt}
                    ]
                )
                raw_ans = response.choices[0].message.content.strip()
                clean_ans = re.sub(r'[\"\']', '', raw_ans).replace('\n', ' ')
                if len(clean_ans) > 0:
                    if len(clean_ans) > max_chars:
                        clean_ans = clean_ans[:max_chars - 3] + "..."
                    return clean_ans, model
            except Exception:
                continue
    except Exception as e:
        logging.warning(f"[SummaryGen] g4f LLM 呼叫跳過: {e}")

    # 本地備用提煉算法
    sentences = [s.strip() for s in re.split(r'[。！!？?\n]', content) if len(s.strip()) > 8]
    selected = sentences[:2] if sentences else ["本章精彩劇情展開"]
    fallback_summary = "。".join(selected) + "。"
    if len(fallback_summary) > max_chars:
        fallback_summary = fallback_summary[:max_chars - 3] + "..."
    return fallback_summary, "Local Extractor"

def get_or_generate_chapter_summary(workspace_dir, book_title, chap_num):
    """
    取得或自動生成指定章節的摘要文字，並儲存至 Workspace/{book_title}/Summaries/
    """
    summaries_dir = os.path.join(workspace_dir, "Summaries")
    os.makedirs(summaries_dir, exist_ok=True)

    summary_file = os.path.join(summaries_dir, f"{book_title}_chapter_{chap_num}_summary.txt")
    if os.path.exists(summary_file):
        try:
            with open(summary_file, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if content:
                return content
        except Exception:
            pass

    # 尋找 CleanText 或 RawText 內文
    clean_path = os.path.join(workspace_dir, "CleanText", f"{book_title}_chapter_{chap_num}_clean.txt")
    raw_path   = os.path.join(workspace_dir, "RawText", f"{book_title}_chapter_{chap_num}_raw.txt")
    
    text_content = ""
    if os.path.exists(clean_path):
        try:
            with open(clean_path, "r", encoding="utf-8") as f:
                text_content = f.read()
        except Exception:
            pass
    elif os.path.exists(raw_path):
        try:
            with open(raw_path, "r", encoding="utf-8") as f:
                text_content = f.read()
        except Exception:
            pass

    if not text_content:
        summary_text = f"第{chap_num}章 精彩劇情演繹。"
    else:
        summary_text, model_used = generate_ai_chapter_summary(chap_num, text_content, max_chars=50)
        logging.info(f"[SummaryGen] ✓ 第 {chap_num} 章 AI 摘要生成成功 (來源: {model_used}): {summary_text}")

    try:
        with open(summary_file, "w", encoding="utf-8") as f:
            f.write(summary_text)
    except Exception as e:
        logging.warning(f"[SummaryGen] 寫入摘要檔失敗: {e}")

    return summary_text
