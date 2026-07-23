import os
import sys
import glob
import re
import json
import time

# 設定 Windows UTF-8 控制台輸出
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')

# ---------------------------------------------------------
# 設定路徑與參數
# ---------------------------------------------------------
PROJECT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(PROJECT_DIR, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from summary_gen import generate_ai_chapter_summary

CLEAN_TEXT_DIR = os.path.join(PROJECT_DIR, "Workspace", "凡人修仙傳", "CleanText")
OUTPUT_JSON = os.path.join(PROJECT_DIR, "Workspace", "凡人修仙傳", "chapter_summaries.json")
OUTPUT_MD = os.path.join(PROJECT_DIR, "Workspace", "凡人修仙傳", "chapter_summaries.md")


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
