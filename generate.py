#!/usr/bin/env python3
"""US local news site generator.
Flow: Google News RSS -> Gemini (Google Search grounded, sourced brief) -> Pixabay image -> static HTML in ./site
"""
import os, re, json, html, hashlib, time, datetime
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote
import requests

# ================= CONFIG =================
SITE_NAME = "Luvoxi News"
SITE_URL = "https://luvoxi.online"
CONTACT_EMAIL = "contact@luvoxi.online"
CITIES = ["New York", "Los Angeles", "Chicago", "Houston", "Phoenix",
          "Philadelphia", "San Antonio", "Dallas", "Austin", "Miami"]
MAX_POSTS_PER_RUN = 3
REMOVE_THIN_OLD_POSTS = True   # purani chhoti posts hata deta hai


def pick_model():
    """Gemini ka chalta hua model apne aap dhoondhta hai."""
    pref = os.getenv("GEMINI_MODEL")
    if pref:
        return pref
    names = []
    try:
        r = requests.get("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
                         headers={"x-goog-api-key": os.getenv("GEMINI_API_KEY", "")}, timeout=30)
        names = [m["name"].split("/")[-1] for m in r.json().get("models", [])
                 if "generateContent" in m.get("supportedGenerationMethods", [])]
    except Exception as e:
        print("model list fail:", e)
    for alias in ("gemini-flash-lite-latest", "gemini-flash-latest"):
        if alias in names:
            return alias
    bad = ("image", "tts", "live", "audio", "embed", "robotics", "computer", "gemma", "imagen", "veo", "learnlm")
    ok = [n for n in names if n.startswith("gemini") and not any(b in n for b in bad)]
    for key in ("flash-lite", "flash"):
        for n in sorted(ok, reverse=True):
            if key in n:
                return n
    return "gemini-flash-latest"


MODEL = pick_model()
print("Using model:", MODEL)

# Adsterra code yahan paste karna (baad me)
AD_HEADER = ""
AD_INARTICLE = ""
AD_FOOTER = ""
AD_SIDEBAR = ""

# Sensitive topics skip
BLOCK_WORDS = ["kill", "shoot", "murder", "dead", "death", "died", "arrest", "crash",
               "stabb", "rape", "assault", "suspect", "victim", "abuse", "suicide",
               "lawsuit", "sued", "charged", "police", "remembered", "obituary",
               "funeral", "memorial", "passed away", "threat", "lockdown", "gunman",
               "overdose", "missing", "fire", "explosion", "injur", "hospital"]
# ==========================================

GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
PIXABAY_KEY = os.getenv("PIXABAY_API_KEY", "")
ROOT = Path(__file__).parent
SITE = ROOT / "site"
DATA = ROOT / "data" / "posts.json"
UA = {"User-Agent": "Mozilla/5.0"}


def esc(s):
    return html.escape(s or "", quote=True)


def strip_tags(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60]


# ---------------- 1. NEWS FETCH ----------------
def fetch_news(city):
    url = (f"https://news.google.com/rss/search?q={quote(city)}+when:1d"
           "&hl=en-US&gl=US&ceid=US:en")
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    out = []
    for it in ET.fromstring(r.content).iter("item"):
        title = it.findtext("title") or ""
        src = it.findtext("source") or ""
        if " - " in title and not src:
            title, src = title.rsplit(" - ", 1)
        elif src and title.endswith(" - " + src):
            title = title[: -len(src) - 3]
        out.append({"title": title, "source": src, "link": it.findtext("link") or "",
                    "desc": strip_tags(it.findtext("description"))})
    return out


def is_blocked(text):
    t = text.lower()
    return any(w in t for w in BLOCK_WORDS)


# ---------------- 2. AI WRITER ----------------
def gemini(body, tries=4):
    r = None
    for attempt in range(tries):
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent",
            headers={"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"},
            json=body, timeout=120)
        if r.status_code in (429, 500, 503):
            print("gemini busy, retry", attempt + 1, r.status_code)
            time.sleep(10 * (attempt + 1))
            continue
        break
    r.raise_for_status()
    return r.json()


def text_of(resp):
    parts = resp["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts)


def parse_json(txt):
    a, b = txt.find("{"), txt.rfind("}")
    return json.loads(txt[a:b + 1])


def write_grounded(item, city):
    """Google Search ke saath research karke lamba, sourced article likhta hai."""
    prompt = f"""You are a careful local news editor for a US news site.
Use Google Search to find current, reliable reporting about the story below, then write an
original, well-structured news brief (350-500 words) in your own words.

City: {city}
Headline: {item['title']}
Source: {item['source']}
Snippet: {item['desc']}

STRICT RULES:
- Include ONLY facts that are supported by the search results.
- Do NOT invent names, numbers, quotes, dates or details.
- Never copy sentences from sources. No direct quotes longer than 5 words.
- Neutral tone, no clickbait, no speculation. If something is unconfirmed, say reports indicate it.
- If you cannot find solid reporting, return {{"skip": true}}.

Return ONLY one JSON object (no markdown fences) with these keys:
"title": rewritten headline (max 90 chars),
"summary": one-sentence meta description (max 150 chars),
"bullets": list of 3-4 short key points (each under 20 words),
"sections": list of 3-4 objects {{"heading": "...", "text": "..."}} where text is 80-140 words
  (headings such as What happened, What we know, Background, What's next),
"image_query": 2-3 generic English words for a stock photo (no people names)."""
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "tools": [{"google_search": {}}],
            "generationConfig": {"temperature": 0.3}}
    resp = gemini(body)
    data = parse_json(text_of(resp))
    if data.get("skip"):
        return None
    secs = [s for s in data["sections"] if s.get("text")]
    assert data["title"] and secs
    chunks = resp["candidates"][0].get("groundingMetadata", {}).get("groundingChunks", [])
    sources = []
    for c in chunks:
        w = c.get("web") or {}
        if w.get("uri") and w["uri"] not in [s["url"] for s in sources]:
            sources.append({"title": w.get("title") or "Source", "url": w["uri"]})
    data["sections"] = secs
    data["paragraphs"] = [s["text"] for s in secs]
    data["sources"] = sources[:4]
    data["v"] = 2
    return data


def write_short(item, city):
    """Backup: sirf headline+snippet se chhota brief (jab search wali call fail ho)."""
    prompt = f"""You are a careful local news editor for a US news site.
Write a short original news brief (150-250 words, 3-4 paragraphs) about {city}
using ONLY the facts present in the headline and snippet below.

STRICT RULES:
- Do NOT invent names, numbers, quotes, dates or details that are not given.
- If details are missing, stay general. Never speculate.
- Use your own words. Do not copy the snippet.
- Neutral tone. No clickbait.

Headline: {item['title']}
Source: {item['source']}
Snippet: {item['desc']}

Return ONLY JSON with these keys:
"title": rewritten headline (max 90 chars),
"summary": one-sentence meta description (max 150 chars),
"paragraphs": list of 3-4 paragraph strings (plain text),
"image_query": 2-3 generic English words for a stock photo (no people names)."""
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.4}}
    data = parse_json(text_of(gemini(body)))
    assert data["title"] and data["paragraphs"]
    data["sections"] = [{"heading": "", "text": t} for t in data["paragraphs"]]
    data["sources"] = []
    data["v"] = 1
    return data


def write_article(item, city):
    try:
        return write_grounded(item, city)  # None matlab skip
    except Exception as e:
        print("grounded failed, short brief use ho raha hai:", str(e)[:150])
    return write_short(item, city)


# ---------------- 3. IMAGE ----------------
def get_image(query, slug, fallback=""):
    if not PIXABAY_KEY:
        print("PIXABAY_API_KEY missing")
        return ""
    for q in [x for x in (query, fallback) if x]:
        try:
            r = requests.get("https://pixabay.com/api/", timeout=30, params={
                "key": PIXABAY_KEY.strip(), "q": q, "image_type": "photo",
                "orientation": "horizontal", "safesearch": "true", "per_page": 5})
            try:
                hits = r.json().get("hits", [])
            except ValueError:
                print("pixabay error:", r.status_code, r.text[:150])
                return ""
            if not hits:
                continue
            img = requests.get(hits[0]["webformatURL"], timeout=30).content
            (SITE / "img").mkdir(parents=True, exist_ok=True)
            (SITE / "img" / f"{slug}.jpg").write_bytes(img)
            return f"/img/{slug}.jpg"
        except Exception as e:
            print("image fail:", e)
            return ""
    return ""


# ---------------- 4. HTML BUILD ----------------
CSS = """
:root{--bg:#f3f4f6;--card:#fff;--ink:#101828;--mute:#667085;--acc:#e03a2f;--nav:#0b1a2b;--line:#e4e7ec;--r:10px}
@media(prefers-color-scheme:dark){:root{--bg:#0a1018;--card:#121b26;--ink:#eef2f6;--mute:#9aa7b5;--acc:#ff6a5c;--nav:#060b11;--line:#223040}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;min-height:100vh;display:flex;flex-direction:column;background:var(--bg);color:var(--ink);font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
a{color:inherit;text-decoration:none}
img{max-width:100%}
.wrap{width:100%;max-width:1120px;margin:0 auto;padding:0 14px}
.grow{flex:1 0 auto}
header.top{position:sticky;top:0;z-index:30;background:var(--nav);color:#fff;box-shadow:0 2px 12px #0004}
.bar{display:flex;align-items:center;justify-content:space-between;padding-top:12px}
.brand{font:800 25px/1 Georgia,serif;letter-spacing:-.5px;border-left:5px solid var(--acc);padding-left:10px}
.date{font-size:13px;color:#9fb1c3;display:none}
@media(min-width:700px){.date{display:block}}
header nav{display:flex;gap:8px;overflow-x:auto;padding:10px 0 12px;scrollbar-width:none}
header nav::-webkit-scrollbar{display:none}
header nav a{flex:none;padding:8px 14px;border-radius:999px;background:#ffffff14;font-size:14px;color:#e6edf3}
header nav a:hover,header nav a:focus-visible{background:var(--acc);color:#fff}
.ad{margin:14px 0;text-align:center;overflow:hidden;max-width:100%}
.ad:empty{display:none}
.layout{display:grid;gap:22px;grid-template-columns:minmax(0,1fr);margin-bottom:30px;align-items:start}
@media(min-width:960px){.layout{grid-template-columns:minmax(0,1fr) 320px}}
.hero{position:relative;display:block;border-radius:var(--r);overflow:hidden;min-height:240px;aspect-ratio:16/10;background:linear-gradient(135deg,var(--nav),#17324f);margin-top:16px}
.hero img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.hero::after{content:"";position:absolute;inset:0;background:linear-gradient(to top,#000d 5%,#0000 65%)}
.hero .t{position:absolute;z-index:2;left:0;right:0;bottom:0;padding:16px;color:#fff}
.hero h2{font:800 24px/1.18 Georgia,serif;margin:8px 0 6px}
.hero .meta{color:#d0d9e2}
@media(min-width:700px){.hero{aspect-ratio:16/8}.hero h2{font-size:34px}.hero .t{padding:26px}}
.pill{background:var(--acc);color:#fff!important;padding:3px 9px;border-radius:4px;font-size:12px;font-weight:700}
.sec{font:800 20px Georgia,serif;margin:24px 0 12px;padding-left:10px;border-left:4px solid var(--acc)}
.feed{display:grid;gap:12px}
.item{display:grid;grid-template-columns:112px 1fr;gap:12px;background:var(--card);border-radius:var(--r);padding:10px;transition:box-shadow .15s}
.item:hover{box-shadow:0 4px 18px #0002}
.th{border-radius:8px;overflow:hidden;aspect-ratio:1/1;background:linear-gradient(135deg,var(--line),var(--bg))}
.th img,.ph{width:100%;height:100%;object-fit:cover;display:block}
.item h3{font:700 17px/1.28 Georgia,serif;margin:3px 0 4px}
.sum{display:none;margin:0 0 6px;color:var(--mute);font-size:15px}
@media(min-width:700px){.item{grid-template-columns:230px 1fr;padding:12px}.th{aspect-ratio:16/10}.item h3{font-size:21px}.sum{display:block}}
.tag{color:var(--acc);font-weight:700;font-size:13px}
.meta{color:var(--mute);font-size:13px}
aside .box{background:var(--card);border-radius:var(--r);padding:14px 16px;margin-bottom:16px}
aside h3{font:800 17px Georgia,serif;margin:0 0 8px}
.mini{display:block;padding:10px 0;border-top:1px solid var(--line);font-weight:600;font-size:15px;line-height:1.35}
.mini .tag{display:block;margin-bottom:2px}
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chips a{padding:6px 12px;border-radius:999px;background:var(--bg);font-size:14px}
article.post{background:var(--card);border-radius:var(--r);padding:18px;margin-top:16px}
article.post h1{font:800 28px/1.18 Georgia,serif;margin:8px 0 6px}
@media(min-width:700px){article.post{padding:30px}article.post h1{font-size:38px}}
article.post h2{font:800 22px/1.25 Georgia,serif;margin:26px 0 8px}
article.post .cover{width:100%;aspect-ratio:16/9;object-fit:cover;border-radius:8px;margin:14px 0 4px;display:block}
.cap{font-size:12px;color:var(--mute);margin:0 0 14px}
article.post p{font:18px/1.75 Georgia,serif;margin:0 0 16px}
.keys{background:var(--bg);border-left:4px solid var(--acc);border-radius:8px;padding:12px 16px;margin:16px 0}
.keys ul{margin:8px 0 0;padding-left:20px}
.keys li{margin:4px 0;font-size:16px}
.share{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:14px 0;font-size:14px;color:var(--mute)}
.share a{padding:8px 14px;border-radius:999px;background:var(--bg);color:var(--ink);font-weight:600}
.src{border-top:1px solid var(--line);margin-top:22px;padding-top:12px;color:var(--mute);font-size:14px}
.src ul{margin:6px 0;padding-left:20px}
.src a{color:var(--acc);text-decoration:underline}
footer{flex-shrink:0;background:var(--nav);color:#9fb1c3;padding:28px 0 36px;font-size:14px}
.foot{display:grid;gap:18px}
@media(min-width:700px){.foot{grid-template-columns:2fr 1fr 1fr}}
footer .brand{color:#fff;display:inline-block;margin-bottom:8px}
footer p{margin:0;max-width:46ch}
footer h4{margin:0 0 8px;color:#fff;font-size:14px}
footer .links a{display:block;padding:4px 0;color:#e6edf3}
footer .copy{grid-column:1/-1;border-top:1px solid #ffffff22;padding-top:14px}
a:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
@media(prefers-reduced-motion:reduce){*{transition:none!important}}
"""

LATEST = []


def city_slug(c):
    return slugify(c)


def sidebar():
    items = "".join(
        f'<a class="mini" href="/news/{p["slug"]}/"><span class="tag">{esc(p["city"])}</span>{esc(p["title"])}</a>'
        for p in LATEST[:6])
    cities = "".join(f'<a href="/city/{city_slug(c)}/">{esc(c)}</a>' for c in CITIES)
    return (f'<div class="box"><h3>Latest</h3>{items or "<p class=meta>Coming soon.</p>"}</div>'
            f'<div class="ad">{AD_SIDEBAR}</div>'
            f'<div class="box"><h3>Cities</h3><div class="chips">{cities}</div></div>')


def page(title, desc, body, path, image="", head_extra=""):
    nav = '<a href="/">Home</a>' + "".join(
        f'<a href="/city/{city_slug(c)}/">{esc(c)}</a>' for c in CITIES)
    og_img = f'<meta property="og:image" content="{SITE_URL}{image}">' if image else ""
    today = datetime.date.today().strftime("%A, %B %d, %Y")
    fav = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E"
           "%3Crect width='100' height='100' rx='18' fill='%23e03a2f'/%3E%3Ctext x='50' y='72' "
           "font-size='70' text-anchor='middle' fill='white' font-family='Georgia' "
           f"font-weight='bold'%3E{esc(SITE_NAME[0])}%3C/text%3E%3C/svg%3E")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title><meta name="description" content="{esc(desc)}">
<link rel="canonical" href="{SITE_URL}{path}"><link rel="stylesheet" href="/style.css">
<link rel="icon" href="{fav}">
<meta property="og:title" content="{esc(title)}"><meta property="og:description" content="{esc(desc)}">
<meta property="og:type" content="website"><meta property="og:url" content="{SITE_URL}{path}">{og_img}
<meta name="twitter:card" content="summary_large_image">{head_extra}
</head><body>
<header class="top"><div class="wrap bar"><a class="brand" href="/">{esc(SITE_NAME)}</a><span class="date">{today}</span></div>
<div class="wrap"><nav>{nav}</nav></div></header>
<div class="wrap grow"><div class="ad">{AD_HEADER}</div>
<main class="layout"><div class="main">{body}</div><aside>{sidebar()}</aside></main>
<div class="ad">{AD_FOOTER}</div></div>
<footer><div class="wrap foot">
<div><a class="brand" href="/">{esc(SITE_NAME)}</a><p>Short, sourced local news briefs from major US cities. Written with AI assistance and linked to the original reporting.</p></div>
<div class="links"><h4>Site</h4><a href="/about/">About</a><a href="/editorial/">Editorial policy</a><a href="/contact/">Contact</a></div>
<div class="links"><h4>Legal</h4><a href="/privacy/">Privacy policy</a><a href="/disclaimer/">Disclaimer</a></div>
<p class="copy">&copy; {datetime.date.today().year} {esc(SITE_NAME)}. All rights reserved.</p>
</div></footer></body></html>"""


def write(path, content):
    p = SITE / path.strip("/") / "index.html" if not path.endswith((".xml", ".txt", ".css")) \
        else SITE / path.strip("/")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def item(p):
    img = f'<img src="{p["image"]}" alt="" loading="lazy">' if p["image"] else '<div class="ph"></div>'
    return (f'<a class="item" href="/news/{p["slug"]}/"><div class="th">{img}</div><div class="tx">'
            f'<span class="tag">{esc(p["city"])}</span><h3>{esc(p["title"])}</h3>'
            f'<p class="sum">{esc(p["summary"])}</p><span class="meta">{p["date"]}</span></div></a>')


def hero(p):
    img = f'<img src="{p["image"]}" alt="{esc(p["title"])}">' if p["image"] else ""
    return (f'<a class="hero" href="/news/{p["slug"]}/">{img}<div class="t">'
            f'<span class="tag pill">{esc(p["city"])}</span><h2>{esc(p["title"])}</h2>'
            f'<span class="meta">{p["date"]}</span></div></a>')


def article_html(p, url, related):
    cover = ""
    if p["image"]:
        cover = (f'<img class="cover" src="{p["image"]}" alt="{esc(p["title"])}">'
                 '<p class="cap">Representative stock image via Pixabay, not a photo of the event.</p>')
    words = sum(len(x.split()) for x in p["paragraphs"])
    mins = max(1, round(words / 200))
    share = ('<div class="share"><span>Share</span>'
             f'<a href="https://wa.me/?text={quote(p["title"] + " " + url)}" target="_blank" rel="noopener">WhatsApp</a>'
             f'<a href="https://twitter.com/intent/tweet?url={quote(url)}&text={quote(p["title"])}" target="_blank" rel="noopener">X</a>'
             f'<a href="https://www.facebook.com/sharer/sharer.php?u={quote(url)}" target="_blank" rel="noopener">Facebook</a></div>')
    keys = ""
    if p.get("bullets"):
        keys = ('<div class="keys"><strong>Key points</strong><ul>'
                + "".join(f"<li>{esc(x)}</li>" for x in p["bullets"]) + "</ul></div>")
    secs = p.get("sections") or [{"heading": "", "text": t} for t in p["paragraphs"]]
    blocks = [(f'<h2>{esc(s["heading"])}</h2>' if s.get("heading") else "") + f'<p>{esc(s["text"])}</p>'
              for s in secs]
    blocks.insert(len(blocks) // 2 or 1, f'<div class="ad">{AD_INARTICLE}</div>')
    links = "".join(
        f'<li><a href="{esc(s["url"])}" rel="nofollow noopener" target="_blank">{esc(s["title"])}</a></li>'
        for s in (p.get("sources") or []))
    src = ('<div class="src"><strong>Sources</strong><ul>'
           f'<li><a href="{esc(p["link"])}" rel="nofollow noopener" target="_blank">Original report: {esc(p["source"])}</a></li>'
           f'{links}</ul>This brief was written with AI assistance from public reporting. '
           'See our <a href="/editorial/">editorial policy</a>.</div>')
    rel = (f'<h2 class="sec">More from {esc(p["city"])}</h2><div class="feed">'
           + "".join(item(x) for x in related) + "</div>") if related else ""
    return (f'<article class="post"><span class="tag pill">{esc(p["city"])}</span><h1>{esc(p["title"])}</h1>'
            f'<span class="meta">{p["date"]} &middot; {mins} min read</span>{cover}{share}{keys}'
            f'{"".join(blocks)}{src}</article>{rel}')


def build_site(posts):
    global LATEST
    SITE.mkdir(exist_ok=True)
    (SITE / "style.css").write_text(CSS, encoding="utf-8")
    posts = sorted(posts, key=lambda p: p["ts"], reverse=True)
    LATEST = posts
    urls = ["/", "/about/", "/editorial/", "/contact/", "/privacy/", "/disclaimer/"]

    # homepage
    body = '<p class="meta">No posts yet.</p>'
    if posts:
        body = (hero(posts[0]) + '<h2 class="sec">Latest news</h2><div class="feed">'
                + "".join(item(p) for p in posts[1:30]) + "</div>")
    write("/", page(f"{SITE_NAME} - Local news from across the US",
                    "Short, sourced local news briefs from major US cities.", body, "/",
                    posts[0]["image"] if posts else ""))

    # city pages
    for c in CITIES:
        items = [p for p in posts if p["city"] == c]
        b = (f'<h1 class="sec">{esc(c)} news</h1>' + ('<div class="feed">' + "".join(item(p) for p in items)
             + "</div>" if items else '<p class="meta">No posts yet.</p>'))
        path = f"/city/{city_slug(c)}/"
        write(path, page(f"{c} news | {SITE_NAME}", f"Latest {c} local news briefs.", b, path))
        urls.append(path)

    # articles
    for p in posts:
        url = SITE_URL + f"/news/{p['slug']}/"
        related = [x for x in posts if x["city"] == p["city"] and x["slug"] != p["slug"]][:3]
        ld = json.dumps({"@context": "https://schema.org", "@type": "NewsArticle",
                         "headline": p["title"], "description": p["summary"],
                         "datePublished": datetime.datetime.fromtimestamp(p["ts"]).strftime("%Y-%m-%d"),
                         "mainEntityOfPage": url, "publisher": {"@type": "Organization", "name": SITE_NAME},
                         **({"image": SITE_URL + p["image"]} if p["image"] else {})})
        path = f"/news/{p['slug']}/"
        write(path, page(f"{p['title']} | {SITE_NAME}", p["summary"], article_html(p, url, related),
                         path, p["image"], f'<script type="application/ld+json">{ld}</script>'))
        urls.append(path)

    # static pages
    n = esc(SITE_NAME)
    cities = ", ".join(CITIES)
    today = datetime.date.today().strftime("%B %d, %Y")
    static = {
        "/about/": ("About", f"<h1>About {n}</h1>"
            f"<p>{n} is an independent news site that publishes short briefs about local news in major US cities: {cities}.</p>"
            "<p>We look at public headlines and news reports, research each story, and write an original summary in plain language. "
            "Every story lists the sources it is based on, so you can read the full reporting from the original publishers.</p>"
            "<p>Our goal is to give readers a quick, clear view of what is happening in each city. We do not replace local newsrooms, "
            "and we encourage readers to visit the original sources linked in every story.</p>"),
        "/editorial/": ("Editorial Policy", "<h1>Editorial Policy</h1>"
            "<h2>How stories are made</h2>"
            "<p>Stories start from publicly available headlines. We use AI-assisted tools to research the topic and draft an original summary. "
            "We aim to include only facts that are supported by published reporting, and we link to our sources at the end of each story.</p>"
            "<h2>Topics we avoid</h2>"
            "<p>To reduce the risk of errors and harm, we avoid stories about crimes, accidents, deaths, missing people, minors and ongoing emergencies.</p>"
            "<h2>Images</h2>"
            "<p>Images are representative stock photos from Pixabay and are not photos of the events described.</p>"
            f"<h2>Corrections</h2><p>If you see a mistake, email <a href='mailto:{CONTACT_EMAIL}'>{CONTACT_EMAIL}</a> with the story link. "
            "We will review it and correct or remove the story where needed.</p>"),
        "/contact/": ("Contact", "<h1>Contact</h1>"
            f"<p>For corrections, takedown requests, advertising questions or general feedback, email <a href='mailto:{CONTACT_EMAIL}'>{CONTACT_EMAIL}</a>.</p>"
            "<p>Please include the link to the story you are writing about so we can respond faster.</p>"),
        "/privacy/": ("Privacy Policy", "<h1>Privacy Policy</h1>"
            f"<p>Last updated: {today}</p>"
            f"<p>{n} does not ask you to create an account and does not collect personal information directly.</p>"
            "<h2>Cookies and advertising</h2>"
            "<p>We show ads from third-party advertising networks. These partners may use cookies or similar technologies to show ads and measure performance. "
            "You can block or delete cookies in your browser settings.</p>"
            "<h2>Server logs</h2>"
            "<p>Our hosting provider may keep standard server logs such as IP address, browser type and pages visited, for security and performance.</p>"
            "<h2>External links</h2>"
            "<p>Our stories link to other websites. We are not responsible for the privacy practices of those sites.</p>"
            f"<h2>Contact</h2><p>Questions about this policy: <a href='mailto:{CONTACT_EMAIL}'>{CONTACT_EMAIL}</a>.</p>"),
        "/disclaimer/": ("Disclaimer", "<h1>Disclaimer</h1>"
            "<p>The briefs on this site are written with AI assistance from publicly available reporting and may contain errors or omissions. "
            "They are provided for general information only and are not legal, medical, financial or professional advice.</p>"
            "<p>Always check the original sources linked in each story before relying on the information. "
            "Trademarks and names belong to their respective owners.</p>"),
    }
    for path, (t, b) in static.items():
        write(path, page(f"{t} | {SITE_NAME}", t, f'<article class="post">{b}</article>', path))

    sm = "".join(f"<url><loc>{SITE_URL}{u}</loc></url>" for u in urls)
    write("/sitemap.xml", f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{sm}</urlset>')
    write("/robots.txt", f"User-agent: *\nAllow: /\nSitemap: {SITE_URL}/sitemap.xml\n")


# ---------------- MAIN ----------------
def main():
    DATA.parent.mkdir(exist_ok=True)
    posts = json.loads(DATA.read_text()) if DATA.exists() else []

    # purani sensitive posts hata do
    before = len(posts)
    posts = [p for p in posts if not is_blocked(p["title"] + " " + " ".join(p["paragraphs"]))]
    if len(posts) != before:
        print("removed sensitive old posts:", before - len(posts))

    # purani chhoti posts (jinme version "v" nahi hai) hata do
    if REMOVE_THIN_OLD_POSTS:
        before = len(posts)
        posts = [p for p in posts if p.get("v")]
        if len(posts) != before:
            print("removed old thin posts:", before - len(posts))

    # bina image wali posts me image bharo (max 5 per run)
    filled = 0
    for p in posts:
        if not p.get("image") and filled < 5:
            p["image"] = get_image(p["city"] + " city", p["slug"])
            filled += 1

    seen = {p["link"] for p in posts} | {p["title"].lower() for p in posts}
    made = 0

    if GEMINI_KEY:
        start = datetime.date.today().toordinal() * 3 + datetime.datetime.now(datetime.timezone.utc).hour // 8
        order = [CITIES[(start + i) % len(CITIES)] for i in range(len(CITIES))]
        for city in order:
            if made >= MAX_POSTS_PER_RUN:
                break
            try:
                items = fetch_news(city)
            except Exception as e:
                print("rss fail", city, e)
                continue
            tries = 0
            for it in items[:15]:
                if not it["link"] or it["link"] in seen:
                    continue
                if is_blocked(it["title"] + " " + it["desc"]):
                    continue
                tries += 1
                if tries > 3:
                    break
                try:
                    art = write_article(it, city)
                except Exception as e:
                    print("article fail:", e)
                    break
                if not art or is_blocked(art["title"] + " " + " ".join(art["paragraphs"])):
                    print("skipped:", it["title"][:60])
                    seen.add(it["link"])
                    continue
                slug = slugify(art["title"]) + "-" + hashlib.md5(it["link"].encode()).hexdigest()[:5]
                posts.append({
                    "slug": slug, "title": art["title"], "summary": art["summary"][:160],
                    "bullets": art.get("bullets", []), "sections": art["sections"],
                    "paragraphs": art["paragraphs"], "sources": art.get("sources", []),
                    "city": city, "source": it["source"], "link": it["link"], "v": art["v"],
                    "date": datetime.date.today().strftime("%b %d, %Y"), "ts": time.time(),
                    "image": get_image(art.get("image_query") or city + " city", slug, city + " skyline")})
                seen.add(it["link"])
                made += 1
                print("posted:", art["title"], "| version", art["v"])
                time.sleep(6)
                break
    else:
        print("GEMINI_API_KEY not set - sirf site rebuild ho rahi hai")

    DATA.write_text(json.dumps(posts, ensure_ascii=False, indent=1))
    build_site(posts)
    print(f"done. new posts: {made}, total: {len(posts)}")


if __name__ == "__main__":
    main()
