import os
import sys
import glob
import re
import json
import time
import logging

_local_pipeline = None

def get_local_ai_summary(prompt, max_chars=50):
    global _local_pipeline
    try:
        if _local_pipeline is None:
            from transformers import pipeline
            logging.info("[SummaryGen] 🚀 載入在地 AI 模型 (Qwen/Qwen2.5-0.5B-Instruct)...")
            _local_pipeline = pipeline(
                "text-generation",
                model="Qwen/Qwen2.5-0.5B-Instruct",
                device_map="cpu"
            )
        formatted_prompt = f"<|im_start|>system\n你是一位專業小說大綱提煉助手。請用繁體中文寫出一句40字以內的劇情大綱，切勿有任何多餘廢話與重複標題。<|im_end|>\n<|im_start|>user\n{prompt}<|im_end|>\n<|im_start|>assistant\n"
        out = _local_pipeline(formatted_prompt, max_new_tokens=60, do_sample=False)
        gen_text = out[0]['generated_text']
        if "<|im_start|>assistant\n" in gen_text:
            ans = gen_text.split("<|im_start|>assistant\n")[-1].strip()
        else:
            ans = gen_text.replace(formatted_prompt, "").strip()
        ans = re.sub(r'<\|im_end\|>', '', ans)
        ans = re.sub(r'[\"\']', '', ans).replace('\n', ' ')
        ans = re.sub(r'^(大綱|摘要|總結|劇情)[:：]', '', ans).strip()
        if len(ans) > max_chars:
            ans = ans[:max_chars - 1] + "。"
        elif not ans.endswith(("。", "！", "？")):
            ans += "。"
        return ans, "在地AI (Qwen2.5-0.5B)"
    except Exception as e:
        logging.warning(f"[SummaryGen] 在地 AI 執行無法使用: {e}")
        return None, None

def generate_ai_chapter_summary(chapter_num, content, max_chars=50, book_title=""):
    """
    將章節整篇 TXT 內容發送給在地 AI (Qwen) 或線上 API 進行 40 字大綱總結。
    """
    clean_text = content.strip()
    if not clean_text:
        return "本章暫無摘要", "無內文"

    # ── 1. 過濾 RawText 中的網頁廣告與雜訊，擷取真正的章節標題與內文 ──
    lines = [l.strip() for l in clean_text.splitlines() if l.strip()]
    cleaned_lines = []
    for l in lines:
        if any(noise in l for noise in ["請記住本站域名", "黃金屋", "hjwzw", "讀者", "Chapter"]):
            continue
        cleaned_lines.append(l)

    chapter_title = cleaned_lines[0] if cleaned_lines else f"第{chapter_num}章"
    body_lines = [l for l in cleaned_lines[1:] if l != chapter_title and book_title not in l]
    body_text_full = "\n".join(body_lines)

    prompt = (
        f"請讀取小說《{book_title}》第{chapter_num}章（標題：{chapter_title}）的內文，用繁體中文寫出一句40字以內的劇情大綱，"
        f"精準說明本章發生的關鍵事件，切勿包含「本章講述」、「展開冒險」等廢話：\n\n{body_text_full[:3000]}"
    )

    # ── 2. 優先嘗試在地免費 AI 模型 (GitHub Actions / 本機環境) ──
    local_ans, local_model = get_local_ai_summary(prompt, max_chars=max_chars)
    if local_ans:
        return local_ans, local_model

    # ── 3. 在地 AI 無法使用時，嘗試免費線上 API ──
    try:
        import requests
        import urllib.parse

        url = "https://text.pollinations.ai/"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

        # 嘗試模型選項 (openai-fast 與 預設模型)
        models_to_try = ["openai-fast", ""]
        for model_name in models_to_try:
            try:
                payload = {"messages": [{"role": "user", "content": prompt}]}
                if model_name:
                    payload["model"] = model_name

                resp = requests.post(url, json=payload, headers=headers, timeout=8)
                
                if resp.status_code == 200 and resp.text:
                    ans = resp.text.strip()
                    clean_ans = re.sub(r'[\"\']', '', ans).replace('\n', ' ')
                    clean_ans = re.sub(r'^(大綱|摘要|總結|劇情)[:：]', '', clean_ans).strip()
                    if len(clean_ans) >= 10 and not clean_ans.startswith("{"):
                        if len(clean_ans) > max_chars:
                            clean_ans = clean_ans[:max_chars - 1] + "。"
                        elif not clean_ans.endswith(("。", "！", "？")):
                            clean_ans += "。"
                        return clean_ans, f"AI模型 ({model_name or 'default'})"
            except Exception as e:
                logging.debug(f"[SummaryGen] AI 嘗試失敗: {e}")

        # GET Fallback
        try:
            encoded_p = urllib.parse.quote(prompt[:500])
            get_url = f"https://text.pollinations.ai/{encoded_p}?model=openai-fast"
            resp = requests.get(get_url, headers=headers, timeout=8)
            if resp.status_code == 200 and resp.text:
                ans = resp.text.strip()
                clean_ans = re.sub(r'[\"\']', '', ans).replace('\n', ' ')
                clean_ans = re.sub(r'^(大綱|摘要|總結|劇情)[:：]', '', clean_ans).strip()
                if len(clean_ans) >= 10 and not clean_ans.startswith("{"):
                    if len(clean_ans) > max_chars:
                        clean_ans = clean_ans[:max_chars - 1] + "。"
                    elif not clean_ans.endswith(("。", "！", "？")):
                        clean_ans += "。"
                    return clean_ans, "AI模型 (GET)"
        except Exception:
            pass

    except Exception as e:
        logging.warning(f"[SummaryGen] 連線 AI 伺服器失敗: {e}")

    # ── 3. AI 嘗試均失敗，明確回傳 '本章暫無摘要' ──
    return "本章暫無摘要", "AI失敗"

def get_or_generate_chapter_summary(workspace_dir, book_title, chap_num):
    """
    取得或自動生成指定章節的摘要文字，並儲存至 Workspace/{book_title}/Summaries/
    """
    summaries_dir = os.path.join(workspace_dir, "Summaries")
    os.makedirs(summaries_dir, exist_ok=True)

    summary_file = os.path.join(summaries_dir, f"{book_title}_chapter_{chap_num}_summary.txt")

    if os.path.exists(summary_file) and os.path.getsize(summary_file) > 5:
        try:
            with open(summary_file, "r", encoding="utf-8") as f:
                cached_sum = f.read().strip()
            if cached_sum and cached_sum != "本章暫無摘要":
                logging.info(f"[SummaryGen] ✓ 讀取已有摘要快取: {os.path.basename(summary_file)}")
                return cached_sum
        except Exception:
            pass
    # 優先讀取 RawText (包含完整的章節標題，訊息量最大)
    raw_path   = os.path.join(workspace_dir, "RawText", f"{book_title}_chapter_{chap_num}_raw.txt")
    clean_path = os.path.join(workspace_dir, "CleanText", f"{book_title}_chapter_{chap_num}_clean.txt")

    text_content = ""
    if os.path.exists(raw_path):
        try:
            with open(raw_path, "r", encoding="utf-8") as f:
                text_content = f.read()
        except Exception:
            pass
    elif os.path.exists(clean_path):
        try:
            with open(clean_path, "r", encoding="utf-8") as f:
                text_content = f.read()
        except Exception:
            pass

    summary_text, model_used = generate_ai_chapter_summary(chap_num, text_content, max_chars=50, book_title=book_title)
    logging.info(f"[SummaryGen] ✓ 第 {chap_num} 章摘要生成成功 (來源: {model_used}): {summary_text}")

    try:
        with open(summary_file, "w", encoding="utf-8") as f:
            f.write(summary_text)
    except Exception as e:
        logging.warning(f"[SummaryGen] 寫入摘要檔失敗: {e}")

    return summary_text
