# Remix Studio — local starter source code

## Run on Windows 11
1. Install Python 3.11+ from python.org and FFmpeg from ffmpeg.org. Ensure both are in PATH.
2. Extract this ZIP to a folder on your computer.
3. Open `.env` (created on first run) and add the API key(s) you want. Keep this file private.
4. Double-click `start-windows.bat`.
5. Open http://127.0.0.1:8000 in your browser.

## Working features
- Responsive local web dashboard
- AI script generation through OpenRouter, OpenAI, Anthropic or Gemini (requires valid keys/models/credits)
- Video upload, FFmpeg crop/trim, downloadable MP4 clips
- Basic persistent job tracking

## Not implemented yet
- Automatic highlight detection, transcription, subtitles, AI voiceover, timeline editor
- OAuth social connections, auto-posting, scheduling, user accounts, payments
- Production security, scalable queues and crash recovery

This is a runnable **starter**, not a clone or recovered source of the linked website. The original website was inaccessible. Do not expose this development server directly to the internet. Upload and job endpoints are only protected if you configure APP_TOKEN; a production deployment requires proper accounts, rate limiting, quotas and isolation. Process only videos you own or have permission to use.
