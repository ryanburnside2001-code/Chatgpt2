import io
import time
import zipfile

import pytest

import site_team

GOOD_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Paws on Wheels | Mobile dog grooming in Leeds</title>
<meta name="description" content="Mobile dog grooming that comes to your door in Leeds. Call to book.">
<meta property="og:title" content="Paws on Wheels">
<style>a:focus-visible{outline:2px solid}</style>
</head>
<body>
<a href="#main">Skip to content</a>
<header><nav><a href="#book">Book</a></nav></header>
<main id="main">
<h1>Dog grooming that comes to you</h1>
<section id="book"><a href="tel:[OWNER: phone number]">Call to book</a></section>
<img src="data:image/svg+xml,%3Csvg%3E%3C/svg%3E" alt="Van illustration">
</main>
<footer>© 2026 Paws on Wheels</footer>
</body>
</html>
"""

BAD_PAGE = """<html><head><style>.x{transition:all 1s}</style></head><body>
<h1>One</h1><h1>Two</h1>
<img src="https://example.com/stock.jpg">
<a href="#">Buy</a> <a href="#pricing">Pricing</a>
<p>Lorem ipsum dolor sit amet. Voted best in town! Read our 5-star testimonials.</p>
"""


def levels(checks, level):
    return [c['text'] for c in checks if c['level'] == level]


# ---------------------------------------------------------------- checks + parsing

def test_good_page_has_no_failures():
    checks = site_team.check_site(GOOD_PAGE)
    assert levels(checks, 'fail') == []
    assert any('placeholder' in text for text in levels(checks, 'info'))


def test_bad_page_is_caught():
    checks = site_team.check_site(BAD_PAGE)
    fails = ' | '.join(levels(checks, 'fail'))
    warns = ' | '.join(levels(checks, 'warn'))
    for expected in ('cut off', 'lang', 'viewport', '<title>', 'meta description', 'no alt text',
                     'go nowhere', '#pricing', 'lorem ipsum'):
        assert expected in fails, expected
    for expected in ('2 <h1>', 'other websites', 'Voted best', 'testimonials', ':focus', 'prefers-reduced-motion'):
        assert expected in warns, expected


def test_extract_html_strips_fences_and_chatter():
    reply = "Sure! Here's the site:\n```html\n" + GOOD_PAGE + "```\nLet me know if you want changes."
    html = site_team.extract_html(reply)
    assert html.startswith('<!doctype html>') and html.rstrip().endswith('</html>')
    with pytest.raises(ValueError):
        site_team.extract_html('I cannot help with that.')


def test_parse_review_json_and_fallbacks():
    parsed = site_team.parse_review('```json\n{"verdict":"pass","score":"91.4","summary":"Solid.",'
                                    '"must_fix":[],"owner_must_supply":["Phone"]}\n```')
    assert parsed['verdict'] == 'PASS' and parsed['score'] == 91 and parsed['owner_must_supply'] == ['Phone']
    # A PASS that still lists must-fix items is really a FIX.
    assert site_team.parse_review('{"verdict":"PASS","must_fix":["Hero is cut off"]}')['verdict'] == 'FIX'
    plain = site_team.parse_review('Verdict: FIX\n1. Contrast too low\n2. No CTA')
    assert plain['verdict'] == 'FIX' and plain['must_fix'] == ['Contrast too low', 'No CTA']
    assert site_team.parse_review('')['verdict'] == 'FIX'


def test_brief_title_and_slug():
    brief = '# Brief: **Paws on Wheels**   \n- **Goal:** bookings'
    assert site_team.brief_title(brief) == 'Paws on Wheels'
    assert site_team.slugify('Paws on Wheels — Leeds!') == 'paws-on-wheels-leeds'
    assert site_team.slugify('!!!') == 'website'


def test_provider_requests_carry_system_prompt_and_token_limit(app_module, monkeypatch):
    for provider in ('openrouter', 'openai', 'anthropic', 'gemini'):
        monkeypatch.setenv(app_module.PROVIDER_KEYS[provider], 'k')
        _, _, _, payload = app_module.provider_request(provider, 'user text', 'system text', 9000)
        flat = str(payload)
        assert 'system text' in flat and 'user text' in flat and '9000' in flat, provider
    _, _, _, payload = app_module.provider_request('anthropic', 'hi')
    assert 'system' not in payload and payload['max_tokens'] == 1200


# ---------------------------------------------------------------- full team run

class FakeTeam:
    """Stands in for the AI providers. QA fails the first version and passes the second."""

    def __init__(self, fail_role=None):
        self.calls = []
        self.reviews = 0
        self.fail_role = fail_role

    def __call__(self, provider, system, prompt, max_tokens):
        role = ('strategist' if system.startswith('You are the strategist') else
                'reviewer' if system.startswith('You are the QA reviewer') else
                'shorts' if system.startswith('You are the YouTube Shorts producer') else 'builder')
        self.calls.append({'role': role, 'provider': provider, 'prompt': prompt, 'max_tokens': max_tokens})
        if role == self.fail_role:
            raise site_team_error()
        if role == 'strategist':
            return '# Brief: Paws on Wheels\n- **Goal / how it makes money:** phone bookings'
        if role == 'builder':
            version = sum(1 for c in self.calls if c['role'] == 'builder')
            return '```html\n' + GOOD_PAGE.replace('</footer>', f'</footer><!-- v{version} -->') + '```'
        if role == 'reviewer':
            self.reviews += 1
            if self.reviews == 1:
                return '{"verdict":"FIX","score":70,"summary":"Close.","must_fix":["Hero CTA is below the fold on phones"]}'
            return '{"verdict":"PASS","score":92,"summary":"Ready once placeholders are filled.","must_fix":[],' \
                   '"owner_must_supply":["Phone number"]}'
        return '## Short 1: Grooming at your door'


def site_team_error():
    import app
    return app.AIError('Claude returned HTTP 529: overloaded')


@pytest.fixture
def keys(monkeypatch):
    for env in ('OPENROUTER_API_KEY', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'GEMINI_API_KEY'):
        monkeypatch.setenv(env, 'test-key')


def wait_site(client, jid, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        site = client.get(f'/api/sites/{jid}').json()
        if site['status'] != 'processing':
            return site
        time.sleep(0.05)
    raise AssertionError('team never finished')


def build(client, **overrides):
    body = {'idea': 'Mobile dog grooming in Leeds', 'facts': 'Name: Paws on Wheels',
            'strategist': 'gemini', 'builder': 'anthropic', 'reviewer': 'openai', 'shorts': 'gemini',
            'fix_rounds': 2, **overrides}
    response = client.post('/api/sites', json=body)
    assert response.status_code == 200, response.text
    return response.json()['job_id']


def test_team_plans_builds_reviews_fixes_and_packages(client, app_module, monkeypatch, keys):
    fake = FakeTeam()
    monkeypatch.setattr(app_module, 'ai_complete', fake)
    jid = build(client)
    site = wait_site(client, jid)

    assert site['status'] == 'completed' and site['error'] is None
    assert site['name'] == 'Paws on Wheels' and site['slug'] == 'paws-on-wheels'
    assert site['version'] == 2 and site['verdict'] == 'PASS' and site['score'] == 92
    assert [c['role'] for c in fake.calls] == ['strategist', 'builder', 'reviewer', 'builder', 'reviewer', 'shorts']
    assert {c['role']: c['provider'] for c in fake.calls} == {
        'strategist': 'gemini', 'builder': 'anthropic', 'reviewer': 'openai', 'shorts': 'gemini'}
    fix_call = fake.calls[3]
    assert 'Hero CTA is below the fold' in fix_call['prompt'] and '<!-- v1 -->' in fix_call['prompt']
    assert 'Name: Paws on Wheels' in fake.calls[1]['prompt']  # owner facts reach the builder verbatim
    assert [s['agent'] for s in site['steps']] == ['strategist', 'builder', 'reviewer', 'builder', 'reviewer',
                                                   'shorts', 'publisher']
    assert all(s['status'] == 'done' for s in site['steps'])
    assert 'Nothing was published' in site['steps'][-1]['text']
    assert site['shorts'].startswith('## Short 1')
    assert len(site['reviews']) == 2 and site['reviews'][-1]['checks']

    preview = client.get(site['preview'])
    assert preview.status_code == 200 and '<!-- v2 -->' in preview.text
    assert preview.headers['content-security-policy'].startswith('sandbox')

    download = client.get(site['output'])
    assert download.status_code == 200
    assert 'paws-on-wheels-v2.zip' in download.headers['content-disposition']
    archive = zipfile.ZipFile(io.BytesIO(download.content))
    assert set(archive.namelist()) == {'index.html', 'brief.md', 'review.md', 'README.md', 'robots.txt',
                                       'promo-shorts.md'}
    assert '[OWNER: phone number]' in archive.read('README.md').decode()
    assert 'Verdict: PASS' in archive.read('review.md').decode()

    listed = next(j for j in client.get('/api/jobs').json() if j['id'] == jid)
    assert listed['kind'] == 'site' and 'settings' not in listed and 'brief' not in listed


def test_owner_change_request_goes_to_the_builder(client, app_module, monkeypatch, keys):
    fake = FakeTeam()
    monkeypatch.setattr(app_module, 'ai_complete', fake)
    jid = build(client, promo=False, fix_rounds=0)
    site = wait_site(client, jid)
    assert site['version'] == 1 and site['verdict'] == 'FIX' and site['open_issues'] == 1
    assert 'shorts' not in [c['role'] for c in fake.calls]

    fake.calls.clear()
    response = client.post(f'/api/sites/{jid}/revise', json={'instructions': 'Make the hero green'})
    assert response.status_code == 200, response.text
    site = wait_site(client, jid)
    assert [c['role'] for c in fake.calls] == ['builder', 'reviewer']
    assert 'Make the hero green' in fake.calls[0]['prompt']
    assert site['version'] == 2 and site['verdict'] == 'PASS'

    # Nothing to fix and no instructions: refuse instead of burning an AI call.
    assert client.post(f'/api/sites/{jid}/revise', json={}).status_code == 400


def test_fix_remaining_issues_uses_the_last_review(client, app_module, monkeypatch, keys):
    fake = FakeTeam()
    monkeypatch.setattr(app_module, 'ai_complete', fake)
    jid = build(client, promo=False, fix_rounds=0)
    wait_site(client, jid)
    fake.calls.clear()
    assert client.post(f'/api/sites/{jid}/revise', json={}).status_code == 200
    site = wait_site(client, jid)
    assert [c['role'] for c in fake.calls] == ['builder', 'reviewer']
    assert 'Hero CTA is below the fold' in fake.calls[0]['prompt']
    assert site['verdict'] == 'PASS'


def test_automatic_failures_block_a_pass(client, app_module, monkeypatch, keys):
    fake = FakeTeam()

    def broken_builder(provider, system, prompt, max_tokens):
        reply = fake(provider, system, prompt, max_tokens)
        if fake.calls[-1]['role'] == 'builder':
            return reply.replace('<h1>', '<h2>').replace('</h1>', '</h2>')
        if fake.calls[-1]['role'] == 'reviewer':
            return '{"verdict":"PASS","score":95,"must_fix":[]}'
        return reply

    monkeypatch.setattr(app_module, 'ai_complete', broken_builder)
    site = wait_site(client, build(client, promo=False, fix_rounds=0))
    assert site['verdict'] == 'FIX'
    assert any('No <h1>' in item for item in site['reviews'][-1]['must_fix'])


def test_ai_failure_is_reported(client, app_module, monkeypatch, keys):
    monkeypatch.setattr(app_module, 'ai_complete', FakeTeam(fail_role='builder'))
    jid = build(client)
    site = wait_site(client, jid)
    assert site['status'] == 'failed' and 'overloaded' in site['error']
    assert site['steps'][-1]['status'] == 'failed' and site['version'] == 0
    assert client.get(f'/api/sites/{jid}/preview').status_code == 404

    # "Try again" keeps the brief and resumes with the builder.
    fake = FakeTeam()
    monkeypatch.setattr(app_module, 'ai_complete', fake)
    assert client.post(f'/api/sites/{jid}/revise', json={}).status_code == 200
    site = wait_site(client, jid)
    assert site['status'] == 'completed' and fake.calls[0]['role'] == 'builder'


def test_failed_revision_keeps_the_last_version(client, app_module, monkeypatch, keys):
    monkeypatch.setattr(app_module, 'ai_complete', FakeTeam())
    jid = build(client, promo=False, fix_rounds=0)
    wait_site(client, jid)
    monkeypatch.setattr(app_module, 'ai_complete', FakeTeam(fail_role='builder'))
    client.post(f'/api/sites/{jid}/revise', json={'instructions': 'Add a FAQ'})
    site = wait_site(client, jid)
    assert site['status'] == 'completed' and site['version'] == 1
    assert 'previous version' in site['error']
    assert client.get(site['preview']).status_code == 200


def test_missing_keys_and_bad_input_are_rejected(client, monkeypatch):
    for env in ('OPENROUTER_API_KEY', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'GEMINI_API_KEY'):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'k')
    response = client.post('/api/sites', json={'idea': 'x', 'strategist': 'anthropic', 'builder': 'anthropic',
                                               'reviewer': 'openai', 'shorts': 'gemini', 'promo': False})
    assert response.status_code == 400
    detail = response.json()['detail']
    assert 'OPENAI_API_KEY' in detail and 'GEMINI' not in detail  # shorts is off, so Gemini isn't needed
    assert client.post('/api/sites', json={'idea': '   '}).status_code == 400
    assert client.post('/api/sites', json={'idea': 'x', 'builder': 'mystery'}).status_code == 422
    assert client.get('/api/sites/' + 'a' * 32).status_code == 404
    assert client.get('/api/sites/not-an-id').status_code == 400


def test_site_preview_needs_the_token(client, app_module, monkeypatch, keys):
    monkeypatch.setattr(app_module, 'ai_complete', FakeTeam())
    jid = build(client, promo=False, fix_rounds=0)
    site = wait_site(client, jid)
    monkeypatch.setenv('APP_TOKEN', 's3cret')
    assert client.get(site['preview']).status_code == 401
    assert client.get(site['preview'] + '?token=s3cret').status_code == 200
    assert client.get(site['output'] + '?token=s3cret').status_code == 200


def test_delete_site_removes_its_file(client, app_module, monkeypatch, keys):
    monkeypatch.setattr(app_module, 'ai_complete', FakeTeam())
    jid = build(client, promo=False, fix_rounds=0)
    wait_site(client, jid)
    assert app_module.site_file(jid).exists()
    assert client.delete(f'/api/jobs/{jid}').status_code == 200
    assert not app_module.site_file(jid).exists()
    assert client.get(f'/api/sites/{jid}').status_code == 404


def test_interrupted_site_work_is_closed_out(app_module, tmp_path):
    store = app_module.JobStore(tmp_path / 'jobs.json')
    working = [{'agent': 'builder', 'status': 'working', 'text': 'Building…'}]
    store.update('a' * 32, id='a' * 32, kind='site', status='processing', version=0, steps=working)
    store.update('b' * 32, id='b' * 32, kind='site', status='processing', version=2, steps=working)
    app_module.recover_interrupted(store)
    fresh, built = store.get('a' * 32), store.get('b' * 32)
    assert fresh['status'] == 'failed' and 'Try again' in fresh['error']
    assert built['status'] == 'completed' and 'last finished version' in built['error']
    assert fresh['steps'][0]['status'] == 'failed'
