"""
Deep scan for JavaScript-heavy websites (React/Angular menus, buttons and menu
items that have no real link). Many government portals are built like this.

1. Open the site in a real (headless) Chrome, close pop-ups, open every menu.
2. Collect menu items / buttons that have no link.
3. Click each one in a fresh tab and record where it goes (new URL, new tab or
   downloaded file). Those become normal links the app can turn into buttons.

Setup:  pip install playwright  &&  python -m playwright install chromium
"""
import asyncio
import hashlib
import json
import os
import time

import security      # blocks private / internal addresses, limits parallel scans

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
CACHE_HOURS = float(os.environ.get("RENDER_CACHE_HOURS", 24))   # result cache lifetime
MAX_CLICKS = int(os.environ.get("RENDER_MAX_CLICKS", 45))       # link-less items to try clicking
PARALLEL = int(os.environ.get("RENDER_PARALLEL", 6))           # tabs open at the same time
CLICK_WAIT_S = 5     # how long to wait for something to happen after a click
MAX_TOTAL_S = int(os.environ.get("RENDER_MAX_SECONDS", 180))   # hard deadline for one scan
EXPLORE_PAGES = int(os.environ.get("RENDER_EXPLORE_PAGES", 3)) # internal nav pages also visited

HELPERS_JS = r"""
window.__cssPath = el => {
  const parts = [];
  while (el && el.nodeType === 1 && el !== document.body) {
    let i = 1, s = el;
    while ((s = s.previousElementSibling)) if (s.tagName === el.tagName) i++;
    parts.unshift(el.tagName.toLowerCase() + ':nth-of-type(' + i + ')');
    el = el.parentElement;
  }
  return 'body > ' + parts.join(' > ');
};
window.__own = el => [...el.childNodes]
  .filter(n => n.nodeType === 3 || (n.nodeType === 1 && !n.matches('ul,ol,.dropdown-menu,[role=menu],svg,script,style')))
  .map(n => n.textContent).join(' ').replace(/\s+/g, ' ').trim();
"""

DISMISS_JS = r"""() => {
  const closeSel = '.modal.show .btn-close, .modal.show [data-dismiss], .modal.show [data-bs-dismiss],' +
                   '[role=dialog] [aria-label="Close" i], [aria-label="close" i]';
  document.querySelectorAll(closeSel).forEach(b => { try { b.click(); } catch (e) {} });
  const okWords = /^(ok|okay|close|accept|accept all|i agree|agree|got it|continue|×|x)$/i;
  document.querySelectorAll('[role=dialog] button, .modal button, [class*=popup] button, [class*=cookie] button')
    .forEach(b => { if (okWords.test((b.innerText || '').trim())) { try { b.click(); } catch (e) {} } });
}"""

OPENERS_JS = r"""() => [...document.querySelectorAll(
    '.dropdown-toggle, [aria-haspopup="true"], [aria-haspopup="menu"], [data-toggle="dropdown"], [data-bs-toggle="dropdown"], nav [aria-expanded="false"], header [aria-expanded="false"]')]
  .filter(e => e.offsetWidth || e.offsetHeight)
  .slice(0, 20)
  .map(e => ({path: __cssPath(e), text: __own(e) || e.getAttribute('title') || ''}))"""

CANDIDATES_JS = r"""([openerPath, openerText]) => {
  const SKIP = /^(view more|more|next|prev|previous|close|ok|cancel|×|x|skip to main content|go to top|back to top|menu|toggle navigation|search|submit|\d+|«|»|‹|›|<|>|show more|load more)$/i;
  const sel = 'button, [role=button], [role=menuitem], [role=tab], [role=link], [onclick], a:not([href]), ' +
              'a[href="#"], a[href^="javascript"], li[tabindex], .dropdown-menu li, [role=menu] li, nav li, header li';
  const realLink = 'a[href]:not([href="#"]):not([href^="javascript"])';
  const out = [];
  for (const el of document.querySelectorAll(sel)) {
    if (el.closest(realLink)) continue;                                  // normal links are read from HTML
    if (el.matches('.dropdown-toggle, [aria-haspopup="true"], [aria-haspopup="menu"]')) continue;
    if (el.querySelector('ul,ol,.dropdown-menu,[role=menu]')) continue;  // opens a sub-menu
    if (el.tagName === 'LI' && el.querySelector('a, button')) continue;  // the inner a/button is used
    if (el.closest('[role=dialog], .modal, form')) continue;
    const text = (__own(el) || el.getAttribute('aria-label') || el.getAttribute('title') || '')
                   .replace(/^[|•·\s]+|[|•·\s]+$/g, '');
    if (!text || text.length > 60 || SKIP.test(text)) continue;
    const ctx = [];
    for (let p = el.parentElement; p && p !== document.body; p = p.parentElement)
      if (p.tagName === 'LI') {            // parent menu entries, e.g. "Apply For"
        const t = __own(p);
        if (t && t.length <= 40 && !(openerText && t.startsWith(openerText))) ctx.unshift(t);
      }
    if (openerText) ctx.unshift(openerText);
    out.push({path: __cssPath(el), text, context: ctx.join(' > '), opener: openerPath,
              nav: !!el.closest('nav, header, [role=navigation], .navbar, .dropdown-menu, [role=menu]')});
  }
  return out;
}"""

LINKS_JS = r"""() => [...document.querySelectorAll('a[href]')]
  .filter(a => (a.offsetWidth || a.offsetHeight) && (a.innerText || '').trim())
  .slice(0, 400)
  .map(a => ({text: (a.innerText || '').replace(/\s+/g, ' ').trim(), href: a.href,
              nav: !!a.closest('nav, header, [role=navigation], .navbar, [role=menu]')}))"""


CLICK_JS = r"""async ([opener, path, text]) => {
  if (opener) { const o = document.querySelector(opener); if (o) o.click();
                await new Promise(r => setTimeout(r, 500)); }
  let el = document.querySelector(path);
  if (!el || __own(el) !== text)   // page changed a bit: find the item by its text instead
    el = [...document.querySelectorAll('li, button, a, [role=button], [role=menuitem]')]
           .find(e => __own(e) === text) || el;
  if (!el) return false;
  el.click();
  return true;
}"""


def _strip_hash(url):
    return (url or "").split("#")[0].rstrip("/")


async def _open(page, url):
    from playwright.async_api import TimeoutError as PWTimeout
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except PWTimeout:
        pass                                # keep whatever DOM loaded - partial result
    # Wait until the control count settles (lazy JS navs build menus late),
    # not for full network idle which never fires on ad/analytics-heavy sites.
    try:
        last, stable = -1, 0
        for _ in range(24):                 # <= 12 s
            n = await page.evaluate(
                "() => document.querySelectorAll('a[href],button,[role=menuitem],[role=tab]').length")
            if n == last:
                stable += 1
                if stable >= 2:
                    break
            else:
                last, stable = n, 0
            await page.wait_for_timeout(500)
        await page.evaluate("() => window.scrollBy(0, 800)")  # trigger lazy nav sections
        await page.wait_for_timeout(700)
    except Exception:
        pass
    try:
        await page.evaluate(DISMISS_JS)
        await page.keyboard.press("Escape")
    except Exception:
        pass


async def _follow(context, url, cand, sem):
    """Click one link-less item in a fresh tab and see where it leads."""
    async with sem:
        page = await context.new_page()
        popups, downloads = [], []
        page.on("popup", lambda p: popups.append(p))
        page.on("download", lambda d: downloads.append(d.url))
        try:
            await _open(page, url)
            start = _strip_hash(page.url)
            try:
                await page.evaluate(CLICK_JS, [cand["opener"], cand["path"], cand["text"]])
            except Exception:
                pass  # page navigated while the script ran - that's what we want
            target = None
            for _ in range(int(CLICK_WAIT_S / 0.25)):
                await asyncio.sleep(0.25)
                if popups:
                    try:
                        await popups[0].wait_for_load_state("domcontentloaded", timeout=10000)
                    except Exception:
                        pass
                    target = popups[0].url
                    break
                if downloads:
                    target = downloads[0]
                    break
                if _strip_hash(page.url) != start:
                    target = page.url
                    break
            if target and target.startswith("http") and _strip_hash(target) != start:
                return {**cand, "url": target}
        except Exception:
            pass
        finally:
            for p in popups:
                await p.close()
            await page.close()
        return None


async def _render(url):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--disable-http2"],  # some govt CDNs break on HTTP/2
                                           **security.browser_launch_options())
        context = await browser.new_context(
            viewport={"width": 1366, "height": 900}, accept_downloads=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
        await context.add_init_script(HELPERS_JS)
        await context.route("**/*", lambda r: r.abort() if r.request.resource_type in
                            ("image", "media", "font") else r.continue_())
        await security.guard_browser(context)

        page = await context.new_page()
        await _open(page, url)
        final_url = page.url
        candidates = await page.evaluate(CANDIDATES_JS, [None, None])
        known = {c["path"] for c in candidates}
        for op in await page.evaluate(OPENERS_JS):   # open each menu, read its new items
            try:
                await page.evaluate("p => document.querySelector(p)?.click()", op["path"])
                await page.wait_for_timeout(500)
                for c in await page.evaluate(CANDIDATES_JS, [op["path"], op["text"]]):
                    if c["path"] not in known:
                        known.add(c["path"])
                        candidates.append(c)
                await page.keyboard.press("Escape")
            except Exception:
                continue
        html = await page.content()
        title = await page.title()
        anchors = await page.evaluate(LINKS_JS)
        await page.close()

        seen, todo = set(), []
        for c in sorted(candidates, key=lambda c: not c["nav"]):
            key = (c["text"].lower(), c["context"].lower())
            if key not in seen:
                seen.add(key)
                todo.append(c)
        todo = todo[:MAX_CLICKS]

        sem = asyncio.Semaphore(PARALLEL)
        tasks = [asyncio.create_task(_follow(context, final_url, c, sem)) for c in todo]
        done, pending = await asyncio.wait(tasks, timeout=MAX_TOTAL_S)
        for t in pending:                       # hit the scan deadline: keep what finished
            t.cancel()
        results = [t.result() for t in done if not t.cancelled() and t.exception() is None]

        # Bounded depth-1 exploration inside the same browser: JS-only sites
        # (every page an empty app shell) give nothing to plain HTTP fetches,
        # so the top internal nav pages are visited once here.
        from urllib.parse import urlparse as _up
        root = _up(final_url).netloc.replace("www.", "")
        seen_i, picks = {_strip_hash(final_url)}, []
        for a in sorted(anchors, key=lambda a: not a["nav"]):
            if len(picks) >= EXPLORE_PAGES:
                break
            href = a["href"].split("#")[0]
            h = _up(href)
            if (h.scheme not in ("http", "https") or h.netloc.replace("www.", "") != root
                    or _strip_hash(href) in seen_i):
                continue
            seen_i.add(_strip_hash(href))
            picks.append(a)

        async def _visit(parent):
            async with sem:
                p = await context.new_page()
                try:
                    await _open(p, parent["href"])
                    return parent["text"], await p.evaluate(LINKS_JS)
                except Exception:
                    return parent["text"], []
                finally:
                    await p.close()

        explored = []
        visited = await asyncio.gather(*(_visit(a) for a in picks))
        for parent_text, sub in visited:
            for a in sub:
                explored.append({"text": a["text"], "url": a["href"], "nav": a["nav"],
                                 "context": parent_text})
        await browser.close()

    clicked = [{"text": r["text"], "context": r["context"], "url": r["url"], "nav": r["nav"]}
               for r in results if r]
    return {"final_url": final_url, "title": title, "html": html, "clicked": clicked,
            "links": anchors, "explored": explored}


def render_site(url, use_cache=True):
    """Render `url` in headless Chrome. Results are cached for CACHE_HOURS."""
    security.check_url(url)
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, hashlib.sha1(("v2|" + url).encode()).hexdigest() + ".json")
    if use_cache and os.path.exists(path) and time.time() - os.path.getmtime(path) < CACHE_HOURS * 3600:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    with security.deep_scan_slot():
        data = asyncio.run(_render(url))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:   # atomic write: readers never see half a file
        json.dump(data, f)
    os.replace(tmp, path)
    return data


if __name__ == "__main__":   # quick test:  python renderer.py https://www.passportindia.gov.in/psp
    import sys
    t = time.time()
    d = render_site(sys.argv[1], use_cache=False)
    for c in d["clicked"]:
        print(f'{c["context"] + " > " if c["context"] else ""}{c["text"]}  ->  {c["url"]}')
    print(f'{len(d["clicked"])} items in {time.time() - t:.0f}s')
