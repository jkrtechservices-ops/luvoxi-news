#!/usr/bin/env python3
"""US local news site generator.
Flow: Google News RSS -> Gemini (short original brief) -> Pixabay image -> static HTML in ./site
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
               "lawsuit", "sued", "charged", "police"]
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
def write_article(item, city):
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
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent",
        headers={"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"},
        json=body, timeout=90)
    r.raise_for_status()
    txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    data = json.loads(txt)
    assert data["title"] and data["paragraphs"]
    return data


# ---------------- 3. IMAGE ----------------
def get_image(query, slug):
    if not PIXABAY_KEY:
        return ""
    try:
        r = requests.get("https://pixabay.com/api/", timeout=30, params={
            "key": PIXABAY_KEY, "q": query, "image_type": "photo",
            "orientation": "horizontal", "safesearch": "true", "per_page": 5})
        hits = r.json().get("hits", [])
        if not hits:
            return ""
        img = requests.get(hits[0]["webformatURL"], timeout=30).content
        (SITE / "img").mkdir(parents=True, exist_ok=True)
        (SITE / "img" / f"{slug}.jpg").write_bytes(img)
        return f"/img/{slug}.jpg"
    except Exception as e:
        print("image fail:", e)
        return ""


# ---------------- 4. HTML BUILD ----------------
CSS = """
:root{--bg:#f3f4f6;--card:#fff;--ink:#101828;--mute:#667085;--acc:#e03a2f;--nav:#0b1a2b;--line:#e4e7ec;--r:10px}
@media(prefers-color-scheme:dark){:root{--bg:#0a1018;--card:#121b26;--ink:#eef2f6;--mute:#9aa7b5;--acc:#ff6a5c;--nav:#060b11;--line:#223040}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
a{color:inherit;text-decoration:none}
img{max-width:100%}
.wrap{max-width:1120px;margin:0 auto;padding:0 14px}
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
.layout{display:grid;gap:22px;grid-template-columns:minmax(0,1fr);margin-bottom:30px}
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
article.post .cover{width:100%;aspect-ratio:16/9;object-fit:cover;border-radius:8px;margin:14px 0}
article.post p{font:18px/1.75 Georgia,serif;margin:0 0 16px}
.share{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:14px 0;font-size:14px;color:var(--mute)}
.share a{padding:8px 14px;border-radius:999px;background:var(--bg);color:var(--ink);font-weight:600}
.src{border-top:1px solid var(--line);margin-top:18px;padding-top:12px;color:var(--mute);font-size:14px}
.src a{color:var(--acc);text-decoration:underline}
footer{background:var(--nav);color:#9fb1c3;padding:26px 0 40px;font-size:14px;text-align:center}
footer a{margin:0 10px;color:#e6edf3}
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
<div class="wrap"><div class="ad">{AD_HEADER}</div>
<main class="layout"><div class="main">{body}</div><aside>{sidebar()}</aside></main>
<div class="ad">{AD_FOOTER}</div></div>
<footer><div class="wrap"><a href="/about/">About</a><a href="/contact/">Contact</a>
<a href="/privacy/">Privacy</a><a href="/disclaimer/">Disclaimer</a>
<p>&copy; {datetime.date.today().year} {esc(SITE_NAME)}</p></div></footer></body></html>"""


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


def build_site(posts):
    global LATEST
    SITE.mkdir(exist_ok=True)
    (SITE / "style.css").write_text(CSS, encoding="utf-8")
    posts = sorted(posts, key=lambda p: p["ts"], reverse=True)
    LATEST = posts
    urls = ["/", "/about/", "/contact/", "/privacy/", "/disclaimer/"]

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
        parts = [f"<p>{esc(x)}</p>" for x in p["paragraphs"]]
        parts.insert(len(parts) // 2 or 1, f'<div class="ad">{AD_INARTICLE}</div>')
        cover = f'<img class="cover" src="{p["image"]}" alt="{esc(p["title"])}">' if p["image"] else ""
        mins = max(1, round(sum(len(x.split()) for x in p["paragraphs"]) / 200))
        share = ('<div class="share"><span>Share</span>'
                 f'<a href="https://wa.me/?text={quote(p["title"] + " " + url)}" target="_blank" rel="noopener">WhatsApp</a>'
                 f'<a href="https://twitter.com/intent/tweet?url={quote(url)}&text={quote(p["title"])}" target="_blank" rel="noopener">X</a>'
                 f'<a href="https://www.facebook.com/sharer/sharer.php?u={quote(url)}" target="_blank" rel="noopener">Facebook</a></div>')
        related = [x for x in posts if x["city"] == p["city"] and x["slug"] != p["slug"]][:3]
        rel = (f'<h2 class="sec">More from {esc(p["city"])}</h2><div class="feed">'
               + "".join(item(x) for x in related) + "</div>") if related else ""
        b = (f'<article class="post"><span class="tag pill">{esc(p["city"])}</span><h1>{esc(p["title"])}</h1>'
             f'<span class="meta">{p["date"]} &middot; {mins} min read</span>{cover}{share}{"".join(parts)}'
             f'<div class="src">Based on reporting by {esc(p["source"])}. '
             f'<a href="{esc(p["link"])}" rel="nofollow noopener" target="_blank">Read the original story</a>. '
             'This brief was written with AI assistance from public headlines.</div></article>' + rel)
        ld = json.dumps({"@context": "https://schema.org", "@type": "NewsArticle",
                         "headline": p["title"], "description": p["summary"],
                         "datePublished": datetime.datetime.fromtimestamp(p["ts"]).strftime("%Y-%m-%d"),
                         "mainEntityOfPage": url, "publisher": {"@type": "Organization", "name": SITE_NAME},
                         **({"image": SITE_URL + p["image"]} if p["image"] else {})})
        path = f"/news/{p['slug']}/"
        write(path, page(f"{p['title']} | {SITE_NAME}", p["summary"], b, path, p["image"],
                         f'<script type="application/ld+json">{ld}</script>'))
        urls.append(path)

    # static pages
    static = {
        "/about/": ("About", f"<h1>About</h1><p>{esc(SITE_NAME)} publishes short local news briefs from major US cities. Every brief links to the original reporting.</p>"),
        "/contact/": ("Contact", f"<h1>Contact</h1><p>Corrections or takedown requests: <a href='mailto:{CONTACT_EMAIL}'>{CONTACT_EMAIL}</a></p>
