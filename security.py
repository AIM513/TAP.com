"""
Security layer for Simple UI Generator (TAP.com).

Kept in its own file so app.py / renderer.py keep working exactly as before;
they only call a few helpers from here.

What it does
- SSRF protection: never fetch private / internal addresses (localhost, LAN,
  cloud metadata 169.254.169.254, ...). Checked before the request, on every
  redirect, on the IP actually connected to, and for the deep-scan browser
  (all its traffic goes through a small checking proxy, redirects included).
- Only http(s) links are sent to the page (no javascript:/data: links -> no XSS).
- Checks and trims everything the browser sends to /api/analyze and /api/ask.
- Per-IP rate limits, request size limit, page size limit, max parallel deep scans.
- Security headers: Content-Security-Policy with a per-request nonce, no
  framing (clickjacking), no MIME sniffing, no referrer, locked-down permissions.
- API calls from other websites are refused (Origin / Sec-Fetch-Site check).

Settings (environment variables or .env):
  ALLOW_PRIVATE_URLS=1   allow localhost / LAN addresses (only for local testing)
  TRUST_PROXY=1          running behind a reverse proxy (nginx, Render, ...): use X-Forwarded-For
  RATE_ANALYZE=12/60     max /api/analyze calls per IP per N seconds
  RATE_DEEP=4/600        max "look deeper" scans per IP per N seconds
  RATE_ASK=40/60         max /api/ask calls per IP per N seconds
  MAX_DEEP_SCANS=2       deep scans (headless Chrome) running at once on the whole server
"""
import ipaddress
import os
import re
import secrets
import socket
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from urllib.parse import urljoin, urlparse

import requests
from flask import g, jsonify, request

MAX_REQUEST_BYTES = 4 * 1024 * 1024    # JSON body sent by the browser
MAX_PAGE_BYTES = 8 * 1024 * 1024       # HTML downloaded from a website
MAX_FETCH_SECONDS = 40                 # total time to download one page
MAX_URL_CHARS = 2048
MAX_QUERY_CHARS = 120
MAX_TEXT_CHARS = 300
MAX_LINKS = 2000
DNS_CACHE_SECONDS = 60

BAD_URL_MSG = "Please type a normal website address, like example.com."
PRIVATE_URL_MSG = "That address points to a private or internal network, so TAP.com will not open it."
RATE_MSG = "Too many requests from your computer. Please wait {s} seconds and try again."


class UnsafeURL(ValueError):
    """The address may not be opened (not http/https, or it points inside a private network)."""


def _flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


# ----------------------------- SSRF protection -----------------------------
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_6TO4 = ipaddress.ip_network("2002::/16")


def ip_is_public(ip):
    ip = ipaddress.ip_address(ip)
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        elif ip in _NAT64:
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        elif ip.sixtofour:
            ip = ip.sixtofour
    return ip.is_global and not ip.is_multicast


_dns_cache, _dns_lock = {}, threading.Lock()


def resolve(host):
    now = time.time()
    with _dns_lock:
        hit = _dns_cache.get(host)
        if hit and now - hit[0] < DNS_CACHE_SECONDS:
            return hit[1]
    try:
        ips = sorted({i[4][0].split("%")[0] for i in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)})
    except (socket.gaierror, UnicodeError, OSError):
        ips = []    # does not exist: the normal "site not found" message will be shown
    with _dns_lock:
        if len(_dns_cache) > 5000:
            _dns_cache.clear()
        _dns_cache[host] = (now, ips)
    return ips


def host_is_safe(host):
    if _flag("ALLOW_PRIVATE_URLS"):
        return True
    host = (host or "").strip("[]").rstrip(".").lower()
    if not host:
        return False
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".home.arpa")):
        return False
    try:
        return ip_is_public(host)
    except ValueError:
        pass
    return all(ip_is_public(ip) for ip in resolve(host))


def safe_url(url):
    """True for a normal http(s) address (used for links shown to the user)."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL_CHARS:
        return False
    if re.search(r"[\x00-\x20\x7f]", url):
        return False
    p = urlparse(url)
    return p.scheme.lower() in ("http", "https") and bool(p.netloc) and "\\" not in p.netloc


def check_url(url):
    """Raise UnsafeURL unless `url` is a public http(s) address the server may fetch."""
    if not safe_url(url):
        raise UnsafeURL(BAD_URL_MSG)
    p = urlparse(url)
    try:
        p.port
    except ValueError:
        raise UnsafeURL(BAD_URL_MSG) from None
    if not p.hostname or not host_is_safe(p.hostname):
        raise UnsafeURL(PRIVATE_URL_MSG)
    return url


def _peer_ip(resp):
    """IP address the response really came from (catches DNS-rebinding tricks)."""
    if requests.utils.get_environ_proxies(resp.url):
        return None     # through a proxy we only see the proxy
    for path in (("_connection", "sock"), ("_fp", "fp", "raw", "_sock")):
        obj = resp.raw
        try:
            for attr in path:
                obj = getattr(obj, attr)
            return obj.getpeername()[0]
        except (AttributeError, OSError, TypeError):
            continue
    return None


def _response_guard(resp, *args, **kwargs):
    try:
        ip = _peer_ip(resp)
        if ip and not _flag("ALLOW_PRIVATE_URLS") and not ip_is_public(ip.split("%")[0]):
            raise UnsafeURL(PRIVATE_URL_MSG)
        if resp.is_redirect:     # check where it redirects *before* it is followed
            target = requests.Session().get_redirect_target(resp)
            check_url(urljoin(resp.url, target or ""))
    except UnsafeURL:
        resp.close()
        raise
    return resp


# Extra arguments for requests.get(...) calls that download user-chosen websites.
REQUEST_GUARD = {"hooks": {"response": [_response_guard]}, "stream": True}


def read_body(resp, limit=MAX_PAGE_BYTES):
    """Download the page body with a size and time limit."""
    deadline = time.monotonic() + MAX_FETCH_SECONDS
    chunks, size = [], 0
    try:
        for chunk in resp.iter_content(64 * 1024):
            chunks.append(chunk)
            size += len(chunk)
            if size >= limit:
                break       # enough HTML to find the links
            if time.monotonic() > deadline:
                raise requests.exceptions.Timeout("page download took too long")
    finally:
        resp.close()
    return b"".join(chunks)[:limit]


async def guard_browser(context):
    """Stop the deep-scan browser from loading anything on a private address."""
    import asyncio
    loop = asyncio.get_running_loop()

    async def check(route):
        p = urlparse(route.request.url)
        if p.scheme in ("http", "https", "ws", "wss"):
            ok = bool(p.hostname) and await loop.run_in_executor(None, host_is_safe, p.hostname)
            if not ok:
                await route.abort("blockedbyclient")
                return
        await route.fallback()

    await context.route("**/*", check)   # registered last -> runs before the other routes


def _pick_ip(host):
    """Resolve `host` once, refuse private addresses, return the IP to connect to."""
    host = (host or "").strip("[]").rstrip(".").lower()
    try:
        ips = [str(ipaddress.ip_address(host))]
    except ValueError:
        ips = resolve(host)
    if not ips:
        raise OSError(f"cannot resolve {host}")
    if not _flag("ALLOW_PRIVATE_URLS"):
        if not host_is_safe(host) or not all(ip_is_public(ip) for ip in ips):
            raise UnsafeURL(PRIVATE_URL_MSG)
    return sorted(ips, key=lambda ip: ":" in ip)[0]     # IPv4 first


class _BrowserProxy:
    """Tiny local proxy for the deep-scan browser. Every connection the browser
    makes (pages, redirects, scripts, websockets) goes through it, and it only
    connects to public IP addresses - the one it checked, so DNS tricks don't work."""

    def __init__(self):
        self.port, ready = None, threading.Event()
        threading.Thread(target=self._run, args=(ready,), daemon=True, name="browser-guard-proxy").start()
        ready.wait(10)

    def _run(self, ready):
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        server = loop.run_until_complete(asyncio.start_server(self._client, "127.0.0.1", 0))
        self.port = server.sockets[0].getsockname()[1]
        ready.set()
        loop.run_forever()

    @staticmethod
    async def _pipe(reader, writer):
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()

    async def _client(self, reader, writer):
        import asyncio
        replied = False
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
            line, _, rest = head.partition(b"\r\n")
            method, target, version = line.decode("latin-1").split(" ", 2)
            if method.upper() == "CONNECT":
                host, _, port = target.rpartition(":")
                port = int(port)
            else:
                p = urlparse(target)
                if p.scheme != "http" or not p.hostname:
                    raise UnsafeURL(BAD_URL_MSG)
                host, port = p.hostname, p.port or 80
                path = (p.path or "/") + (f"?{p.query}" if p.query else "")
            ip = await asyncio.get_running_loop().run_in_executor(None, _pick_ip, host)
            up_reader, up_writer = await asyncio.wait_for(asyncio.open_connection(ip, port), 20)
            if method.upper() == "CONNECT":
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            else:
                skip = (b"proxy-connection:", b"connection:", b"keep-alive:", b"proxy-authorization:")
                headers = [h for h in rest.split(b"\r\n") if h and not h.lower().startswith(skip)]
                up_writer.write(f"{method} {path} {version}\r\n".encode("latin-1")
                                + b"\r\n".join(headers + [b"Connection: close"]) + b"\r\n\r\n")
            replied = True
            await asyncio.gather(self._pipe(reader, up_writer), self._pipe(up_reader, writer),
                                 return_exceptions=True)
        except Exception as e:
            if not replied:
                code = b"403 Forbidden" if isinstance(e, UnsafeURL) else b"502 Bad Gateway"
                try:
                    writer.write(b"HTTP/1.1 " + code + b"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                    await writer.drain()
                except Exception:
                    pass
        finally:
            writer.close()


_proxy, _proxy_lock = None, threading.Lock()


def browser_launch_options():
    """Extra arguments for chromium.launch(...): send all browser traffic through the guard proxy."""
    global _proxy
    with _proxy_lock:
        if _proxy is None:
            _proxy = _BrowserProxy()
    # "<-loopback>" stops Chrome from skipping the proxy for localhost addresses
    return {"proxy": {"server": f"http://127.0.0.1:{_proxy.port}", "bypass": "<-loopback>"}}


_deep_slots, _deep_slots_lock = None, threading.Lock()


@contextmanager
def deep_scan_slot(wait_s=120):
    """Allow only MAX_DEEP_SCANS headless browsers at the same time."""
    global _deep_slots
    with _deep_slots_lock:
        if _deep_slots is None:
            _deep_slots = threading.BoundedSemaphore(max(1, int(os.environ.get("MAX_DEEP_SCANS", 2))))
    if not _deep_slots.acquire(timeout=wait_s):
        raise RuntimeError("too many deep scans running, try again soon")
    try:
        yield
    finally:
        _deep_slots.release()


# ----------------------------- input / output cleaning ---------------------
def _text(value, limit=MAX_TEXT_CHARS):
    if not isinstance(value, str):
        return ""
    return re.sub(r"[\x00-\x1f\x7f]+", " ", value)[:limit]


def safe_links(items):
    """Drop anything whose url is not a normal http(s) link (e.g. javascript:, data:),
    plus links to literal private/reserved IP addresses (e.g. 169.254.169.254)."""
    out = []
    for i in items or []:
        if not (isinstance(i, dict) and safe_url(i.get("url"))):
            continue
        host = (urlparse(i["url"]).hostname or "").strip("[]").lower()
        try:
            if not ip_is_public(host):      # private-IP literal: never shown to the user
                continue
        except ValueError:
            pass                            # hostname: DNS policy applied when we fetch it
        out.append(i)
    return out


def safe_search_form(form):
    if (isinstance(form, dict) and safe_url(form.get("action"))
            and isinstance(form.get("param"), str) and 0 < len(form["param"]) <= 100):
        return {"action": form["action"], "param": form["param"]}
    return None


def clean_analyze_payload(data):
    data = data if isinstance(data, dict) else {}
    url = data.get("url")
    return {"url": url[:MAX_URL_CHARS] if isinstance(url, str) else "", "deep": data.get("deep") is True}


def clean_ask_payload(data):
    data = data if isinstance(data, dict) else {}
    links = []
    raw = data.get("links") if isinstance(data.get("links"), list) else []
    for l in raw[:MAX_LINKS]:
        if isinstance(l, dict) and safe_url(l.get("url")) and isinstance(l.get("text"), str):
            links.append({"text": _text(l["text"]), "name": _text(l.get("name")) or _text(l["text"]),
                          "url": l["url"], "context": _text(l.get("context")),
                          "nav": bool(l.get("nav")), "internal": bool(l.get("internal"))})
    site = data.get("url")
    return {"query": " ".join(_text(data.get("query"), MAX_QUERY_CHARS).split()),
            "links": links, "url": site if safe_url(site) else "",
            "search_form": safe_search_form(data.get("search_form"))}


# ----------------------------- rate limiting -------------------------------
class RateLimiter:
    def __init__(self):
        self.hits, self.lock, self.swept = defaultdict(deque), threading.Lock(), time.time()

    def hit(self, key, limit, window):
        """Count one request; returns 0 if allowed, else seconds to wait."""
        now = time.time()
        with self.lock:
            q = self.hits[key]
            while q and now - q[0] > window:
                q.popleft()
            if len(q) >= limit:
                return int(window - (now - q[0])) + 1
            q.append(now)
            if now - self.swept > 300:
                self.swept = now
                for k in [k for k, v in self.hits.items() if not v or now - v[-1] > 3600]:
                    del self.hits[k]
            return 0


limiter = RateLimiter()


def _rate(name, default):
    try:
        limit, window = os.environ.get(name, default).split("/")
        return int(limit), float(window)
    except ValueError:
        limit, window = default.split("/")
        return int(limit), float(window)


# ----------------------------- Flask hooks ---------------------------------
def csp_nonce():
    if "csp_nonce" not in g:
        g.csp_nonce = secrets.token_urlsafe(18)
    return g.csp_nonce


def _same_origin():
    origin = request.headers.get("Origin")
    if origin is not None:
        return urlparse(origin).netloc.lower() == request.host.lower()
    return request.headers.get("Sec-Fetch-Site", "same-origin") in ("same-origin", "none")


def _json_error(message, code, **extra):
    resp = jsonify(error=message, **extra)
    resp.status_code = code
    return resp


def _before_request():
    if not request.path.startswith("/api/"):
        return None
    if request.method != "GET" and not _same_origin():
        return _json_error("Requests from other websites are not allowed.", 403)
    ip = request.remote_addr or "?"
    rules = {"analyze": [("RATE_ANALYZE", "12/60")], "ask": [("RATE_ASK", "40/60")]}.get(request.endpoint, [])
    body = request.get_json(silent=True)
    if request.endpoint == "analyze" and isinstance(body, dict) and body.get("deep") is True:
        rules.append(("RATE_DEEP", "4/600"))
    for name, default in rules:
        wait = limiter.hit(f"{name}:{ip}", *_rate(name, default))
        if wait:
            msg = RATE_MSG.format(s=wait)
            resp = (_json_error(msg, 429, results=[], source="keywords", ai_note=msg)
                    if request.endpoint == "ask" else _json_error(msg, 429))
            resp.headers["Retry-After"] = str(wait)
            return resp
    return None


def _security_headers(resp):
    nonce = csp_nonce()
    resp.headers.setdefault("Content-Security-Policy", "; ".join([
        "default-src 'self'",
        f"script-src 'self' 'nonce-{nonce}'",
        f"style-src 'self' 'nonce-{nonce}' https://fonts.googleapis.com",
        "font-src 'self' https://fonts.gstatic.com",
        "img-src 'self' data:",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]))
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    resp.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    resp.headers.setdefault("Permissions-Policy",
                            "camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()")
    if request.path.startswith("/api/"):
        resp.headers.setdefault("Cache-Control", "no-store")
    if request.is_secure:
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return resp


def install(app):
    """Turn on all protections for the Flask app."""
    if not app.config.get("MAX_CONTENT_LENGTH"):
        app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES
    if _flag("TRUST_PROXY"):
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.before_request(_before_request)
    app.after_request(_security_headers)
    app.context_processor(lambda: {"csp_nonce": csp_nonce})
    app.register_error_handler(413, lambda e: _json_error("That request is too big.", 413))
    app.register_error_handler(415, lambda e: _json_error("Please send JSON.", 415))
    app.register_error_handler(400, lambda e: _json_error("Bad request.", 400))
    return app
