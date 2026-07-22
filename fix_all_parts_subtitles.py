import os
import sys
import time
import subprocess
from datetime import datetime

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.force-ssl"
]

PARTS_INFO = [
    {"part": 1, "video_id": "eFGXvcO0BEI", "srt_name": "Part_01_Ch0001_to_Ch0103.srt"},
    {"part": 2, "video_id": "FZP28Kbg8J4", "srt_name": "Part_02_Ch0104_to_Ch0184.srt"},
    {"part": 3, "video_id": "d2FrlcFQUQA", "srt_name": "Part_03_Ch0185_to_Ch0253.srt"},
    {"part": 4, "video_id": "CQ9K07saP0A", "srt_name": "Part_04_Ch0254_to_Ch0322.srt"},
    {"part": 5, "video_id": "Bv7Cgs_P7v8", "srt_name": "Part_05_Ch0323_to_Ch0392.srt"},
    {"part": 6, "video_id": "R1nYUI_cA38", "srt_name": "Part_06_Ch0393_to_Ch0462.srt"},
    {"part": 7, "video_id": "vOu1-U68NBo", "srt_name": "Part_07_Ch0463_to_Ch0531.srt"},
]

def upload_single_caption(yt, p):
    part_num = p["part"]
    video_id = p["video_id"]
    srt_path = os.path.abspath(os.path.join("Output_Subtitles", p["srt_name"]))

    if not os.path.exists(srt_path):
        print(f"❌ [Part {part_num}] 找不到字幕檔: {srt_path}")
        return False

    file_size_kb = os.path.getsize(srt_path) / 1024.0
    print(f"\n==================================================")
    print(f"🎬 嘗試 API 上傳【第 {part_num} 部】CC 字幕 (Video ID: {video_id}, 檔案: {p['srt_name']}, 大小: {file_size_kb:.1f} KB)...")

    # 1. 刪除既有舊字幕軌
    try:
        cap_list = yt.captions().list(part="snippet", videoId=video_id).execute()
        for item in cap_list.get("items", []):
            c_id = item["id"]
            print(f"  🧹 清除歷史字幕軌 (ID: {c_id})...")
            try:
                yt.captions().delete(id=c_id).execute()
            except Exception as e:
                print(f"    無法刪除 {c_id}: {e}")
    except Exception as e:
        if "quotaExceeded" in str(e):
            raise e
        print(f"  無法查詢字幕軌: {e}")

    # 2. 上傳全新 SRT
    body = {
        "snippet": {
            "videoId": video_id,
            "language": "zh-TW",
            "name": "繁體中文",
            "isDraft": False
        }
    }
    media = MediaFileUpload(srt_path, mimetype="*/*", resumable=False)

    req = yt.captions().insert(part="snippet", body=body, media_body=media)
    res = req.execute()
    cap_id = res.get("id")
    print(f"🎉 🎉 成功！【第 {part_num} 部】完整 10+ 小時 CC 字幕實打實上傳生效！(Caption ID: {cap_id})")
    return True

def main():
    print("=== 🚀 啟動字幕自動補傳哨兵服務 (Quota 重置自動續傳) ===")
    
    creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    yt = build("youtube", "v3", credentials=creds)

    pending_parts = list(PARTS_INFO)

    while pending_parts:
        current_p = pending_parts[0]
        try:
            success = upload_single_caption(yt, current_p)
            if success:
                pending_parts.pop(0)
                time.sleep(2.0)
        except Exception as e:
            err_msg = str(e)
            if "quotaExceeded" in err_msg or "403" in err_msg:
                print(f"\n⚠️ [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 觸發 YouTube API 今日 Quota 上限！")
                print("   自動進入休眠防護模式，每 30 分鐘自動重試，直到配額重置並完成全部上傳...")
                time.sleep(1800) # 休眠 30 分鐘再試
            else:
                print(f"❌ 上傳發生異常: {e}，5 分鐘後重試...")
                time.sleep(300)

    print("\n🎉🎉🎉 全部 7 部影片的完整 10+ 小時 CC 字幕已全數補傳完畢並上線生效！")

if __name__ == "__main__":
    main()
