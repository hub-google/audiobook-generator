import os
import sys
import glob
import re
import json
import time
from g4f.client import Client

# 設定 Windows UTF-8 控制台輸出
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')

# ---------------------------------------------------------
# 設定路徑與參數
# ---------------------------------------------------------
PROJECT_DIR = r"g:\我的雲端硬碟\作品\有聲小說"
CLEAN_TEXT_DIR = os.path.join(PROJECT_DIR, "Workspace", "凡人修仙傳", "CleanText")
OUTPUT_JSON = os.path.join(PROJECT_DIR, "Workspace", "凡人修仙傳", "chapter_summaries.json")
OUTPUT_MD = os.path.join(PROJECT_DIR, "Workspace", "凡人修仙傳", "chapter_summaries.md")

# ---------------------------------------------------------
# 1. 免 API AI 總結函數 (結合 g4f 與 備用 Local 精准抽取)
# ---------------------------------------------------------
def generate_ai_chapter_summary(chapter_num, content, max_chars=50):
    """
    使用免費 AI (g4f / GPT-4o-mini / LLM) 對章節內文進行 50 字內的大綱總結
    """
    # 取前 2500 字進行摘要（涵蓋大部分章節精華與主要動態）
    sample_text = content[:2500]
    
    prompt = (
        f"請閱讀以下《凡人修仙傳》第 {chapter_num} 章的內文，用繁體中文總結這一章的故事大綱與核心劇情發展。\n"
        f"【硬性要求】：\n"
        f"1. 必須使用繁體中文。\n"
        f"2. 內容精準，切中核心人物與關鍵事件。\n"
        f"3. 總字數嚴格控制在 {max_chars} 字以內。\n\n"
        f"章節內文摘要：\n{sample_text}"
    )

    # 嘗試 1: 使用 g4f Client 多模型重試
    models_to_try = ["gpt-4o-mini", "qwen-2.5-72b", "llama-3.3-70b"]
    client = Client()

    for model in models_to_try:
        try:
            print(f"   --> 嘗試使用免 API AI ({model}) 生成摘要...")
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "你是一位專業的小說劇情總結專家，善於提煉故事核心，請精準控制字數在50字以內。"},
                    {"role": "user", "content": prompt}
                ]
            )
            raw_ans = response.choices[0].message.content.strip()
            # 清理可能的 markdown 標籤或引號
            clean_ans = re.sub(f'[\"\']', '', raw_ans).replace('\n', ' ')
            if len(clean_ans) > 0:
                # 確保字數截斷在 50 字以內
                if len(clean_ans) > max_chars:
                    clean_ans = clean_ans[:max_chars-3] + "..."
                return clean_ans, model
        except Exception as e:
            # 繼續嘗試下一個模型
            continue
            
    # 嘗試 2: 備用本地抽取法 (如網路異常時的保底機制)
    print("   --> [備用機制] 線上 AI 暫時忙碌，啟用關鍵句本地提煉算法...")
    sentences = [s.strip() for s in re.split(r'[。！!？?\n]', content) if len(s.strip()) > 8]
    # 選取開頭與情節關鍵句
    selected = sentences[:2]
    fallback_summary = "。".join(selected) + "。"
    if len(fallback_summary) > max_chars:
        fallback_summary = fallback_summary[:max_chars-3] + "..."
    return fallback_summary, "Local Extractor"


# ---------------------------------------------------------
# 主執行流程
# ---------------------------------------------------------
def main():
    print("==================================================")
    print("      免 API AI 章節大綱總結器 (50字內測試)      ")
    print("==================================================\n")
    
    if not os.path.exists(CLEAN_TEXT_DIR):
        print(f"錯誤：找不到 CleanText 目錄 ({CLEAN_TEXT_DIR})")
        return

    # 搜尋所有 CleanText 檔案
    txt_files = glob.glob(os.path.join(CLEAN_TEXT_DIR, "*_clean.txt"))
    if not txt_files:
        print("未找到任何 CleanText 章節檔案！")
        return

    # 解析章節編號並排序
    chapter_items = []
    for filepath in txt_files:
        match = re.search(r'chapter_(\d+)_clean\.txt', os.path.basename(filepath))
        if match:
            chap_num = int(match.group(1))
            chapter_items.append((chap_num, filepath))

    chapter_items.sort(key=lambda x: x[0])
    
    # 限制處理前 10 章
    chapter_items = chapter_items[:10]
    print(f"找到 {len(chapter_items)} 章節，開始逐章進行 AI 摘要分析...\n")

    summaries = []

    for chap_num, filepath in chapter_items:
        print(f"正處理：第 {chap_num:02d} 章 [{os.path.basename(filepath)}]")
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()

        summary_text, source_model = generate_ai_chapter_summary(chap_num, content, max_chars=50)
        
        print(f" -> 來源: [{source_model}]")
        print(f" -> 摘要 ({len(summary_text)}字): {summary_text}\n")

        summaries.append({
            "chapter": chap_num,
            "filename": os.path.basename(filepath),
            "summary": summary_text,
            "length": len(summary_text),
            "model_used": source_model
        })

        # 適度間隔避免連線過密
        time.sleep(1)

    # ---------------------------------------------------------
    # 保存總結結果 (JSON & Markdown)
    # ---------------------------------------------------------
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(summaries, f, ensure_ascii=False, indent=2)

    with open(OUTPUT_MD, "w", encoding="utf-8") as f:
        f.write("# 《凡人修仙傳》1-10 章 AI 精簡劇情大綱（50字內）\n\n")
        for item in summaries:
            f.write(f"### 第 {item['chapter']} 章\n")
            f.write(f"- **劇情摘要**：{item['summary']}\n")
            f.write(f"- **字數**：{item['length']} 字\n")
            f.write(f"- **AI 模型**：{item['model_used']}\n\n")

    print("==================================================")
    print("摘要完成！結果已保存至：")
    print(f"1. JSON: {OUTPUT_JSON}")
    print(f"2. Markdown: {OUTPUT_MD}")
    print("==================================================")

if __name__ == "__main__":
    main()
