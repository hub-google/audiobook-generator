@echo off
chcp 65001 > nul
echo ==========================================
echo 正在啟動 全書單一 MP4 影片無損合併工具 GUI...
echo ==========================================

cd /d "%~dp0"
"C:\Users\cyt18\anaconda3\python.exe" gui_merge_app.py

pause
