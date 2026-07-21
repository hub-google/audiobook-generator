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

def generate_chapter_srt(part_wav_paths, lines, srt_output_path):
    """
    根據每個分段 WAV 的長度，產生 SRT 檔案
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
            
            f.write(f"{idx + 1}\n")
            f.write(f"{start_ts} --> {end_ts}\n")
            f.write(f"{text}\n\n")
            
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
                                out_f.write(f"{text_line}\n")
                            out_f.write("\n")
                            
                            global_index += 1
            
            current_offset += duration

    logging.info(f"[Subtitle] ✓ Merged full SRT: {os.path.basename(full_srt_output_path)}")
