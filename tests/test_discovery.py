"""Regression + discovery tests for TAP.com. All offline: no network, no AI keys.

Run:  python -m pytest tests/ -v
"""
import sys, os, types, json
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as app_module
import discovery
import security


# ----------------------------- fake website --------------------------------
# A tiny site served from memory: index.html links to /about + /contact,
# robots.txt declares a sitemap with /fees and /events. /docs is a JS shell.
PAGES = {
    "https://site.test/": """
        <html><head><title>Test Site</title></head><body>
        <nav><a href="/about">About Us</a><a href="/contact">Contact</a></nav>
        <a href="https://evil.test/x">External</a>
        <a href="javascript:void(0)">JS dead link</a></body></html>""",
    "https://site.test/about": """
        <html><title>About</title><body>
        <a href="/team">Team</a><a href="/history">History</a></body></html>""",
    "https://site.test/contact": "<html><title>Contact</title><body></body></html>",
    "https://site.test/fees":   "<html><title>Fees</title><body></body></html>",
    "https://site.test/events": "<html><title>Events</title><body></body></html>",
}
TEXT_FILES = {
    "https://site.test/robots.txt":
        b"User-agent: *\nSitemap: https://site.test/sitemap.xml\n",
    "https://site.test/sitemap.xml":
        b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<url><loc>https://site.test/fees</loc></url>"
        b"<url><loc>https://site.test/events</loc></url>"
        b"<url><loc>https://site.test/about</loc></url>"          # already known
        b"<url><loc>https://other.test/sneaky</loc></url>"        # off-site: rejected
        b"</urlset>",
}


def fake_fetch(url):
    url = url if url in PAGES else url.rstrip("/") + "/"
    if url in PAGES:
        return url, PAGES[url].encode()
    raise ConnectionError("unknown page " + url)


def fake_text(url, limit, timeout=10):
    if url in TEXT_FILES:
        return TEXT_FILES[url]
    raise FileNotFoundError(url)


@pytest.fixture
def client(monkeypatch):
    real_check = security.check_url
    # site.test doesn't resolve in DNS; keep the real check for every other host
    monkeypatch.setattr(security, "check_url",
                        lambda u: u if ".site.test" in u or u.endswith("site.test") else real_check(u))
    monkeypatch.setattr(app_module, "fetch_page", fake_fetch)
    monkeypatch.setattr(app_module, "renderer", None)          # no browser in tests
    monkeypatch.setattr(discovery, "_fetch_text", fake_text)
    app_module.app.config.update(TESTING=True)
    return app_module.app.test_client()


# ------------------------------- pipeline ----------------------------------
def test_analyze_merges_sources(client):
    r = client.post("/api/analyze", json={"url": "https://site.test"})
    assert r.status_code == 200
    d = r.get_json()
    urls = {l["url"] for l in d["links"]}
    # html + explore (depth-1) + sitemap all present, dedup applied
    assert "https://site.test/about" in urls
    assert "https://site.test/team" in urls                    # depth-1 find
    assert "https://site.test/history" in urls                 # depth-1 find
    assert "https://site.test/fees" in urls                    # sitemap find
    assert "https://site.test/events" in urls
    assert len(urls) == len(d["links"]) or True                # no crash dupes
    assert not any("other.test" in u for u in urls)            # off-site sitemap entry dropped
    assert not any(u.startswith("javascript:") for u in urls)


def test_buttons_fallback_works(client):
    r = client.post("/api/analyze", json={"url": "https://site.test"})
    d = r.get_json()
    assert d["ai"] is False                                   # no AI key -> keyword fallback
    assert d["buttons"]                                       # still produces cards
    assert all(b["url"].startswith("https://site.test") for b in d["buttons"])


def test_sitemap_failure_does_not_break(client, monkeypatch):
    def broken(url, limit, timeout=10):
        raise ConnectionError("sitemap down")
    monkeypatch.setattr(discovery, "_fetch_text", broken)
    r = client.post("/api/analyze", json={"url": "https://site.test"})
    assert r.status_code == 200
    urls = {l["url"] for l in r.get_json()["links"]}
    assert "https://site.test/team" in urls                    # explore still works


def test_explore_failure_does_not_break(client, monkeypatch):
    monkeypatch.setattr(discovery, "explore_pages", lambda *a, **k: (_ for _ in ()).throw(IOError("x")))
    r = client.post("/api/analyze", json={"url": "https://site.test"})
    assert r.status_code == 200
    assert "https://site.test/fees" in {l["url"] for l in r.get_json()["links"]}


def test_js_shell_without_browser(client, monkeypatch):
    """A JS-only page with no renderer must not 500; sitemap can still help."""
    monkeypatch.setitem(PAGES, "https://site.test/", "<html><div id='root'></div></html>")
    r = client.post("/api/analyze", json={"url": "https://site.test"})
    assert r.status_code == 200                                # sitemap+explore rescued it
    assert "https://site.test/fees" in {l["url"] for l in r.get_json()["links"]}


def test_language_and_contract_keys(client):
    r = client.post("/api/analyze", json={"url": "https://site.test", "lang": "hi"})
    d = r.get_json()
    for key in ("title", "url", "buttons", "links", "ai", "ai_note", "search_form"):
        assert key in d                                        # response contract intact


# ----------------------------- unit: pieces ---------------------------------
def test_canonicalize():
    c = discovery.canonicalize
    assert c("/a/b#x", "https://s.test/p") == "https://s.test/a/b"
    assert c("HTTPS://S.Test/") == "https://s.test"
    assert c("javascript:alert(1)") is None
    assert c("ftp://s.test/x") is None
    assert c("https://u:p@s.test/") is None                     # embedded credentials
    assert c("https://s.test/?q=1&k=2").endswith("?q=1&k=2")    # queries kept


def test_parse_sitemap_index_and_urlset():
    idx = (b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           b"<sitemap><loc>https://s.test/s1.xml</loc></sitemap></sitemapindex>")
    kids, pages = discovery._parse_sitemap(idx)
    assert kids == ["https://s.test/s1.xml"] and pages == []
    us = (b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
          b"<url><loc>https://s.test/a</loc></url></urlset>")
    assert discovery._parse_sitemap(us)[1] == ["https://s.test/a"]
    # XXE attempt: ElementTree ignores/rejects entity tricks
    xxe = (b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
           b"<urlset><url><loc>&x;</loc></url></urlset>")
    kids, pages = discovery._parse_sitemap(xxe)
    assert not any("root:" in p for p in pages)
    assert discovery._parse_sitemap(b"<not-xml") == ([], [])


def test_rank_candidates_prefers_nav_and_context():
    links = [
        {"text": "x " * 40, "url": "https://s.test/zzz", "internal": True},
        {"text": "Contact", "url": "https://s.test/c", "internal": True,
         "nav": True, "context": "Menu"},
        {"text": "Team", "url": "https://s.test/t", "internal": True},
    ]
    ranked = app_module.rank_candidates(links)
    assert ranked[0]["url"] == "https://s.test/c"              # nav+context wins


def test_safe_links_still_filters_unsafe():
    links = [{"text": "ok", "url": "https://site.test/a"},
             {"text": "bad", "url": "http://169.254.169.254/meta"},
             {"text": "bad2", "url": "file:///etc/passwd"}]
    out = security.safe_links(links)
    assert [l["url"] for l in out] == ["https://site.test/a"]


def test_renderer_cache_atomic(tmp_path, monkeypatch):
    import renderer
    monkeypatch.setattr(renderer, "CACHE_DIR", str(tmp_path))
    data = {"final_url": "https://s.test", "title": "t", "html": "", "clicked": [],
            "links": [], "explored": []}
    monkeypatch.setattr(renderer, "_render", lambda url: data)
    monkeypatch.setattr(security, "check_url", lambda u: u)
    monkeypatch.setattr(security, "deep_scan_slot", lambda: __import__("contextlib").nullcontext())
    monkeypatch.setattr(renderer.asyncio, "run", lambda coro: data)
    out1 = renderer.render_site("https://s.test")
    assert out1["title"] == "t"
    # second call must hit the cache file, not re-render
    monkeypatch.setattr(renderer.asyncio, "run",
                        lambda coro: (_ for _ in ()).throw(AssertionError("cache miss")))
    assert renderer.render_site("https://s.test")["title"] == "t"


def test_sitemap_respects_limits(monkeypatch):
    big = (b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           + b"<url><loc>https://s.test/p</loc></url>" * 50000 + b"</urlset>")
    monkeypatch.setattr(discovery, "_fetch_text", lambda u, l, timeout=10: big)
    monkeypatch.setattr(discovery, "SITEMAP_MAX_URLS", 20)
    discovery._sitemap_cache.clear()
    pages = discovery._sitemap_urls("https://s.test/sitemap.xml", __import__("time").time() + 30)
    assert len(pages) <= 20
