# Project Rules for Audiobook Generator

## YouTube Upload Guidelines

1. **CC Subtitle Integration (Mandatory)**:
   - When uploading videos or multi-chapter Part videos to YouTube via `youtube_api_uploader.py`, ALWAYS automatically merge chapter `.srt` files into a complete Part `.srt` file using `generate_part_srt()`.
   - ALWAYS save and store all merged Part `.srt` files into the `Upload_Subtitles/` workspace directory (`Upload_Subtitles/<filename>.srt`).
   - ALWAYS upload the merged `.srt` file to YouTube as a CC caption track using `upload_caption_file()` immediately after video upload succeeds (`v_id`).
   - Before uploading new CC captions, always list and delete old/stale test caption tracks on the video to prevent stuck/failed processing or 10-second test subtitle residue.
   - Use `https://www.googleapis.com/auth/youtube.force-ssl` in OAuth `SCOPES` to ensure caption modification permissions.

2. **API Quota & Failure Safeguards**:
   - If YouTube Data API returns `403 quotaExceeded`, gracefully log the quota limit, keep the local/merged `.srt` files saved in `Upload_Subtitles/`, and use backoff retry or notify the user rather than leaving incomplete state.

3. **No Automatic RTMP Live Streaming (Strict Prohibition)**:
   - NEVER automatically insert or trigger RTMP live streaming (`stream_to_youtube.py`) inside routine production workflows (`audiobook.yml`) or fast upload workflows.
   - Standard production workflows must ONLY render MP4/SRT artifacts and terminate cleanly.
   - RTMP live streaming is strictly isolated as an optional manual backup button inside GUI.

