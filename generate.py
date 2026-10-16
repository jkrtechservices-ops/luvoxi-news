#!/usr/bin/env python3
"""Luvoxi News generator (v3).
Trending topics (Google Trends RSS) + evergreen city guides
-> Gemini research (Google Search) -> Groq long article -> Pixabay image -> static site in ./site
"""
import os, re, sys, json, html, hashlib, time, datetime, random
import xml.etree.ElementTree as ET
from email.utils import formatdate
from pathlib import Path
from urllib.parse import quote
import requests

# ================= CONFIG =================
SITE_NAME = "Luvoxi News"
SITE_URL = "https://luvoxi.online"
CONTACT_EMAIL = "contact@luvoxi.online"
CITIES = ["New York", "Los Angeles", "Chicago", "Houston", "Phoenix",
          "Philadelphia", "San Antonio", "Dallas", "Austin", "Miami"]
TREND_PER_RUN = int(os.getenv("TREND_PER_RUN") or "1")   # har run me trending posts
GUIDE_PER_RUN = int(os.getenv("GUIDE_PER_RUN") or "1")   # har run me city guides
MIN_WORDS = {"trend": 700, "guide": 1000}
POST_VERSION = 3
GROQ_GAP = 40   # free limit (8K tokens/min) ke liye do Groq calls ke beech seconds

GUIDE_TEMPLATES = [
    ("families", "Best Neighborhoods in {city} for Families"),
    ("living", "Is {city} a Good Place to Live? Pros and Cons"),
    ("free", "Best Free Things to Do in {city}"),
    ("moving", "Moving to {city}: A Practical Checklist"),
    ("transit", "Getting Around {city}: Transit, Driving and Parking"),
    ("season", "Best Time to Visit {city}: Weather and Seasons"),
    ("pros", "Best Neighborhoods in {city} for Young Professionals"),
    ("weekend", "48 Hours in {city}: A Weekend Itinerary"),
    ("parks", "Best Parks and Outdoor Activities in {city}"),
    ("stay", "Where to Stay in {city}: Best Areas for Visitors"),
    ("daytrips", "Best Day Trips from {city}"),
    ("food", "{city} Food Guide: What the City Is Known For"),
    ("culture", "Best Museums and Cultural Attractions in {city}"),
    ("cost", "Cost of Living in {city}: What to Expect"),
]

# Adsterra code yahan paste karna (baad me)
AD_HEADER = ""
AD_INARTICLE = ""
AD_FOOTER = ""
AD_SIDEBAR = ""

# Topic chunne me ye sab skip (crime, death, politics, health, disaster)
BLOCK_PREFIX = ["kill", "shoot", "shot", "murder", "dead", "death", "died", "dies", "dying", "arrest",
                "crash", "stabb", "rape", "assault", "suspect", "victim", "abuse", "suicide", "lawsuit",
                "sued", "charged", "police", "remembered", "obituar", "funeral", "memorial",
                "passed away", "threat", "lockdown", "gunman", "overdose", "missing", "explosion",
                "injur", "hospital", "trump", "biden", "harris", "election", "senate", "congress",
                "democrat", "republican", "israel", "gaza", "ukraine", "russia", "shutdown",
                "impeach", "immigra", "abortion", "covid", "vaccine", "cancer", "diagnos",
                "hurricane", "tornado", "earthquake", "flood", "evacuat", "wildfire"]
BLOCK_EXACT = ["war", "fire", "vote", "gun", "guns"]
SEVERE_PREFIX = ["murder", "rape", "suicide", "gunman", "overdose", "killed", "shooting", "stabb"]
# ==========================================

BLOCK_RE = re.compile(r"\b(?:" + "|".join(BLOCK_PREFIX) + r")|\b(?:" + "|".join(BLOCK_EXACT) + r")\b", re.I)
SEVERE_RE = re.compile(r"\b(?:" + "|".join(SEVERE_PREFIX) + r")", re.I)

GEMINI_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GROQ_KEYS = [k.strip() for k in os.getenv("GROQ_API_KEY", "").split(",") if k.strip()]
PIXABAY_KEY = os.getenv("PIXABAY_API_KEY", "").strip()
ROOT = Path(__file__).parent
SITE = ROOT / "site"
DATA = ROOT / "data" / "posts.json"
STATE = ROOT / "data" / "state.json"
UA = {"User-Agent": "Mozilla/5.0"}


def esc(s):
    return html.escape(s or "", quote=True)


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:70]


def clean(t):
    t = re.sub(r"(\*\*|__|`)", "", str(t or ""))
    t = re.sub(r"^#+\s*", "", t, flags=re.M)
    return t.strip()


def is_blocked(text):
    return bool(BLOCK_RE.search(text or ""))


def is_severe(text):
    return bool(SEVERE_RE.search(text or ""))


def parse_json(txt):
    a, b = txt.find("{"), txt.rfind("}")
    return json.loads(txt[a:b + 1])


# ---------------- 1. TOPICS ----------------
def _loc(el):
    return el.tag.split("}")[-1]


def _traffic(s):
    m = re.match(r"\s*([\d,\.]+)\s*([KkMm]?)", s or "")
    if not m:
        return 0
    n = float(m.group(1).replace(",", ""))
    return int(n * {"": 1, "k": 1e3, "m": 1e6}[m.group(2).lower()])


def fetch_trends():
    """Google Trends (US) ke trending searches + unki related news headlines."""
    try:
        r = requests.get("https://trends.google.com/trending/rss?geo=US", headers=UA, timeout=30)
        r.raise_for_status()
        out = []
        for it in ET.fromstring(r.content).iter("item"):
            d = {"title": "", "traffic": 0, "news": []}
            for ch in it:
                t = _loc(ch)
                if t == "title":
                    d["title"] = (ch.text or "").strip()
                elif t == "approx_traffic":
                    d["traffic"] = _traffic(ch.text)
                elif t == "news_item":
                    n = {"title": "", "url": "", "source": ""}
                    for c in ch:
                        k = _loc(c)
                        if k == "news_item_title":
                            n["title"] = (c.text or "").strip()
                        elif k == "news_item_url":
                            n["url"] = (c.text or "").strip()
                        elif k == "news_item_source":
                            n["source"] = (c.text or "").strip()
                    d["news"].append(n)
            if d["title"]:
                out.append(d)
        out.sort(key=lambda x: -x["traffic"])
        return out
    except Exception as e:
        print("trends fail:", str(e)[:120])
        return []


def fetch_topic_news():
    """Backup: Trends na mile to Google News ke topic feeds."""
    out = []
    for topic in ("TECHNOLOGY", "BUSINESS", "ENTERTAINMENT", "SPORTS", "SCIENCE"):
        try:
            r = requests.get("https://news.google.com/rss/headlines/section/topic/" + topic +
                             "?hl=en-US&gl=US&ceid=US:en", headers=UA, timeout=30)
            r.raise_for_status()
            for it in list(ET.fromstring(r.content).iter("item"))[:6]:
                title = it.findtext("title") or ""
                src = it.findtext("source") or ""
                if src and title.endswith(" - " + src):
                    title = title[: -len(src) - 3]
                out.append({"title": title, "traffic": 0,
                            "news": [{"title": title, "url": it.findtext("link") or "", "source": src}]})
        except Exception as e:
            print("topic fail:", topic, str(e)[:100])
    random.shuffle(out)
    return out


def trend_candidates(seen):
    cands = fetch_trends()
    if not cands:
        print("Trends RSS nahi mila, Google News topics use ho rahe hain")
        cands = fetch_topic_news()
    for c in cands:
        key = "trend:" + slugify(c["title"])
        if key in seen:
            continue
        if is_blocked(c["title"] + " " + " ".join(n["title"] for n in c["news"])):
            continue
        hints = "\n".join(f'- {n["title"]} ({n["source"]})' for n in c["news"][:4])
        yield {"kind": "trend", "key": key, "city": "USA", "topic": c["title"], "hints": hints}


def next_guide(seen):
    for tid, text in GUIDE_TEMPLATES:
        for city in CITIES:
            key = f"guide:{city}:{tid}"
            if key not in seen:
                return {"kind": "guide", "key": key, "city": city, "topic": text.format(city=city), "hints": ""}
    return None


# ---------------- 2. AI: RESEARCH + WRITING ----------------
_G = {"model": None, "fails": 0}


def gemini_model():
    if _G["model"]:
        return _G["model"]
    pref = os.getenv("GEMINI_MODEL")
    name = pref or "gemini-flash-latest"
    if not pref:
        try:
            r = requests.get("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
                             headers={"x-goog-api-key": GEMINI_KEY}, timeout=30)
            names = [m["name"].split("/")[-1] for m in r.json().get("models", [])
                     if "generateContent" in m.get("supportedGenerationMethods", [])]
            for alias in ("gemini-flash-lite-latest", "gemini-flash-latest"):
                if alias in names:
                    name = alias
                    break
        except Exception as e:
            print("model list fail:", str(e)[:100])
    _G["model"] = name
    print("Gemini model:", name)
    return name


def gemini(body, tries=4):
    r = None
    for attempt in range(tries):
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{gemini_model()}:generateContent",
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
    return "".join(p.get("text", "") for p in resp["candidates"][0]["content"]["parts"])


_GQ = {"last": 0.0}


def groq_chat(prompt, max_tokens=5000):
    """Groq (free tier) se jawab. Limit lage to wait/retry, phir dusra model."""
    last = None
    for model in ("openai/gpt-oss-120b", "llama-3.3-70b-versatile"):
        for attempt in range(5):
            key = GROQ_KEYS[attempt % len(GROQ_KEYS)]
            gap = GROQ_GAP - (time.time() - _GQ["last"])
            if gap > 0:
                time.sleep(gap)
            body = {"model": model, "temperature": 0.7, "max_completion_tokens": max_tokens,
                    "messages": [{"role": "user", "content": prompt}]}
            if "gpt-oss" in model:
                body["reasoning_effort"] = "low"
            try:
                r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                                  headers={"Authorization": f"Bearer {key}"}, json=body, timeout=180)
            except requests.RequestException as e:
                last = e
                time.sleep(5)
                continue
            _GQ["last"] = time.time()
            if r.status_code == 400 and "reasoning_effort" in body:
                last = r.text[:150]
                continue
            if r.status_code == 429:
                last = r.text[:150]
                if "per day" in r.text.lower():
                    break
                try:
                    wait = float(r.headers.get("retry-after", 30))
                except ValueError:
                    wait = 30
                time.sleep(min(wait + 2, 90))
                continue
            if r.status_code in (413, 404):
                last = r.text[:150]
                break
            if r.status_code >= 500:
                last = r.status_code
                time.sleep(10)
                continue
            r.raise_for_status()
            txt = (r.json()["choices"][0]["message"].get("content") or "").strip()
            if txt:
                return txt
            last = "empty reply"
    raise RuntimeError(f"groq failed: {last}")


def llm(prompt, max_tokens=5000):
    if GROQ_KEYS:
        try:
            return groq_chat(prompt, max_tokens)
        except Exception as e:
            print("groq failed:", str(e)[:150])
    if GEMINI_KEY:
        resp = gemini({"contents": [{"parts": [{"text": prompt}]}],
                       "generationConfig": {"temperature": 0.7, "maxOutputTokens": max_tokens}})
        return text_of(resp)
    raise RuntimeError("koi AI key nahi mili (GROQ_API_KEY / GEMINI_API_KEY)")


def research(cand):
    """Gemini + Google Search se verified facts. None = research available nahi."""
    if not GEMINI_KEY or _G["fails"] >= 2:
        return None
    if cand["kind"] == "guide":
        what = f'a practical city guide titled "{cand["topic"]}"'
        skip = "Set skip to true only if you cannot find reliable sources."
    else:
        what = f'the trending US topic "{cand["topic"]}" (why people are searching for it right now)'
        skip = ("Set skip to true if the topic involves a death, crime, accident, missing person, minor, "
                "health claim, politics or elections, or if you cannot find reliable sources.")
    hints = ("Related headlines:\n" + cand["hints"] + "\n") if cand["hints"] else ""
    prompt = f"""You are a research assistant for a US news and guide website.
Use Google Search to research {what}. Report verified facts only.
{hints}
{skip}

Return ONLY one JSON object (no markdown) like:
{{"skip": false, "reason": "", "facts": ["14-18 short standalone facts, one sentence each, with concrete names, places and dates where sources support them"], "questions": ["5 questions readers commonly ask about this"]}}"""
    try:
        resp = gemini({"contents": [{"parts": [{"text": prompt}]}],
                       "tools": [{"google_search": {}}], "generationConfig": {"temperature": 0.2}})
        data = parse_json(text_of(resp))
        if data.get("skip"):
            return {"skip": True, "reason": str(data.get("reason", ""))[:100]}
        facts = [clean(f) for f in data.get("facts", []) if str(f).strip()]
        if len(facts) < 5:
            return {"skip": True, "reason": "too few facts"}
        sources = []
        for c in resp["candidates"][0].get("groundingMetadata", {}).get("groundingChunks", []):
            w = c.get("web") or {}
            if w.get("uri") and w["uri"] not in [s["url"] for s in sources]:
                sources.append({"title": w.get("title") or "Source", "url": w["uri"]})
        return {"skip": False, "facts": facts[:18], "questions": [clean(q) for q in data.get("questions", [])][:6],
                "sources": sources[:5]}
    except Exception as e:
        _G["fails"] += 1
        print("research failed:", str(e)[:150])
        return None


SCHEMA = """{
 "title": "SEO title, max 60 characters, includes the main keyword",
 "meta_description": "max 155 characters, specific, no clickbait",
 "intro": "60-100 words that answer the main question directly",
 "key_points": ["4-5 takeaways, each under 20 words"],
 "sections": [{"heading": "specific H2 heading", "text": "plain text, paragraphs separated by a blank line"}],
 "faq": [{"q": "question", "a": "40-70 word answer"}],
 "conclusion": "50-80 words, practical, no cliches",
 "image_query": "2-3 generic words for a stock photo (no people names)"
}"""


def writer_prompt(cand, notes):
    if cand["kind"] == "guide":
        goal = f'a helpful, practical city guide titled "{cand["topic"]}" for people searching Google for this topic'
        length = "1,200-1,500 words in total: 6-7 sections of 150-220 words each, plus 4-5 FAQ answers"
    else:
        goal = f'an explainer about the trending US topic "{cand["topic"]}" for readers who just searched for it'
        length = "800-1,100 words in total: 5 sections of 130-190 words each, plus 4 FAQ answers"
    if notes:
        research_txt = "RESEARCH NOTES (the only facts you may state as facts):\n" + \
            "\n".join(f"- {f}" for f in notes["facts"])
        qs = notes.get("questions") or []
        if qs:
            research_txt += "\n\nReader questions (use for the FAQ where relevant):\n" + "\n".join(f"- {q}" for q in qs)
    else:
        research_txt = ("No research notes available. Use only well-established general knowledge. "
                        "Do NOT give prices, dates, opening hours, statistics or phone numbers.")
    return f"""You are an experienced US journalist and SEO editor. Write {goal}.

{research_txt}

RULES:
- Write for people first: plain English, short paragraphs, varied sentence length, specific names and examples when the notes support them.
- Use the main keyword naturally in the title, the intro and one heading. Never stuff keywords.
- Never invent facts, numbers, quotes, names or dates. Never claim personal experience (no "I visited").
- Do not copy sentences from any source. No quotes longer than 5 words.
- No filler openers ("In today's fast-paced world") and avoid the words delve, tapestry, bustling, vibrant.
- Each section answers a different question; headings are specific (never "Introduction").
- Plain text only: no markdown, no asterisks, no hashtags.
- Length: {length}.

Return ONLY one JSON object, no code fences, shaped like:
{SCHEMA}"""


def texts_of(art):
    return [art["intro"]] + [s["text"] for s in art["sections"]] + [f["a"] for f in art["faq"]] + [art["conclusion"]]


def words_of(art):
    return sum(len(t.split()) for t in texts_of(art))


def normalize(d):
    secs = []
    for s in d.get("sections", []):
        h, t = clean(s.get("heading", "")), clean(s.get("text", ""))
        if h and t:
            secs.append({"heading": h, "text": t})
    faq = [{"q": clean(f.get("q", "")), "a": clean(f.get("a", ""))} for f in d.get("faq", [])
           if f.get("q") and f.get("a")]
    return {"title": clean(d.get("title", ""))[:90], "summary": clean(d.get("meta_description", ""))[:160],
            "intro": clean(d.get("intro", "")), "bullets": [clean(x) for x in d.get("key_points", [])][:5],
            "sections": secs, "faq": faq[:6], "conclusion": clean(d.get("conclusion", "")),
            "image_query": clean(d.get("image_query", ""))}


def write_long(cand, notes):
    art = None
    for attempt in range(2):
        try:
            art = normalize(parse_json(llm(writer_prompt(cand, notes))))
            if art["title"] and len(art["sections"]) >= 4 and art["intro"]:
                break
        except Exception as e:
            print("writer parse fail:", str(e)[:120])
        art = None
    if not art:
        return None
    need = MIN_WORDS[cand["kind"]]
    facts = "\n".join(f"- {f}" for f in (notes or {}).get("facts", []))
    for _ in range(4):  # chhoti sections ko lamba karo
        if words_of(art) >= need:
            break
        sec = sorted(art["sections"], key=lambda s: len(s["text"].split()))[0]
        prompt = (f'Expand this section of an article titled "{art["title"]}" to about 220 words.\n'
                  f'Heading: {sec["heading"]}\nCurrent text: {sec["text"]}\n\n'
                  + (f"Facts you may use:\n{facts}\n\n" if facts else "Use only well-established general knowledge; no prices, dates or statistics.\n\n")
                  + "Add concrete, accurate detail. Do not repeat other sections. "
                    "Return ONLY the section text as plain paragraphs separated by a blank line.")
        try:
            new = clean(llm(prompt, 1500))
            if len(new.split()) > len(sec["text"].split()):
                sec["text"] = new
        except Exception as e:
            print("expand fail:", str(e)[:100])
            break
    print("words:", words_of(art), "need:", need)
    return art if words_of(art) >= need * 0.75 else None


# ---------------- 3. IMAGE ----------------
def get_image(query, slug, fallback=""):
    if not PIXABAY_KEY:
        print("PIXABAY_API_KEY missing")
        return ""
    for q in [x for x in (query, fallback) if x]:
        try:
            r = requests.get("https://pixabay.com/api/", timeout=30, params={
                "key": PIXABAY_KEY, "q": q, "image_type": "photo",
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


# ---------------- 4. BUILD ONE POST ----------------
_SLUGS = set()


def build_post(cand):
    """Return (status, post). status: ok / skip / fail / stop"""
    notes = research(cand)
    if notes is None and cand["kind"] == "trend":
        return "stop", None  # bina research ke trending post nahi likhenge
    if notes and notes.get("skip"):
        print("skip:", cand["topic"][:60], "-", notes.get("reason", ""))
        return "skip", None
    art = write_long(cand, notes)
    if not art:
        return "fail", None
    if is_severe(" ".join(texts_of(art))):
        print("skip (sensitive text):", art["title"])
        return "skip", None
    slug = slugify(art["title"])
    n = 2
    while slug in _SLUGS:
        slug = f"{slugify(art['title'])}-{n}"
        n += 1
    _SLUGS.add(slug)
    now = datetime.datetime.now(datetime.timezone.utc)
    fallback = (cand["city"] + " skyline") if cand["kind"] == "guide" else "news headlines"
    post = {
        "v": POST_VERSION, "kind": cand["kind"], "key": cand["key"], "slug": slug, "city": cand["city"],
        "title": art["title"], "summary": art["summary"], "intro": art["intro"], "bullets": art["bullets"],
        "sections": art["sections"], "faq": art["faq"], "conclusion": art["conclusion"],
        "paragraphs": texts_of(art), "sources": (notes or {}).get("sources", []),
        "researched": bool(notes), "date": now.strftime("%b %d, %Y"), "iso": now.strftime("%Y-%m-%d"),
        "ts": time.time(),
        "image": get_image(art["image_query"] or cand["topic"], slug, fallback)}
    return "ok", post


def produce(kind, seen, state, posts):
    if kind == "guide":
        cand = next_guide(seen)
        if cand:
            status, post = build_post(cand)
            if status == "skip":
                state["skipped"].append(cand["key"])
                seen.add(cand["key"])
            return post
        print("sab guides ban chuki hain, trending post banega")
    tries = 0
    for cand in trend_candidates(seen):
        if tries >= 5:
            break
        tries += 1
        if cand["key"] in seen:
            continue
        status, post = build_post(cand)
        if status == "stop":
            print("research nahi hui, trending skip")
            return None
        if status == "skip":
            state["skipped"].append(cand["key"])
            seen.add(cand["key"])
        if post:
            return post
    return None


# ---------------- 5. HTML ----------------
CSS = """
:root{--bg:#f3f4f6;--card:#fff;--ink:#101828;--mute:#667085;--acc:#e03a2f;--nav:#0b1a2b;--line:#e4e7ec;--r:10px}
@media(prefers-color-scheme:dark){:root{--bg:#0a1018;--card:#121b26;--ink:#eef2f6;--mute:#9aa7b5;--acc:#ff6a5c;--nav:#060b11;--line:#223040}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%;scroll-padding-top:110px}
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
.more{display:inline-block;margin-top:10px;color:var(--acc);font-weight:600}
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
article.post h3{font:700 18px/1.3 Georgia,serif;margin:18px 0 4px}
article.post .cover{width:100%;height:auto;aspect-ratio:16/9;object-fit:cover;border-radius:8px;margin:14px 0 4px;display:block}
.cap{font-size:12px;color:var(--mute);margin:0 0 14px}
article.post p{font:18px/1.75 Georgia,serif;margin:0 0 16px}
article.post ul{font:18px/1.7 Georgia,serif;margin:0 0 16px;padding-left:22px}
article.post p.lead{font-size:20px}
.crumbs{font-size:13px;color:var(--mute);margin-bottom:8px}
.crumbs a{text-decoration:underline}
.keys,.toc{background:var(--bg);border-radius:8px;padding:12px 16px;margin:16px 0}
.keys{border-left:4px solid var(--acc)}
.keys ul,.toc ol{margin:8px 0 0;padding-left:20px;font:16px/1.6 system-ui,sans-serif}
.toc a{color:var(--acc)}
.share{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:14px 0;font-size:14px;color:var(--mute)}
.share a{padding:8px 14px;border-radius:999px;background:var(--bg);color:var(--ink);font-weight:600}
.src{border-top:1px solid var(--line);margin-top:22px;padding-top:12px;color:var(--mute);font-size:14px}
.src ul{margin:6px 0;padding-left:20px;font:14px/1.6 system-ui,sans-serif}
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


def tag_label(p):
    return "Trending" if p["kind"] == "trend" else p["city"]


def ttl(t):
    return t if len(t) + len(SITE_NAME) + 3 > 60 else f"{t} | {SITE_NAME}"


def sidebar():
    items = "".join(
        f'<a class="mini" href="/news/{p["slug"]}/"><span class="tag">{esc(tag_label(p))}</span>{esc(p["title"])}</a>'
        for p in LATEST[:6])
    cities = "".join(f'<a href="/city/{city_slug(c)}/">{esc(c)}</a>' for c in CITIES)
    return (f'<div class="box"><h3>Latest</h3>{items or "<p class=meta>Coming soon.</p>"}</div>'
            f'<div class="ad">{AD_SIDEBAR}</div>'
            f'<div class="box"><h3>City guides</h3><div class="chips">{cities}</div></div>')


def page(title, desc, body, path, image="", head_extra="", robots=""):
    nav = ('<a href="/">Home</a><a href="/trending/">Trending</a><a href="/guides/">Guides</a>'
           + "".join(f'<a href="/city/{city_slug(c)}/">{esc(c)}</a>' for c in CITIES))
    og_img = f'<meta property="og:image" content="{SITE_URL}{image}">' if image else ""
    rb = f'<meta name="robots" content="{robots}">' if robots else ""
    today = datetime.date.today().strftime("%A, %B %d, %Y")
    fav = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E"
           "%3Crect width='100' height='100' rx='18' fill='%23e03a2f'/%3E%3Ctext x='50' y='72' "
           "font-size='70' text-anchor='middle' fill='white' font-family='Georgia' "
           f"font-weight='bold'%3E{esc(SITE_NAME[0])}%3C/text%3E%3C/svg%3E")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title><meta name="description" content="{esc(desc)}">{rb}
<link rel="canonical" href="{SITE_URL}{path}"><link rel="stylesheet" href="/style.css">
<link rel="alternate" type="application/rss+xml" title="{esc(SITE_NAME)}" href="/feed.xml">
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
<div><a class="brand" href="/">{esc(SITE_NAME)}</a><p>Trending explainers and practical city guides for the USA. Written with AI assistance, researched from public reporting, with sources listed.</p></div>
<div class="links"><h4>Explore</h4><a href="/trending/">Trending</a><a href="/guides/">City guides</a><a href="/archive/">Archive</a></div>
<div class="links"><h4>About</h4><a href="/about/">About us</a><a href="/editorial/">Editorial policy</a><a href="/contact/">Contact</a><a href="/privacy/">Privacy policy</a><a href="/disclaimer/">Disclaimer</a></div>
<p class="copy">&copy; {datetime.date.today().year} {esc(SITE_NAME)}. All rights reserved.</p>
</div></footer></body></html>"""


def write(path, content):
    if path.endswith((".xml", ".txt", ".css", ".html")) or path == "/_headers":
        p = SITE / path.strip("/")
    else:
        p = SITE / path.strip("/") / "index.html"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def render_text(t, cls=""):
    out = []
    for block in re.split(r"\n\s*\n", t.strip()):
        lines = [l.strip() for l in block.split("\n") if l.strip()]
        if lines and all(re.match(r"^[-*\u2022]\s+", l) for l in lines):
            out.append("<ul>" + "".join(f"<li>{esc(re.sub(r'^[-*\u2022]\s+', '', l))}</li>" for l in lines) + "</ul>")
        elif lines:
            c = f' class="{cls}"' if cls else ""
            out.append(f"<p{c}>{esc(' '.join(lines))}</p>")
    return "".join(out)


def item(p):
    img = (f'<img src="{p["image"]}" alt="{esc(p["title"])}" loading="lazy">' if p["image"]
           else '<div class="ph"></div>')
    return (f'<a class="item" href="/news/{p["slug"]}/"><div class="th">{img}</div><div class="tx">'
            f'<span class="tag">{esc(tag_label(p))}</span><h3>{esc(p["title"])}</h3>'
            f'<p class="sum">{esc(p["summary"])}</p><span class="meta">{p["date"]}</span></div></a>')


def hero(p):
    img = f'<img src="{p["image"]}" alt="{esc(p["title"])}">' if p["image"] else ""
    return (f'<a class="hero" href="/news/{p["slug"]}/">{img}<div class="t">'
            f'<span class="tag pill">{esc(tag_label(p))}</span><h2>{esc(p["title"])}</h2>'
            f'<span class="meta">{p["date"]}</span></div></a>')


def feed(items):
    return '<div class="feed">' + "".join(item(p) for p in items) + "</div>"


def article_html(p, url, related):
    mid_name, mid_url = (("Trending", "/trending/") if p["kind"] == "trend"
                         else (p["city"], f"/city/{city_slug(p['city'])}/"))
    crumbs = (f'<nav class="crumbs" aria-label="Breadcrumb"><a href="/">Home</a> &rsaquo; '
              f'<a href="{mid_url}">{esc(mid_name)}</a></nav>')
    mins = max(1, round(sum(len(x.split()) for x in p["paragraphs"]) / 200))
    cover = ""
    if p["image"]:
        cover = (f'<img class="cover" src="{p["image"]}" width="640" height="360" alt="{esc(p["title"])}">'
                 '<p class="cap">Representative stock image via Pixabay, not a photo of the event.</p>')
    share = ('<div class="share"><span>Share</span>'
             f'<a href="https://wa.me/?text={quote(p["title"] + " " + url)}" target="_blank" rel="noopener">WhatsApp</a>'
             f'<a href="https://twitter.com/intent/tweet?url={quote(url)}&text={quote(p["title"])}" target="_blank" rel="noopener">X</a>'
             f'<a href="https://www.facebook.com/sharer/sharer.php?u={quote(url)}" target="_blank" rel="noopener">Facebook</a></div>')
    keys = ""
    if p.get("bullets"):
        keys = ('<div class="keys"><strong>Key takeaways</strong><ul>'
                + "".join(f"<li>{esc(x)}</li>" for x in p["bullets"]) + "</ul></div>")
    ids = [f's{i + 1}-{slugify(s["heading"])[:30]}' for i, s in enumerate(p["sections"])]
    toc_items = [f'<li><a href="#{i}">{esc(s["heading"])}</a></li>' for i, s in zip(ids, p["sections"])]
    if p.get("faq"):
        toc_items.append('<li><a href="#faq">Frequently asked questions</a></li>')
    toc = '<nav class="toc" aria-label="Contents"><strong>In this article</strong><ol>' + "".join(toc_items) + "</ol></nav>"
    blocks = [f'<h2 id="{i}">{esc(s["heading"])}</h2>' + render_text(s["text"]) for i, s in zip(ids, p["sections"])]
    blocks.insert(min(2, len(blocks)), f'<div class="ad">{AD_INARTICLE}</div>')
    faq = ""
    if p.get("faq"):
        faq = '<h2 id="faq">Frequently asked questions</h2>' + "".join(
            f'<h3>{esc(f["q"])}</h3>' + render_text(f["a"]) for f in p["faq"])
    concl = ('<h2>The bottom line</h2>' + render_text(p["conclusion"])) if p.get("conclusion") else ""
    links = "".join(f'<li><a href="{esc(s["url"])}" rel="nofollow noopener" target="_blank">{esc(s["title"])}</a></li>'
                    for s in (p.get("sources") or []))
    note = ("Details such as prices, hours and rules change, so check official sources before you decide. "
            if p["kind"] == "guide" else "News develops quickly, so details may change after publication. ")
    src = ('<div class="src">' + (f"<strong>Sources</strong><ul>{links}</ul>" if links else "")
           + f'{note}Written with AI assistance. See our <a href="/editorial/">editorial policy</a>.</div>')
    rel = (f'<h2 class="sec">Keep reading</h2>{feed(related)}') if related else ""
    return (f'<article class="post">{crumbs}<span class="tag pill">{esc(tag_label(p))}</span>'
            f'<h1>{esc(p["title"])}</h1><span class="meta">By {esc(SITE_NAME)} Editorial Team &middot; '
            f'{p["date"]} &middot; {mins} min read</span>{cover}{share}{render_text(p["intro"], "lead")}'
            f'{keys}{toc}{"".join(blocks)}{faq}{concl}{src}</article>{rel}')


def build_site(posts):
    global LATEST
    SITE.mkdir(exist_ok=True)
    (SITE / "style.css").write_text(CSS, encoding="utf-8")
    posts = sorted(posts, key=lambda p: p["ts"], reverse=True)
    LATEST = posts
    today = datetime.date.today().isoformat()
    urls = [("/", today)] + [(u, today) for u in ("/about/", "/editorial/", "/contact/", "/privacy/", "/disclaimer/")]
    trends = [p for p in posts if p["kind"] == "trend"]
    guides = [p for p in posts if p["kind"] == "guide"]

    # homepage
    if posts:
        body = hero(posts[0])
        rest = [p for p in posts[1:]]
        t_items = [p for p in rest if p["kind"] == "trend"][:6]
        g_items = [p for p in rest if p["kind"] == "guide"][:8]
        if t_items:
            body += '<h2 class="sec">Trending now</h2>' + feed(t_items) + '<a class="more" href="/trending/">All trending stories &rsaquo;</a>'
        if g_items:
            body += '<h2 class="sec">City guides</h2>' + feed(g_items) + '<a class="more" href="/guides/">All city guides &rsaquo;</a>'
    else:
        body = '<p class="meta">New stories are on the way.</p>'
    write("/", page(f"{SITE_NAME} - Trending explainers and US city guides",
                    "Trending explainers and practical guides to major US cities, with sources.", body, "/",
                    posts[0]["image"] if posts else ""))

    # hubs
    for path, name, intro, items in (
            ("/trending/", "Trending", "Explainers on what Americans are searching for right now.", trends),
            ("/guides/", "City guides", "Practical guides to living in and visiting major US cities.", guides)):
        b = f'<h1 class="sec">{name}</h1><p class="meta">{intro}</p>' + (feed(items) if items else "")
        write(path, page(ttl(f"{name} | {SITE_NAME}"), intro, b, path, robots="" if items else "noindex,follow"))
        if items:
            urls.append((path, items[0]["iso"]))
    for c in CITIES:
        items = [p for p in guides if p["city"] == c]
        path = f"/city/{city_slug(c)}/"
        intro = f"Guides to living in and visiting {c}."
        b = f'<h1 class="sec">{esc(c)} guides</h1><p class="meta">{intro}</p>' + (feed(items) if items else "")
        write(path, page(ttl(f"{c} City Guides | {SITE_NAME}"), intro, b, path, robots="" if items else "noindex,follow"))
        if items:
            urls.append((path, items[0]["iso"]))

    # archive
    arch = ""
    month = ""
    for p in posts:
        m = datetime.datetime.fromtimestamp(p["ts"]).strftime("%B %Y")
        if m != month:
            arch += ("</ul>" if month else "") + f"<h2>{m}</h2><ul>"
            month = m
        arch += f'<li><a href="/news/{p["slug"]}/">{esc(p["title"])}</a></li>'
    arch += "</ul>" if month else ""
    write("/archive/", page(ttl(f"Archive | {SITE_NAME}"), "All stories and guides.",
                            f'<article class="post"><h1>Archive</h1>{arch}</article>', "/archive/"))
    urls.append(("/archive/", today))

    # posts
    for p in posts:
        url = SITE_URL + f"/news/{p['slug']}/"
        same = [x for x in posts if x["slug"] != p["slug"] and x["kind"] == p["kind"]
                and (p["kind"] == "trend" or x["city"] == p["city"])]
        other = [x for x in posts if x["slug"] != p["slug"] and x not in same]
        related = (same + other)[:3]
        mid_name, mid_url = (("Trending", "/trending/") if p["kind"] == "trend"
                             else (p["city"], f"/city/{city_slug(p['city'])}/"))
        art = {"@context": "https://schema.org", "@type": "Article", "headline": p["title"],
               "description": p["summary"], "datePublished": p["iso"], "dateModified": p["iso"],
               "mainEntityOfPage": url, "author": {"@type": "Organization", "name": SITE_NAME, "url": SITE_URL},
               "publisher": {"@type": "Organization", "name": SITE_NAME, "url": SITE_URL}}
        if p["image"]:
            art["image"] = SITE_URL + p["image"]
        bc = {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Home", "item": SITE_URL + "/"},
            {"@type": "ListItem", "position": 2, "name": mid_name, "item": SITE_URL + mid_url},
            {"@type": "ListItem", "position": 3, "name": p["title"], "item": url}]}
        ld = "".join(f'<script type="application/ld+json">{json.dumps(x)}</script>' for x in (art, bc))
        path = f"/news/{p['slug']}/"
        write(path, page(ttl(p["title"]), p["summary"], article_html(p, url, related), path, p["image"], ld))
        urls.append((path, p["iso"]))

    # static pages
    n = esc(SITE_NAME)
    cities = ", ".join(CITIES)
    upd = datetime.date.today().strftime("%B %d, %Y")
    static = {
        "/about/": ("About", f"<h1>About {n}</h1>"
            f"<p>{n} publishes two kinds of content: explainers on topics that are trending in the United States, and practical guides to major US cities ({cities}).</p>"
            "<p>For every article we research the topic with search tools, write an original piece in plain language with AI assistance, and list the sources we used so you can read the original reporting.</p>"
            "<p>We do not replace local newsrooms. We aim to give readers a clear, quick starting point and we encourage them to visit the sources linked in each article.</p>"),
        "/editorial/": ("Editorial Policy", "<h1>Editorial Policy</h1>"
            "<h2>How articles are made</h2>"
            "<p>Topics come from public trend data and a fixed list of city guide subjects. We research each topic with search tools, then write the article with AI assistance and run automated checks on length, structure and sensitive content. We aim to state only facts supported by published sources.</p>"
            "<h2>Topics we avoid</h2>"
            "<p>To reduce the risk of errors and harm we do not cover crimes, accidents, deaths, missing people, minors, health claims, elections or politics, or ongoing emergencies.</p>"
            "<h2>Images</h2>"
            "<p>Images are representative stock photos from Pixabay and are not photos of the events described.</p>"
            f"<h2>Corrections</h2><p>If you find a mistake, email <a href='mailto:{CONTACT_EMAIL}'>{CONTACT_EMAIL}</a> with the article link. We will review it and correct or remove the article where needed.</p>"),
        "/contact/": ("Contact", "<h1>Contact</h1>"
            f"<p>For corrections, takedown requests, advertising questions or feedback, email <a href='mailto:{CONTACT_EMAIL}'>{CONTACT_EMAIL}</a>.</p>"
            "<p>Please include the link to the article you are writing about so we can respond faster.</p>"),
        "/privacy/": ("Privacy Policy", "<h1>Privacy Policy</h1>"
            f"<p>Last updated: {upd}</p>"
            f"<p>{n} does not ask you to create an account and does not collect personal information directly.</p>"
            "<h2>Cookies and advertising</h2>"
            "<p>We show ads from third-party advertising networks. These partners may use cookies or similar technologies to show ads and measure performance. You can block or delete cookies in your browser settings.</p>"
            "<h2>Server logs</h2>"
            "<p>Our hosting provider may keep standard server logs such as IP address, browser type and pages visited, for security and performance.</p>"
            "<h2>External links</h2>"
            "<p>Our articles link to other websites. We are not responsible for their privacy practices.</p>"
            f"<h2>Contact</h2><p>Questions about this policy: <a href='mailto:{CONTACT_EMAIL}'>{CONTACT_EMAIL}</a>.</p>"),
        "/disclaimer/": ("Disclaimer", "<h1>Disclaimer</h1>"
            "<p>Articles on this site are written with AI assistance from publicly available information and may contain errors or omissions. They are for general information only and are not legal, medical, financial or professional advice.</p>"
            "<p>Prices, opening hours, rules and news details change. Always check official sources before you act on anything you read here. Trademarks and names belong to their respective owners.</p>"),
    }
    for path, (t, b) in static.items():
        write(path, page(ttl(f"{t} | {SITE_NAME}"), t, f'<article class="post">{b}</article>', path))

    write("/404.html", page(ttl(f"Page not found | {SITE_NAME}"), "Page not found.",
                            '<article class="post"><h1>Page not found</h1><p>That page does not exist. '
                            'Try the <a href="/">home page</a> or the <a href="/archive/">archive</a>.</p></article>',
                            "/404.html", robots="noindex"))

    sm = "".join(f"<url><loc>{SITE_URL}{u}</loc><lastmod>{d}</lastmod></url>" for u, d in urls)
    write("/sitemap.xml", f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{sm}</urlset>')
    write("/robots.txt", f"User-agent: *\nAllow: /\nSitemap: {SITE_URL}/sitemap.xml\n")
    entries = "".join(
        f"<item><title>{esc(p['title'])}</title><link>{SITE_URL}/news/{p['slug']}/</link>"
        f"<guid>{SITE_URL}/news/{p['slug']}/</guid><pubDate>{formatdate(p['ts'])}</pubDate>"
        f"<description>{esc(p['summary'])}</description></item>" for p in posts[:20])
    write("/feed.xml", f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>{esc(SITE_NAME)}</title>'
                       f'<link>{SITE_URL}/</link><description>Trending explainers and US city guides</description>{entries}</channel></rss>')
    key = indexnow_key()
    write(f"/{key}.txt", key)
    write("/_headers", "/*\n  X-Content-Type-Options: nosniff\n  Referrer-Policy: strict-origin-when-cross-origin\n"
                       "/img/*\n  Cache-Control: public, max-age=31536000, immutable\n"
                       "/style.css\n  Cache-Control: public, max-age=86400\n")

    # kaam na aane wali purani images hata do
    used = {Path(p["image"]).name for p in posts if p["image"]}
    if (SITE / "img").exists():
        for f in (SITE / "img").iterdir():
            if f.name not in used:
                f.unlink()


def indexnow_key():
    return hashlib.md5(SITE_URL.encode()).hexdigest()


def ping_indexnow():
    f = DATA.parent / "new_urls.json"
    urls = json.loads(f.read_text()) if f.exists() else []
    if not urls:
        print("ping: koi nayi url nahi")
        return
    key = indexnow_key()
    try:
        r = requests.post("https://api.indexnow.org/indexnow", timeout=30, json={
            "host": SITE_URL.split("//")[1], "key": key, "keyLocation": f"{SITE_URL}/{key}.txt",
            "urlList": urls[:100]})
        print("indexnow:", r.status_code, len(urls), "urls")
    except Exception as e:
        print("indexnow fail:", str(e)[:100])


# ---------------- MAIN ----------------
def main():
    if "--ping" in sys.argv:
        return ping_indexnow()
    DATA.parent.mkdir(exist_ok=True)
    posts = json.loads(DATA.read_text()) if DATA.exists() else []
    state = json.loads(STATE.read_text()) if STATE.exists() else {"skipped": []}

    before = len(posts)
    posts = [p for p in posts if p.get("v") == POST_VERSION]   # purani chhoti posts hata do
    if len(posts) != before:
        print("removed old thin posts:", before - len(posts))

    filled = 0   # bina image wali posts me image bharo
    for p in posts:
        if not p.get("image") and filled < 5:
            p["image"] = get_image(p["title"], p["slug"], "news headlines" if p["kind"] == "trend" else p["city"] + " skyline")
            filled += 1

    seen = {p["key"] for p in posts if p.get("key")} | set(state["skipped"])
    _SLUGS.update(p["slug"] for p in posts)
    new_urls = []
    if GEMINI_KEY or GROQ_KEYS:
        for kind in ["trend"] * TREND_PER_RUN + ["guide"] * GUIDE_PER_RUN:
            try:
                post = produce(kind, seen, state, posts)
            except Exception as e:
                print("produce fail:", str(e)[:200])
                post = None
            if post:
                posts.append(post)
                seen.add(post["key"])
                new_urls.append(f"{SITE_URL}/news/{post['slug']}/")
                print("posted:", post["title"], "|", post["kind"], "|", len(" ".join(post["paragraphs"]).split()), "words")
    else:
        print("AI keys nahi mili - sirf site rebuild ho rahi hai")

    state["skipped"] = state["skipped"][-500:]
    STATE.write_text(json.dumps(state))
    (DATA.parent / "new_urls.json").write_text(json.dumps(new_urls))
    DATA.write_text(json.dumps(posts, ensure_ascii=False, indent=1))
    build_site(posts)
    print(f"done. new posts: {len(new_urls)}, total: {len(posts)}")


if __name__ == "__main__":
    main()
