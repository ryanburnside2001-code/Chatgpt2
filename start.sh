#!/usr/bin/env bash
# Remix Studio launcher for macOS and Linux.
set -euo pipefail
cd "$(dirname "$0")"
command -v ffmpeg >/dev/null 2>&1 || echo "Warning: FFmpeg not found on PATH. Clipping and highlights won't work until it's installed."
[ -d .venv ] || python3 -m venv .venv
# shellcheck disable=SC1091
. .venv/bin/activate
python -m pip install -q -r requirements.txt
[ -f .env ] || cp .env.example .env
echo "Remix Studio is starting at http://127.0.0.1:8000"
exec python -m uvicorn app:app --host 127.0.0.1 --port 8000 --env-file .env
