'use strict';

const MAX_CLIP = 180;
const MAX_UPLOAD = 1024 * 1024 * 1024;
const VIDEO_EXTS = ['.mp4', '.mov', '.mkv', '.webm'];
const PROVIDER_NAMES = { openrouter: 'OpenRouter', openai: 'OpenAI', anthropic: 'Claude', gemini: 'Gemini' };

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
  const names = PROVIDER_NAMES;
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
  for (const select of [$('provider'), ...document.querySelectorAll('.provider-select')]) {
    for (const option of select.options) {
      const ok = providers[option.value];
      option.textContent = names[option.value] + (ok ? '' : ' (no key in .env)');
    }
    if (!providers[select.value] && configured.length) select.value = configured[0];
  }

  $('tokenNotice').hidden = !(h.auth_required && !state.token);
}

// ---------------------------------------------------------------- jobs

async function loadJobs() {
  try {
    state.jobs = await api('/api/jobs');
    renderJobs();
    renderLibrary();
    syncSites();
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
    root.replaceChildren(make('p', { class: 'muted', text: 'Nothing here yet. Build a website or upload a video to get started.' }));
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
  } else if (job.kind === 'site') {
    if (job.version) meta.push(`version ${job.version}`);
    if (job.verdict) meta.push(job.verdict === 'PASS' ? 'QA passed' : `QA: ${job.open_issues || 0} open issue(s)`);
  } else {
    if (job.clip_duration) meta.push(`${fmtTime(job.start || 0)} + ${job.clip_duration.toFixed(1)}s`);
    if (job.layout) meta.push(LAYOUT_NAMES[job.layout] || job.layout);
  }
  meta.push(fmtDate(job.created));

  const actions = [];
  if (job.kind === 'upload') {
    actions.push(make('button', { class: 'secondary compact', type: 'button', text: 'Open in clipper', onclick: () => selectVideo(job.id, true) }));
  }
  if (job.kind === 'site') {
    actions.push(make('button', { class: 'secondary compact', type: 'button', text: 'Open in website team', onclick: () => openSite(job.id, true) }));
  }
  if (job.status === 'completed' && job.output) {
    actions.push(make('a', { class: 'button compact', href: withToken(job.output), download: '', text: '⬇ Download' }));
  }
  if (job.status !== 'processing') {
    actions.push(make('button', { class: 'danger compact', type: 'button', text: 'Delete', onclick: () => deleteJob(job) }));
  }

  let progress = null;
  if (job.status === 'processing' && job.kind !== 'site') {
    progress = make('div', { class: 'progress' }, [make('div', { class: 'progress-bar' })]);
    progress.firstChild.style.width = (job.progress || 0) + '%';
  }

  return make('article', { class: 'job' }, [
    make('div', { class: 'job-top' }, [
      make('div', {}, [
        make('div', { class: 'job-name', text: ({ upload: '🎞 ', site: '🌐 ' }[job.kind] || '✂️ ') + (job.name || 'Video') }),
        make('div', { class: 'job-meta', text: meta.filter(Boolean).join(' · ') }),
      ]),
      make('span', { class: 'badge ' + job.status, text: badgeText(job) }),
    ]),
    progress,
    job.error ? make('div', { class: 'job-error', text: job.error }) : null,
    actions.length ? make('div', { class: 'job-actions' }, actions) : null,
  ]);
}

function badgeText(job) {
  if (job.status !== 'processing') return job.status;
  return job.kind === 'site' ? 'working' : `${job.progress || 0}%`;
}

async function deleteJob(job) {
  const what = { upload: 'this video and its highlight data', site: 'this website and its files' }[job.kind] || 'this clip';
  if (!confirm(`Delete ${what}? This can't be undone.`)) return;
  try {
    await api('/api/jobs/' + job.id, { method: 'DELETE' });
    if (state.current && state.current.id === job.id) clearPlayer();
    if (team.site && team.site.id === job.id) closeSite();
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

// ---------------------------------------------------------------- AI website team

const ROLE_INFO = {
  strategist: ['🧭', 'Strategist'],
  builder: ['🛠️', 'Web builder'],
  reviewer: ['🔍', 'QA reviewer'],
  shorts: ['🎬', 'Shorts producer'],
  publisher: ['🚀', 'Publisher'],
};
const ROLE_SELECTS = { strategist: 'roleStrategist', builder: 'roleBuilder', reviewer: 'roleReviewer', shorts: 'roleShorts' };
const team = { site: null, frameKey: '', opened: false, loading: false, wasBusy: false };

function prefsGet() {
  try { return JSON.parse(localStorage.getItem('remixTeam') || '{}') || {}; } catch { return {}; }
}
function prefsSave() {
  const prefs = { rounds: $('siteRounds').value, promo: $('sitePromo').checked };
  for (const [role, id] of Object.entries(ROLE_SELECTS)) prefs[role] = $(id).value;
  try { localStorage.setItem('remixTeam', JSON.stringify(prefs)); } catch { /* storage blocked */ }
}

function initTeamForm() {
  const prefs = prefsGet();
  for (const [role, id] of Object.entries(ROLE_SELECTS)) {
    const select = $(id);
    select.replaceChildren(...Object.keys(PROVIDER_NAMES).map((p) => make('option', { value: p, text: PROVIDER_NAMES[p] })));
    if (prefs[role] in PROVIDER_NAMES) select.value = prefs[role];
    select.addEventListener('change', prefsSave);
  }
  if (['1', '2', '3'].includes(prefs.rounds)) $('siteRounds').value = prefs.rounds;
  if (typeof prefs.promo === 'boolean') $('sitePromo').checked = prefs.promo;
  $('roleShorts').disabled = !$('sitePromo').checked;
  $('siteRounds').addEventListener('change', prefsSave);
  $('sitePromo').addEventListener('change', () => { $('roleShorts').disabled = !$('sitePromo').checked; prefsSave(); });
}

function fmtSpan(startIso, endIso) {
  const ms = new Date(endIso) - new Date(startIso);
  if (!Number.isFinite(ms) || ms < 0) return '';
  const s = Math.max(1, Math.round(ms / 1000));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}

function syncSites() {
  renderSiteSelect();
  if (team.site) {
    const listed = state.jobs.find((j) => j.id === team.site.id);
    if (!listed) closeSite();
    else if (team.site.status === 'processing' || listed.status !== team.site.status || listed.version !== team.site.version) refreshSite();
  } else if (!team.opened) {
    const newest = state.jobs.find((j) => j.kind === 'site');
    if (newest) openSite(newest.id);
  }
  team.opened = true;
}

function renderSiteSelect() {
  const sites = state.jobs.filter((j) => j.kind === 'site');
  const select = $('siteSelect');
  const current = team.site ? team.site.id : '';
  select.replaceChildren(
    make('option', { value: '', text: sites.length ? 'Your websites…' : 'No websites yet' }),
    ...sites.map((j) => make('option', { value: j.id, text: (j.name || 'Website') + (j.version ? ` · v${j.version}` : '') })));
  select.value = sites.some((j) => j.id === current) ? current : '';
}

async function openSite(id, scroll = false) {
  try {
    team.site = await api('/api/sites/' + id);
    team.wasBusy = team.site.status === 'processing';
    renderSite();
    renderSiteSelect();
    if (scroll) $('siteTeam').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (e) {
    setStatus('siteStatus', e.message, true);
  }
}

async function refreshSite() {
  if (!team.site || team.loading) return;
  team.loading = true;
  try {
    const fresh = await api('/api/sites/' + team.site.id);
    if (team.site && team.site.id === fresh.id) {
      team.site = fresh;
      renderSite();
    }
  } catch { /* the next poll will retry */ } finally {
    team.loading = false;
  }
}

function closeSite() {
  team.site = null;
  team.frameKey = '';
  $('siteFrame').removeAttribute('src');
  renderSite();
  renderSiteSelect();
}

function renderStep(step) {
  const [icon, who] = ROLE_INFO[step.agent] || ['•', step.agent];
  const classes = [step.status];
  if (step.agent === 'reviewer' && step.status === 'done') classes.push(step.text.startsWith('PASS') ? 'pass' : 'fix');
  let stateNode;
  if (step.status === 'working') {
    stateNode = make('span', { class: 'state' }, [make('span', { class: 'spinner', 'aria-hidden': 'true' }), ' working']);
  } else if (step.status === 'failed') {
    stateNode = make('span', { class: 'state', text: '✗ failed' });
  } else {
    stateNode = make('span', { class: 'state', text: '✓ ' + fmtSpan(step.started, step.finished) });
  }
  return make('li', { class: classes.join(' ') }, [
    make('span', { class: 'icon', 'aria-hidden': 'true', text: icon }),
    make('div', {}, [make('span', { class: 'who', text: who }), make('span', { class: 'what', text: step.text })]),
    stateNode,
  ]);
}

function renderQA(site) {
  const root = $('qaReport');
  const reviews = site.reviews || [];
  const r = reviews[reviews.length - 1];
  if (!r) {
    root.replaceChildren(make('p', { class: 'muted', text: "QA hasn't reviewed this site yet." }));
    return;
  }
  const parts = [make('h4', {}, [
    '🔍 QA report',
    make('span', { class: 'verdict ' + r.verdict.toLowerCase(), text: r.verdict }),
    r.score != null ? make('span', { class: 'score', text: `${r.score}/100` }) : null,
    make('span', { class: 'muted small', text: `round ${r.round} · version ${r.version}` }),
  ])];
  if (r.version !== site.version) {
    parts.push(make('p', { class: 'notice', text: `This report is for version ${r.version}. Version ${site.version} hasn't been reviewed yet.` }));
  }
  if (r.summary) parts.push(make('p', { class: 'muted small', text: r.summary }));
  const section = (title, items, ordered) => (items && items.length
    ? [make('h5', { text: title }), make(ordered ? 'ol' : 'ul', {}, items.map((t) => make('li', { text: t })))]
    : []);
  parts.push(...section('Must fix', r.must_fix, true));
  parts.push(...section('You need to supply before publishing', r.owner_must_supply));
  parts.push(...section('Nice to have', r.nice_to_have));
  if (r.checks && r.checks.length) {
    parts.push(make('h5', { text: 'Automatic checks' }),
      make('ul', { class: 'checks' }, r.checks.map((c) => make('li', { class: c.level, text: c.text }))));
  }
  if (reviews.length > 1) {
    const history = reviews.map((x) => `v${x.version} ${x.verdict}${x.score != null ? ' ' + x.score : ''}`).join(' → ');
    parts.push(make('p', { class: 'muted small', text: 'History: ' + history }));
  }
  root.replaceChildren(...parts);
}

function renderSite() {
  const site = team.site;
  const busy = Boolean(site && site.status === 'processing');
  const list = $('siteActivity');
  if (!site) {
    list.replaceChildren(make('li', { class: 'activity-empty muted', text: "Describe a site and press Build my website. Each teammate's work shows up here as it happens." }));
  } else {
    const steps = site.steps || [];
    const items = steps.map(renderStep);
    if (busy && !steps.some((s) => s.status === 'working')) {
      items.push(renderStep({ agent: 'strategist', status: 'working', text: 'Getting the team together…' }));
    }
    list.replaceChildren(...items);
  }
  $('siteError').hidden = !(site && site.error);
  $('siteError').textContent = (site && site.error) || '';
  $('siteRetryRow').hidden = !(site && site.status === 'failed' && !site.version);
  if (!busy && team.wasBusy) {  // the team just finished: clear "working on it" messages
    setStatus('siteStatus', '');
    setStatus('reviseStatus', '');
  }
  team.wasBusy = busy;

  const built = Boolean(site && site.version);
  $('siteResult').hidden = !built;
  if (!built) return;

  $('siteName').textContent = site.name || 'Website';
  const verdict = site.verdict === 'PASS' ? 'QA passed' : site.verdict ? `QA: ${site.open_issues || 0} open issue(s)` : 'not reviewed yet';
  $('siteMeta').textContent = [`Version ${site.version}`, busy ? 'the team is working on a new version' : verdict,
    fmtDate(site.finished || site.created)].filter(Boolean).join(' · ');
  $('siteOpen').href = withToken(`${site.preview}?v=${site.version}`);
  $('siteDownload').href = withToken(site.output);
  const key = `${site.id}:${site.version}:${state.token}`;
  if (team.frameKey !== key) {
    $('siteFrame').src = withToken(`${site.preview}?v=${site.version}`);
    team.frameKey = key;
  }
  renderQA(site);
  $('briefOut').textContent = site.brief || '';
  $('shortsBox').hidden = !site.shorts;
  $('shortsOut').textContent = site.shorts || '';

  $('siteReviseBtn').disabled = busy;
  const openIssues = site.verdict === 'FIX' ? site.open_issues || 0 : 0;
  $('siteFixBtn').hidden = busy || !openIssues;
  $('siteFixBtn').textContent = `Fix remaining ${openIssues} issue${openIssues === 1 ? '' : 's'}`;
  if (busy) setStatus('reviseStatus', 'The team is working on it. The preview updates when the new version is ready.');
}

async function startRevision(instructions, statusId) {
  if (!team.site) return;
  setStatus(statusId, 'Sending to the team…');
  try {
    await api(`/api/sites/${team.site.id}/revise`, { json: { instructions } });
    if (instructions) $('siteChanges').value = '';
    await openSite(team.site.id);
    await loadJobs();
  } catch (e) {
    setStatus(statusId, e.message, true);
  }
}

$('buildSiteBtn').addEventListener('click', async () => {
  const idea = $('siteIdea').value.trim();
  if (!idea) {
    setStatus('siteStatus', 'Describe the website you want first.', true);
    $('siteIdea').focus();
    return;
  }
  const body = {
    idea,
    audience: $('siteAudience').value.trim(),
    facts: $('siteFacts').value.trim(),
    look: $('siteLook').value,
    promo: $('sitePromo').checked,
    fix_rounds: Number($('siteRounds').value),
  };
  for (const [role, id] of Object.entries(ROLE_SELECTS)) body[role] = $(id).value;
  const button = $('buildSiteBtn');
  button.disabled = true;
  setStatus('siteStatus', 'Sending your request to the team…');
  try {
    const result = await api('/api/sites', { json: body });
    setStatus('siteStatus', 'The team is on it. A full build usually takes a few minutes.');
    await openSite(result.job_id);
    await loadJobs();
  } catch (e) {
    setStatus('siteStatus', e.message, true);
  } finally {
    button.disabled = false;
  }
});

$('siteReviseBtn').addEventListener('click', () => {
  const text = $('siteChanges').value.trim();
  if (!text) {
    setStatus('reviseStatus', 'Describe the changes you want first.', true);
    $('siteChanges').focus();
    return;
  }
  startRevision(text, 'reviseStatus');
});
$('siteFixBtn').addEventListener('click', () => startRevision('', 'reviseStatus'));
$('siteRetryBtn').addEventListener('click', () => startRevision('', 'siteStatus'));
$('siteSelect').addEventListener('change', (e) => (e.target.value ? openSite(e.target.value) : closeSite()));

for (const button of document.querySelectorAll('.seg')) {
  button.addEventListener('click', () => {
    for (const other of document.querySelectorAll('.seg')) {
      const on = other === button;
      other.classList.toggle('active', on);
      other.setAttribute('aria-pressed', String(on));
    }
    $('frameWrap').classList.toggle('phone', button.dataset.view === 'phone');
  });
}

// ---------------------------------------------------------------- settings

$('saveTokenBtn').addEventListener('click', async () => {
  state.token = $('token').value.trim();
  storageSet('remixToken', state.token);
  setStatus('tokenStatus', state.token ? 'Token applied for this browser session.' : 'Token cleared.');
  if (state.health) renderHealth();
  await loadJobs();
  if (state.current) loadPlayerSource(withToken(state.current.media));
  if (team.site) renderSite();
});

// ---------------------------------------------------------------- start

state.token = storageGet('remixToken');
$('token').value = state.token;
initTeamForm();
renderTimeline();
loadHealth();
loadJobs();
