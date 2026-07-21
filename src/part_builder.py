"""
part_builder.py — 音檔/影片 10~11 小時無縫分部 (Part) 切分與自動打包工具

對應使用者需求：
1. 每部影片目標長度為 10~11 小時 (避免 12 小時 YouTube 上限與 6 小時 CI/CD 上限)。
2. 各部之間無縫銜接，絕不遺漏任何章節 (例如：第一部 1~76 章，第二部必定從 77 章開始)。
"""

import os
import sys
import glob
import re
import yaml
import wave
import contextlib
import subprocess
import shutil
import logging

SRC_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SRC_DIR)

def get_ffmpeg_path():
    cmd = shutil.which("ffmpeg")
    if cmd:
        return cmd
    local_path = r"C:\Users\cyt18\anaconda3\Library\bin\ffmpeg.exe"
    if os.path.exists(local_path):
        return local_path
    return "ffmpeg"

def get_ffprobe_path():
    cmd = shutil.which("ffprobe")
    if cmd:
        return cmd
    local_path = r"C:\Users\cyt18\anaconda3\Library\bin\ffprobe.exe"
    if os.path.exists(local_path):
        return local_path
    return "ffprobe"

FFMPEG_PATH = get_ffmpeg_path()
FFPROBE_PATH = get_ffprobe_path()

def parse_chapter_num(filename):
    """從檔名提取章節編號 (如 chapter_76.mp4 -> 76)"""
    m = re.search(r'chapter_(\d+)', filename, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return 999999

def get_media_duration(file_path):
    """取得 WAV / MP4 的精準時長（秒）"""
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".wav":
        try:
            with contextlib.closing(wave.open(file_path, 'r')) as f:
                frames = f.getnframes()
                rate = f.getframerate()
                if rate > 0:
                    return frames / float(rate)
        except Exception:
            pass

    # fallback 到 ffprobe
    cmd = [
        FFPROBE_PATH, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        file_path
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode == 0 and res.stdout.strip():
            return float(res.stdout.strip())
    except Exception as e:
        logging.warning(f"ffprobe 無法讀取時長 {file_path}: {e}")

    # fallback 2: ffmpeg -i 解析 Duration
    try:
        cmd_ffmpeg = [FFMPEG_PATH, "-i", file_path]
        res_ff = subprocess.run(cmd_ffmpeg, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        m = re.search(r'Duration:\s*(\d+):(\d+):(\d+\.\d+)', res_ff.stderr)
        if m:
            h, m_m, s = float(m.group(1)), float(m.group(2)), float(m.group(3))
            return h * 3600.0 + m_m * 60.0 + s
    except Exception:
        pass

    return 0.0

def format_timestamp(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def partition_chapters(file_list, min_hours=10.0, max_hours=11.0):
    """
    動態視窗分部演算邏輯：
    1. 將所有可用的章節按自然數編號正序排列 1, 2, 3...
    2. 逐章累加時長至 10~11 小時（36,000 秒 ~ 39,600 秒）。
    3. 若加入下一章會超過 max_hours (11 小時)，則在此封存該 Part。
    4. 下一部 (Part) 永遠且無縫接續下一章，保證 100% 不遺漏章節。
    """
    items = []
    for fp in file_list:
        c_num = parse_chapter_num(os.path.basename(fp))
        dur = get_media_duration(fp)
        items.append({
            "path": fp,
            "chap_num": c_num,
            "dur": dur
        })

    # 按章節編號升序排序
    items.sort(key=lambda x: x["chap_num"])

    min_seconds = min_hours * 3600.0
    max_seconds = max_hours * 3600.0

    parts = []
    current_part_items = []
    current_duration = 0.0

    for item in items:
        dur = item["dur"]

        # 如果目前的 Part 已經有內容，且加入這一章會超過 11 小時（或已達到 10 小時且下一章會超過 11 小時）
        if current_part_items and (current_duration + dur > max_seconds or (current_duration >= min_seconds and current_duration + dur > max_seconds)):
            # 封存目前 Part
            parts.append({
                "part_num": len(parts) + 1,
                "start_chap": current_part_items[0]["chap_num"],
                "end_chap": current_part_items[-1]["chap_num"],
                "files": [x["path"] for x in current_part_items],
                "items": current_part_items,
                "duration": current_duration
            })
            current_part_items = []
            current_duration = 0.0

        current_part_items.append(item)
        current_duration += dur

    # 處理最後剩餘的章節
    if current_part_items:
        parts.append({
            "part_num": len(parts) + 1,
            "start_chap": current_part_items[0]["chap_num"],
            "end_chap": current_part_items[-1]["chap_num"],
            "files": [x["path"] for x in current_part_items],
            "items": current_part_items,
            "duration": current_duration
        })

    logging.info(f"[PartBuilder] 完成分部規劃：全書 {len(items)} 章共劃分為 {len(parts)} 部影片 (每部 ~{min_hours}-{max_hours} 小時)")
    for p in parts:
        logging.info(
            f"  └─ 部數 【第 {p['part_num']} 部】: 第 {p['start_chap']:04d} ~ {p['end_chap']:04d} 章 "
            f"({len(p['files'])} 章，總長度: {p['duration']/3600:.2f} 小時 / {p['duration']:.1f}s)"
        )
    return parts

def merge_part_videos(part_info, output_video_path):
    """使用 FFmpeg concat demuxer (-c copy) 極速無損合併單一部數的所有章節 MP4 影片"""
    files = part_info["files"]
    if not files:
        return False

    if len(files) == 1:
        shutil.copy(files[0], output_video_path)
        return True

    os.makedirs(os.path.dirname(output_video_path), exist_ok=True)
    concat_txt = os.path.join(os.path.dirname(output_video_path), f"concat_part_{part_info['part_num']}.txt")

    with open(concat_txt, "w", encoding="utf-8") as f:
        for mp4 in files:
            safe_p = os.path.abspath(mp4).replace("\\", "/")
            f.write(f"file '{safe_p}'\n")

    cmd = [
        FFMPEG_PATH, "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", concat_txt,
        "-c", "copy",
        output_video_path
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if os.path.exists(concat_txt):
        try:
            os.remove(concat_txt)
        except Exception:
            pass

    ok = res.returncode == 0 and os.path.exists(output_video_path)
    if ok:
        size_mb = os.path.getsize(output_video_path) / (1024 * 1024)
        logging.info(f"✅ 【第 {part_info['part_num']} 部】無損影片合併成功 -> {os.path.basename(output_video_path)} ({size_mb:.1f} MB)")
    else:
        logging.error(f"❌ 【第 {part_info['part_num']} 部】影片合併失敗: {res.stderr}")
    return ok

def build_all_parts(book_title, workspace_dir=None, output_dir=None, min_hours=10.0, max_hours=11.0):
    """
    主整合入口：讀取 Workspace/ 下的章節 MP4，自動執行 10~11 小時無縫分部封裝。
    """
    if not workspace_dir:
        workspace_dir = os.path.abspath(os.path.join(SRC_DIR, "..", "Workspace", book_title))
    if not output_dir:
        output_dir = os.path.abspath(os.path.join(SRC_DIR, "..", "Output", book_title))

    video_dir = os.path.join(workspace_dir, "Video")
    mp4_files = sorted(glob.glob(os.path.join(video_dir, "*.mp4")), key=parse_chapter_num)

    if not mp4_files:
        logging.warning(f"[PartBuilder] 找不到章節 MP4 檔案: {video_dir}")
        return []

    parts = partition_chapters(mp4_files, min_hours=min_hours, max_hours=max_hours)
    parts_out_dir = os.path.join(output_dir, "Parts")
    os.makedirs(parts_out_dir, exist_ok=True)

    built_parts = []
    from metadata_gen import save_book_metadata, get_chapter_title

    for p in parts:
        part_num = p["part_num"]
        start_c = p["start_chap"]
        end_c = p["end_chap"]
        part_filename = f"{book_title}_Part_{part_num:02d}_Ch{start_c:04d}_to_Ch{end_c:04d}.mp4"
        part_video_path = os.path.join(parts_out_dir, part_filename)

        # 1. 無損合併影片
        merge_ok = merge_part_videos(p, part_video_path)

        # 2. 生成對應 Part 的獨立 Metadata (標題, 簡介, 2K 封面)
        part_ws_dir = os.path.join(workspace_dir, f"Part_{part_num:02d}")
        meta = save_book_metadata(
            book_title=book_title,
            start_chap=start_c,
            end_chap=end_c,
            workspace_dir=part_ws_dir,
            is_completed=True,
            part_num=part_num
        )

        # 3. 導出該 Part 的章節時間戳選單 (Chapter Timestamps Menu)
        ts_file = os.path.join(part_ws_dir, "youtube_metadata.txt")
        with open(ts_file, "w", encoding="utf-8") as f:
            f.write(f"【{book_title}】第 {start_c}~{end_c} 章【第 {part_num} 部】\n\n")
            f.write("⏳ 章節時間戳選單：\n")
            curr_time = 0.0
            for item in p["items"]:
                c_num = item["chap_num"]
                c_title = get_chapter_title(workspace_dir, book_title, c_num)
                f.write(f"{format_timestamp(curr_time)} {c_title}\n")
                curr_time += item["dur"]
            f.write("\n---\n")
            f.write("⚠️ 本內容採用 AI 輔助製作，配音與視覺皆經優化處理。\n")

        p["merged_video"] = part_video_path if merge_ok else None
        p["metadata"] = meta
        p["timestamps_file"] = ts_file
        built_parts.append(p)

    return built_parts

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # 測試模擬章節檔列表
    dummy_files = [f"Workspace/Test/Video/Test_chapter_{i}.mp4" for i in range(1, 151)]
    print(f"測試分部 logic (150 個章節)...")
