"""
Simple UI Generator
-------------------
Give it any website URL. It reads the page, finds the main things a person
can do there, and turns them into big 1-2 word buttons. A small search box
understands short requests ("pay bill", "login", "jobs") and opens the right
page on the real website.

Run:  pip install flask requests beautifulsoup4
      python app.py            -> http://localhost:5000
Optional: put your AI key in a file called .env next to this file, e.g.
          OPENAI_API_KEY=sk-...    (or GEMINI_API_KEY / GROQ_API_KEY / ANTHROPIC_API_KEY)
          -> AI picks and names the buttons and understands requests.
Optional: pip install playwright && python -m playwright install chromium
          -> "deep scan" for JavaScript-heavy sites (e.g. many government portals).
"""
import json
import logging
import os
import re

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
from difflib import SequenceMatcher
from urllib.parse import urljoin, urlparse, urlencode

import requests
import urllib3
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template, request

def load_env_file(path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")):
    """Read KEY=value lines from .env (no extra package needed)."""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.removeprefix("export ").split("=", 1)
                    v = v.strip().strip('"').strip("'")
                    if v:   # .env wins over an old key left in the terminal
                        os.environ[k.strip()] = v


load_env_file()
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    import renderer          # optional deep scan (needs Playwright)
except ImportError:
    renderer = None

import security              # SSRF guard, rate limits, security headers, input checks
import priority              # important-topic buttons that hold all their pages
import discovery             # sitemap + depth-1 exploration + shared URL canonicalization

app = Flask(__name__)
security.install(app)

HEADERS = {"User-Agent": "Mozilla/5.0 (SimpleUI sample; +https://example.com)"}

# ---------------------------------------------------------------------------
# Intent catalogue: the "main things" people usually want to do on a website.
# `words` are matched against link text / URLs; `asks` are extra words a
# non-technical user might type for the same goal.
# ---------------------------------------------------------------------------
INTENTS = {
    "home":     {"label": "Home",     "icon": "🏠", "words": ["home", "homepage", "start"], "asks": ["main", "beginning", "front"]},
    "login":    {"label": "Log In",   "icon": "🔑", "words": ["login", "log in", "sign in", "signin", "log-in", "sign-in"], "asks": ["enter", "password", "access", "my login"]},
    "signup":   {"label": "Sign Up",  "icon": "📝", "words": ["sign up", "signup", "register", "join", "create account", "get started"], "asks": ["new account", "membership", "enroll", "start"]},
    "apply":    {"label": "Apply",    "icon": "✍️", "words": ["apply", "apply for", "apply online", "new application", "fresh", "application form", "online application"], "asks": ["new passport", "application", "aavedan", "form", "apply karna"]},
    "status":   {"label": "Status",   "icon": "📍", "words": ["track application status", "application status", "status", "track status", "check status", "know your status"], "asks": ["where is", "progress", "sthiti", "track", "status kya"]},
    "renew":    {"label": "Renew",    "icon": "🔄", "words": ["renew", "renewal", "re issue", "reissue", "re-issue"], "asks": ["expired", "expiry", "old passport", "update", "change details"]},
    "documents": {"label": "Documents", "icon": "📄", "words": ["document advisory", "documents required", "required documents", "documents", "document", "checklist"], "asks": ["papers", "proof", "dastavez", "kagaz", "id proof", "address proof"]},
    "office":   {"label": "Find Office", "icon": "🏢", "words": ["locator", "office locator", "passport offices", "locate", "find office", "offices", "locations", "near me"], "asks": ["nearest", "office", "where", "kendra", "location", "branch"]},
    "account":  {"label": "Account",  "icon": "👤", "words": ["account", "profile", "my account", "dashboard", "settings"], "asks": ["me", "my details", "my info", "preferences"]},
    "search":   {"label": "Search",   "icon": "🔍", "words": ["search", "find"], "asks": ["look", "look up", "lookup"]},
    "shop":     {"label": "Shop",     "icon": "🛍️", "words": ["shop", "store", "products", "buy", "catalog", "deals"], "asks": ["purchase", "order", "things", "items", "sale"]},
    "cart":     {"label": "Cart",     "icon": "🛒", "words": ["cart", "basket", "bag", "checkout"], "asks": ["my order", "pay now", "trolley"]},
    "orders":   {"label": "Orders",   "icon": "📦", "words": ["orders", "order status", "track", "tracking", "returns"], "asks": ["delivery", "package", "parcel", "shipping", "return"]},
    "pricing":  {"label": "Prices",   "icon": "💲", "words": ["pricing", "prices", "plans", "fees", "fee", "fee calculator", "charges", "rates"], "asks": ["cost", "how much", "money", "price", "cheap", "shulk", "kitna"]},
    "billing":  {"label": "Pay Bill", "icon": "💳", "words": ["billing", "pay", "payment", "invoice", "bill"], "asks": ["pay bill", "card", "money", "due"]},
    "contact":  {"label": "Contact",  "icon": "📞", "words": ["contact", "contact us", "call", "call center", "call centre", "email us", "get in touch", "helpline"], "asks": ["phone", "email", "talk", "address", "reach", "call"]},
    "help":     {"label": "Help",     "icon": "❓", "words": ["help", "support", "faq", "faqs", "customer service", "help center"], "asks": ["problem", "issue", "question", "stuck", "broken", "assist"]},
    "about":    {"label": "About",    "icon": "ℹ️", "words": ["about", "about us", "who we are", "our story", "company"], "asks": ["info", "information", "who", "history"]},
    "news":     {"label": "News",     "icon": "📰", "words": ["news", "blog", "press", "updates", "articles", "events"], "asks": ["latest", "new", "stories", "happening"]},
    "jobs":     {"label": "Jobs",     "icon": "💼", "words": ["careers", "jobs", "join us", "work with us", "hiring"], "asks": ["job", "work", "career", "employment", "vacancy"]},
    "docs":     {"label": "Guides",   "icon": "📘", "words": ["docs", "documentation", "guide", "guides", "tutorial", "learn", "manual"], "asks": ["how to", "howto", "instructions", "teach"]},
    "download": {"label": "Download", "icon": "⬇️", "words": ["download", "downloads", "install", "get the app"], "asks": ["get", "install", "setup"]},
    "book":     {"label": "Book",     "icon": "📅", "words": ["book", "booking", "appointment", "appointment availability", "check appointment", "schedule", "reserve", "reservation"], "asks": ["appointment", "slot", "visit", "meeting"]},
    "complaint": {"label": "Complaint", "icon": "📣", "words": ["grievance", "grievances", "complaint", "complaints", "feedback"], "asks": ["shikayat", "unhappy", "report problem"]},
    "donate":   {"label": "Donate",   "icon": "❤️", "words": ["donate", "donation", "give", "support us"], "asks": ["charity", "give money", "contribute"]},
    "privacy":  {"label": "Privacy",  "icon": "🔒", "words": ["privacy", "terms", "cookies", "legal"], "asks": ["data", "rules", "policy"]},
}

STOPWORDS = {"the", "a", "an", "to", "of", "and", "or", "for", "in", "on", "our", "your",
             "my", "with", "at", "by", "is", "us", "we", "you", "i", "want", "please", "go", "page"}
GENERIC_TEXT = {"know more", "read more", "click here", "here", "more", "view more", "details",
                "view", "link", "hindi", "english", "open", "learn more", "see more", "view details"}
LABEL_SKIP = {"the", "a", "an", "and", "or", "our", "your", "my", "its", "their", "on", "for", "of", "to", "with"}
NOISE = re.compile(r"screen reader|sitemap|site map|skip to|accessibility|font size|"
                   r"text size|^a[+-]?$|language|login|log in|sign in", re.I)
MAX_EXTRA_BUTTONS = 8


# ----------------------------- helpers -------------------------------------
def norm(text):
    return re.sub(r"\s+", " ", re.sub(r"[\W_]", " ", (text or "").lower())).strip()


def tokens(text):
    return [t for t in norm(text).split() if t not in STOPWORDS]


def link_label(link, max_words=4):
    return (short_label(link.get("name") or link["text"], max_words)
            or short_label(link["text"], max_words) or "Open")


def short_label(text, max_words=2):
    words = [w for w in re.findall(r"[\w&'./-]+", text or "")
             if w.lower() not in LABEL_SKIP and re.search(r"[^\W\d_]", w)
             and not any(c.isdigit() for c in w)]
    label = " ".join(words[:max_words]).strip(".")
    return label.title() if label.islower() or label.isupper() else label


def fetch_page(url):
    if not urlparse(url).scheme:
        url = "https://" + url
    security.check_url(url)
    resp = requests.get(url, headers=HEADERS, timeout=12, **security.REQUEST_GUARD)
    resp.raise_for_status()  # TLS verification always on (no insecure retry)
    return resp.url, security.read_body(resp)  # raw bytes: BeautifulSoup detects the encoding


# ----------------------------- page analysis -------------------------------
def extract_links(base_url, html):
    """Collect visible links with the text a human would see."""
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else urlparse(base_url).netloc
    host = urlparse(base_url).netloc.replace("www.", "")
    links, seen = [], set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        full = urljoin(base_url, href).split("#")[0]
        text = a.get_text(" ", strip=True) or a.get("aria-label") or a.get("title") or ""
        if img := (not text and a.find("img")):
            text = img.get("alt", "")
        text = re.sub(r"\s+", " ", text).strip()
        # First visible piece of text is usually the "name" ("Actions" in
        # "Actions  Automate any workflow"), handy for short button labels.
        first = next(a.stripped_strings, "") or text
        if norm(text) in GENERIC_TEXT and (ctx := nearby_text(a, text)):
            text, first = f"{ctx} ({text})", ctx
        if not text or len(text) > 80 or full in seen:
            continue
        seen.add(full)
        in_nav = bool(a.find_parent(["nav", "header"])) or bool(
            a.find_parent(attrs={"role": "navigation"}))
        links.append({
            "text": text,
            "name": first,
            "url": full,
            "internal": urlparse(full).netloc.replace("www.", "").endswith(host),
            "nav": in_nav,
        })
    return title, links, find_search_form(base_url, soup)


def nearby_text(a, own_text, max_words=8):
    """For links like 'Know More', borrow the words around them."""
    for parent in list(a.parents)[:4]:
        words = parent.get_text(" ", strip=True).replace(own_text, " ").split()
        if len(words) >= 2:
            return " ".join(words[:max_words])
    return ""


def find_search_form(base_url, soup):
    """If the site has its own search box, remember how to use it."""
    for form in soup.find_all("form"):
        field = form.find("input", attrs={"type": "search"}) or form.find(
            "input", attrs={"name": re.compile(r"^(q|query|s|search|keyword|k|term)$", re.I)})
        if field and field.get("name") and (form.get("method", "get").lower() == "get"):
            return {"action": urljoin(base_url, form.get("action") or base_url),
                    "param": field["name"]}
    return None


def score_link_for_intent(link, intent):
    """How well does this link fit an intent? Short, exact link text wins."""
    text = norm(link["text"])
    segments = [norm(p) for p in urlparse(link["url"]).path.split("/") if p]
    last = segments[-1] if segments else ""
    short = len(text.split()) <= 5
    context = norm(link.get("context", ""))
    score = 0.0
    for w in intent["words"]:
        w = norm(w)
        pattern = rf"\b{re.escape(w)}\b"
        if text == w:
            score = max(score, 10)
        elif text.startswith(w + " ") and len(text.split()) <= 6:
            score = max(score, 7)
        elif ((short and len(text.split()) <= 4) or " " in w) and re.search(pattern, text):
            score = max(score, 5)   # multi-word phrases ("call center") count anywhere
        if not score and context and re.search(pattern, context):
            score = 4     # e.g. item inside the "Apply For" menu
        if last == w:
            score += 4
        elif re.search(pattern, " ".join(segments)):
            score += 1
    if score:
        score += 2 if link["nav"] else 0
        score += 1 if link["internal"] else -2
        score -= len(text.split()) * 0.3      # prefer short, clear links
    return score


def build_buttons(links):
    """Pick the best link for each common intent, then add main nav links."""
    buttons, used = [], set()
    for key, intent in INTENTS.items():
        best = max(links, key=lambda l: score_link_for_intent(l, intent), default=None)
        if best and score_link_for_intent(best, intent) >= 5 and best["url"] not in used:
            used.add(best["url"])
            buttons.append({"label": intent["label"], "icon": intent["icon"],
                            "url": best["url"], "hint": best["text"], "intent": key})

    labels = {b["label"].lower() for b in buttons}
    for link in links:  # top navigation items that didn't match a known intent
        if sum(1 for b in buttons if not b["intent"]) >= MAX_EXTRA_BUTTONS:
            break
        label = short_label(link["name"])
        if (link["nav"] and link["internal"] and label and len(label) > 2
                and not NOISE.search(link["text"])
                and link["url"] not in used and label.lower() not in labels):
            used.add(link["url"])
            labels.add(label.lower())
            buttons.append({"label": label, "icon": "➡️", "url": link["url"],
                            "hint": link["text"], "intent": None})
    return buttons


# ----------------------------- understanding requests ----------------------
def guess_intents(query):
    """Map a 1-2 word request to likely intents (cheap, offline 'AI')."""
    q, q_tokens = norm(query), set(tokens(query))
    scores = {}
    for key, intent in INTENTS.items():
        s = 0.0
        for phrase in intent["words"] + intent["asks"] + [intent["label"]]:
            p = norm(phrase)
            if p == q:
                s = max(s, 10)
            elif set(p.split()) & q_tokens:
                s = max(s, 5)
            else:  # typo tolerance: "logn", "contcat", "carrer"
                for t in q_tokens:
                    for pw in p.split():
                        if len(t) > 3 and SequenceMatcher(None, t, pw).ratio() >= 0.8:
                            s = max(s, 4)
        if s:
            scores[key] = s
    return scores


def rank_links(query, links):
    intent_scores = guess_intents(query)
    q_tokens = tokens(query)
    results = []
    for link in links:
        text_tokens = (tokens(link["text"]) + tokens(link.get("context", ""))
                       + tokens(urlparse(link["url"]).path))
        s = 0.0
        for key, weight in intent_scores.items():
            s += min(score_link_for_intent(link, INTENTS[key]), 10) * weight / 10
        for t in q_tokens:  # direct word / fuzzy overlap with the link itself
            best = max((SequenceMatcher(None, t, w).ratio() for w in text_tokens), default=0)
            s += 6 if best == 1 else (3 if best >= 0.8 else 0)
        if s > 0:
            results.append((s, link))
    results.sort(key=lambda r: r[0], reverse=True)
    return results


# ----------------------------- AI (optional) -------------------------------
PROVIDERS = {   # provider -> (env var, default model)
      "nvidia":    ("NVIDIA_API_KEY", "nvidia/nemotron-3.5-super-vl-120b-a12b"),
      "openai":    ("OPENAI_API_KEY", "gpt-4o-mini"),
      "gemini":    ("GEMINI_API_KEY", "gemini-3-flash"),
      "groq":      ("GROQ_API_KEY", "llama-3.3-70b-versatile"),
      "anthropic": ("ANTHROPIC_API_KEY", "claude-3-5-haiku-latest"),
}


def ai_provider():
    """Find a key in the environment / .env and work out which company it is for."""
    line = next((n for n, (v, _) in PROVIDERS.items() if os.environ.get(v)), None)
    key = os.environ[PROVIDERS[line][0]] if line else ""
    key = (key or os.environ.get("AI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
    # Work out the company from how the key starts, so a key on the "wrong" line still works.
    if key.startswith("sk-ant-"):
        return "anthropic", key
    if key.startswith("AIza"):
        return "gemini", key
    if key.startswith("gsk_"):
        return "groq", key
    if key.startswith("sk-"):
        return "openai", key
    return (line, key) if line else (None, None)


_gemini_models = {}


def gemini_model(key):
    """Ask Google which models this key can use and pick a fast, stable one (names change over time)."""
    if key in _gemini_models:
        return _gemini_models[key]
    r = requests.get("https://generativelanguage.googleapis.com/v1beta/models", timeout=15,
                     headers={"x-goog-api-key": key}, params={"pageSize": 1000})
    r.raise_for_status()
    names = [m["name"].split("/")[-1] for m in r.json().get("models", [])
             if "generateContent" in m.get("supportedGenerationMethods", [])]
    odd = ("lite", "image", "tts", "live", "audio", "embed", "exp", "preview", "native", "robotics",
           "computer", "thinking", "learnlm", "gemma", "pro")

    def version(n):
        v = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
        return float(v.group(1)) if v else 0

    stable_flash = [n for n in names if "flash" in n and not any(w in n for w in odd)]
    pick = (max(stable_flash, key=lambda n: (version(n), -len(n)), default=None)
            or ("gemini-flash-latest" if "gemini-flash-latest" in names else None)
            or next((n for n in names if "flash" in n), None)
            or (names[0] if names else PROVIDERS["gemini"][1]))
    print(f"Using Gemini model: {pick}", flush=True)
    _gemini_models[key] = pick
    return pick


def llm_json(prompt):
    """Send a prompt to whichever AI is configured and return its JSON answer."""
    name, key = ai_provider()
    if not name:
        return None
    model = os.environ.get("AI_MODEL") or (gemini_model(key) if name == "gemini" else PROVIDERS[name][1])
    
    if name in ("openai", "groq", "nvidia"):
        base = (
            "https://api.openai.com/v1" if name == "openai" 
            else "https://api.groq.com/openai/v1" if name == "groq" 
            else "https://integrate.api.nvidia.com/v1"
        )
        r = requests.post(f"{base}/chat/completions", timeout=40,
                         headers={"Authorization": f"Bearer {key}"},
                         json={"model": model, "messages": [{"role": "user", "content": prompt}],
                               "response_format": {"type": "json_object"}, "temperature": 0})
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]
    elif name == "gemini":
        config = {"responseMimeType": "application/json", "temperature": 0}
        if "2.5-flash" in model:  # skip slow "thinking" so the answer comes back in time
            config["thinkingConfig"] = {"thinkingBudget": 0}
        r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                         timeout=60, headers={"x-goog-api-key": key},
                         json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": config})
        r.raise_for_status()
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    else:  # anthropic
        r = requests.post("https://api.anthropic.com/v1/messages", timeout=40,
                         headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                         json={"model": model, "max_tokens": 1500,
                               "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        text = r.json()["content"][0]["text"]
    match = re.search(r"\{.*\}", text, re.S)
    return json.loads(match.group(0)) if match else None

AI_CANDIDATES = int(os.environ.get("AI_CANDIDATES", 150))


def rank_candidates(links):
    """Deterministic pre-ranking so the model sees the best candidates first,
    not just the first 150 discovered. Keeps real links only - the AI still
    picks by index, so ordering never lets it invent a URL."""
    def score(l):
        s = 0.0
        s += 4 if l.get("nav") else 0
        s += 3 if l.get("internal") else 0
        s += 2 if l.get("context") else 0          # menu context = better label
        s += 1 if l.get("source") in (None, "html") else 0
        s += 1 if l.get("depth", 0) == 0 else 0    # top-level first, then deeper
        words = len((l.get("text") or "").split())
        s += 1 if 1 <= words <= 5 else 0
        return s
    return sorted(links, key=score, reverse=True)


def link_menu(links, limit=None):
    rows = []
    for i, l in enumerate(links[:(limit or AI_CANDIDATES)]):
        where = f" [menu: {l['context']}]" if l.get("context") else ""
        rows.append(f"{i}: {l['text'][:80]}{where} -> {l['url'][:120]}")
    return "\n".join(rows)


def ai_problem(e):
    """Turn an AI error into a plain-English note for the user."""
    resp = getattr(e, "response", None)
    code, body = (resp.status_code, resp.text) if resp is not None else (None, "")
    print(f"AI error: {code or type(e).__name__} {body[:300] or e}", flush=True)
    if isinstance(e, requests.exceptions.Timeout):
        return "The AI took too long to answer, so simple word matching is used. Try again in a minute."
    if code == 429 and ("quota" in body or "credit" in body) and "PerMinute" not in body:
        return "Your AI key has no credit left (or its free limit is used up), so simple word matching is used. Check your AI account's billing page."
    if code in (401, 403) or "API_KEY_INVALID" in body or "API key not valid" in body:
        return "Your AI key was refused (wrong or deleted key), so simple word matching is used."
    if code == 429:
        return "The AI is busy (too many requests). Simple word matching is used; try again in a minute."
    if code == 404:
        return "The AI model was not found (if .env has an AI_MODEL line, delete it). Simple word matching is used for now."
    if code == 400 and "location" in body.lower():
        return "This AI is not available in your country, so simple word matching is used."
    return f"The AI did not answer ({code or type(e).__name__}), so simple word matching is used."


def ai_buttons(title, links):
    """Let the AI choose the main actions and give each a 1-2 word name."""
    prompt = (
        f'Website: "{title}". Below are its links. Choose the 8-14 things an ordinary, '
        "non-technical citizen most often comes to this site to do (e.g. apply, check status, "
        "download a card, pay, find an office, complaint, login). Skip PDFs of circulars, "
        "tenders, RTI, accessibility, sitemap and social media unless they are the main purpose. "
        "Give each a very simple 1-2 word English label and one emoji icon. "
        'Reply ONLY with JSON: {"buttons":[{"i":<link number>,"label":"Apply","icon":"✍️"}]}\n\n'
        + link_menu(links))
    data = llm_json(prompt) or {}
    buttons, used = [], set()
    for b in data.get("buttons", []):
        i = int(b["i"]) if str(b.get("i", "")).strip().isdigit() else None
        if i is not None and 0 <= i < len(links) and links[i]["url"] not in used:
            used.add(links[i]["url"])
            buttons.append({"label": " ".join(str(b.get("label", "")).split()[:2])[:24] or link_label(links[i], 2),
                            "icon": str(b.get("icon", "➡️"))[:4], "url": links[i]["url"],
                            "hint": links[i]["text"], "intent": "ai"})
    return buttons


def ask_llm(query, links):
    """Let the AI pick links for a short request like 'pay bill'."""
    prompt = (f'A non-technical user typed "{query}" on a website (it may be Hindi/Hinglish or '
              "misspelled). Which links best do what they want? "
              'Reply ONLY with JSON like {"picks":[3,7]} (max 3, best first; [] if none fit).\n\n'
              + link_menu(links))
    data = llm_json(prompt) or {}
    picks = data.get("picks", [])
    picks = [int(i) for i in picks if str(i).strip().isdigit()]
    return [links[i] for i in picks if 0 <= i < len(links)]


# ----------------------------- routes --------------------------------------
@app.get("/")
def index():
    return render_template("index.html")


def looks_javascript_built(html, links):
    text = html[:300000].decode("utf-8", "ignore").lower() if isinstance(html, bytes) else html.lower()
    return (len(links) < 10 or "enable javascript" in text
            or re.search(r'<div id="(root|app|__next)">\s*</div>', text) is not None)


def deep_scan(url):
    """Render with a real browser and click menu items that have no link."""
    page = renderer.render_site(url)
    if re.search(r"access denied|request could not be satisfied|forbidden|captcha", page["title"] or "", re.I):
        raise PermissionError("blocked")
    title, links, search_form = extract_links(page["final_url"], page["html"])
    host = urlparse(page["final_url"]).netloc.replace("www.", "")
    seen = {(l["url"], l["text"].lower()) for l in links}
    for c in page["clicked"]:
        if (c["url"], c["text"].lower()) not in seen:
            seen.add((c["url"], c["text"].lower()))
            links.append({"text": c["text"], "name": c["text"], "url": c["url"],
                          "context": c["context"], "nav": c["nav"] or bool(c["context"]),
                          "internal": urlparse(c["url"]).netloc.replace("www.", "").endswith(host),
                          "source": "click"})
    for a in page.get("explored", []):        # depth-1 pages visited inside the browser
        if (a["url"], a["text"].lower()) not in seen:
            seen.add((a["url"], a["text"].lower()))
            links.append({"text": a["text"], "name": a["text"], "url": a["url"],
                          "context": a["context"], "nav": a["nav"] or bool(a["context"]),
                          "internal": urlparse(a["url"]).netloc.replace("www.", "").endswith(host),
                          "source": "explore", "depth": 1})
    return (page["final_url"], page["title"] or title, links, search_form,
            page.get("clicked", []), page.get("explored", []))


BLOCKED_MSG = ("This website blocked automatic reading (it refused our request). Government sites "
               "often block cloud servers; it usually works when run on your own computer/internet.")


@app.post("/api/analyze")
def analyze():
    data = request.json or {}
    data = security.clean_analyze_payload(data)
    url, deep = data.get("url", "").strip(), bool(data.get("deep"))
    if not url:
        return jsonify(error="Please type a website address."), 400
    if not urlparse(url).scheme:
        url = "https://" + url
    try:
        security.check_url(url)
    except security.UnsafeURL as e:
        return jsonify(error=str(e)), 400
    links, html, clicked_links, explored_links = [], b"", [], []
    try:
        final_url, html = fetch_page(url)
        title, links, search_form = extract_links(final_url, html)
    except Exception as e:
        if isinstance(e, security.UnsafeURL):   # e.g. redirected to a private address
            return jsonify(error=str(e)), 400
        if not renderer:
            return jsonify(error=f"Could not open that website ({e.__class__.__name__})."), 502
        final_url, deep = url, True     # some sites block simple downloads; try a real browser

    scanned = False
    if renderer and (deep or looks_javascript_built(html, links)):
        try:
            final_url, title, links, search_form, clicked_links, explored_links = deep_scan(final_url)
            scanned = True
        except Exception as e:
            app.logger.warning("deep scan failed for %s: %s", final_url, e)
            if not links:
                msg = str(e)
                if isinstance(e, security.UnsafeURL):
                    msg = str(e)
                elif isinstance(e, PermissionError) or "HTTP2" in msg or "CONNECTION_RESET" in msg:
                    msg = BLOCKED_MSG
                elif "NAME_NOT_RESOLVED" in msg or "TUNNEL_CONNECTION_FAILED" in msg:
                    msg = "That website address does not exist (or the site is down). Please check the spelling."
                else:
                    msg = "Could not read that website, even with a full browser."
                return jsonify(error=msg), 502
    links = security.safe_links(links)

    # --- complementary sources: sitemap + bounded depth-1 exploration --------
    # Runs even when `links` is empty (e.g. a JS shell with no renderer
    # available) - a sitemap alone can still yield destinations.
    diag = {"html_links": len(links), "deep_scanned": scanned, "deep_clicked": len(clicked_links),
            "deep_explored": len(explored_links),
            "sitemap_links": 0, "explore_links": 0, "sitemap_error": None, "explore_error": None}
    try:
        sm_links = discovery.sitemap_links(final_url, extract_links)
        diag["sitemap_links"] = len(sm_links)
        links += [l for l in sm_links if l["url"] not in {x["url"] for x in links}]
    except Exception as e:
        diag["sitemap_error"] = type(e).__name__
        app.logger.warning("sitemap discovery failed for %s: %s", final_url, e)
    try:
        # HTTP exploration only when no browser ran - rendered scans already
        # visited depth-1 pages inside the browser itself.
        ex_links = [] if scanned else discovery.explore_pages(final_url, links, fetch_page, extract_links)
        diag["explore_links"] = len(ex_links)
        links += ex_links
    except Exception as e:
        diag["explore_error"] = type(e).__name__
        app.logger.warning("exploration failed for %s: %s", final_url, e)

    if not links:
        return jsonify(error=BLOCKED_MSG), 502

    links = security.safe_links(links)
    links = rank_candidates(links)
    diag["candidates"] = len(links)

    buttons, ai_used, ai_note, groups = [], False, None, []
    if ai_provider()[0]:
        try:
            groups = priority.ai_groups(title, links, llm_json, link_menu, link_label)
            ai_used = bool(groups)
        except Exception as e:
            app.logger.warning("AI buttons failed: %s", e)
            ai_note = ai_problem(e)
    groups = priority.merge_groups(groups, priority.keyword_groups(links, link_label))
    buttons = priority.group_buttons(groups)
    if len(buttons) < 4:    # few important topics found: add the main menu items as before
        labels = {b["label"].lower() for b in buttons}
        buttons += [b for b in build_buttons(links) if b["url"] not in {x["url"] for x in buttons}
                    and b["label"].lower() not in labels]
    if not buttons:
        buttons = build_buttons(links)
    links, buttons = security.safe_links(links), security.safe_links(buttons)
    search_form = security.safe_search_form(search_form)
    diag["buttons"] = len(buttons)
    diag["ai_used"] = ai_used
    app.logger.info("discovery %s %s", final_url, json.dumps(diag, sort_keys=True))
    return jsonify(title=title, url=final_url, buttons=buttons, links=links, ai=ai_used, ai_note=ai_note,
                   search_form=search_form, deep=scanned, can_deep=bool(renderer) and not scanned)


@app.post("/api/ask")
def ask():
    data = request.json or {}
    data = security.clean_ask_payload(data)
    query, links = data.get("query", "").strip(), data.get("links", [])
    site, search_form = data.get("url", ""), data.get("search_form")
    if not query:
        return jsonify(results=[])

    ai_note = None
    try:
        llm_links = ask_llm(query, links) if ai_provider()[0] else None
    except Exception as e:
        app.logger.warning("AI request failed: %s", e)
        llm_links, ai_note = None, ai_problem(e)
    if llm_links:
        results = [{"label": link_label(l), "hint": l["text"],
                    "url": l["url"]} for l in llm_links]
        source = "ai"
    else:
        results, seen = [], set()
        for s, l in rank_links(query, links):
            if s >= 3 and l["url"] not in seen and len(results) < 3:
                seen.add(l["url"])
                results.append({"label": link_label(l), "hint": l["text"], "url": l["url"]})
        source = "keywords"

    # Always offer a search fallback so the user is never stuck.
    if search_form:
        search_url = f"{search_form['action']}?{urlencode({search_form['param']: query})}"
    else:
        host = urlparse(site).netloc
        search_url = "https://www.google.com/search?" + urlencode({"q": f"site:{host} {query}"})
    results.append({"label": f"Search “{query}”", "hint": "Search this website", "url": search_url})
    return jsonify(results=results, source=source, ai_note=ai_note)


if __name__ == '__main__':
    # Render assigns a port dynamically via os.environ.get('PORT')
    port = int(os.environ.get('PORT', 10000))
    # Bind to 0.0.0.0 so Render can detect the open port externally
    app.run(host='0.0.0.0', port=port)
