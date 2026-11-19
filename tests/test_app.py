import json
import subprocess
import time

import pytest

from conftest import needs_ffmpeg


def wait_for(client, jid, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = next(j for j in client.get('/api/jobs').json() if j['id'] == jid)
        if job['status'] != 'processing':
            return job
        time.sleep(0.3)
    raise AssertionError('render timed out')


def probe(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-print_format', 'json', '-show_format',
                          '-show_streams', str(path)], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


# ---------------------------------------------------------------- basics

def test_health_reports_tools_and_providers(client):
    data = client.get('/api/health').json()
    assert data['ok'] is True
    assert set(data['providers']) == {'openrouter', 'openai', 'anthropic', 'gemini'}
    assert 'openrouter_configured' in data and 'auth_required' in data


def test_dashboard_and_assets_are_served(client):
    page = client.get('/')
    assert page.status_code == 200 and 'Remix' in page.text
    assert client.get('/static/app.js').status_code == 200
    assert client.get('/static/app.css').status_code == 200


def test_upload_rejects_unsupported_extension(client):
    response = client.post('/api/upload', files={'file': ('notes.txt', b'hello', 'text/plain')})
    assert response.status_code == 400


@needs_ffmpeg
def test_upload_rejects_files_that_are_not_video(client, app_module):
    before = set(app_module.DATA.iterdir())
    response = client.post('/api/upload', files={'file': ('fake.mp4', b'not a video at all', 'video/mp4')})
    assert response.status_code == 400
    assert set(app_module.DATA.iterdir()) == before  # rejected file is cleaned up


# ---------------------------------------------------------------- upload + preview

@needs_ffmpeg
def test_upload_stores_metadata_without_leaking_paths(client, uploaded):
    assert uploaded['kind'] == 'upload'
    assert uploaded['duration'] == pytest.approx(20, abs=0.2)
    assert (uploaded['width'], uploaded['height']) == (1280, 720)
    assert uploaded['has_audio'] is True
    listed = next(j for j in client.get('/api/jobs').json() if j['id'] == uploaded['id'])
    assert 'source' not in listed
    assert listed['media'] == f"/api/media/{uploaded['id']}"


@needs_ffmpeg
def test_media_endpoint_supports_range_requests(client, uploaded):
    response = client.get(uploaded['media'], headers={'Range': 'bytes=0-99'})
    assert response.status_code == 206
    assert len(response.content) == 100


# ---------------------------------------------------------------- highlights

def test_pick_highlights_prefers_loud_windows(app_module):
    levels = [-40.0] * 20 + [-6.0] * 4 + [-40.0] * 20 + [-12.0] * 4 + [-40.0] * 10
    picks = app_module.pick_highlights(levels, window_seconds=2, count=2)
    assert picks[0] == {'start': 10.0, 'end': 12.0, 'score': 100}
    assert picks[1]['start'] == 22.0 and picks[1]['score'] < 100


def test_pick_highlights_handles_short_media(app_module):
    assert app_module.pick_highlights([], 30, 3) == []
    assert app_module.pick_highlights([-20.0] * 4, 30, 3) == [{'start': 0.0, 'end': 2.0, 'score': 100}]


@needs_ffmpeg
def test_highlights_find_the_loud_section(client, uploaded):
    response = client.post('/api/highlights', json={'job_id': uploaded['id'], 'duration': 4, 'count': 3})
    assert response.status_code == 200, response.text
    picks = response.json()['highlights']
    assert len(picks) == 3
    assert 7.5 <= picks[0]['start'] <= 8.5  # loud tone runs 8-12 s
    assert picks[0]['score'] == 100


# ---------------------------------------------------------------- clips

@needs_ffmpeg
@pytest.mark.parametrize('layout,size', [('crop', (1080, 1920)), ('blur', (1080, 1920)), ('original', (1280, 720))])
def test_clip_renders_downloads_and_reports_progress(client, app_module, uploaded, layout, size):
    response = client.post('/api/clip', json={'job_id': uploaded['id'], 'start': 2, 'duration': 3, 'layout': layout})
    assert response.status_code == 200, response.text
    job = wait_for(client, response.json()['job_id'])
    assert job['status'] == 'completed', job.get('error')
    assert job['progress'] == 100 and job['source_id'] == uploaded['id']

    download = client.get(job['output'])
    assert download.status_code == 200 and download.headers['content-type'] == 'video/mp4'
    info = probe(app_module.DATA / f"{job['id']}.mp4")
    video = next(s for s in info['streams'] if s['codec_type'] == 'video')
    assert (video['width'], video['height']) == size
    assert float(info['format']['duration']) == pytest.approx(3, abs=0.3)
    assert any(s['codec_type'] == 'audio' for s in info['streams'])


@needs_ffmpeg
def test_clip_is_trimmed_to_the_end_of_the_video(client, uploaded):
    response = client.post('/api/clip', json={'job_id': uploaded['id'], 'start': 18, 'duration': 30})
    job = wait_for(client, response.json()['job_id'])
    assert job['status'] == 'completed'
    assert job['clip_duration'] == pytest.approx(2, abs=0.1)


@needs_ffmpeg
def test_clip_rejects_bad_times(client, uploaded):
    past_end = client.post('/api/clip', json={'job_id': uploaded['id'], 'start': 25, 'duration': 5})
    assert past_end.status_code == 400 and 'past the end' in past_end.json()['detail']
    too_long = client.post('/api/clip', json={'job_id': uploaded['id'], 'start': 0, 'duration': 500})
    assert too_long.status_code == 400
    bad_layout = client.post('/api/clip', json={'job_id': uploaded['id'], 'layout': 'square'})
    assert bad_layout.status_code == 422


def test_clip_and_download_validate_ids(client):
    assert client.post('/api/clip', json={'job_id': '../../etc'}).status_code == 400
    assert client.get('/api/download/nothex').status_code == 400
    assert client.get('/api/download/' + 'a' * 32).status_code == 404


# ---------------------------------------------------------------- delete

@needs_ffmpeg
def test_delete_removes_upload_and_files(client, app_module, uploaded):
    jid = uploaded['id']
    client.post('/api/highlights', json={'job_id': jid, 'duration': 4})
    assert (app_module.DATA / f'{jid}.loudness.json').exists()
    assert client.delete(f'/api/jobs/{jid}').status_code == 200
    assert not list(app_module.DATA.glob(f'{jid}*'))
    assert all(j['id'] != jid for j in client.get('/api/jobs').json())
    assert client.get(f'/api/media/{jid}').status_code == 404


# ---------------------------------------------------------------- auth + recovery

@needs_ffmpeg
def test_app_token_protects_api_and_media(client, uploaded, monkeypatch):
    monkeypatch.setenv('APP_TOKEN', 's3cret')
    assert client.get('/api/health').json()['auth_required'] is True
    assert client.get('/api/jobs').status_code == 401
    assert client.get('/api/jobs', headers={'Authorization': 'Bearer wrong'}).status_code == 401
    assert client.get('/api/jobs', headers={'Authorization': 'Bearer s3cret'}).status_code == 200
    assert client.get(uploaded['media']).status_code == 401
    assert client.get(uploaded['media'] + '?token=s3cret').status_code == 200


def test_interrupted_renders_are_marked_failed(app_module, tmp_path):
    path = tmp_path / 'jobs.json'
    path.write_text(json.dumps({'a' * 32: {'id': 'a' * 32, 'status': 'processing'},
                                'b' * 32: {'id': 'b' * 32, 'status': 'uploaded'}}))
    store = app_module.JobStore(path)
    app_module.recover_interrupted(store)
    saved = json.loads(path.read_text())
    assert saved['a' * 32]['status'] == 'failed'
    assert saved['b' * 32]['status'] == 'uploaded'


def test_legacy_jobs_get_a_kind(app_module):
    assert app_module.public({'id': 'x', 'status': 'uploaded'})['kind'] == 'upload'
    assert app_module.public({'id': 'y', 'status': 'completed', 'source': '/secret'}) == {
        'id': 'y', 'status': 'completed', 'kind': 'clip'}


# ---------------------------------------------------------------- script writer

def test_script_requires_a_configured_key(client, monkeypatch):
    monkeypatch.delenv('OPENROUTER_API_KEY', raising=False)
    response = client.post('/api/script', json={'topic': 'comebacks', 'provider': 'openrouter'})
    assert response.status_code == 400 and 'OPENROUTER_API_KEY' in response.json()['detail']


def test_script_builds_prompt_and_returns_text(client, app_module, monkeypatch):
    seen = {}

    async def fake_chat(prompt, provider):
        seen.update(prompt=prompt, provider=provider)
        return 'Hook. Story. Call to action.'

    monkeypatch.setattr(app_module, 'ai_chat', fake_chat)
    response = client.post('/api/script', json={'topic': 'learning to skate at 40', 'style': 'funny',
                                                'duration': 60, 'provider': 'anthropic'})
    assert response.json() == {'script': 'Hook. Story. Call to action.'}
    assert seen['provider'] == 'anthropic'
    assert 'learning to skate at 40' in seen['prompt'] and 'funny' in seen['prompt'] and '60 seconds' in seen['prompt']


def test_script_rejects_unknown_provider_and_empty_topic(client):
    assert client.post('/api/script', json={'topic': 'x', 'provider': 'mystery'}).status_code == 422
    assert client.post('/api/script', json={'topic': '   '}).status_code == 400


def test_anthropic_model_is_configurable(app_module, monkeypatch):
    monkeypatch.setenv('ANTHROPIC_MODEL', 'my-model')
    _, url, headers, payload = app_module.provider_request('anthropic', 'hi')
    assert url.endswith('/v1/messages') and payload['model'] == 'my-model'
    assert headers['anthropic-version'] == '2023-06-01'
