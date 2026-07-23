import os
import sys
import glob
import re
import json
import time
import logging

_local_pipeline = None

def get_local_ai_summary(prompt, max_chars=130):
    global _local_pipeline
    if _local_pipeline is False:
        return None, None
    try:
        if _local_pipeline is None:
            from transformers import pipeline, AutoModelForCausalLM, AutoTokenizer
            logging.info("[SummaryGen] 🚀 載入在地 AI 模型 (Qwen/Qwen2.5-1.5B-Instruct)...")
            model_id = "Qwen/Qwen2.5-1.5B-Instruct"
            tokenizer = AutoTokenizer.from_pretrained(model_id)
            model = AutoModelForCausalLM.from_pretrained(model_id, low_cpu_mem_usage=False)
            _local_pipeline = pipeline(
                "text-generation",
                model=model,
                tokenizer=tokenizer,
                device=-1
            )
        formatted_prompt = (
            f"<|im_start|>system\n"
            f"你是一位精準的小說劇情摘要專家。請嚴格根據提供的小說章節內文，用繁體中文寫出一段100字左右（約80~130字）的劇情大綱。\n"
            f"【硬性要求】：\n"
            f"1. 必須100%忠實於原文，嚴禁憑空捏造文中未出現的人物或未發生的情節。\n"
            f"2. 必須清楚說明本章出場的核心人物與關鍵劇情發展。\n"
            f"3. 繁體中文，語意完整流暢，字數嚴格控制在 80~130 字之間。\n"
            f"<|im_end|>\n"
            f"<|im_start|>user\n{prompt}<|im_end|>\n"
            f"<|im_start|>assistant\n"
        )
        out = _local_pipeline(
            formatted_prompt,
            max_new_tokens=180,
            do_sample=True,
            temperature=0.1,
            top_p=0.9,
            repetition_penalty=1.15
        )
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
        return ans, "在地AI (Qwen2.5-1.5B)"
    except Exception as e:
        logging.warning(f"[SummaryGen] 在地 AI 執行無法使用: {e}")
        _local_pipeline = False
        return None, None

def generate_ai_chapter_summary(chapter_num, content, max_chars=130, book_title=""):
    """
    將章節整篇 TXT 內容發送給在地 AI (Qwen2.5-1.5B) 進行 100 字左右的大綱總結。
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
        f"請讀取小說《{book_title}》第{chapter_num}章（標題：{chapter_title}）的內文，用繁體中文寫出一段100字左右的劇情大綱，"
        f"精準說明本章登場人物與發生的關鍵事件：\n\n{body_text_full[:3000]}"
    )

    # ── 2. 完全使用在地 AI 模型 (Qwen/Qwen2.5-1.5B-Instruct) ──
    local_ans, local_model = get_local_ai_summary(prompt, max_chars=max_chars)
    if local_ans:
        return local_ans, local_model

    # ── 3. 在地 AI 無法使用時，明確回傳 '本章暫無摘要' (不使用線上免費 API) ──
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

    summary_text, model_used = generate_ai_chapter_summary(chap_num, text_content, max_chars=130, book_title=book_title)
    logging.info(f"[SummaryGen] ✓ 第 {chap_num} 章摘要生成成功 (來源: {model_used}): {summary_text}")

    try:
        with open(summary_file, "w", encoding="utf-8") as f:
            f.write(summary_text)
    except Exception as e:
        logging.warning(f"[SummaryGen] 寫入摘要檔失敗: {e}")

    return summary_text

if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding='utf-8')
    logging.basicConfig(level=logging.INFO)
    print("==================================================")
    print("      🚀 測試執行 summary_gen.py 摘要生成        ")
    print("==================================================")
    
    # 測試執行第 1 章與第 2 章摘要
    for chap in [1, 2]:
        res = get_or_generate_chapter_summary("Workspace/凡人修仙傳", "凡人修仙傳", chap)
        print(f"\n第 {chap} 章結果 ({len(res)}字):")
        print(res)
        print("-" * 50)

