"""
Priority buttons: only the important things (fees, admissions, results, login ...)
become big buttons, and every button keeps ALL the pages that belong to it.

- With an AI key the AI groups the links (most important first); the keyword
  groups below are then added for anything important the AI missed.
- Without an AI key the keyword groups are used on their own.
- The page shows the pages of a button when it is tapped (4+ pages); with fewer
  pages the button opens the best page directly.

Tune it by editing CATEGORIES (order = priority, first is most important).
"""
import re
from urllib.parse import urlparse

MAX_GROUPS = 12
MAX_PAGES = 30
DIRECT_BELOW = 4          # fewer pages than this -> button opens the page directly

# (key, label, icon, words) - most important first
CATEGORIES = [
    ("fees", "Fees", "💳", ["fee", "fees", "fee structure", "fee payment", "pay fee", "pay fees", "online payment",
                           "payment", "pay online", "pay", "charges", "fee calculator", "pricing", "prices", "plans",
                           "bill", "billing", "pay bill"]),
    ("admissions", "Admissions", "🎓", ["admission", "admissions", "apply", "apply online", "apply now", "apply for",
                                       "application form", "online application", "new application", "enroll",
                                       "enrol", "enrolment", "enrollment", "prospectus", "eligibility", "seat matrix",
                                       "counselling", "counseling"]),
    ("login", "Log In", "🔑", ["login", "log in", "sign in", "signin", "sign up", "signup", "register",
                              "registration", "my account", "account", "dashboard", "portal"]),
    ("results", "Results", "📊", ["result", "results", "marks", "scorecard", "score card", "merit list", "grade card",
                                 "marksheet", "mark sheet", "rank list"]),
    ("status", "Status", "📍", ["status", "track", "tracking", "application status", "check status",
                               "know your status", "track application"]),
    ("exams", "Exams", "📝", ["exam", "exams", "examination", "examinations", "admit card", "hall ticket",
                             "date sheet", "datesheet", "time table", "timetable", "exam schedule", "answer key",
                             "question paper", "question papers"]),
    ("documents", "Forms", "📄", ["form", "forms", "documents", "document", "certificate", "certificates",
                                 "checklist", "required documents", "document advisory", "affidavit", "verification"]),
    ("scholarships", "Scholarships", "🏅", ["scholarship", "scholarships", "financial aid", "fee waiver", "stipend",
                                           "fellowship", "fellowships"]),
    ("courses", "Courses", "📘", ["course", "courses", "programme", "programmes", "program", "programs", "syllabus",
                                 "curriculum", "academics", "academic calendar", "departments", "department"]),
    ("services", "Services", "🧾", ["services", "online services", "e services", "eservices", "citizen services",
                                   "schemes", "scheme", "citizen corner"]),
    ("book", "Book", "📅", ["appointment", "appointments", "book", "booking", "book now", "reserve",
                           "reservation", "slot", "schedule appointment"]),
    ("downloads", "Downloads", "⬇️", ["download", "downloads", "install", "get the app", "app"]),
    ("guides", "Guides", "📘", ["docs", "documentation", "guide", "guides", "tutorial", "tutorials", "manual",
                               "how to", "instructions", "user manual", "handbook"]),
    ("shop", "Shop", "🛍️", ["shop", "store", "products", "buy", "catalog", "deals", "cart", "basket", "checkout",
                           "orders", "order status", "returns"]),
    ("jobs", "Jobs", "💼", ["jobs", "job", "careers", "career", "recruitment", "recruitments", "vacancy",
                           "vacancies", "hiring", "placement", "placements", "work with us", "join us"]),
    ("notices", "Notices", "📢", ["notice", "notices", "notice board", "circular", "circulars", "announcement",
                                 "announcements", "news", "latest news", "updates", "notification",
                                 "notifications", "whats new", "what s new", "events", "press release"]),
    ("contact", "Contact", "📞", ["contact", "contact us", "helpline", "help", "help desk", "helpdesk", "support",
                                 "faq", "faqs", "call centre", "call center", "email us", "customer care",
                                 "get in touch"]),
    ("office", "Find Office", "🏢", ["office", "offices", "locate", "locator", "office locator", "find office",
                                    "near me", "branches", "branch", "directions", "how to reach", "location"]),
    ("complaint", "Complaint", "📣", ["grievance", "grievances", "complaint", "complaints", "feedback",
                                     "public grievance"]),
    ("donate", "Donate", "❤️", ["donate", "donation", "donations", "support us"]),
    ("about", "About", "ℹ️", ["about", "about us", "overview", "who we are", "history", "introduction",
                             "vision", "mission"]),
]

UNIMPORTANT = re.compile(
    r"privacy|terms|disclaimer|copyright|cookie|\brti\b|right to information|tender|sitemap|site map|"
    r"accessibility|screen reader|skip to|font size|text size|hyperlink|web information manager|"
    r"facebook|twitter|instagram|youtube|linkedin|whatsapp|telegram|archive|^a[+-]?$|^hindi$|^english$|"
    r"help us improve|website policy|policies", re.I)


def _norm(text):
    return re.sub(r"\s+", " ", re.sub(r"[\W_]", " ", (text or "").lower())).strip()


def _has(phrase, text):
    return bool(text) and re.search(rf"\b{re.escape(phrase)}\b", text) is not None


def _score(link, words):
    """How well a link fits a category (0 = not at all)."""
    text, ctx = _norm(link.get("text")), _norm(link.get("context"))
    path = _norm(urlparse(link.get("url", "")).path)
    best = 0.0
    for w in words:
        w = _norm(w)
        if text == w:
            s = 10
        elif _has(w, text):
            s = 6 if len(text.split()) <= 5 else 4
        elif _has(w, ctx):
            s = 3          # e.g. an item inside the "Fees" menu
        elif _has(w, path):
            s = 2
        else:
            continue
        best = max(best, s + 0.2 * len(w.split()))      # longer phrase = a bit more specific
    if best:
        best += 1 if link.get("nav") else 0
        best += 0.5 if link.get("internal") else 0
    return best


def category_for_label(label):
    """Which category an AI label like 'Fee Payment' belongs to (or None)."""
    lab = _norm(label)
    for key, name, _, words in CATEGORIES:
        if lab == _norm(name) or any(_has(_norm(w), lab) for w in words):
            return key
    return None


def _page(link, link_label):
    return {"label": link_label(link), "url": link["url"], "hint": link.get("text", "")}


def keyword_groups(links, link_label):
    """Put every important-looking link into its best category."""
    buckets = {key: [] for key, *_ in CATEGORIES}
    rank = {key: i for i, (key, *_) in enumerate(CATEGORIES)}
    seen = set()
    for link in links:
        if link["url"] in seen or UNIMPORTANT.search(link.get("text", "")):
            continue
        # small boost for more important topics: "How to pay your fee" -> Fees, not Guides
        scores = [(sc + 0.6 * (len(CATEGORIES) - rank[key]) / len(CATEGORIES) if sc else 0, key)
                  for key, _, _, words in CATEGORIES for sc in [_score(link, words)]]
        s, key = max(scores)
        if s >= 3:
            seen.add(link["url"])
            buckets[key].append((s, link))
    groups = []
    for key, label, icon, _ in CATEGORIES:
        items = sorted(buckets[key], key=lambda x: -x[0])
        if items:
            groups.append({"key": key, "label": label, "icon": icon,
                           "pages": [_page(l, link_label) for _, l in items[:MAX_PAGES]]})
    return groups


def ai_groups(title, links, llm_json, link_menu, link_label):
    """Ask the AI to group the links into the most important things to do."""
    prompt = (
        f'Website: "{title}". Below are its links (number: text [menu] -> url). Group them into the '
        "6-12 MOST IMPORTANT things an ordinary, non-technical visitor comes to this site for, most "
        "important first (for example: fees / payment, admissions / apply, login, results, application "
        "status, admit card / exams, forms / documents, scholarships, courses, contact). Leave out "
        "unimportant things (privacy, terms, RTI, tenders, sitemap, accessibility, social media, old "
        "archives) unless they are the main purpose of the site. For each group give a very simple 1-2 "
        "word English label, one emoji icon and ALL the link numbers that belong to it, best link first. "
        "A link can be in only one group. "
        'Reply ONLY with JSON: {"groups":[{"label":"Fees","icon":"💳","links":[12,4,30]}]}\n\n'
        + link_menu(links))
    data = llm_json(prompt) or {}
    groups, used = [], set()
    for g in data.get("groups", []) if isinstance(data.get("groups"), list) else []:
        if not isinstance(g, dict):
            continue
        pages = []
        for i in g.get("links", []) if isinstance(g.get("links"), list) else []:
            i = int(i) if str(i).strip().isdigit() else -1
            if 0 <= i < len(links) and links[i]["url"] not in used:
                used.add(links[i]["url"])
                pages.append(_page(links[i], link_label))
        label = " ".join(str(g.get("label", "")).split()[:2])[:24]
        if pages and label:
            groups.append({"key": category_for_label(label), "label": label,
                           "icon": str(g.get("icon") or "➡️")[:4], "pages": pages[:MAX_PAGES]})
    return groups


def merge_groups(ai, keyword):
    """AI groups first; add pages / groups the AI missed from the keyword groups."""
    if not ai:
        return keyword[:MAX_GROUPS]
    used = {p["url"] for g in ai for p in g["pages"]}
    by_key = {g["key"]: g for g in ai if g["key"]}
    extra = []
    for kg in keyword:
        missing = [p for p in kg["pages"] if p["url"] not in used]
        if not missing:
            continue
        target = by_key.get(kg["key"])
        if target:                    # e.g. AI "Fees" group gets the other fee pages too
            target["pages"] = (target["pages"] + missing)[:MAX_PAGES]
            used.update(p["url"] for p in missing)
        elif len(missing) == len(kg["pages"]) and kg["key"] in IMPORTANT_KEYS:
            extra.append(kg)          # important topic the AI left out completely
            used.update(p["url"] for p in missing)
    return (ai + extra)[:MAX_GROUPS]


IMPORTANT_KEYS = {key for key, *_ in CATEGORIES[:9]}      # fees ... scholarships


def group_buttons(groups):
    """Turn groups into the big buttons the page shows."""
    buttons = []
    for g in groups:
        main = g["pages"][0]
        b = {"label": g["label"], "icon": g["icon"], "url": main["url"], "hint": main["hint"],
             "intent": g.get("key") or "group"}
        if len(g["pages"]) >= DIRECT_BELOW:
            b["pages"] = g["pages"]
        buttons.append(b)
    return buttons
