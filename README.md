# Remix Studio

A local creator dashboard: write original short-video scripts with the AI provider of your choice, find the best moments in your videos, and export clips ready for TikTok, Reels and Shorts.

Everything runs on your own computer. Videos never leave your machine, and API keys stay in your `.env` file.

## Features

- **Video clipper with live preview.** Upload a video, scrub it in the browser, and set the clip's start and end from the playhead (keyboard: `I` start, `O` end, `P` preview selection).
- **Loudest-moment finder.** Scans the audio track and suggests the loudest sections at your chosen clip length. Cheering, laughter, music drops and raised voices tend to be the moments worth clipping. Suggestions show on the timeline; click one to jump to it.
- **Three export formats:**
  - Vertical 9:16, cropped to fill.
  - Vertical 9:16, full frame on a blurred background (nothing gets cut off).
  - Original aspect ratio.
- **Live progress.** Uploads and renders show progress bars, and the jobs list refreshes itself while a clip renders.
- **Library.** Re-open any uploaded video, download finished clips, and delete what you no longer need (files are removed from disk).
- **AI script writer.** Supports OpenRouter, OpenAI, Claude (Anthropic) or Gemini, with a word count and estimated read time. Providers without a key are flagged in the UI.
- **Optional app token.** Protects every endpoint when `APP_TOKEN` is set.

## Run it

You need **Python 3.11+** and **FFmpeg** (both on your `PATH`). FFmpeg is used for clipping and highlight detection; the script writer works without it.

**Windows 11:** double-click `start-windows.bat`.

**macOS / Linux:**

```bash
./start.sh
```

Then open <http://127.0.0.1:8000>.

The first run creates a virtual environment, installs dependencies and copies `.env.example` to `.env`. Add the API key(s) you want to `.env` and restart.

Install FFmpeg with `winget install ffmpeg` (Windows), `brew install ffmpeg` (macOS) or `sudo apt install ffmpeg` (Debian/Ubuntu).

## Configuration (`.env`)

| Variable | Purpose |
| --- | --- |
| `OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY` | Keys for the script writer. Set any you want to use. |
| `OPENROUTER_MODEL`, `OPENAI_MODEL`, `ANTHROPIC_MODEL`, `GEMINI_MODEL` | Optional model overrides. |
| `APP_TOKEN` | Optional. A long random value that every API request must present. |
| `REMIX_DATA_DIR` | Optional. Where uploads, clips and `jobs.json` are stored (default `./data`). |

## Browser preview notes

The preview player plays whatever your browser supports: MP4 (H.264) and WebM work in Chrome, Edge, Firefox and Safari. MKV and some MOV files can't be previewed in browsers. You can still clip them by typing start and end times.

## API

| Method | Path | What it does |
| --- | --- | --- |
| `GET` | `/api/health` | FFmpeg status, configured providers, whether a token is required |
| `GET` | `/api/jobs` | Uploads and clips, newest first |
| `DELETE` | `/api/jobs/{id}` | Delete an upload or clip and its files |
| `POST` | `/api/upload` | Upload a video (multipart `file`); returns its duration and size |
| `GET` | `/api/media/{id}` | Stream an uploaded video (supports range requests) |
| `POST` | `/api/highlights` | `{job_id, duration, count}`: loudest non-overlapping windows |
| `POST` | `/api/clip` | `{job_id, start, duration, layout}`: start a render (`crop`, `blur` or `original`) |
| `GET` | `/api/download/{id}` | Download a finished clip |
| `POST` | `/api/script` | `{topic, style, duration, provider}`: generate a script |

When `APP_TOKEN` is set, send `Authorization: Bearer <token>`. The media and download routes also accept `?token=` so the video player and download links work.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The tests generate a short sample video with FFmpeg, so they cover real uploads, highlight detection and all three render layouts. The FFmpeg-dependent tests are skipped if FFmpeg isn't installed.

## Not implemented yet

- Transcription, subtitles and AI voiceover.
- A multi-track timeline editor.
- OAuth social connections, auto-posting and scheduling.
- User accounts and payments.
- Production hosting: scalable queues, rate limiting, quotas and isolation.

## Safety

This is a local development server. Don't expose it directly to the internet. If you must reach it from another device, set `APP_TOKEN`, and know that a real deployment also needs accounts, rate limits and isolation. Process only videos you own or have permission to use.
