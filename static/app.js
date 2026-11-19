'use strict';

const MAX_CLIP = 180;
const MAX_UPLOAD = 1024 * 1024 * 1024;
const VIDEO_EXTS = ['.mp4', '.mov', '.mkv', '.webm'];

const $ = (id) => document.getElementById(id);
const state = {
  token: '',
  health: null,
  jobs: [],
  current: null,      // the upload job loaded in the clipper
  localUrl: null,     // object URL when previewing a file before/while uploading
  highlights: [],
  selection: { start: 0, end: 30 },
  previewing: false,
  pollTimer: null,
};

// ---------------------------------------------------------------- helpers

function storageGet(key) { try { return sessionStorage.getItem(key) || ''; } catch { return ''; } }
function storageSet(key, value) { try { sessionStorage.setItem(key, value); } catch { /* private mode */ } }

function authHeaders() { return state.token ? { Authorization: 'Bearer ' + state.token } : {}; }
function withToken(url) {
  return state.token ? url + (url.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(state.token) : url;
}

function errorText(data, status) {
  const detail = data && data.detail;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    return detail.map((d) => (d.loc ? d.loc[d.loc.length - 1] + ': ' : '') + d.msg).join('; ');
  }
  return `Request failed (HTTP ${status})`;
}

async function api(url, { method, json } = {}) {
  const headers = { ...authHeaders() };
  if (json !== undefined) headers['Content-Type'] = 'application/json';
  const response = await fetch(url, {
    method: method || (json !== undefined ? 'POST' : 'GET'),
    headers,
    body: json !== undefined ? JSON.stringify(json) : undefined,
  });
  let data = null;
  try { data = await response.json(); } catch { /* empty body */ }
  if (!response.ok) throw new Error(errorText(data, response.status));
  return data;
}

function fmtTime(seconds) {
  const t = Number.isFinite(seconds) && seconds > 0 ? Math.round(seconds * 10) / 10 : 0;
  const m = Math.floor(t / 60);
  const s = t - m * 60;
  return `${m}:${s.toFixed(1).padStart(4, '0')}`;
}
function fmtDuration(seconds) {
  if (!seconds) return '';
  const total = Math.round(seconds);
  const h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60), s = total % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${m}:${String(s).padStart(2, '0')}`;
}
function fmtDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
}
const round1 = (n) => Math.round(n * 10) / 10;

function setStatus(id, text, isError = false) {
  const node = $(id);
  node.textContent = text;
  node.classList.toggle('error', isError);
}

function make(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  for (const child of children) if (child) node.append(child);
  return node;
}

// ---------------------------------------------------------------- health

async function loadHealth() {
  try {
    const health = await api('/api/health');
    state.health = health;
    renderHealth();
  } catch (e) {
    $('healthPills').replaceChildren(make('span', { class: 'pill bad', text: 'Server unreachable' }));
  }
}

function renderHealth() {
  const h = state.health;
  const providers = h.providers || { openrouter: h.openrouter_configured };
  const names = { openrouter: 'OpenRouter', openai: 'OpenAI', anthropic: 'Claude', gemini: 'Gemini' };
  const configured = Object.keys(providers).filter((p) => providers[p]);

  $('healthPills').replaceChildren(
    make('span', { class: 'pill ' + (h.ffmpeg ? 'ok' : 'bad'), text: h.ffmpeg ? 'FFmpeg ready' : 'FFmpeg missing' }),
    make('span', { class: 'pill ' + (configured.length ? 'ok' : 'bad'), text: `AI: ${configured.length}/${Object.keys(providers).length} providers` }),
  );

  const item = (ok, text) => make('li', {}, [make('span', { class: ok ? 'ok' : 'bad', text: ok ? '✓ ' : '✗ ' }), text]);
  $('healthList').replaceChildren(
    item(h.ffmpeg, h.ffmpeg ? 'FFmpeg installed' : 'FFmpeg not found: install it and add it to PATH'),
    ...Object.keys(names).map((p) => item(providers[p], `${names[p]} ${providers[p] ? 'configured' : 'key not set'}`)),
  );

  // Mark providers without keys so people don't pick one that will fail.
  const select = $('provider');
  for (const option of select.options) {
    const ok = providers[option.value];
    option.textContent = names[option.value] + (ok ? '' : ' (no key in .env)');
  }
  if (!providers[select.value] && configured.length) select.value = configured[0];

  $('tokenNotice').hidden = !(h.auth_required && !state.token);
}

// ---------------------------------------------------------------- jobs

async function loadJobs() {
  try {
    state.jobs = await api('/api/jobs');
    renderJobs();
    renderLibrary();
    schedulePoll();
  } catch (e) {
    $('jobs').replaceChildren(make('p', { class: 'status error', text: e.message }));
  }
}

function schedulePoll() {
  clearTimeout(state.pollTimer);
  if (state.jobs.some((j) => j.status === 'processing')) {
    state.pollTimer = setTimeout(loadJobs, 1500);
  }
}

function renderJobs() {
  const root = $('jobs');
  if (!state.jobs.length) {
    root.replaceChildren(make('p', { class: 'muted', text: 'Nothing here yet. Upload a video to get started.' }));
    return;
  }
  root.replaceChildren(...state.jobs.map(renderJob));
}

const LAYOUT_NAMES = { crop: '9:16 crop', blur: '9:16 blurred background', original: 'original ratio' };

function renderJob(job) {
  const meta = [];
  if (job.kind === 'upload') {
    if (job.duration) meta.push(fmtDuration(job.duration));
    if (job.width && job.height) meta.push(`${job.width}×${job.height}`);
    if (job.has_audio === false) meta.push('no audio');
  } else {
    if (job.clip_duration) meta.push(`${fmtTime(job.start || 0)} + ${job.clip_duration.toFixed(1)}s`);
    if (job.layout) meta.push(LAYOUT_NAMES[job.layout] || job.layout);
  }
  meta.push(fmtDate(job.created));

  const actions = [];
  if (job.kind === 'upload') {
    actions.push(make('button', { class: 'secondary compact', type: 'button', text: 'Open in clipper', onclick: () => selectVideo(job.id, true) }));
  }
  if (job.status === 'completed' && job.output) {
    actions.push(make('a', { class: 'button compact', href: withToken(job.output), download: '', text: '⬇ Download' }));
  }
  if (job.status !== 'processing') {
    actions.push(make('button', { class: 'danger compact', type: 'button', text: 'Delete', onclick: () => deleteJob(job) }));
  }

  let progress = null;
  if (job.status === 'processing') {
    progress = make('div', { class: 'progress' }, [make('div', { class: 'progress-bar' })]);
    progress.firstChild.style.width = (job.progress || 0) + '%';
  }

  return make('article', { class: 'job' }, [
    make('div', { class: 'job-top' }, [
      make('div', {}, [
        make('div', { class: 'job-name', text: (job.kind === 'upload' ? '🎞 ' : '✂️ ') + (job.name || 'Video') }),
        make('div', { class: 'job-meta', text: meta.filter(Boolean).join(' · ') }),
      ]),
      make('span', { class: 'badge ' + job.status, text: job.status === 'processing' ? `${job.progress || 0}%` : job.status }),
    ]),
    progress,
    job.error ? make('div', { class: 'job-error', text: job.error }) : null,
    actions.length ? make('div', { class: 'job-actions' }, actions) : null,
  ]);
}

async function deleteJob(job) {
  const what = job.kind === 'upload' ? 'this video and its highlight data' : 'this clip';
  if (!confirm(`Delete ${what}? This can't be undone.`)) return;
  try {
    await api('/api/jobs/' + job.id, { method: 'DELETE' });
    if (state.current && state.current.id === job.id) clearPlayer();
    await loadJobs();
  } catch (e) {
    alert(e.message);
  }
}

function renderLibrary() {
  const select = $('library');
  const uploads = state.jobs.filter((j) => j.kind === 'upload');
  const current = state.current ? state.current.id : '';
  const placeholder = make('option', { value: '', text: uploads.length ? 'Choose a video…' : 'No uploaded videos yet' });
  select.replaceChildren(placeholder, ...uploads.map((j) =>
    make('option', { value: j.id, text: j.name + (j.duration ? ` (${fmtDuration(j.duration)})` : '') })));
  select.value = uploads.some((j) => j.id === current) ? current : '';
}

// ---------------------------------------------------------------- player + selection

const player = $('player');

function videoDuration() {
  if (Number.isFinite(player.duration) && player.duration > 0) return player.duration;
  return (state.current && state.current.duration) || 0;
}

function clearPlayer() {
  state.current = null;
  if (state.localUrl) URL.revokeObjectURL(state.localUrl);
  state.localUrl = null;
  player.removeAttribute('src');
  player.load();
  $('playerEmpty').hidden = false;
  $('playerNotice').hidden = true;
  $('videoMeta').textContent = '';
  state.highlights = [];
  renderHighlights();
  renderTimeline();
}

function loadPlayerSource(src) {
  $('playerNotice').hidden = true;
  $('playerEmpty').hidden = true;
  player.src = src;
}

function selectVideo(id, scroll = false) {
  const job = state.jobs.find((j) => j.id === id);
  if (!job) { clearPlayer(); return; }
  const sameVideo = state.current && state.current.id === id;
  state.current = job;
  $('library').value = id;
  const meta = [job.duration && fmtDuration(job.duration), job.width && `${job.width}×${job.height}`, job.has_audio === false && 'no audio']
    .filter(Boolean).join(' · ');
  $('videoMeta').textContent = meta;
  if (!sameVideo) {
    if (state.localUrl) { URL.revokeObjectURL(state.localUrl); state.localUrl = null; }
    loadPlayerSource(withToken(job.media));
    state.highlights = [];
    renderHighlights();
    const dur = job.duration || 0;
    setSelection(0, dur ? Math.min(30, dur) : 30);
  }
  if (scroll) $('clipperTitle').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function normalizeSelection(start, end, anchor) {
  const dur = videoDuration() || Infinity;
  start = Math.min(Math.max(0, start || 0), dur);
  end = Math.min(Math.max(0, end || 0), dur);
  if (anchor === 'end') {
    if (end <= start) start = Math.max(0, end - Math.min(30, end));
    if (end - start > MAX_CLIP) start = end - MAX_CLIP;
  } else {
    if (end <= start) end = Math.min(dur, start + 30);
    if (end - start > MAX_CLIP) end = start + MAX_CLIP;
  }
  return [round1(start), round1(end)];
}

function setSelection(start, end, anchor = 'start') {
  [state.selection.start, state.selection.end] = normalizeSelection(start, end, anchor);
  $('startInput').value = state.selection.start;
  $('endInput').value = state.selection.end;
  const length = state.selection.end - state.selection.start;
  setStatus('selLength', `Clip length: ${length.toFixed(1)} s` + (length < 1 ? ' (needs to be at least 1 s)' : ''), length < 1);
  for (const node of $('hlList').children) {
    const h = state.highlights[Number(node.dataset.index)];
    node.classList.toggle('active', !!h && Math.abs(h.start - state.selection.start) < 0.05 && Math.abs(h.end - state.selection.end) < 0.05);
  }
  renderTimeline();
}

function renderTimeline() {
  const dur = videoDuration();
  const timeline = $('timeline');
  timeline.classList.toggle('disabled', !dur);
  timeline.setAttribute('aria-valuemax', String(Math.round(dur)));
  timeline.setAttribute('aria-valuenow', String(Math.round(player.currentTime || 0)));
  $('tlTotal').textContent = fmtTime(dur);
  $('tlNow').textContent = fmtTime(player.currentTime || 0);
  const pct = (t) => (dur ? Math.min(100, Math.max(0, (t / dur) * 100)) : 0);
  const sel = $('tlSelection');
  sel.hidden = !dur;
  sel.style.left = pct(state.selection.start) + '%';
  sel.style.width = (pct(state.selection.end) - pct(state.selection.start)) + '%';
  $('tlHead').style.left = pct(player.currentTime || 0) + '%';
  $('tlMarkers').replaceChildren(...(dur ? state.highlights : []).map((h) => {
    const marker = make('div', { class: 'timeline-marker' });
    marker.style.left = pct(h.start) + '%';
    marker.style.width = Math.max(0.5, pct(h.end) - pct(h.start)) + '%';
    return marker;
  }));
}

function seek(seconds) {
  const dur = videoDuration();
  if (!dur || !player.currentSrc) return;
  player.currentTime = Math.min(Math.max(0, seconds), dur);
}

function setStartHere() { setSelection(player.currentTime || 0, state.selection.end, 'start'); }
function setEndHere() { setSelection(state.selection.start, player.currentTime || 0, 'end'); }

function previewSelection() {
  if (!player.currentSrc) return;
  state.previewing = true;
  player.currentTime = state.selection.start;
  player.play().catch(() => { state.previewing = false; });
}

player.addEventListener('loadedmetadata', () => {
  setSelection(state.selection.start, state.selection.end);
});
player.addEventListener('timeupdate', () => {
  if (state.previewing && player.currentTime >= state.selection.end) {
    player.pause();
    state.previewing = false;
  }
  renderTimeline();
});
player.addEventListener('pause', () => { state.previewing = false; });
player.addEventListener('error', () => {
  if (!player.getAttribute('src')) return;
  const notice = $('playerNotice');
  notice.hidden = false;
  notice.textContent = state.current
    ? "Your browser can't preview this video (MKV and some MOV files don't play in browsers). You can still type start and end times and create the clip."
    : "Your browser can't preview this file. You can still upload it and clip it by typing times.";
});

$('timeline').addEventListener('click', (event) => {
  const dur = videoDuration();
  if (!dur) return;
  const rect = event.currentTarget.getBoundingClientRect();
  seek(((event.clientX - rect.left) / rect.width) * dur);
});
$('timeline').addEventListener('keydown', (event) => {
  const step = event.shiftKey ? 5 : 1;
  if (event.key === 'ArrowRight') { seek(player.currentTime + step); event.preventDefault(); }
  if (event.key === 'ArrowLeft') { seek(player.currentTime - step); event.preventDefault(); }
});

$('startInput').addEventListener('change', () => setSelection(Number($('startInput').value), state.selection.end, 'start'));
$('endInput').addEventListener('change', () => setSelection(state.selection.start, Number($('endInput').value), 'end'));
$('setStartBtn').addEventListener('click', setStartHere);
$('setEndBtn').addEventListener('click', setEndHere);
$('previewSelBtn').addEventListener('click', previewSelection);
$('library').addEventListener('change', (e) => (e.target.value ? selectVideo(e.target.value) : clearPlayer()));

document.addEventListener('keydown', (event) => {
  if (event.ctrlKey || event.metaKey || event.altKey) return;
  if (event.target.closest('input, textarea, select, [contenteditable]')) return;
  const key = event.key.toLowerCase();
  if (key === 'i') setStartHere();
  else if (key === 'o') setEndHere();
  else if (key === 'p') previewSelection();
});

// ---------------------------------------------------------------- upload

function uploadWithProgress(file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/upload');
    if (state.token) xhr.setRequestHeader('Authorization', 'Bearer ' + state.token);
    xhr.upload.addEventListener('progress', (e) => { if (e.lengthComputable) onProgress(e.loaded / e.total); });
    xhr.addEventListener('load', () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch { /* not JSON */ }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else reject(new Error(errorText(data, xhr.status)));
    });
    xhr.addEventListener('error', () => reject(new Error('Network error during upload')));
    const form = new FormData();
    form.append('file', file);
    xhr.send(form);
  });
}

function validateFile(file) {
  const ext = file.name.slice(file.name.lastIndexOf('.')).toLowerCase();
  if (!VIDEO_EXTS.includes(ext)) return 'Upload MP4, MOV, MKV or WebM';
  if (file.size > MAX_UPLOAD) return 'Max file size is 1 GB';
  return '';
}

// Preview a chosen file right away, before it finishes uploading.
$('videoFile').addEventListener('change', () => {
  const file = $('videoFile').files[0];
  if (!file) return;
  const problem = validateFile(file);
  setStatus('uploadStatus', problem || `Ready to upload ${file.name}`, !!problem);
  if (problem) return;
  state.current = null;
  $('library').value = '';
  $('videoMeta').textContent = '';
  state.highlights = [];
  renderHighlights();
  if (state.localUrl) URL.revokeObjectURL(state.localUrl);
  state.localUrl = URL.createObjectURL(file);
  loadPlayerSource(state.localUrl);
  setSelection(0, 30);
});

$('uploadBtn').addEventListener('click', async () => {
  const file = $('videoFile').files[0];
  if (!file) { setStatus('uploadStatus', 'Choose a video file first', true); return; }
  const problem = validateFile(file);
  if (problem) { setStatus('uploadStatus', problem, true); return; }

  const button = $('uploadBtn');
  const bar = $('uploadProgress');
  button.disabled = true;
  bar.hidden = false;
  bar.firstElementChild.style.width = '0%';
  setStatus('uploadStatus', 'Uploading…');
  try {
    const result = await uploadWithProgress(file, (fraction) => {
      bar.firstElementChild.style.width = Math.round(fraction * 100) + '%';
      setStatus('uploadStatus', fraction < 1 ? `Uploading… ${Math.round(fraction * 100)}%` : 'Checking video…');
    });
    await loadJobs();
    // Keep playing the local copy so the playhead doesn't jump.
    const keepLocal = state.localUrl;
    state.localUrl = null;
    const job = state.jobs.find((j) => j.id === result.job_id);
    if (job) {
      state.current = job;
      selectVideo(job.id);
      if (keepLocal) state.localUrl = keepLocal;
    }
    setStatus('uploadStatus', 'Uploaded. Pick your moment, then create a clip.');
    $('videoFile').value = '';
  } catch (e) {
    setStatus('uploadStatus', e.message, true);
  } finally {
    button.disabled = false;
    setTimeout(() => { bar.hidden = true; }, 600);
  }
});

// ---------------------------------------------------------------- highlights

function renderHighlights() {
  $('hlList').replaceChildren(...state.highlights.map((h, index) => {
    const button = make('button', { class: 'highlight', type: 'button', 'data-index': String(index) }, [
      make('span', { text: `#${index + 1}  ${fmtTime(h.start)} – ${fmtTime(h.end)}` }),
      make('span', { class: 'score', text: index === 0 ? 'loudest' : `${h.score}% as loud` }),
    ]);
    button.addEventListener('click', () => { setSelection(h.start, h.end); seek(h.start); });
    return button;
  }));
  renderTimeline();
}

$('findHlBtn').addEventListener('click', async () => {
  if (!state.current) { setStatus('clipStatus', 'Upload or choose a video first', true); return; }
  const button = $('findHlBtn');
  const length = state.selection.end - state.selection.start;
  button.disabled = true;
  button.textContent = 'Analysing audio…';
  try {
    const result = await api('/api/highlights', {
      json: { job_id: state.current.id, duration: length >= 1 ? length : 30, count: 3 },
    });
    state.highlights = result.highlights;
    renderHighlights();
    if (state.highlights.length) {
      const top = state.highlights[0];
      setSelection(top.start, top.end);
      seek(top.start);
      setStatus('clipStatus', 'Selected the loudest moment. Click another suggestion to compare.');
    } else {
      setStatus('clipStatus', 'No audio found to analyse.', true);
    }
  } catch (e) {
    setStatus('clipStatus', e.message, true);
  } finally {
    button.disabled = false;
    button.textContent = '✨ Find loudest moments';
  }
});

// ---------------------------------------------------------------- clip

$('clipBtn').addEventListener('click', async () => {
  if (!state.current) {
    setStatus('clipStatus', state.localUrl ? 'Upload the video first, then create the clip.' : 'Upload or choose a video first', true);
    return;
  }
  const { start, end } = state.selection;
  if (end - start < 1) { setStatus('clipStatus', 'The clip needs to be at least 1 second long.', true); return; }
  const button = $('clipBtn');
  button.disabled = true;
  setStatus('clipStatus', 'Starting render…');
  try {
    await api('/api/clip', {
      json: { job_id: state.current.id, start, duration: round1(end - start), layout: $('layout').value },
    });
    setStatus('clipStatus', 'Rendering. Progress shows under Library & jobs.');
    await loadJobs();
  } catch (e) {
    setStatus('clipStatus', e.message, true);
  } finally {
    button.disabled = false;
  }
});

// ---------------------------------------------------------------- script writer

$('scriptBtn').addEventListener('click', async () => {
  const topic = $('topic').value.trim();
  const box = $('scriptBox');
  box.hidden = false;
  if (!topic) {
    $('scriptOut').textContent = 'Describe a topic first.';
    $('scriptMeta').textContent = '';
    return;
  }
  const button = $('scriptBtn');
  button.disabled = true;
  $('scriptOut').textContent = 'Generating…';
  $('scriptMeta').textContent = '';
  try {
    const result = await api('/api/script', {
      json: { topic, style: $('style').value, provider: $('provider').value, duration: Number($('seconds').value) || 45 },
    });
    const text = (result.script || '').trim();
    $('scriptOut').textContent = text;
    const words = text.split(/\s+/).filter(Boolean).length;
    $('scriptMeta').textContent = `${words} words · about ${Math.round(words / 2.5)} s read aloud`;
  } catch (e) {
    $('scriptOut').textContent = e.message;
  } finally {
    button.disabled = false;
  }
});

$('copyScriptBtn').addEventListener('click', async () => {
  const text = $('scriptOut').textContent;
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const area = make('textarea');
    area.value = text;
    document.body.append(area);
    area.select();
    document.execCommand('copy');
    area.remove();
  }
  $('copyScriptBtn').textContent = 'Copied ✓';
  setTimeout(() => { $('copyScriptBtn').textContent = 'Copy'; }, 1500);
});

// ---------------------------------------------------------------- settings

$('saveTokenBtn').addEventListener('click', async () => {
  state.token = $('token').value.trim();
  storageSet('remixToken', state.token);
  setStatus('tokenStatus', state.token ? 'Token applied for this browser session.' : 'Token cleared.');
  if (state.health) renderHealth();
  await loadJobs();
  if (state.current) loadPlayerSource(withToken(state.current.media));
});

// ---------------------------------------------------------------- start

state.token = storageGet('remixToken');
$('token').value = state.token;
renderTimeline();
loadHealth();
loadJobs();
