"""AI Website Team: prompts, parsing, automatic checks and packaging.

The team works like a small agency:

1. Strategist: turns the owner's request into a brief (audience, offer, page plan, style).
2. Web builder: builds a single-file website from the brief.
3. QA reviewer: reviews it against the brief and a strict checklist, backed by
   automatic checks that no AI can talk its way past.
4. Web builder: fixes everything QA found. Steps 3 and 4 repeat until QA passes
   or the round limit is reached.
5. Shorts producer (optional): writes promo YouTube Shorts scripts for the site.
6. Publisher: packages the site for download. Nothing is ever published
   automatically; the owner decides where it goes.

This module has no web or storage code, so it is easy to test on its own.
"""
import io
import json
import re
import zipfile
from datetime import datetime, timezone
from html.parser import HTMLParser

OWNER_RE = re.compile(r'\[OWNER:[^\]\n]{1,300}\]')

LOOKS = {
    'auto': 'Strategist decides what fits the audience best',
    'clean': 'Clean and modern: light background, lots of white space, one strong accent colour',
    'bold': 'Bold and colourful: confident colour blocks, big type, energetic',
    'dark': 'Dark and premium: dark background, refined type, subtle glow accents',
    'warm': 'Warm and friendly: soft warm colours, rounded shapes, approachable copy',
    'minimal': 'Minimal and editorial: mostly black and white, strong typography, little decoration',
}

HONESTY_RULES = (
    'Use only facts the owner gave. Never invent testimonials, reviews, star ratings, client names or logos, '
    'awards, statistics, prices, years in business, guarantees, licences or credentials, and never make '
    'income, health, legal or "#1 / best" claims. Where real information is needed, write a visible '
    'placeholder in exactly this format: [OWNER: what to add].'
)

# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------

STRATEGIST_SYSTEM = f"""You are the strategist on a small team that builds websites that earn money for the owner.
You decide what the site must achieve and plan it so the web builder can build it without guessing.
Be blunt and realistic: a clear "this won't make money, here's why" is worth more than enthusiasm.
{HONESTY_RULES}

Return Markdown in exactly this template and nothing else (under 500 words):

# Brief: <site name>
- **Goal / how it makes money:**
- **Audience:** who they are, what they want, what worries them before buying
- **Offer & call to action:** the one action a visitor should take, and the exact button wording
- **Page plan:** an ordered list of sections. For each: purpose, headline idea, key content
- **Key messages / keywords:** the phrases this audience actually searches for
- **Style:** tone of voice; a palette as hex codes (background, surface, text, muted text, accent) with
  readable contrast; a font pairing; visual motifs; one sentence on how it will look better than typical competitors
- **Facts to use:** only facts from the owner, copied exactly
- **Needs from owner:** [OWNER: ...] items the site will need
- **Honest assessment:** will this make money, the biggest risk, and what to measure in the first 30 days"""

BUILDER_SYSTEM = f"""You are the web builder on a small agency team. You turn the strategist's brief into a polished,
production-quality website delivered as ONE self-contained index.html file.

Output format:
- Return only the complete HTML document, from <!doctype html> to </html>. No commentary, no Markdown fences.
- All CSS in one <style> in the <head>. Any JavaScript in one <script> before </body>. No frameworks, no
  build step, no external JavaScript. You may load up to two Google Fonts families; otherwise use a system font stack.
- No external or hotlinked images (they break and may be unlicensed). Make visuals with CSS (gradients, shapes)
  and inline SVG illustrations and icons. Use an inline SVG data URI as the favicon.

Design bar (this is what separates a great site from a generic one):
- The hero headline says what is offered and for whom, in plain words. One primary call to action, visible
  without scrolling on a 390px-wide phone, repeated near the end of the page.
- A deliberate visual identity: palette as CSS custom properties with WCAG AA contrast, a type scale with
  clamp(), spacing on an 8px rhythm, content max-width around 1120px, consistent radius and shadows.
- Mobile-first layout with grid and flexbox. No horizontal scrolling at 320px. Tap targets at least 44px.
- Only sections that earn their place (pick from the brief: hero, benefits, how it works, offer/pricing,
  FAQ using <details>, about, contact). Specific, concrete copy in the brief's tone. No lorem ipsum, no filler
  such as "Welcome to our website", no buzzword soup.
- Tasteful motion only: hover and focus states, optional reveal-on-scroll with IntersectionObserver, all
  turned off under prefers-reduced-motion.

Required:
- <html lang>, meta charset, viewport, <title> (60 characters max), meta description (155 characters max),
  Open Graph title/description/type, theme-color.
- header, nav, main, footer landmarks; exactly one <h1>; logical heading order; a skip-to-content link;
  visible :focus-visible styles; alt text on every <img>; aria-label on icon-only buttons.
- A mobile menu button with aria-expanded if the nav has more than three links. Every in-page link points at
  an id that exists. No href="#" and no buttons that do nothing.
- Contact through tel: and mailto: links with the owner's real details. Include a form only if the owner gave
  a form endpoint. Footer with the year and business name.
- {HONESTY_RULES}
- Keep the file under about 120 KB."""

FIX_SYSTEM = BUILDER_SYSTEM + """

You are now revising an existing index.html. Apply every requested change, keep everything that already
works, and return the complete updated document (never a diff or a fragment)."""

REVIEWER_SYSTEM = f"""You are the QA reviewer: the last line of defence before a website goes public under the owner's name.
You do not fix the work. You find problems and describe them precisely so the builder can fix them.

Check the site against the brief and this checklist:
- Delivers the brief: right audience, clear offer, one obvious call to action that actually works.
- Design quality: looks professional and distinctive, consistent spacing and type, readable contrast, works at
  320px, 390px and 1440px widths (judge from the CSS), nothing that looks broken or unfinished.
- Copy: specific and persuasive, no filler, no typos, no placeholder text other than [OWNER: ...] items.
- Accessibility and SEO: title, meta description, Open Graph, one h1, heading order, alt text, focus styles,
  labelled controls, landmarks, skip link.
- Code: no dead links or buttons, in-page links match ids, mobile menu works, no JavaScript errors you can spot.
- Policy (automatic FIX if any fail): {HONESTY_RULES} No copyrighted images, logos or fonts without a licence.
  No impersonating real people or brands.

Rules:
- PASS only if the owner could publish it today once the [OWNER: ...] placeholders are filled in.
  If anything is in must_fix, the verdict is FIX.
- Every automatic check marked FAIL must appear in must_fix.
- [OWNER: ...] placeholders are not defects. List what the owner must supply in owner_must_supply.
- At most 10 must_fix items, most important first. Each one: where, the problem, and what good looks like.
- score: 0-100 (design 30, copy and offer 25, accessibility and SEO 20, mobile 15, honesty and policy 10).

Return only JSON, no Markdown fences:
{{"verdict": "PASS" or "FIX", "score": 0-100, "summary": "one sentence",
 "must_fix": ["..."], "nice_to_have": ["..."], "owner_must_supply": ["..."]}}"""

SHORTS_SYSTEM = f"""You are the YouTube Shorts producer. You write short vertical videos that send viewers to the
owner's website. Each Short is one distinct, useful idea in your own words, not an advert read aloud.
{HONESTY_RULES}

Write 3 different Shorts scripts in Markdown. For each:
## Short N: <title, under 60 characters>
- **Hook (first 2 seconds):** a bold claim, question or "stop doing X". No intros.
- **Scenes:** 3 to 6 numbered scenes, each with on-screen text (10 words max) and the voiceover line
- **Payoff and call to action:** a soft one, pointing to the website
- **Description:** 1-2 sentences plus 3 relevant hashtags including #Shorts
Keep each Short 15-45 seconds when read aloud."""


def strategist_prompt(idea, audience, facts, look):
    parts = [f"Owner's request:\n{idea}"]
    if audience:
        parts.append(f'Audience (from the owner): {audience}')
    parts.append(f'Look and feel: {LOOKS.get(look, LOOKS["auto"])}')
    parts.append(f"Owner's real details (the only facts you may use):\n{facts or '(none given)'}")
    return STRATEGIST_SYSTEM, '\n\n'.join(parts)


def builder_prompt(brief, facts):
    return BUILDER_SYSTEM, (
        f'Brief from the strategist:\n\n{brief}\n\n'
        f"Owner's real details, verbatim (the only facts you may use):\n{facts or '(none given)'}\n\n"
        'Build the complete index.html now.')


def fix_prompt(brief, facts, html, fixes=None, owner_request=None):
    parts = [f'Brief:\n\n{brief}',
             f"Owner's real details, verbatim:\n{facts or '(none given)'}"]
    if owner_request:
        parts.append(f'The owner asked for these changes. They take priority over the brief:\n{owner_request}')
    if fixes:
        parts.append('The QA reviewer found these problems. Fix every one:\n'
                     + '\n'.join(f'{n}. {item}' for n, item in enumerate(fixes, 1)))
    parts.append(f'Current index.html:\n\n{html}')
    parts.append('Return the complete updated index.html now.')
    return FIX_SYSTEM, '\n\n'.join(parts)


def review_prompt(brief, html, checks, round_number):
    lines = [f"- {c['level'].upper()}: {c['text']}" for c in checks if c['level'] in ('fail', 'warn')]
    automatic = '\n'.join(lines) or '- All automatic checks passed.'
    return REVIEWER_SYSTEM, (
        f'Review round {round_number}.\n\nBrief:\n\n{brief}\n\n'
        f'Automatic checks:\n{automatic}\n\n'
        f'index.html:\n\n{html}')


def shorts_prompt(brief):
    return SHORTS_SYSTEM, f'The website and its brief:\n\n{brief}\n\nWrite the 3 Shorts scripts now.'


# --------------------------------------------------------------------------
# Parsing AI replies
# --------------------------------------------------------------------------

def extract_html(text: str) -> str:
    """Pull the HTML document out of a reply that may include fences or chatter."""
    text = (text or '').strip()
    fence = re.search(r'```(?:html)?[ \t]*\n(.*?)(?:```|\Z)', text, re.S | re.I)
    if fence and '<html' in fence.group(1).lower():
        text = fence.group(1).strip()
    start = re.search(r'<!doctype html|<html[\s>]', text, re.I)
    if not start:
        raise ValueError("The web builder didn't return a web page. Try again, or pick a different AI for the builder.")
    text = text[start.start():]
    end = text.lower().rfind('</html>')
    if end != -1:
        text = text[:end + len('</html>')]
    return text + '\n'


def brief_title(brief: str) -> str:
    match = re.search(r'^#\s*Brief:\s*(.+?)\s*$', brief or '', re.M)
    title = match.group(1) if match else ''
    return re.sub(r'[*_`#]', '', title).strip()[:80]


def slugify(text: str) -> str:
    slug = re.sub(r'[^a-z0-9]+', '-', (text or '').lower()).strip('-')
    return slug[:48].rstrip('-') or 'website'


def _str_list(value, limit=12):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    items = []
    for item in value:
        text = str(item).strip()
        if text:
            items.append(text[:600])
    return items[:limit]


def parse_review(text: str) -> dict:
    """Normalise the reviewer's reply. Falls back to reading a plain-text list."""
    data = None
    match = re.search(r'\{.*\}', text or '', re.S)
    if match:
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            data = None
    if not isinstance(data, dict):
        items = [re.sub(r'^\s*(?:\d+[.)]|[-*])\s*', '', line).strip()
                 for line in (text or '').splitlines() if re.match(r'\s*(?:\d+[.)]|[-*])\s+\S', line)]
        passed = bool(re.search(r'verdict\W{0,5}pass', text or '', re.I)) and not items
        data = {'verdict': 'PASS' if passed else 'FIX', 'must_fix': items,
                'summary': "The reviewer didn't return its usual format, so these are its notes as written."}
        if not passed and not items:
            data['must_fix'] = [(text or 'The reviewer returned an empty reply.').strip()[:600]]
    must_fix = _str_list(data.get('must_fix'))
    verdict = 'PASS' if str(data.get('verdict', '')).strip().upper() == 'PASS' and not must_fix else 'FIX'
    try:
        score = max(0, min(100, int(round(float(data.get('score'))))))
    except (TypeError, ValueError):
        score = None
    return {
        'verdict': verdict,
        'score': score,
        'summary': str(data.get('summary') or '').strip()[:400],
        'must_fix': must_fix,
        'nice_to_have': _str_list(data.get('nice_to_have')),
        'owner_must_supply': _str_list(data.get('owner_must_supply')),
    }


# --------------------------------------------------------------------------
# Automatic checks (the part of QA that can't be talked out of a FAIL)
# --------------------------------------------------------------------------

class _PageScan(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.ids = set()
        self.title = ''
        self.text = []
        self.css = []
        self._in_title = False
        self._in_style = False
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        attrs = {name.lower(): (value or '') for name, value in attrs}
        self.tags.append((tag, attrs))
        if attrs.get('id'):
            self.ids.add(attrs['id'])
        if tag == 'title':
            self._in_title = True
        elif tag in ('script', 'style'):
            self._skip += 1
            self._in_style = tag == 'style'

    def handle_endtag(self, tag):
        if tag == 'title':
            self._in_title = False
        elif tag in ('script', 'style') and self._skip:
            self._skip -= 1
            self._in_style = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif self._in_style:
            self.css.append(data)
        elif not self._skip:
            self.text.append(data)


RISKY_CLAIMS = re.compile(
    r"#\s?1\b|number one|\bguarantee[ds]?\b|100% satisf|best in (?:the )?(?:world|town|city|country|uk|us)"
    r"|\bvoted best\b|award[- ]winning", re.I)
REVIEW_HINTS = re.compile(r'testimonial|what (?:our|my) (?:clients|customers) say|★|5[- ]star|five[- ]star', re.I)


def check_site(html: str) -> list[dict]:
    """Deterministic checks. level: fail (must fix), warn (reviewer decides), ok, info."""
    checks = []

    def add(level, text):
        checks.append({'level': level, 'text': text})

    lower = html.lower()
    scan = _PageScan()
    try:
        scan.feed(html)
        scan.close()
    except Exception:  # the stdlib parser is lenient; this is belt and braces
        add('fail', "The HTML couldn't be read. Rebuild the page.")
        return checks

    tags = scan.tags

    def first(tag, **want):
        for name, attrs in tags:
            if name == tag and all(attrs.get(k, '').lower() == v for k, v in want.items()):
                return attrs
        return None

    if '</html>' not in lower:
        add('fail', 'The page is cut off (no closing </html>). The AI probably ran out of room. Shorten the CSS and copy.')
    if not lower.lstrip().startswith('<!doctype html'):
        add('warn', 'The page has no <!doctype html> at the top.')

    html_tag = first('html')
    if html_tag is None or not html_tag.get('lang'):
        add('fail', 'The <html> tag needs a lang attribute (for example lang="en").')
    if first('meta', name='viewport') is None:
        add('fail', "Missing the viewport meta tag, so the site won't scale on phones.")
    else:
        add('ok', 'Phone-friendly viewport tag')

    title = ' '.join(scan.title.split())
    if not title:
        add('fail', 'Missing a <title>.')
    elif len(title) > 65:
        add('warn', f'The title is {len(title)} characters. Search results cut it off after about 60.')
    else:
        add('ok', f'Title: "{title}"')

    description = first('meta', name='description')
    content = ' '.join((description or {}).get('content', '').split())
    if not content:
        add('fail', 'Missing a meta description (the text shown under the link in search results).')
    elif len(content) > 165:
        add('warn', f'The meta description is {len(content)} characters. Keep it under about 155.')
    else:
        add('ok', 'Meta description set')
    if first('meta', property='og:title') is None:
        add('warn', 'No Open Graph tags, so shared links show no title or description.')

    h1_count = sum(1 for name, _ in tags if name == 'h1')
    if h1_count == 0:
        add('fail', 'No <h1> headline.')
    elif h1_count > 1:
        add('warn', f'{h1_count} <h1> headlines. Use exactly one.')
    else:
        add('ok', 'One <h1> headline')

    images = [attrs for name, attrs in tags if name == 'img']
    no_alt = [attrs for attrs in images if 'alt' not in attrs]
    if no_alt:
        add('fail', f'{len(no_alt)} image(s) have no alt text.')
    elif images:
        add('ok', f'All {len(images)} images have alt text')
    hotlinked = [attrs.get('src', '') for attrs in images if re.match(r'(?:https?:)?//', attrs.get('src', ''))]
    if hotlinked:
        add('warn', f'{len(hotlinked)} image(s) load from other websites ({hotlinked[0][:80]}). '
                    "They can break or be unlicensed unless they're the owner's own.")

    dead, missing = 0, set()
    for name, attrs in tags:
        if name != 'a':
            continue
        href = attrs.get('href')
        if href is None or href.strip() in ('', '#') or href.strip().lower().startswith('javascript:'):
            dead += 1
        elif href.startswith('#') and href[1:] not in scan.ids and href.lower() != '#top':
            missing.add(href)
    if dead:
        add('fail', f'{dead} link(s) go nowhere (empty, "#" or javascript: href).')
    if missing:
        sample = ', '.join(sorted(missing)[:5])
        add('fail', f'In-page links point at ids that do not exist: {sample}.')
    if not dead and not missing:
        add('ok', 'Every link has a destination')

    for name, attrs in tags:
        if name == 'form' and attrs.get('action', '').strip() in ('', '#'):
            add('warn', "A form has no working destination (action). Use the owner's form endpoint or mailto:/tel: links.")
            break

    external_js = [attrs.get('src') for name, attrs in tags if name == 'script' and attrs.get('src')]
    if external_js:
        add('warn', f'Loads outside JavaScript ({external_js[0][:80]}). The site should be self-contained.')

    text = ' '.join(' '.join(scan.text).split())
    if re.search(r'lorem ipsum|dolor sit amet', lower):
        add('fail', 'Contains lorem ipsum placeholder text.')
    claims = sorted({m.group(0) for m in RISKY_CLAIMS.finditer(text)})
    if claims:
        add('warn', 'Check these claims are true and provable, or remove them: ' + ', '.join(f'"{c}"' for c in claims[:5]))
    if REVIEW_HINTS.search(text):
        add('warn', 'Mentions reviews, ratings or testimonials. Only real ones supplied by the owner are allowed.')

    css = ' '.join(scan.css)
    if ':focus' not in css:
        add('warn', 'No :focus or :focus-visible styles, so keyboard users can\'t see where they are.')
    if re.search(r'animation|transition', css) and 'prefers-reduced-motion' not in css:
        add('warn', 'Has animation but no prefers-reduced-motion rule to turn it off.')

    size_kb = len(html.encode()) / 1024
    if size_kb > 200:
        add('warn', f'The page is {size_kb:.0f} KB. Aim for under about 120 KB.')
    else:
        add('ok', f'Lightweight page ({size_kb:.0f} KB)')

    placeholders = owner_placeholders(html)
    if placeholders:
        add('info', f'{len(placeholders)} [OWNER: …] placeholder(s) for you to fill in before publishing')
    return checks


def owner_placeholders(html: str) -> list[str]:
    seen = []
    for match in OWNER_RE.finditer(html or ''):
        if match.group(0) not in seen:
            seen.append(match.group(0))
    return seen


# --------------------------------------------------------------------------
# Packaging (the publisher's job: ready to deploy, never deployed)
# --------------------------------------------------------------------------

def review_markdown(review: dict) -> str:
    if not review:
        return '# QA review\n\nNo review yet.\n'
    out = [f"# QA review (round {review.get('round', '?')}, version {review.get('version', '?')})",
           f"**Verdict: {review.get('verdict')}**" + (f"  ·  score {review['score']}/100" if review.get('score') is not None else '')]
    if review.get('summary'):
        out.append(review['summary'])
    for key, heading in (('must_fix', 'Must fix'), ('nice_to_have', 'Nice to have'),
                         ('owner_must_supply', 'Owner must supply before publishing')):
        items = review.get(key) or []
        if items:
            out.append(f'## {heading}')
            out.extend(f'{n}. {item}' for n, item in enumerate(items, 1))
    checks = review.get('checks') or []
    if checks:
        out.append('## Automatic checks')
        out.extend(f"- {c['level'].upper()}: {c['text']}" for c in checks)
    return '\n\n'.join(out) + '\n'


def readme_markdown(name: str, html: str, review: dict | None) -> str:
    placeholders = owner_placeholders(html)
    verdict = (review or {}).get('verdict')
    lines = [f'# {name}', '',
             'Built by the Remix Studio AI website team. Nothing has been published.', '',
             '## Before you publish']
    if verdict != 'PASS':
        lines.append('- QA has not passed this version yet. Read `review.md` and fix the open issues first.')
    if placeholders:
        lines.append('- Replace every placeholder in `index.html` with real information:')
        lines.extend(f'  - `{p}`' for p in placeholders)
    lines += ['- Open `index.html` on your phone and your computer and click every button.',
              '- Only publish facts, prices and reviews that are real.', '',
              '## Publish it (all have free plans)',
              '- **Netlify Drop:** go to app.netlify.com/drop and drag this folder onto the page.',
              '- **GitHub Pages:** put the files in a repository, then Settings > Pages > Deploy from branch.',
              '- **Cloudflare Pages:** create a project and upload this folder.', '',
              'When you have a domain, add a `sitemap.xml` that lists your URL and submit it in Google Search Console.', '',
              '## Files',
              '- `index.html`: the website (one file, no build step)',
              '- `brief.md`: the strategist\'s plan',
              '- `review.md`: the latest QA report',
              '- `promo-shorts.md`: promo YouTube Shorts scripts (if you asked for them)',
              '- `robots.txt`: lets search engines index the site']
    return '\n'.join(lines) + '\n'


def package_zip(name, html, brief, review, shorts=None) -> bytes:
    buffer = io.BytesIO()
    stamp = datetime.now(timezone.utc).timetuple()[:6]
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        def put(path, text):
            info = zipfile.ZipInfo(path, date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, text)
        put('index.html', html)
        put('brief.md', brief or '')
        put('review.md', review_markdown(review))
        put('README.md', readme_markdown(name, html, review))
        put('robots.txt', 'User-agent: *\nAllow: /\n')
        if shorts:
            put('promo-shorts.md', shorts)
    return buffer.getvalue()
