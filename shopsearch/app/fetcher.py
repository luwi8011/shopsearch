"""Tiered page fetcher.

Tier 1 is a plain HTTP request with a real browser's TLS fingerprint (curl_cffi).
It is fast (~1 s) and gets through most sites, including Amazon and eBay once a
session has cookies from the homepage. Tier 2 is a stealth Chromium (Scrapling /
patchright) that runs JavaScript and solves Cloudflare challenges; it is slow
(5-20 s) and is used only when tier 1 is blocked or the page needs JavaScript.

The tier that worked for each domain is remembered in /data/tiers.json so later
requests go straight to it.
"""

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from curl_cffi.requests import AsyncSession

log = logging.getLogger("shopsearch.fetch")

DATA_DIR = os.environ.get("DATA_DIR", "/data")
TIERS_FILE = os.path.join(DATA_DIR, "tiers.json")
BROWSER_PAGES = int(os.environ.get("BROWSER_PAGES", "3"))
BROWSER_TIMEOUT_S = int(os.environ.get("BROWSER_TIMEOUT_S", "60"))
# Politeness: minimum seconds between requests to the same domain, and how long a
# fetched page is reused. Bursts of requests are what get an IP flagged by bot managers.
MIN_INTERVAL_S = float(os.environ.get("MIN_INTERVAL_S", "2.5"))
CACHE_TTL_S = int(os.environ.get("CACHE_TTL_S", "600"))

# Fingerprints tried in order. Safari first: in testing, eBay and Amazon accepted it
# when Chrome's fingerprint was refused.
IMPERSONATIONS = ["safari", "chrome"]

BLOCK_MARKERS = [
    "<title>Just a moment...</title>",
    "challenges.cloudflare.com",
    "Attention Required! | Cloudflare",
    "/errors/validateCaptcha",
    "Robot Check",
    "Pardon Our Interruption",
    "px-captcha",
    "Request unsuccessful. Incapsula",
    "<title>Access Denied</title>",
    "Sorry! Something went wrong!",
    "verify you are a human",
]


@dataclass
class Page:
    url: str
    status: int
    html: str
    tier: str
    seconds: float
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and self.status < 400 and not looks_blocked(self.status, self.html)


def looks_blocked(status: int, html: str) -> bool:
    if status in (401, 403, 429, 503):
        return True
    if len(html) < 40000:
        low = html[:40000]
        return any(m in low for m in BLOCK_MARKERS)
    return False


def domain_of(url: str) -> str:
    host = urlsplit(url).hostname or ""
    return host[4:] if host.startswith("www.") else host


class Fetcher:
    def __init__(self):
        self._sessions: dict[tuple[str, str], AsyncSession] = {}
        self._warmed: set[tuple[str, str]] = set()
        self._browser = None
        self._browser_lock = asyncio.Lock()
        self._browser_sem = asyncio.Semaphore(BROWSER_PAGES)
        self._tiers: dict[str, str] = {}
        self._domain_locks: dict[str, asyncio.Lock] = {}
        self._last_hit: dict[str, float] = {}
        self._cache: dict[tuple[str, str], tuple[float, Page]] = {}
        try:
            with open(TIERS_FILE) as f:
                self._tiers = json.load(f)
        except Exception:
            pass

    def _remember(self, domain: str, tier: str):
        if self._tiers.get(domain) == tier:
            return
        self._tiers[domain] = tier
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(TIERS_FILE, "w") as f:
                json.dump(self._tiers, f, indent=1, sort_keys=True)
        except Exception as e:
            log.warning("could not save tiers: %s", e)

    async def _pace(self, domain: str):
        """Wait so requests to one domain are at least MIN_INTERVAL_S apart."""
        lock = self._domain_locks.setdefault(domain, asyncio.Lock())
        async with lock:
            wait = self._last_hit.get(domain, 0) + MIN_INTERVAL_S - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_hit[domain] = time.time()

    # ---------------- tier 1: TLS-impersonating HTTP ----------------

    def _session(self, domain: str, imp: str) -> AsyncSession:
        key = (domain, imp)
        if key not in self._sessions:
            self._sessions[key] = AsyncSession(impersonate=imp, timeout=25, allow_redirects=True)
        return self._sessions[key]

    async def _http(self, url: str, imp: str, warm: bool) -> Page:
        domain = domain_of(url)
        s = self._session(domain, imp)
        t = time.time()
        try:
            await self._pace(domain)
            if warm and (domain, imp) not in self._warmed:
                # Many sites (eBay, Amazon) refuse a deep link from a cookieless client
                # but accept it after the homepage has set session cookies.
                parts = urlsplit(url)
                await s.get(f"{parts.scheme}://{parts.netloc}/")
                self._warmed.add((domain, imp))
                await self._pace(domain)
            r = await s.get(url)
            return Page(str(r.url), r.status_code, r.text, f"http:{imp}", time.time() - t)
        except Exception as e:
            return Page(url, 0, "", f"http:{imp}", time.time() - t, error=f"{type(e).__name__}: {str(e)[:150]}")

    # ---------------- tier 2: stealth browser ----------------

    async def _get_browser(self):
        async with self._browser_lock:
            if self._browser is None:
                from scrapling.fetchers import AsyncStealthySession

                profile = os.path.join(DATA_DIR, "browser-profile")
                os.makedirs(profile, exist_ok=True)
                b = AsyncStealthySession(
                    headless=True,
                    solve_cloudflare=True,
                    block_ads=True,
                    disable_resources=True,
                    max_pages=BROWSER_PAGES,
                    user_data_dir=profile,
                    timeout=45000,
                    locale="en-US",
                )
                await b.__aenter__()
                self._browser = b
            return self._browser

    async def _restart_browser(self):
        async with self._browser_lock:
            b, self._browser = self._browser, None
        if b is not None:
            try:
                await b.__aexit__(None, None, None)
            except Exception:
                pass

    async def _browser_fetch(self, url: str, wait_selector: str | None = None) -> Page:
        t = time.time()
        async with self._browser_sem:
            try:
                await self._pace(domain_of(url))
                b = await self._get_browser()
                kw = {"network_idle": False, "wait": 1500}
                if wait_selector:
                    kw["wait_selector"] = wait_selector
                r = await asyncio.wait_for(b.fetch(url, **kw), timeout=BROWSER_TIMEOUT_S)
                html = r.html_content if hasattr(r, "html_content") else r.body.decode("utf-8", "replace")
                return Page(str(r.url), int(r.status), html, "browser", time.time() - t)
            except asyncio.TimeoutError:
                return Page(url, 0, "", "browser", time.time() - t, error=f"browser timed out after {BROWSER_TIMEOUT_S}s")
            except Exception as e:
                msg = f"{type(e).__name__}: {str(e)[:200]}"
                if "closed" in msg.lower() or "disconnected" in msg.lower():
                    await self._restart_browser()
                return Page(url, 0, "", "browser", time.time() - t, error=msg)

    # ---------------- public ----------------

    async def fetch(self, url: str, mode: str = "auto", prefer: str | None = None,
                    wait_selector: str | None = None) -> Page:
        key = (url, mode)
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL_S:
            return hit[1]
        page = await self._fetch(url, mode, prefer, wait_selector)
        if page.ok:
            self._cache[key] = (time.time(), page)
            if len(self._cache) > 300:
                for k, _ in sorted(self._cache.items(), key=lambda kv: kv[1][0])[:100]:
                    self._cache.pop(k, None)
        return page

    async def _fetch(self, url: str, mode: str = "auto", prefer: str | None = None,
                     wait_selector: str | None = None) -> Page:
        """mode: auto | http | browser. prefer: a tier name to try first ("browser" or "http:<imp>")."""
        domain = domain_of(url)
        if mode == "browser":
            return await self._browser_fetch(url, wait_selector)

        learned = self._tiers.get(domain)
        first = prefer or learned
        attempts: list[str] = []
        if first:
            attempts.append(first)
        for imp in IMPERSONATIONS:
            if f"http:{imp}" not in attempts:
                attempts.append(f"http:{imp}")
        if mode == "auto" and "browser" not in attempts:
            attempts.append("browser")
        if mode == "http":
            attempts = [a for a in attempts if a != "browser"]

        last: Page | None = None
        tried_warm = False
        for tier in attempts:
            if tier == "browser":
                p = await self._browser_fetch(url, wait_selector)
            else:
                imp = tier.split(":", 1)[1]
                p = await self._http(url, imp, warm=False)
                if not p.ok and not p.error and not tried_warm:
                    tried_warm = True
                    p = await self._http(url, imp, warm=True)
            if p.ok:
                self._remember(domain, p.tier)
                return p
            if p.status in (404, 410) and not p.error:
                p.error = f"HTTP {p.status}: page not found (removed or wrong URL)"
                return p
            log.info("fetch %s via %s: status=%s err=%s", url, tier, p.status, p.error)
            last = p
        if last and not last.error:
            last.error = f"blocked (HTTP {last.status}) on every method tried: {', '.join(attempts)}"
        return last

    async def close(self):
        await self._restart_browser()
        for s in self._sessions.values():
            try:
                await s.close()
            except Exception:
                pass
