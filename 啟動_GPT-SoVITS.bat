@echo off
echo ==========================================
echo Starting GPT-SoVITS WebUI...
echo ==========================================

cd /d "%~dp0GPT-SoVITS"

echo Starting Anaconda Environment...
call "C:\Users\cyt18\anaconda3\Scripts\activate.bat"
call conda activate GPTSoVits

echo Starting WebUI Server...
python webui.py

pause
