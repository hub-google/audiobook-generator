import os
import wave
import contextlib
import logging
from datetime import timedelta

def get_wav_duration(wav_path):
    with contextlib.closing(wave.open(wav_path, 'r')) as f:
        frames = f.getnframes()
        rate = f.getframerate()
        return frames / float(rate)

def format_timestamp(seconds):
    """將秒數轉換為 SRT 時間戳格式 HH:MM:SS,mmm"""
    td = timedelta(seconds=seconds)
    total_seconds = int(td.total_seconds())
    milliseconds = int(td.microseconds / 1000)
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    secs = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"

def parse_timestamp_to_seconds(ts_str):
    """將 SRT 時間戳 HH:MM:SS,mmm 轉為秒數"""
    parts = ts_str.split(',')
    time_parts = parts[0].split(':')
    hours = int(time_parts[0])
    minutes = int(time_parts[1])
    seconds = int(time_parts[2])
    milliseconds = int(parts[1]) if len(parts) > 1 else 0
    return hours * 3600 + minutes * 60 + seconds + milliseconds / 1000.0

def split_long_subtitle_text(text, max_len=22):
    """將過長的單行字幕依標點符號或中點拆分為多行（最大單行 ~22 字）"""
    text = text.strip()
    if len(text) <= max_len:
        return text

    # 找尋最靠近中點的標點符號進行斷句換行
    punct_indices = [i for i, c in enumerate(text) if c in "，；,;!！?？ "]
    if punct_indices:
        mid = len(text) / 2.0
        best_idx = min(punct_indices, key=lambda idx: abs(idx - mid))
        part1 = text[:best_idx + 1].strip()
        part2 = text[best_idx + 1:].strip()
        if part1 and part2:
            if len(part2) > max_len:
                part2 = split_long_subtitle_text(part2, max_len)
            return f"{part1}\n{part2}"

    # 若無標點符號，則從中點硬切
    mid = len(text) // 2
    part1 = text[:mid].strip()
    part2 = text[mid:].strip()
    if len(part2) > max_len:
        part2 = split_long_subtitle_text(part2, max_len)
    return f"{part1}\n{part2}"

import re

def strip_subtitle_punctuation(text):
    """
    符合專業影視字幕規範：
    1. 移除句尾與行內末端的所有標點符號（逗號、句號、頓號、分號等）。
    2. 保持字幕畫面簡潔幹練，無標點干擾。
    """
    if not text:
        return ""
    lines = text.split('\n')
    cleaned_lines = []
    for line in lines:
        line = line.strip()
        # 移除末端的所有標點符號
        line = re.sub(r'[，,。；;、！!？?\s]+$', '', line)
        # 移除開頭的獨立標點
        line = re.sub(r'^[，,。；;、\s]+', '', line)
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines)

def generate_chapter_srt(part_wav_paths, lines, srt_output_path):
    """
    根據每個分段 WAV 的長度，產生 SRT 檔案 (自動清理句尾標點符號與長句折行)
    """
    if len(part_wav_paths) != len(lines):
        logging.warning(f"[Subtitle] Mismatch: {len(part_wav_paths)} wavs vs {len(lines)} lines")
    
    current_time = 0.0
    
    with open(srt_output_path, "w", encoding="utf-8") as f:
        for idx, (wav_path, text) in enumerate(zip(part_wav_paths, lines)):
            text = text.strip()
            if not text:
                continue
                
            try:
                duration = get_wav_duration(wav_path)
            except Exception as e:
                logging.error(f"[Subtitle] Cannot read duration for {wav_path}: {e}")
                duration = 2.0 # fallback
                
            start_ts = format_timestamp(current_time)
            end_time = current_time + duration
            end_ts = format_timestamp(end_time)
            
            # 處理長句折行並清空末端標點符號
            formatted_text = split_long_subtitle_text(text, max_len=22)
            clean_srt_text = strip_subtitle_punctuation(formatted_text)
            
            f.write(f"{idx + 1}\n")
            f.write(f"{start_ts} --> {end_ts}\n")
            f.write(f"{clean_srt_text}\n\n")
            
            current_time = end_time
            
    logging.info(f"[Subtitle] ✓ Generated SRT: {os.path.basename(srt_output_path)}")

def merge_srts(chapter_srt_paths, chapter_durations, full_srt_output_path):
    """
    合併所有章節的 SRT，並根據各章節的起始時間平移時間戳
    chapter_srt_paths: ['chapter_1.srt', 'chapter_2.srt', ...]
    chapter_durations: [300.5, 450.2, ...] 對應各章節的【總時長】
    """
    current_offset = 0.0
    global_index = 1
    
    with open(full_srt_output_path, "w", encoding="utf-8") as out_f:
        for srt_path, duration in zip(chapter_srt_paths, chapter_durations):
            if not os.path.exists(srt_path):
                logging.warning(f"[Subtitle] Missing SRT for merge: {srt_path}")
                current_offset += duration
                continue
                
            with open(srt_path, "r", encoding="utf-8") as in_f:
                content = in_f.read().strip().split('\n\n')
                
                for block in content:
                    lines = block.split('\n')
                    if len(lines) >= 3:
                        time_line = lines[1]
                        if '-->' in time_line:
                            start_str, end_str = time_line.split('-->')
                            start_sec = parse_timestamp_to_seconds(start_str.strip())
                            end_sec = parse_timestamp_to_seconds(end_str.strip())
                            
                            new_start_ts = format_timestamp(start_sec + current_offset)
                            new_end_ts = format_timestamp(end_sec + current_offset)
                            
                            out_f.write(f"{global_index}\n")
                            out_f.write(f"{new_start_ts} --> {new_end_ts}\n")
                            for text_line in lines[2:]:
                                clean_t = strip_subtitle_punctuation(text_line)
                                out_f.write(f"{clean_t}\n")
                            out_f.write("\n")
                            
                            global_index += 1
            
            current_offset += duration

    logging.info(f"[Subtitle] ✓ Merged full SRT: {os.path.basename(full_srt_output_path)}")
