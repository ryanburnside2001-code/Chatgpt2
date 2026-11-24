# Remix Studio

A local creator dashboard: have an AI team plan, build and check websites for you, write original short-video scripts with the AI provider of your choice, find the best moments in your videos, and export clips ready for TikTok, Reels and Shorts.

Everything runs on your own computer. Videos never leave your machine, and API keys stay in your `.env` file.

## Features

- **AI Website Team.** Describe the site you want (plus any real details such as name, phone and prices) and a team of AI roles builds it:
  1. **Strategist** writes a brief: audience, offer, call to action, page plan, style and an honest money assessment.
  2. **Web builder** builds a single-file, responsive, SEO-ready `index.html` from the brief.
  3. **QA reviewer** scores it against the brief and a strict checklist (design, copy, mobile, accessibility, SEO, honesty). Automatic checks back it up: a missing title, dead links, images without alt text, lorem ipsum or a cut-off page can't pass, whatever the AI says.
  4. **Web builder** fixes everything QA found, and QA reviews again, up to 1-3 rounds, until it passes.
  5. **Shorts producer** (optional) writes 3 promo YouTube Shorts scripts for the site.
  6. **Publisher** packages a `.zip` (site, brief, QA report, publishing guide). It never publishes anything.

  Pick a different AI for each role. A different model as reviewer catches mistakes the builder can't see in its own work. Preview the result at desktop or phone size, ask for changes in plain words, and download it. The team uses only facts you give it; anything missing becomes an `[OWNER: …]` placeholder.
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
| `POST` | `/api/sites` | `{idea, audience, facts, look, strategist, builder, reviewer, shorts, promo, fix_rounds}`: start the website team |
| `GET` | `/api/sites/{id}` | Team progress, brief, QA reports and promo scripts |
| `POST` | `/api/sites/{id}/revise` | `{instructions}`: ask for changes. Empty fixes QA's open issues, or retries a failed build |
| `GET` | `/api/sites/{id}/preview` | The generated page, served in a sandbox |
| `GET` | `/api/sites/{id}/download` | `.zip` with the site, brief, QA report and publishing guide |

When `APP_TOKEN` is set, send `Authorization: Bearer <token>`. The media, preview and download routes also accept `?token=` so the video player, site preview and download links work.

## Website quality and cost

The team is only as good as the models you give it. The default models (`gpt-4o-mini`, `gemini-2.5-flash`) are cheap but plain at design. For the web builder and reviewer, set a stronger model in `.env` (for example `ANTHROPIC_MODEL` or `OPENAI_MODEL`).

Each build makes roughly 3-8 AI calls: one brief, one build, one review per round, one fix per failed review, and one for the Shorts scripts. The build and fix calls are the large ones, because they send and return the whole page. Check your provider's pricing.

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
- Deploying websites. The publisher only packages a `.zip`; you choose where to publish it.
- Multi-page websites and real photos. The team builds one-page sites with CSS and SVG artwork.
- User accounts and payments.
- Production hosting: scalable queues, rate limiting, quotas and isolation.

## Safety

This is a local development server. Don't expose it directly to the internet. If you must reach it from another device, set `APP_TOKEN`, and know that a real deployment also needs accounts, rate limits and isolation. Process only videos you own or have permission to use.
