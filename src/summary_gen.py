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

    # 嘗試免 KEY 網路 API (Pollinations AI)
    try:
        import urllib.parse
        import requests
        api_prompt = f"請用繁體中文將《{chapter_num}章》內文總結為50字以內的核心大綱（勿贅述）：\n{content[:1800]}"
        encoded_prompt = urllib.parse.quote(api_prompt)
        url = f"https://text.pollinations.ai/{encoded_prompt}?model=openai"
        resp = requests.get(url, timeout=8)
        if resp.status_code == 200 and resp.text:
            ans = resp.text.strip()
            clean_ans = re.sub(r'[\"\']', '', ans).replace('\n', ' ')
            if len(clean_ans) > 10:
                if len(clean_ans) > max_chars:
                    clean_ans = clean_ans[:max_chars - 3] + "..."
                return clean_ans, "Pollinations AI"
    except Exception as e:
        logging.warning(f"[SummaryGen] Pollinations API 跳過: {e}")

    # ── 智慧全章分區 NLP 提煉演算法 (免網、零 API KEY) ──
    # 將整章切分為【前段 30% 背景、中段 40% 發展、後段 30% 高潮/轉折】
    clean_lines = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("【") or line.startswith("第"):
            continue
        line_no_dialog = re.sub(r'[「"『].*?[」"』]', '', line)
        if len(line_no_dialog) > 8:
            clean_lines.append(line_no_dialog)

    if not clean_lines:
        return f"第{chapter_num}章 精彩故事劇情演繹。", "Local NLP"

    total = len(clean_lines)
    part1_lines = clean_lines[:max(1, int(total * 0.3))]
    part2_lines = clean_lines[int(total * 0.3):int(total * 0.7)]
    part3_lines = clean_lines[int(total * 0.7):]

    def pick_best_sentence(lines, fallback=""):
        if not lines:
            return fallback
        for l in lines:
            if any(kw in l for kw in ["韓立", "二愣子", "七玄門", "三叔", "村長", "考核", "弟子", "消息", "決定", "靈藥", "修煉"]):
                return l.split("。")[0]
        return lines[0].split("。")[0]

    s1 = pick_best_sentence(part1_lines, "")
    s2 = pick_best_sentence(part2_lines, "")
    s3 = pick_best_sentence(part3_lines, "")

    parts = [p for p in [s1, s2, s3] if p]
    full_summary = "。".join(parts) + "。"
    clean_summary = re.sub(r'\s+', '', full_summary)
    if len(clean_summary) > max_chars:
        clean_summary = clean_summary[:max_chars - 3] + "..."
    return clean_summary, "智慧全章 NLP 提煉"

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
