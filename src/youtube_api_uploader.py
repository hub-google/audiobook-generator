"""
youtube_api_uploader.py — YouTube Data API v3 暴速影片上傳 + 自動播放清單建置工具
"""

import os
import sys
import glob
import re
import time
import shutil
import argparse
import logging
import subprocess

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, HttpError

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [YouTube-API] %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube"
]

def get_authenticated_service():
    """獲取與授權 YouTube API v3 Service (支援 client_secret.json / token.json / env refresh token)"""
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    token_path = os.path.join(BASE_DIR, "token.json")
    client_secret_path = os.path.join(BASE_DIR, "client_secret.json")

    creds = None

    # 1. 嘗試從 token.json 讀取
    if os.path.exists(token_path):
        try:
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        except Exception as e:
            logging.warning(f"無法讀取 token.json: {e}")

    # 2. 嘗試從環境變數 (YOUTUBE_REFRESH_TOKEN, YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET) 讀取
    ref_token = os.environ.get("YOUTUBE_REFRESH_TOKEN", "").strip()
    client_id = os.environ.get("YOUTUBE_CLIENT_ID", "").strip()
    client_secret = os.environ.get("YOUTUBE_CLIENT_SECRET", "").strip()

    if (not creds or not creds.valid) and ref_token and client_id and client_secret:
        creds = Credentials(
            token=None,
            refresh_token=ref_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=SCOPES
        )

    # 3. 嘗試動態合成 client_secret.json (如果 CI/CD 或環境中不存在)
    if not os.path.exists(client_secret_path) and client_id and client_secret:
        try:
            import json
            cs_data = {
                "installed": {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token"
                }
            }
            with open(client_secret_path, "w", encoding="utf-8") as f:
                json.dump(cs_data, f)
            logging.info(f"✅ 已由環境變數動態生成 {client_secret_path}")
        except Exception as e:
            logging.warning(f"無法寫入 client_secret.json: {e}")

    # 4. 重新整理 Token
    if creds and creds.refresh_token:
        try:
            creds.refresh(Request())
            with open(token_path, "w", encoding="utf-8") as token_file:
                token_file.write(creds.to_json())
        except Exception as e:
            logging.warning(f"重新整理 Refresh Token 失敗: {e}")
            creds = None

    # 5. 如果是本地執行且沒有有效憑證，開瀏覽器一鍵登入授權
    if not creds or not creds.valid:
        if not os.path.exists(client_secret_path):
            logging.error(f"❌ 找不到 client_secret.json 且未設定 YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET！請在 GitHub Secrets 設定憑證。")
            sys.exit(1)

        try:
            logging.info("🔑 正在開啟瀏覽器進行 YouTube OAuth2 登入授權...")
            flow = InstalledAppFlow.from_client_secrets_file(client_secret_path, SCOPES)
            creds = flow.run_local_server(port=0)

            with open(token_path, "w", encoding="utf-8") as token_file:
                token_file.write(creds.to_json())
            logging.info(f"✅ 授權成功！憑證已儲存至 {token_path}")
        except Exception as e:
            logging.error(f"❌ 無法完成 YouTube API 授權: {e}")
            sys.exit(1)

    return build("youtube", "v3", credentials=creds)

def parse_chapter_info(filename):
    """從檔名解析起始與結束章節號碼，供升序排序"""
    m_range = re.search(r'chapter_(\d+)_to_(\d+)', filename, re.IGNORECASE)
    if m_range:
        return int(m_range.group(1)), int(m_range.group(2))
    
    m_single = re.search(r'chapter_(\d+)', filename, re.IGNORECASE)
    if m_single:
        chap = int(m_single.group(1))
        return chap, chap
        
    m_worker = re.search(r'worker-(\d+)', filename, re.IGNORECASE)
    if m_worker:
        w_id = int(m_worker.group(1))
        return w_id * 120 + 1, (w_id + 1) * 120

    return 999999, 999999

def get_or_create_playlist(youtube, playlist_title, playlist_desc=""):
    """搜尋已存在的同名播放清單，若無則自動建立新播放清單"""
    logging.info(f"🔍 檢查 YouTube 頻道是否存在播放清單:【{playlist_title}】...")
    try:
        request = youtube.playlists().list(
            part="snippet,status",
            mine=True,
            maxResults=50
        )
        response = request.execute()

        for item in response.get("items", []):
            if item["snippet"]["title"].strip() == playlist_title.strip():
                playlist_id = item["id"]
                logging.info(f"✅ 找到已有播放清單 (ID: {playlist_id}):【{playlist_title}】")
                return playlist_id

        logging.info(f"➕ 正在建立全新播放清單:【{playlist_title}】...")
        body = {
            "snippet": {
                "title": playlist_title,
                "description": playlist_desc,
                "defaultLanguage": "zh-TW"
            },
            "status": {
                "privacyStatus": "public"
            }
        }
        create_res = youtube.playlists().insert(
            part="snippet,status",
            body=body
        ).execute()
        playlist_id = create_res["id"]
        logging.info(f"🎉 成功建立播放清單 (ID: {playlist_id}):【{playlist_title}】")
        return playlist_id
    except Exception as e:
        logging.error(f"❌ 查詢/建立播放清單失敗: {e}")
        return None

def add_video_to_playlist(youtube, playlist_id, video_id):
    """將影片加到指定的播放清單中 (依呼叫順序追加)"""
    try:
        body = {
            "snippet": {
                "playlistId": playlist_id,
                "resourceId": {
                    "kind": "youtube#video",
                    "videoId": video_id
                }
            }
        }
        youtube.playlistItems().insert(
            part="snippet",
            body=body
        ).execute()
        logging.info(f"📋 成功將影片 [Video ID: {video_id}] 加入播放清單！")
        return True
    except Exception as e:
        logging.warning(f"⚠️ 將影片 [Video ID: {video_id}] 加入播放清單失敗: {e}")
        return False

def upload_video_file(youtube, video_path, title, description, category_id="22", privacy_status="public", cover_path=None):
    """使用 Resumable 上傳 MP4 到 YouTube"""
    file_size_mb = os.path.getsize(video_path) / (1024 * 1024)
    logging.info(f"📤 開始 API 極速上傳影片: {title} (檔案大小: {file_size_mb:.1f} MB)...")
    sys.stdout.flush()

    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "categoryId": category_id
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": False
        }
    }

    media = MediaFileUpload(video_path, chunksize=10*1024*1024, resumable=True)
    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media
    )

    response = None
    last_logged_pct = -10
    start_time = time.time()

    while response is None:
        status, response = request.next_chunk()
        if status:
            pct = int(status.progress() * 100)
            if pct - last_logged_pct >= 20 or pct == 100:
                last_logged_pct = pct
                elapsed = time.time() - start_time
                speed_mb = (os.path.getsize(video_path) * status.progress() / (1024 * 1024)) / (elapsed if elapsed > 0 else 1)
                logging.info(f"   └─ 上傳進度: {pct}% ({speed_mb:.1f} MB/s)")
                sys.stdout.flush()

    video_id = response.get("id")
    logging.info(f"✅ 上傳成功！影片 ID: {video_id} (網址: https://www.youtube.com/watch?v={video_id})")
    sys.stdout.flush()

    if cover_path and os.path.exists(cover_path):
        try:
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(cover_path)
            ).execute()
            logging.info("🖼️ 成功更新影片封面縮圖！")
        except Exception as e:
            logging.warning(f"⚠️ 設定縮圖失敗: {e}")

    return video_id

def get_run_artifact_names(run_id, repo):
    cmd = ["gh", "api", f"repos/{repo}/actions/runs/{run_id}/artifacts", "--jq", ".artifacts[].name"]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        logging.error(f"Failed to fetch artifacts for run {run_id}: {res.stderr}")
        return []
    names = [n.strip() for n in res.stdout.splitlines() if n.strip().startswith("video-worker-")]
    names.sort(key=lambda x: int(re.search(r'\d+', x).group()) if re.search(r'\d+', x) else 999)
    return names

def download_artifact_task(run_id, repo, artifact_name, dest_dir):
    if os.path.exists(dest_dir):
        shutil.rmtree(dest_dir, ignore_errors=True)
    os.makedirs(dest_dir, exist_ok=True)
    
    dl_cmd = [
        "gh", "run", "download", str(run_id),
        "--repo", repo,
        "--name", artifact_name,
        "--dir", dest_dir
    ]
    res = subprocess.run(dl_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return res.returncode == 0

def main():
    parser = argparse.ArgumentParser(description="YouTube API Fast Uploader & Playlist Builder")
    parser.add_argument("--run-id", help="GitHub Actions Run ID containing video worker artifacts")
    parser.add_argument("--input-dir", help="Local directory containing MP4 files")
    parser.add_argument("--repo", default="hub-google/audiobook-generator", help="GitHub Repository")
    parser.add_argument("--privacy", default="public", choices=["public", "unlisted", "private"], help="Privacy status")
    args = parser.parse_args()

    youtube = get_authenticated_service()

    SRC_DIR = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, SRC_DIR)
    from metadata_gen import save_book_metadata

    book_title = "有聲小說全集"
    start_chap, end_chap = 1, 2400
    config_path = os.path.join(SRC_DIR, "..", "config.yaml")
    if os.path.exists(config_path):
        try:
            import yaml
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
                if cfg:
                    book_title = cfg.get("book_title", book_title)
                    chaps = cfg.get("selected_indices", [])
                    if chaps:
                        start_chap = chaps[0]
                        end_chap = chaps[-1]
        except Exception as e:
            logging.warning(f"Could not load config.yaml: {e}")

    meta_info = save_book_metadata(book_title, start_chap, end_chap)
    cover_path = meta_info["cover_file"]

    playlist_name = f"《{book_title}》有聲小說全集 (第 {start_chap} ~ {end_chap} 章)"
    playlist_desc = f"《{book_title}》完整版有聲書全集 (第 {start_chap} 至 {end_chap} 章)，高音質連續播映版。\n歡迎訂閱開啟小鈴鐺！"
    playlist_id = get_or_create_playlist(youtube, playlist_name, playlist_desc)

    files_to_upload = []

    if args.input_dir and os.path.exists(args.input_dir):
        mp4s = glob.glob(os.path.join(args.input_dir, "**", "*.mp4"), recursive=True)
        files_to_upload.extend(mp4s)
    elif args.run_id:
        logging.info(f"📥 從 GitHub Run ID #{args.run_id} 下載影片產物...")
        artifact_names = get_run_artifact_names(args.run_id, args.repo)
        temp_dl_dir = os.path.abspath("temp_api_upload_workspace")
        os.makedirs(temp_dl_dir, exist_ok=True)
        
        for a_name in artifact_names:
            art_dir = os.path.join(temp_dl_dir, a_name)
            logging.info(f"   下載 Artifact: {a_name}...")
            if download_artifact_task(args.run_id, args.repo, a_name, art_dir):
                m_files = glob.glob(os.path.join(art_dir, "**", "*.mp4"), recursive=True)
                files_to_upload.extend(m_files)

    if not files_to_upload:
        logging.error("❌ 未找到任何可供上傳的 MP4 影片檔案！")
        sys.exit(1)

    files_to_upload.sort(key=lambda p: parse_chapter_info(os.path.basename(p)))

    # ── 自動檢查是否為單章 MP4 檔案，若是則先執行 10~11 小時無縫分部 (Part) 打包 ──
    is_part_files = any("_Part_" in os.path.basename(p) for p in files_to_upload)
    parts_to_upload = []

    if not is_part_files:
        logging.info("📦 檢測到為單章影片產物，正在自動合成為 10~11 小時無縫分部 (Part) 大影片...")
        from part_builder import partition_chapters, merge_part_videos
        partitioned_parts = partition_chapters(files_to_upload, min_hours=10.0, max_hours=11.0)
        
        temp_parts_dir = os.path.abspath("temp_parts_output")
        os.makedirs(temp_parts_dir, exist_ok=True)
        
        for p in partitioned_parts:
            part_num = p["part_num"]
            s_c = p["start_chap"]
            e_c = p["end_chap"]
            out_name = f"{book_title}_Part_{part_num:02d}_Ch{s_c:04d}_to_Ch{e_c:04d}.mp4"
            out_path = os.path.join(temp_parts_dir, out_name)
            
            if merge_part_videos(p, out_path):
                # 為該 Part 生成專屬 Metadata (包含【第 X 部】2K 封面)
                p_meta = save_book_metadata(
                    book_title=book_title,
                    start_chap=s_c,
                    end_chap=e_c,
                    is_completed=True,
                    part_num=part_num
                )
                parts_to_upload.append({
                    "video_path": out_path,
                    "title": p_meta["title"],
                    "description": p_meta["description"],
                    "cover_path": p_meta["cover_file"],
                    "part_num": part_num,
                    "start_chap": s_c,
                    "end_chap": e_c
                })
    else:
        for idx, vp in enumerate(files_to_upload, 1):
            c_start, c_end = parse_chapter_info(os.path.basename(vp))
            p_meta = save_book_metadata(
                book_title=book_title,
                start_chap=c_start,
                end_chap=c_end,
                is_completed=True,
                part_num=idx
            )
            parts_to_upload.append({
                "video_path": vp,
                "title": p_meta["title"],
                "description": p_meta["description"],
                "cover_path": p_meta["cover_file"],
                "part_num": idx,
                "start_chap": c_start,
                "end_chap": c_end
            })

    logging.info(f"\n==================================================")
    logging.info(f"🚀 開始按分部正序【極速上傳】共 {len(parts_to_upload)} 部 10~11 小時大影片！")
    logging.info(f"==================================================\n")

    total_uploaded = 0
    for idx, item in enumerate(parts_to_upload, 1):
        v_path = item["video_path"]
        v_title = item["title"]
        v_desc = item["description"]
        v_cover = item["cover_path"]
        part_n = item["part_num"]

        full_desc = (
            f"{v_desc}\n\n"
            f"播放清單全集：https://www.youtube.com/playlist?list={playlist_id or ''}"
        )

        logging.info(f"[API_UPLOAD_MARKER] START | Part {part_n}/{len(parts_to_upload)} | Ch {item['start_chap']}~{item['end_chap']} | {os.path.basename(v_path)}")
        logging.info(f"▶️ [{idx}/{len(parts_to_upload)}] 正在上傳: {v_title}...")
        sys.stdout.flush()

        v_id = upload_video_file(
            youtube,
            video_path=v_path,
            title=v_title,
            description=full_desc,
            privacy_status=args.privacy,
            cover_path=v_cover
        )

        if v_id:
            total_uploaded += 1
            if playlist_id:
                add_video_to_playlist(youtube, playlist_id, v_id)

            logging.info(f"[API_UPLOAD_MARKER] DONE | Part {part_n}/{len(parts_to_upload)} | VideoID {v_id} | total {total_uploaded}")
            logging.info(f"✅ [{idx}/{len(parts_to_upload)}] 完成上傳並加進播放清單: {v_title}\n")
            sys.stdout.flush()

    logging.info("="*60)
    logging.info(f"🎉 全部影片極速上傳完畢！共上傳 {total_uploaded} 部分部影片至 YouTube 播放清單！")
    if playlist_id:
        logging.info(f"👉 播放清單網址: https://www.youtube.com/playlist?list={playlist_id}")
    logging.info("="*60)

if __name__ == "__main__":
    main()
