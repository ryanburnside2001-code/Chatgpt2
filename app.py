"""Remix Studio — local creator dashboard backend.

A small FastAPI app that writes short-video scripts with an AI provider of
your choice, and cuts uploaded videos into social-ready clips with FFmpeg.
"""
import json
import math
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / '.env')  # no-op when the file is missing; never overrides real env vars

DATA = Path(os.getenv('REMIX_DATA_DIR') or BASE / 'data')
DATA.mkdir(parents=True, exist_ok=True)

VIDEO_EXTS = {'.mp4', '.mov', '.mkv', '.webm'}
MAX_UPLOAD_BYTES = 1024 * 1024 * 1024  # 1 GB
MAX_CLIP_SECONDS = 180
RENDER_TIMEOUT_SECONDS = 900
LOUDNESS_FRAME_SECONDS = 0.5  # resolution of the highlight finder
ID_RE = re.compile(r'[a-f0-9]{32}')


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Job storage
# --------------------------------------------------------------------------

class JobStore:
    """Thread-safe job registry persisted to a JSON file.

    Jobs are kept in memory and written atomically (temp file + rename) so a
    reader can never observe a half-written file.
    """

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        try:
            self.jobs = json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            self.jobs = {}

    def _persist(self):
        tmp = self.path.with_name(self.path.name + '.tmp')
        tmp.write_text(json.dumps(self.jobs, indent=2))
        os.replace(tmp, self.path)

    def get(self, jid):
        with self.lock:
            job = self.jobs.get(jid)
            return dict(job) if job else None

    def all(self):
        with self.lock:
            return [dict(job) for job in self.jobs.values()]

    def update(self, jid, **values):
        with self.lock:
            self.jobs[jid] = {**self.jobs.get(jid, {}), **values}
            self._persist()
            return dict(self.jobs[jid])

    def delete(self, jid):
        with self.lock:
            removed = self.jobs.pop(jid, None)
            self._persist()
            return removed


def job_kind(job):
    # Jobs written by the original starter have no "kind" field.
    return job.get('kind') or ('upload' if job.get('status') == 'uploaded' else 'clip')


def recover_interrupted(store: JobStore):
    """Render threads die with the server, so mark their jobs as failed."""
    for job in store.all():
        if job.get('status') == 'processing':
            store.update(job['id'], status='failed', progress=None,
                         error='The server stopped before this clip finished. Create it again.')


store = JobStore(DATA / 'jobs.json')
recover_interrupted(store)

PUBLIC_FIELDS = ('id', 'kind', 'name', 'status', 'created', 'finished', 'error', 'progress',
                 'duration', 'width', 'height', 'has_audio', 'source_id', 'start',
                 'clip_duration', 'layout', 'output')


def public(job):
    """Job as returned to the browser: never leak server file paths."""
    view = {key: job[key] for key in PUBLIC_FIELDS if key in job}
    view['kind'] = job_kind(job)
    if view['kind'] == 'upload':
        view['media'] = f"/api/media/{job['id']}"
    return view


# --------------------------------------------------------------------------
# App, auth and helpers
# --------------------------------------------------------------------------

app = FastAPI(title='Remix Studio API')
app.mount('/static', StaticFiles(directory=BASE / 'static'), name='static')


def require_auth(authorization: str | None, token: str | None = None):
    """Enforce APP_TOKEN when it is configured.

    Browsers can't add headers to <video> and <a download> requests, so the
    media and download routes also accept the token as a ?token= query value.
    """
    expected = os.getenv('APP_TOKEN', '')
    if not expected:
        return
    supplied = token or ''
    if authorization and authorization.startswith('Bearer '):
        supplied = authorization[len('Bearer '):]
    if not secrets.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(401, 'Invalid or missing app token. Enter it under Connection settings.')


def get_job(jid: str):
    if not ID_RE.fullmatch(jid):
        raise HTTPException(400, 'Invalid ID')
    job = store.get(jid)
    if not job:
        raise HTTPException(404, 'Job not found')
    return job


def data_file(path_str: str | None):
    """Resolve a stored path, but only if it lives inside the data folder."""
    if not path_str:
        return None
    path = Path(path_str).resolve()
    return path if path.parent == DATA.resolve() else None


def probe_video(path: Path):
    """Read duration/size/audio info with ffprobe.

    Returns None when ffprobe isn't installed, and raises HTTPException when
    the file isn't a readable video.
    """
    if not shutil.which('ffprobe'):
        return None
    try:
        proc = subprocess.run(
            ['ffprobe', '-v', 'error', '-print_format', 'json', '-show_format', '-show_streams', str(path)],
            capture_output=True, text=True, timeout=60)
        info = json.loads(proc.stdout or '{}')
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        info = {}
    streams = info.get('streams') or []
    video = next((s for s in streams if s.get('codec_type') == 'video'
                  and not (s.get('disposition') or {}).get('attached_pic')), None)
    if not video:
        raise HTTPException(400, "That file isn't a readable video. Try exporting it again as MP4.")
    try:
        duration = float((info.get('format') or {}).get('duration') or video.get('duration') or 0)
    except ValueError:
        duration = 0
    return {
        'duration': round(duration, 3) if duration > 0 else None,
        'width': video.get('width'),
        'height': video.get('height'),
        'has_audio': any(s.get('codec_type') == 'audio' for s in streams),
    }


# --------------------------------------------------------------------------
# Pages and status
# --------------------------------------------------------------------------

PROVIDER_KEYS = {
    'openrouter': 'OPENROUTER_API_KEY',
    'openai': 'OPENAI_API_KEY',
    'anthropic': 'ANTHROPIC_API_KEY',
    'gemini': 'GEMINI_API_KEY',
}


@app.get('/')
def index():
    return FileResponse(BASE / 'static' / 'index.html')


@app.get('/api/health')
def health():
    providers = {name: bool(os.getenv(env)) for name, env in PROVIDER_KEYS.items()}
    return {
        'ok': True,
        'ffmpeg': shutil.which('ffmpeg') is not None,
        'ffprobe': shutil.which('ffprobe') is not None,
        'auth_required': bool(os.getenv('APP_TOKEN')),
        'providers': providers,
        'openrouter_configured': providers['openrouter'],  # kept for older clients
    }


@app.get('/api/jobs')
def list_jobs(authorization: str | None = Header(None)):
    require_auth(authorization)
    jobs = sorted(store.all(), key=lambda j: j.get('created') or '', reverse=True)
    return [public(job) for job in jobs]


@app.delete('/api/jobs/{jid}')
def delete_job(jid: str, authorization: str | None = Header(None)):
    require_auth(authorization)
    job = get_job(jid)
    if job.get('status') == 'processing':
        raise HTTPException(409, 'Wait for this clip to finish before deleting it.')
    if job_kind(job) == 'upload':
        if any(j.get('source_id') == jid and j.get('status') == 'processing' for j in store.all()):
            raise HTTPException(409, 'A clip is still being made from this video. Try again when it finishes.')
        files = [data_file(job.get('source')), DATA / f'{jid}.loudness.json']
    else:
        files = [DATA / f'{jid}.mp4']
    for path in files:
        if path:
            path.unlink(missing_ok=True)
    store.delete(jid)
    return {'deleted': jid}


# --------------------------------------------------------------------------
# AI script writer
# --------------------------------------------------------------------------

class ScriptRequest(BaseModel):
    topic: str
    style: str = 'motivational'
    duration: int = 45
    provider: Literal['openrouter', 'openai', 'anthropic', 'gemini'] = 'openrouter'


def provider_request(provider: str, prompt: str):
    """Build (key, url, headers, payload) for a provider. Models are configurable in .env."""
    key = os.getenv(PROVIDER_KEYS[provider])
    if provider == 'openrouter':
        model = os.getenv('OPENROUTER_MODEL') or 'openai/gpt-4o-mini'
        return key, 'https://openrouter.ai/api/v1/chat/completions', {'Authorization': f'Bearer {key}'}, {
            'model': model, 'messages': [{'role': 'user', 'content': prompt}]}
    if provider == 'openai':
        model = os.getenv('OPENAI_MODEL') or 'gpt-4o-mini'
        return key, 'https://api.openai.com/v1/chat/completions', {'Authorization': f'Bearer {key}'}, {
            'model': model, 'messages': [{'role': 'user', 'content': prompt}]}
    if provider == 'anthropic':
        model = os.getenv('ANTHROPIC_MODEL') or 'claude-sonnet-5-5'
        return key, 'https://api.anthropic.com/v1/messages', {
            'x-api-key': str(key), 'anthropic-version': '2023-06-01'}, {
            'model': model, 'max_tokens': 1200, 'messages': [{'role': 'user', 'content': prompt}]}
    model = os.getenv('GEMINI_MODEL') or 'gemini-2.5-flash'
    return key, f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent', {
        'x-goog-api-key': str(key)}, {'contents': [{'parts': [{'text': prompt}]}]}


async def ai_chat(prompt: str, provider: str) -> str:
    key, url, headers, payload = provider_request(provider, prompt)
    if not key:
        raise HTTPException(400, f'{provider} API key not configured. Add {PROVIDER_KEYS[provider]} to .env and restart.')
    async with httpx.AsyncClient(timeout=70) as client:
        try:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            result = response.json()
            if provider == 'anthropic':
                return ''.join(block.get('text', '') for block in result['content'] if block.get('type') == 'text')
            if provider == 'gemini':
                return result['candidates'][0]['content']['parts'][0]['text']
            return result['choices'][0]['message']['content']
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:300]
            raise HTTPException(502, f'{provider} returned HTTP {exc.response.status_code}: {detail}')
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise HTTPException(502, f'AI provider error: {str(exc)[:250]}')


@app.post('/api/script')
async def script(req: ScriptRequest, authorization: str | None = Header(None)):
    require_auth(authorization)
    topic = req.topic.strip()
    if not topic or len(topic) > 1500:
        raise HTTPException(400, 'Topic must be 1-1500 characters')
    style = req.style.strip()[:40] or 'motivational'
    seconds = max(15, min(req.duration, MAX_CLIP_SECONDS))
    prompt = (
        f'Write an ORIGINAL compelling {style} short-video voiceover about: {topic}. '
        f'Aim for {seconds} seconds when read aloud (about {round(seconds * 2.5)} words). '
        'Include an opening hook, story and closing call to action. Return just the script. '
        'Avoid unsupported guarantees about engagement.'
    )
    return {'script': await ai_chat(prompt, req.provider)}


# --------------------------------------------------------------------------
# Upload, preview and highlights
# --------------------------------------------------------------------------

@app.post('/api/upload')
async def upload(file: UploadFile = File(...), authorization: str | None = Header(None)):
    require_auth(authorization)
    ext = Path(file.filename or '').suffix.lower()
    if ext not in VIDEO_EXTS:
        raise HTTPException(400, 'Upload MP4, MOV, MKV or WebM')
    jid = uuid.uuid4().hex
    path = DATA / f'{jid}{ext}'
    size = 0
    with path.open('wb') as dest:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                dest.close()
                path.unlink(missing_ok=True)
                raise HTTPException(413, 'Max file size is 1 GB')
            dest.write(chunk)
    try:
        meta = await run_in_threadpool(probe_video, path)
    except HTTPException:
        path.unlink(missing_ok=True)
        raise
    name = Path(file.filename or 'video').name[:200]
    job = store.update(jid, id=jid, kind='upload', name=name, status='uploaded',
                       source=str(path), created=now_iso(), **(meta or {}))
    return {'job_id': jid, 'status': 'uploaded', 'job': public(job)}


@app.get('/api/media/{jid}')
def media(jid: str, authorization: str | None = Header(None), token: str | None = Query(None)):
    """Stream an uploaded video for the preview player (supports HTTP range requests)."""
    require_auth(authorization, token)
    job = get_job(jid)
    source = data_file(job.get('source')) if job_kind(job) == 'upload' else None
    if not source or not source.is_file():
        raise HTTPException(404, 'Video file not found')
    media_type = mimetypes.guess_type(source.name)[0] or 'application/octet-stream'
    return FileResponse(source, media_type=media_type)


class HighlightRequest(BaseModel):
    job_id: str
    duration: float = 30
    count: int = 3


def loudness_profile(job) -> list[float]:
    """RMS loudness (dB) for each LOUDNESS_FRAME_SECONDS slice of the audio, cached per upload."""
    cache = DATA / f"{job['id']}.loudness.json"
    try:
        return json.loads(cache.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    source = data_file(job.get('source'))
    if not source or not source.is_file():
        raise HTTPException(404, 'Video file not found')
    rate = 8000
    audio_filter = (f'aresample={rate},asetnsamples=n={int(rate * LOUDNESS_FRAME_SECONDS)}:p=0,'
                    'astats=metadata=1:reset=1,'
                    'ametadata=mode=print:key=lavfi.astats.Overall.RMS_level:file=-')
    cmd = ['ffmpeg', '-hide_banner', '-nostats', '-loglevel', 'error', '-i', str(source),
           '-vn', '-ac', '1', '-af', audio_filter, '-f', 'null', '-']
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, 'Audio analysis took too long')
    if proc.returncode:
        raise HTTPException(500, f'Audio analysis failed: {proc.stderr[-300:]}')
    levels = []
    for line in proc.stdout.splitlines():
        if line.startswith('lavfi.astats.Overall.RMS_level='):
            try:
                value = float(line.split('=', 1)[1])
            except ValueError:
                value = -120.0
            levels.append(value if math.isfinite(value) else -120.0)
    cache.write_text(json.dumps(levels))
    return levels


def pick_highlights(levels_db: list[float], window_seconds: float, count: int):
    """Choose the `count` loudest non-overlapping windows of `window_seconds`."""
    frames = len(levels_db)
    if not frames:
        return []
    width = max(1, round(window_seconds / LOUDNESS_FRAME_SECONDS))
    if width >= frames:
        return [{'start': 0.0, 'end': round(frames * LOUDNESS_FRAME_SECONDS, 2), 'score': 100}]
    power = [10 ** (db / 10) for db in levels_db]  # average energy, not average dB
    prefix = [0.0]
    for value in power:
        prefix.append(prefix[-1] + value)
    windows = sorted(((prefix[i + width] - prefix[i], i) for i in range(frames - width + 1)), reverse=True)
    chosen = []
    for energy, start in windows:
        if all(abs(start - other) >= width for _, other in chosen):
            chosen.append((energy, start))
            if len(chosen) == count:
                break
    best = chosen[0][0] or 1.0
    # Score = perceived loudness relative to the best window. Loudness roughly
    # doubles every +10 dB, i.e. perceived ratio = energy_ratio ** log10(2).
    return [{
        'start': round(start * LOUDNESS_FRAME_SECONDS, 2),
        'end': round((start + width) * LOUDNESS_FRAME_SECONDS, 2),
        'score': round(100 * (energy / best) ** math.log10(2)),
    } for energy, start in chosen]


@app.post('/api/highlights')
def highlights(req: HighlightRequest, authorization: str | None = Header(None)):
    require_auth(authorization)
    job = get_job(req.job_id)
    if job_kind(job) != 'upload':
        raise HTTPException(400, 'Pick an uploaded video')
    if not shutil.which('ffmpeg'):
        raise HTTPException(503, 'FFmpeg is not installed, so highlights are unavailable.')
    if job.get('has_audio') is False:
        raise HTTPException(400, "This video has no audio track, so there's nothing to measure. Pick times manually.")
    if not 1 <= req.duration <= MAX_CLIP_SECONDS:
        raise HTTPException(400, f'Clip length must be 1-{MAX_CLIP_SECONDS} seconds')
    levels = loudness_profile(job)
    return {'highlights': pick_highlights(levels, req.duration, max(1, min(req.count, 10))),
            'method': 'loudness'}


# --------------------------------------------------------------------------
# Clip rendering
# --------------------------------------------------------------------------

LAYOUT_FILTERS = {
    # Fill a 1080x1920 frame, cropping the sides.
    'crop': '[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1[v]',
    # Fit the whole frame inside 1080x1920 over a blurred, zoomed copy of itself.
    'blur': ('[0:v]split=2[bgsrc][fgsrc];'
             '[bgsrc]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:5[bg];'
             '[fgsrc]scale=1080:1920:force_original_aspect_ratio=decrease[fg];'
             '[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[v]'),
    # Keep the source framing (H.264 needs even dimensions).
    'original': '[0:v]scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1[v]',
}


class ClipRequest(BaseModel):
    job_id: str
    start: float = 0
    duration: float = 30
    layout: Literal['crop', 'blur', 'original'] = 'crop'


def render(jid, source, start, duration, layout):
    output = DATA / f'{jid}.mp4'
    partial = DATA / f'{jid}.part.mp4'
    cmd = ['ffmpeg', '-hide_banner', '-nostats', '-loglevel', 'error', '-y',
           '-ss', f'{start:.3f}', '-i', str(source), '-t', f'{duration:.3f}',
           '-filter_complex', LAYOUT_FILTERS[layout], '-map', '[v]', '-map', '0:a:0?',
           '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23', '-pix_fmt', 'yuv420p',
           '-c:a', 'aac', '-b:a', '160k', '-movflags', '+faststart',
           '-progress', 'pipe:1', str(partial)]
    try:
        with tempfile.TemporaryFile(mode='w+') as errors:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errors, text=True)
            timer = threading.Timer(RENDER_TIMEOUT_SECONDS, proc.kill)
            timer.start()
            try:
                last_saved = 0.0
                for line in proc.stdout:
                    key, _, value = line.strip().partition('=')
                    # ffmpeg reports microseconds under both names
                    if key in ('out_time_us', 'out_time_ms') and value.isdigit():
                        if time.monotonic() - last_saved >= 0.5:
                            percent = min(99, int(int(value) / 1e6 / duration * 100))
                            store.update(jid, progress=percent)
                            last_saved = time.monotonic()
                proc.wait()
            finally:
                timer.cancel()
            if proc.returncode:
                errors.seek(0)
                raise RuntimeError(errors.read()[-1200:].strip() or f'FFmpeg exited with code {proc.returncode}')
        os.replace(partial, output)
        store.update(jid, status='completed', progress=100, output=f'/api/download/{jid}', finished=now_iso())
    except Exception as exc:
        partial.unlink(missing_ok=True)
        store.update(jid, status='failed', progress=None, error=str(exc)[:1200], finished=now_iso())


@app.post('/api/clip')
def clip(req: ClipRequest, authorization: str | None = Header(None)):
    require_auth(authorization)
    job = get_job(req.job_id)
    source = data_file(job.get('source'))
    if job_kind(job) != 'upload' or not source or not source.is_file():
        raise HTTPException(404, 'Upload not found')
    if not shutil.which('ffmpeg'):
        raise HTTPException(503, 'FFmpeg is not installed. See the README for setup steps.')
    start, duration = req.start, req.duration
    if start < 0:
        raise HTTPException(400, 'Start time cannot be negative')
    if not 1 <= duration <= MAX_CLIP_SECONDS:
        raise HTTPException(400, f'Duration must be 1-{MAX_CLIP_SECONDS} seconds')
    total = job.get('duration')
    if total:
        if start >= total - 0.5:
            raise HTTPException(400, f'Start time is past the end of the video ({total:.1f}s long)')
        duration = min(duration, total - start)
    jid = uuid.uuid4().hex
    store.update(jid, id=jid, kind='clip', name=f"Clip of {job['name']}", source=str(source),
                 source_id=job['id'], start=round(start, 3), clip_duration=round(duration, 3),
                 layout=req.layout, status='processing', progress=0, created=now_iso())
    threading.Thread(target=render, args=(jid, source, start, duration, req.layout), daemon=True).start()
    return {'job_id': jid, 'status': 'processing'}


@app.get('/api/download/{jid}')
def download(jid: str, authorization: str | None = Header(None), token: str | None = Query(None)):
    require_auth(authorization, token)
    job = get_job(jid)
    path = DATA / f'{jid}.mp4'
    if job.get('status') != 'completed' or not path.is_file():
        raise HTTPException(404, 'Not ready')
    return FileResponse(path, media_type='video/mp4', filename=f'remix-{jid[:8]}.mp4')
