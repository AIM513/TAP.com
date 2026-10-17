"""
Extra discovery sources for TAP.com (enhancement module).

Complements the existing pipeline in app.py / renderer.py:

- canonicalize(): one shared URL normalization + dedup stage for every source.
- sitemap_links(): robots.txt + /sitemap.xml discovery (bounded, secure).
- explore_pages(): cheap depth-1 visit of the most promising internal nav
  pages, so destinations that live one click inside a section still surface.

Everything here treats website content as untrusted: every candidate URL is
re-checked with security.check_url before it is fetched or returned, and a
failed source never discards results from the others. All limits are
configurable through environment variables.
"""
import os
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlparse, urljoin

import requests

import security

# ------------------------------- limits (env-configurable) ------------------
def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default

ROBOTS_MAX_BYTES = 64 * 1024
SITEMAP_MAX_BYTES = _env_int("SITEMAP_MAX_BYTES", 2 * 1024 * 1024)
SITEMAP_MAX_URLS = _env_int("SITEMAP_MAX_URLS", 200)
SITEMAP_MAX_FILES = _env_int("SITEMAP_MAX_FILES", 8)
SITEMAP_TIMEOUT = _env_int("SITEMAP_TIMEOUT", 10)
EXPLORE_MAX_PAGES = _env_int("EXPLORE_MAX_PAGES", 5)
EXPLORE_MAX_LINKS_PER_PAGE = _env_int("EXPLORE_MAX_LINKS_PER_PAGE", 60)

_sitemap_cache = {}            # url -> (timestamp, [loc])   short-lived, in-memory
SITEMAP_CACHE_SECONDS = _env_int("SITEMAP_CACHE_SECONDS", 3600)

# XML namespaces used by sitemap files.
_NS = ("{http://www.sitemaps.org/schemas/sitemap/0.9}",
       "{http://www.google.com/schemas/sitemap/0.9}",
       "{http://www.sitemaps.org/schemas/sitemap/0.84}")


def canonicalize(url, base=None):
    """One shared normalization for every discovery source.

    Returns the cleaned absolute URL or None when the URL is unusable
    (bad scheme, embedded credentials, control chars, over-length).
    Security (private hosts) is enforced by security.check_url on the caller
    side for anything we fetch, and by security.safe_links for anything we
    show the user; this step only does cheap syntactic checks.
    """
    if not isinstance(url, str):
        return None
    url = url.strip()
    if not url:
        return None
    if base:
        url = urljoin(base, url)
    url = url.split("#")[0]                    # fragments don't change the page
    p = urlparse(url)
    if p.scheme.lower() not in ("http", "https"):
        return None
    if not p.netloc or "@" in p.netloc:        # no embedded credentials
        return None
    # lowercase scheme+host, consistent trailing slash on bare hosts
    url = p._replace(scheme=p.scheme.lower(), netloc=p.netloc.lower()).geturl()
    if urlparse(url).path in ("", "/"):
        url = url.rstrip("/")
    if not security.safe_url(url):
        return None
    return url


def _same_site(url, root_host):
    """A destination counts as 'of this site' only when its host is the site
    itself or a subdomain of it - not merely the same registrable domain."""
    host = urlparse(url).netloc.replace("www.", "")
    return host == root_host or host.endswith("." + root_host)


# ------------------------------- robots / sitemap ---------------------------
def _fetch_text(url, limit, timeout=SITEMAP_TIMEOUT):
    """GET a small text resource under the existing SSRF guard."""
    security.check_url(url)                        # raises UnsafeURL
    resp = requests.get(url, timeout=timeout, **security.REQUEST_GUARD)
    resp.raise_for_status()
    body = security.read_body(resp, limit)
    return body


def _parse_sitemap(data):
    """Parse a sitemap (index or urlset). Returns (child_sitemaps, page_urls).
    ElementTree never resolves external entities, so XXE is not possible."""
    head = data[:2048].lower()
    if b"<!doctype" in head or b"<!entity" in head:
        return [], []                            # no DTD -> no entity expansion (bomb/XXE)
    try:
        root = ET.fromstring(data[:SITEMAP_MAX_BYTES])
    except ET.ParseError:
        return [], []
    tag = root.tag.lower()
    children, pages = [], []
    if tag.endswith("sitemapindex"):
        for sm in root:
            loc = next((c.text for c in sm if c.tag.endswith("loc")), None)
            if loc and loc.strip():
                children.append(loc.strip())
    elif tag.endswith("urlset"):
        for u in root:
            loc = next((c.text for c in u if c.tag.endswith("loc")), None)
            if loc and loc.strip():
                pages.append(loc.strip())
    return children, pages


def _sitemap_urls(url, deadline):
    """All page URLs inside one sitemap file (following sitemap indexes)."""
    if url in _sitemap_cache and time.time() - _sitemap_cache[url][0] < SITEMAP_CACHE_SECONDS:
        return _sitemap_cache[url][1]
    pages, files, seen = [], [url], set()
    while files and len(seen) < SITEMAP_MAX_FILES and time.time() < deadline:
        sm = files.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        try:
            data = _fetch_text(sm, SITEMAP_MAX_BYTES)
        except Exception:
            continue                            # one bad file never kills the rest
        children, found = _parse_sitemap(data)
        files += [c for c in children if c not in seen][:SITEMAP_MAX_FILES - len(seen)]
        pages += found[:max(0, SITEMAP_MAX_URLS - len(pages))]
        if len(pages) >= SITEMAP_MAX_URLS:
            break
    if len(_sitemap_cache) > 200:
        _sitemap_cache.clear()
    _sitemap_cache[url] = (time.time(), pages)
    return pages


def sitemap_links(site_url, extract_links):
    """Discover candidate destinations from robots.txt + sitemap files.

    `extract_links` is app's own HTML link extractor; sitemap URLs carry no
    text, so the visible label is derived from the URL path (last segment).
    Returns a list of links in the app's existing record shape.
    """
    base = urlparse(site_url)
    origin = f"{base.scheme}://{base.netloc}"
    root_host = base.netloc.replace("www.", "")
    deadline = time.time() + SITEMAP_TIMEOUT * (SITEMAP_MAX_FILES + 1)

    sitemaps = []
    try:
        robots = _fetch_text(urljoin(origin, "/robots.txt"), ROBOTS_MAX_BYTES)
        sitemaps += re.findall(r"(?im)^sitemap:\s*(\S+)", robots.decode("utf-8", "ignore"))
    except Exception:
        pass                                    # no robots.txt is normal
    if not sitemaps:
        sitemaps = [urljoin(origin, "/sitemap.xml")]   # conventional location

    found, seen = [], set()
    for sm in sitemaps[:SITEMAP_MAX_FILES]:
        if time.time() > deadline:
            break
        try:
            sm = security.check_url(sm)
            locs = _sitemap_urls(sm, deadline)
        except Exception:
            continue
        for loc in locs:
            loc = canonicalize(loc)
            if not loc or loc in seen or not _same_site(loc, root_host):
                continue
            try:
                security.check_url(loc)         # sitemap content is untrusted
            except Exception:
                continue
            seen.add(loc)
            slug = next((s for s in urlparse(loc).path.split("/") if s), "")
            label = re.sub(r"[-_+]", " ", slug).strip() or root_host
            found.append({"text": label.title(), "name": label.title(),
                          "url": loc, "nav": False, "internal": True,
                          "source": "sitemap"})
    return found


# ------------------------- depth-1 exploration ------------------------------
def explore_pages(final_url, links, fetch_page, extract_links, deadline_s=45):
    """Fetch the few most promising internal pages (top nav links) once and
    extract their links, so destinations one click inside a section are found.

    Bounded: at most EXPLORE_MAX_PAGES pages, EXPLORE_MAX_LINKS_PER_PAGE links
    each, and a total time deadline. Each fetched page goes through the same
    fetch_page() guard as the initial page (SSRF + redirect + size limits).
    New links keep `depth=1` and `source='explore'` plus their parent nav label
    as context, e.g. "Robotics Competition > Register".
    """
    root_host = urlparse(final_url).netloc.replace("www.", "")
    known = {l["url"] for l in links}
    picks, seen = [], set()
    # nav links first, then other internal pages, up to the page budget
    for cand in (l for l in links if l.get("nav")), \
                (l for l in links if not l.get("nav")):
        for l in cand:
            if len(picks) >= EXPLORE_MAX_PAGES:
                break
            if l.get("internal") and l["url"] not in seen:
                seen.add(l["url"])
                picks.append(l)

    out = []
    deadline = time.time() + deadline_s
    for parent in picks:
        if time.time() > deadline:
            break
        try:
            page_url, html = fetch_page(parent["url"])
        except Exception:
            continue                            # this page failed; keep the rest
        _, sub, _ = extract_links(page_url, html)
        for l in sub[:EXPLORE_MAX_LINKS_PER_PAGE]:
            url = canonicalize(l["url"])
            if not url or url in known or not _same_site(url, root_host):
                continue
            known.add(url)
            out.append({**l, "url": url, "depth": 1,
                        "context": parent["text"],   # cheap local enrichment
                        "source": "explore"})
    return out
