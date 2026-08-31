@echo off
chcp 65001 >nul
cd /d "%~dp0\.."
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo [ERROR] 套件安裝失敗
  pause
  exit /b 1
)
python tools\youtube_backfill_gui.py
if errorlevel 1 (
  echo.
  echo [ERROR] 工具執行失敗，請查看上方訊息
)
pause
