import importlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Point the app at a throwaway data folder *before* it is imported.
os.environ['REMIX_DATA_DIR'] = tempfile.mkdtemp(prefix='remix-test-data-')
os.environ.pop('APP_TOKEN', None)

HAS_FFMPEG = bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason='FFmpeg/ffprobe not installed')


@pytest.fixture(scope='session')
def app_module():
    return importlib.import_module('app')


@pytest.fixture(scope='session')
def client(app_module):
    from fastapi.testclient import TestClient
    return TestClient(app_module.app)


@pytest.fixture(scope='session')
def sample_video(tmp_path_factory):
    """20 s 1280x720 test video: quiet tone, loud between 8 s and 12 s."""
    if not HAS_FFMPEG:
        pytest.skip('FFmpeg not installed')
    path = tmp_path_factory.mktemp('media') / 'sample.mp4'
    subprocess.run([
        'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
        '-f', 'lavfi', '-i', 'testsrc=size=1280x720:rate=30:duration=20',
        '-f', 'lavfi', '-i', "aevalsrc='if(between(t,8,12),0.8,0.03)*sin(2*PI*440*t)':s=44100:d=20",
        '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac', '-shortest', str(path),
    ], check=True)
    return path


@pytest.fixture
def uploaded(client, sample_video):
    with sample_video.open('rb') as fh:
        response = client.post('/api/upload', files={'file': ('sample.mp4', fh, 'video/mp4')})
    assert response.status_code == 200, response.text
    return response.json()['job']
