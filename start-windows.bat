@echo off
cd /d "%~dp0"
where ffmpeg >nul 2>nul || echo Warning: FFmpeg not found on PATH. Clipping and highlights won't work until it's installed.
if not exist .venv (py -m venv .venv)
call .venv\Scripts\activate.bat
python -m pip install -q -r requirements.txt
if not exist .env copy .env.example .env
echo Remix Studio is starting at http://127.0.0.1:8000
python -m uvicorn app:app --host 127.0.0.1 --port 8000 --env-file .env
pause
