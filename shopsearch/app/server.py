"""shopsearch: price-research tools for a local LLM, served over MCP (Streamable HTTP).

Tools:
  shop_search  - search one or more stores/marketplaces, return one price-sorted table
  web_search   - SearXNG web search with compact results
  fetch_page   - read any page (stealth browser fallback), product facts first, size-capped
  watch_create / watch_report / watch_manage - saved searches re-run on a schedule, grouped into hunts (watches.py)

REST mirrors for testing / other programs:
  GET /api/search?q=...&sites=amazon,ebay   GET /api/fetch?url=...   GET /api/web?q=...   GET /health
  POST /api/import (a watches.json from an earlier install; only while there are no hunts)

With API_KEY set, every route except /health needs the header "Authorization: Bearer <API_KEY>".
"""

import asyncio
import base64
import contextlib
import hmac
import logging
import logging.handlers
import os
import re
import signal
import time
from urllib.parse import quote_plus, urlsplit

import addon_options

addon_options.apply()  # before the app modules below read their settings from os.environ

import httpx  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse, PlainTextResponse  # noqa: E402

import agent  # noqa: E402
import ebay_api  # noqa: E402
import extract  # noqa: E402
import health  # noqa: E402
import review  # noqa: E402
import sites  # noqa: E402
import watches  # noqa: E402
from fetcher import Fetcher, domain_of  # noqa: E402

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request (HA polls every 5 s) drowns the log
# Watch/Home Assistant events also go to a file in the data volume, which survives container rebuilds.
_fh = logging.handlers.RotatingFileHandler(os.path.join(os.environ.get("DATA_DIR", "/data"), "shopsearch.log"),
                                           maxBytes=2_000_000, backupCount=3)
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
_fh.setLevel(logging.INFO)
for _name in ("shopsearch.watch", "shopsearch.ha", "shopsearch.agent", "shopsearch.review", "shopsearch.requests",
              "shopsearch.health"):
    logging.getLogger(_name).addHandler(_fh)
log = logging.getLogger("shopsearch")

SEARXNG_URL = os.environ.get("SEARXNG_URL", "").rstrip("/")
DEFAULT_SITES = [s.strip() for s in os.environ.get("DEFAULT_SITES", "amazon,ebay").split(",") if s.strip()]
SITE_TIMEOUT_S = int(os.environ.get("SITE_TIMEOUT_S", "90"))
PORT = int(os.environ.get("PORT", "8765"))
API_KEY = os.environ.get("API_KEY", "")

F = Fetcher()

KNOWN = ", ".join(f"{k} ({v['name']})" for k, v in sites.SITES.items())

mcp = FastMCP(
    "shopsearch",
    instructions=(
        "Tools for finding the cheapest place to buy something. Use shop_search first: it searches "
        "store sites directly and returns real listings with prices. Use web_search to discover other "
        "stores or information, and fetch_page to read a specific product page (price, stock, shipping). To keep checking a search over time use watch_create; when the user asks for an update, use watch_report. "
        "If a tool reports that a site failed or was blocked, tell the user which site failed; never "
        "invent prices or listings."
    ),
    host="0.0.0.0",
    port=PORT,
    stateless_http=True,
    json_response=True,
)


# ---------------------------------------------------------------- core search

def _parse(recipe: dict, html: str, url: str) -> list[dict]:
    if recipe.get("parser") == "facebook":
        return extract.facebook_listings(html)
    items = extract.recipe_listings(html, url, recipe) if recipe.get("card") else []
    if not items:
        items = extract.generic_listings(html, url)
    if recipe.get("domain") == "craigslist.org":
        # $0 on Craigslist means "no price given", not free; keep it out of the cheapest rows.
        for i in items:
            if i.get("price") == 0:
                i["price"] = None
                i["details"] = ", ".join(x for x in (i.get("details"), "no price listed") if x)
    return items


async def _search_with_recipe(key: str, recipe: dict, query: str, opts: dict) -> dict:
    if recipe.get("api") == "ebay" or (key == "ebay" and ebay_api.enabled()):
        t = time.time()
        res = {"site": key, "name": recipe["name"], "url": "", "tier": "official API", "seconds": 0.0, "items": [],
               "error": ""}
        if not ebay_api.enabled():
            res["error"] = "needs the eBay API keys (ebay_client_id / ebay_client_secret in the settings)"
            return res
        pickup = (opts["radius_mi"] or sites.LOCAL_RADIUS_MI) if recipe.get("pickup") else None
        try:
            res["items"] = await ebay_api.search(query, price_sort=opts["price_sort"], min_price=opts["min_price"],
                                                 max_price=opts["max_price"], condition=opts["condition"],
                                                 pickup_radius_mi=pickup)
        except Exception as e:
            res["error"] = f"eBay API error: {type(e).__name__}: {str(e)[:200]}"
        res["seconds"] = round(time.time() - t, 1)
        return res
    url = sites.build_url(recipe, query, price_sort=opts["price_sort"], min_price=opts["min_price"],
                          max_price=opts["max_price"], condition=opts["condition"], radius_mi=opts["radius_mi"])
    page = await F.fetch(url, prefer=recipe.get("prefer"))
    res = {"site": key, "name": recipe.get("name", key), "url": url, "tier": page.tier,
           "seconds": round(page.seconds, 1), "items": [], "error": ""}
    if not page.ok:
        res["error"] = page.error or f"HTTP {page.status}"
        return res
    items = _parse(recipe, page.html, page.url)
    if not items and page.tier != "browser" and recipe.get("parser") != "facebook":
        # The page may build its results with JavaScript; render it.
        p2 = await F.fetch(url, mode="browser")
        if p2.ok:
            items = _parse(recipe, p2.html, p2.url)
            res["tier"], res["seconds"] = "browser", round(page.seconds + p2.seconds, 1)
    res["items"] = items
    if not items:
        res["error"] = "page loaded but no product listings were found on it (no results, or an unrecognised layout)"
    return res


async def _discover_and_search(host: str, query: str, opts: dict) -> dict:
    """A store we have no recipe for: find its search URL from its homepage, then search."""
    res = {"site": host, "name": host, "url": "", "tier": "", "seconds": 0.0, "items": [], "error": ""}
    t = time.time()
    home = await F.fetch(f"https://{host}/")
    if not home.ok:
        res["error"] = f"could not load homepage: {home.error or home.status}"
        return res
    for tmpl in sites.discover_templates(home.html, home.url):
        url = tmpl.replace("{q}", quote_plus(query))
        page = await F.fetch(url)
        if not page.ok:
            continue
        items = extract.generic_listings(page.html, page.url)
        if not items and page.tier != "browser":
            p2 = await F.fetch(url, mode="browser")
            if p2.ok:
                page, items = p2, extract.generic_listings(p2.html, p2.url)
        rel = [i for i in items if extract.relevance(query, i["title"]) >= 0.5]
        if rel:
            sites.save_learned(host, {"name": host, "domain": host, "search": tmpl, "kind": "learned"})
            res.update(url=url, tier=page.tier, items=items, seconds=round(time.time() - t, 1))
            return res
    # Last resort: web search restricted to the site, then read the product pages it finds.
    hits = await _searx(f"site:{host} {query}", 6)
    pages = await asyncio.gather(*(F.fetch(h["url"]) for h in hits), return_exceptions=True)
    for h, p in zip(hits, pages):
        if isinstance(p, Exception) or not p.ok:
            continue
        info = extract.product_details(p.html, p.url)
        if info.get("price"):
            res["items"].append({"title": info.get("name") or h["title"], "price": info["price"],
                                 "price_text": f"${info['price']:,.2f}", "url": extract.tidy_url(p.url),
                                 "details": " · ".join(str(info[k]) for k in ("availability", "condition") if info.get(k))})
    res["seconds"] = round(time.time() - t, 1)
    res["tier"] = "web-search + product pages"
    if not res["items"]:
        res["error"] = "could not find this store's search page, and web search found no priced product pages on it"
    return res


async def _search_site(site: str, query: str, opts: dict) -> dict:
    found = sites.resolve(site)
    try:
        if found:
            coro = _search_with_recipe(found[0], found[1], query, opts)
        else:
            host = domain_of(site if "://" in site else "https://" + site.strip())
            if "." not in host:
                return {"site": site, "name": site, "items": [], "tier": "", "seconds": 0, "url": "",
                        "error": f"unknown site '{site}'. Use a known key ({', '.join(sites.SITES)}) or a domain like example.com"}
            coro = _discover_and_search(host, query, opts)
        return await asyncio.wait_for(coro, timeout=SITE_TIMEOUT_S)
    except asyncio.TimeoutError:
        return {"site": site, "name": site, "items": [], "tier": "", "seconds": SITE_TIMEOUT_S, "url": "",
                "error": f"timed out after {SITE_TIMEOUT_S}s"}
    except Exception as e:
        log.exception("search %s failed", site)
        return {"site": site, "name": site, "items": [], "tier": "", "seconds": 0, "url": "",
                "error": f"{type(e).__name__}: {e}"}


def _fmt_price(i: dict) -> str:
    if i.get("price") is None:
        return (i.get("price_text") or "see site")[:20]
    if i["price"] == 0:
        return "Free"
    return f"${i['price']:,.2f}"


async def collect(query: str, site_list: list[str], *, max_price=None, min_price=None,
                  condition="any", price_sort=False, min_relevance=0.7, per_site=12,
                  radius_miles=None) -> tuple[list[tuple[str, dict]], list[str], list[dict]]:
    """Search the sites and filter. Returns (rows sorted by price as (store name, item), status lines, raw results)."""
    opts = {"price_sort": price_sort, "min_price": min_price, "max_price": max_price, "radius_mi": radius_miles,
            "condition": None if condition in (None, "", "any") else condition}
    results = await asyncio.gather(*(_search_site(s, query, opts) for s in site_list))

    rows, status = [], []
    for r in results:
        items = r["items"]
        total = len(items)
        seen, kept, dropped = set(), [], 0
        for i in items:
            key = (i["title"].lower()[:80], i.get("price"))
            if key in seen:
                continue
            seen.add(key)
            if extract.relevance(query, i["title"]) < min_relevance:
                dropped += 1
                continue
            p = i.get("price")
            if p is not None and ((max_price is not None and p > max_price) or (min_price is not None and p < min_price)):
                dropped += 1
                continue
            kept.append(i)
        kept.sort(key=lambda i: (i.get("price") is None, i.get("price") or 0))
        kept = kept[:per_site]
        for i in kept:
            rows.append((r["name"], i))
        if r["error"] and not kept:
            status.append(f"- {r['name']}: FAILED - {r['error']}")
        else:
            note = f", {dropped} off-topic/out-of-range dropped" if dropped else ""
            status.append(f"- {r['name']}: {len(kept)} shown of {total} listings{note} ({r['tier']}, {r['seconds']}s)")

    rows.sort(key=lambda x: (x[1].get("price") is None, x[1].get("price") or 0))
    return rows, status, results


def table(rows: list[tuple[str, dict]], extra: str | None = None) -> list[str]:
    """Markdown table of (store, item) rows. extra: name of an item key shown as an additional column."""
    head = "| # | Price | Item | Store | Details | Link |" if not extra else f"| # | Price | {extra.title()} | Item | Store | Details | Link |"
    out = [head, "|" + "---|" * (head.count("|") - 1)]
    for n, (store, i) in enumerate(rows, 1):
        title = i["title"].replace("|", "/")[:110]
        det = (i.get("details") or "").replace("|", "/")
        mid = f" {i.get(extra, '')} |" if extra else ""
        out.append(f"| {n} | {_fmt_price(i)} |{mid} {title} | {store} | {det} | {i['url']} |")
    return out


async def run_shop_search(query: str, site_list: list[str], *, max_price=None, min_price=None,
                          condition="any", price_sort=False, max_results=25, min_relevance=0.7,
                          per_site=12, radius_miles=None) -> str:
    rows, status, _ = await collect(query, site_list, max_price=max_price, min_price=min_price,
                                    condition=condition, price_sort=price_sort, min_relevance=min_relevance,
                                    per_site=per_site, radius_miles=radius_miles)
    rows = rows[:max_results]
    out = [f'## Price search: "{query}"', "", "Sites searched:", *status, ""]
    if rows:
        out += table(rows)
        out += ["", "Notes: prices are as listed on the search page and usually exclude tax and shipping unless "
                "Details says otherwise. eBay/marketplace items may be auctions or used. Use fetch_page on a link "
                "to confirm price, stock, and shipping before recommending it."]
    else:
        out.append("No matching listings were found. Try a shorter or different query, other sites, or "
                   "lower min_relevance.")
    return "\n".join(out)


# ---------------------------------------------------------------- web search / fetch

async def _searx(q: str, n: int) -> list[dict]:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"{SEARXNG_URL}/search", params={"q": q, "format": "json", "language": "en-US"})
        r.raise_for_status()
        data = r.json()
    out, seen = [], set()
    for x in data.get("results", []):
        u = x.get("url")
        if not u or u in seen:
            continue
        seen.add(u)
        out.append({"title": extract.clean(x.get("title")), "url": u, "snippet": extract.clean(x.get("content"))[:240]})
        if len(out) >= n:
            break
    return out


async def run_web_search(query: str, site: str | None = None, max_results: int = 8) -> str:
    if not SEARXNG_URL:
        return "web_search is not set up: no SearXNG server is configured (searxng_url)."
    q = f"site:{site} {query}" if site else query
    try:
        hits = await _searx(q, max_results)
    except Exception as e:
        return f"web_search FAILED: SearXNG at {SEARXNG_URL} returned an error: {type(e).__name__}: {e}"
    if not hits:
        return f'No web results for "{q}".'
    lines = [f'Web results for "{q}":']
    for n, h in enumerate(hits, 1):
        lines.append(f"{n}. {h['title']}\n   {h['url']}\n   {h['snippet']}")
    return "\n".join(lines)


async def run_fetch_page(url: str, max_chars: int = 6000, offset: int = 0, render_js: bool = False) -> str:
    if not re.match(r"^https?://", url):
        url = "https://" + url
    page = await F.fetch(url, mode="browser" if render_js else "auto")
    if not page.ok:
        return f"fetch_page FAILED for {url}: {page.error or 'HTTP ' + str(page.status)} (methods: {page.tier})"
    head = [f"URL: {page.url}", f"Fetched via: {page.tier} in {page.seconds:.1f}s"]
    info = extract.product_details(page.html, page.url)
    if info.get("price") is not None or len(info) > 1:
        head.append("Product facts (from the page's structured data):")
        for k, v in info.items():
            head.append(f"  {k}: {v}")
    listings = []
    if not info.get("price"):
        try:
            listings = extract.generic_listings(page.html, page.url)
        except Exception:
            listings = []
    if len(listings) >= 3:
        head.append(f"Listings found on this page ({len(listings)}, first 15):")
        for i in listings[:15]:
            head.append(f"  - {_fmt_price(i)} | {i['title'][:100]} | {i['url']}")
    md = extract.page_markdown(page.html)
    max_chars = max(1000, min(int(max_chars), 30000))
    chunk = md[offset: offset + max_chars]
    more = len(md) - (offset + len(chunk))
    tail = (f"\n\n[... {more} more characters. Call fetch_page again with offset={offset + len(chunk)} to read on.]"
            if more > 0 else "")
    return "\n".join(head) + "\n\n--- Page text ---\n" + chunk + tail


# ---------------------------------------------------------------- MCP tools

# The description lists the live site keys, so it is built rather than a docstring.
_SHOP_DESC = (
    "Search stores and marketplaces for a product and get ONE table of real listings sorted by price "
    "(cheapest first), with links.\n\n"
    "Args:\n"
    '    query: What to buy, as a short product search (e.g. "ar15 lower parts kit", "ktm 300 brake pads"). '
    'Use words a listing title would contain; leave out filler like "cheapest" or "best".\n'
    f"    sites: List of stores to search in parallel. Known keys: {KNOWN}. Any other store also works: pass "
    'its domain (e.g. "brownells.com") and its search page is found automatically. '
    f"Default when omitted: {', '.join(DEFAULT_SITES)}. Search several at once to compare prices.\n"
    "    max_price: Drop listings above this price (USD).\n"
    "    min_price: Drop listings below this price; useful to skip cheap accessories that match the words.\n"
    '    condition: "any", "new", or "used" (applied where the site supports it: eBay, Facebook).\n'
    f"    radius_miles: Pickup distance for craigslist, facebook and ebay-local (default {sites.LOCAL_RADIUS_MI}). "
    "Raise it (e.g. 60-100) for bigger or rarer items.\n"
    "    sort_on_site_by_price: Ask each site for its own lowest-price sort instead of its relevance sort. "
    "Relevance (default) gives better matches; the table is sorted by price either way.\n"
    "    max_results: Rows in the final table (default 25).\n"
    "    min_relevance: 0-1, share of query words a listing title must contain (default 0.7); words with digits "
    "(model numbers like ar15, g19, 300) are always required, so put only one model number per query. Lower it if "
    "good results are being dropped, raise it to cut noise."
)

@mcp.tool(description=_SHOP_DESC)
async def shop_search(query: str, sites: list[str] | str | None = None, max_price: float | None = None,
                      min_price: float | None = None, condition: str = "any",
                      sort_on_site_by_price: bool = False, max_results: int = 25,
                      min_relevance: float = 0.7, radius_miles: int | None = None) -> str:
    site_list = sites or DEFAULT_SITES
    if isinstance(site_list, str):
        site_list = [s for s in re.split(r"[,\s]+", site_list) if s]
    return await run_shop_search(query, site_list[:12], max_price=max_price, min_price=min_price,
                                 condition=condition, price_sort=sort_on_site_by_price,
                                 max_results=max(1, min(int(max_results), 60)), min_relevance=min_relevance,
                                 radius_miles=max(1, min(int(radius_miles), 500)) if radius_miles else None)


@mcp.tool()
async def web_search(query: str, site: str | None = None, max_results: int = 8) -> str:
    """Search the web (via SearXNG) and get titles, URLs and short snippets.

    Use it to find stores that sell an item, part numbers, compatibility info, reviews, or deals.
    For prices on specific stores prefer shop_search, which reads store search pages directly.

    Args:
        query: Search terms.
        site: Optional domain to restrict results to (e.g. "brownells.com").
        max_results: Number of results (default 8, max 20).
    """
    return await run_web_search(query, site, max(1, min(int(max_results), 20)))


@mcp.tool()
async def fetch_page(url: str, max_chars: int = 6000, offset: int = 0, render_js: bool = False) -> str:
    """Read a web page, including sites protected by Cloudflare or other bot checks.

    For product pages it returns the key facts first (name, price, availability, condition, brand, SKU),
    then the page text as Markdown, cut to max_chars. For a page that lists many products it also
    returns the listings it finds.

    Args:
        url: Full URL to read.
        max_chars: Maximum characters of page text to return (default 6000, max 30000). Keep it small.
        offset: Character offset to continue reading a long page (the previous result tells you the value).
        render_js: Force the full browser (slower). Only needed if the normal result is missing content.
    """
    return await run_fetch_page(url, max_chars, offset, render_js)




# ---------------------------------------------------------------- watches (scheduled searches)

def _site_list(v) -> list[str]:
    v = v or DEFAULT_SITES
    if isinstance(v, str):
        v = [s for s in re.split(r"[,\s]+", v) if s]
    return list(v)[:12]


async def _watch_search(p: dict):
    return await collect(p["query"], p["sites"], max_price=p.get("max_price"), min_price=p.get("min_price"),
                         condition=p.get("condition") or "any", min_relevance=p.get("min_relevance", 0.7),
                         radius_miles=p.get("radius_miles"), per_site=40)


async def describe_listing(url: str) -> dict:
    """One listing's own page for the reviewer: {"text": seller's description, "photo": data URL or None}.
    eBay goes through the API when keys are set (scraping eBay item pages gets the IP blocked) and is skipped
    otherwise."""
    m = re.search(r"ebay\.com/itm/(\d+)", url)
    if m:
        if not ebay_api.enabled():
            return {"text": "", "photo": None}
        info = await ebay_api.item(m.group(1))
    else:
        page = await F.fetch(url)
        if not page.ok:
            raise RuntimeError(page.error or f"HTTP {page.status}")
        info = extract.listing_page(page.html)
    return {"text": info["text"], "photo": await _photo(info.get("image"))}


async def _photo(url: str | None) -> str | None:
    """The image as a data URL for the LLM (local servers can't be counted on to fetch URLs themselves)."""
    if not url:
        return None
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as c:
            r = await c.get(url)
    except httpx.HTTPError:
        return None
    ctype = r.headers.get("content-type", "").split(";")[0]
    if r.status_code != 200 or not ctype.startswith("image/") or len(r.content) > 3_000_000:
        return None
    return f"data:{ctype};base64,{base64.b64encode(r.content).decode()}"


W = watches.Watches(_watch_search, describe_listing)
H = health.Health(lambda key, query: _search_site(key, query, {"price_sort": False, "min_price": None, "max_price": None,
                                                               "radius_mi": None, "condition": None}))


def _describe(w: dict) -> str:
    p = w["params"]
    bits = [f'"{p["query"]}" on {", ".join(p["sites"])}']
    if p.get("min_price") is not None or p.get("max_price") is not None:
        bits.append(f"price {p.get('min_price') or 0:g}-{p['max_price']:g}" if p.get("max_price") is not None
                    else f"price from {p['min_price']:g}")
    if p.get("condition") not in (None, "", "any"):
        bits.append(p["condition"])
    if p.get("radius_miles"):
        bits.append(f"{p['radius_miles']} mi pickup radius")
    state = "active" if w["active"] else "PAUSED"
    nxt = f", next {watches.ago(w['next_check'])}" if w["active"] else ""
    return (f"**{w['id']}** {w['label']}: {'; '.join(bits)}. Every {w['every_hours']:g} h, {state}, "
            f"last checked {watches.ago(w['last_check'])}{nxt}, {len(w['items'])} listings tracked.")


def _describe_hunt(h: dict) -> list[str]:
    if h.get("ha"):
        phone = "Home Assistant switch on; phone alerts for new matches and price drops"
    elif watches.ha.enabled():
        phone = "Home Assistant switch being created"
    else:
        phone = ""
    out = [f"## Hunt {h['id']}: {h['label']}",
           f"Looking for: {(h.get('want') or '(not set)').rstrip('.')}. {len(h.get('alerts', []))} alerts sent so far."
           + (f" {phone}." if phone else "")]
    out += [f"- {_describe(w)}" for w in W.watches_in(h)]
    return out


def _report(w: dict, since: float, top: int = 8, changes: bool = True) -> list[str]:
    out = [f"### Search {w['id']}: {w['label']}", _describe(w)]
    failed = [s for s in w["last_status"] if "FAILED" in s]
    if failed:
        out += ["Sites that failed on the last check:", *failed]
    items = w["items"]
    if w.get("review_error"):
        out.append(f"LLM review of new listings is failing ({w['review_error']}); they are held until it works.")

    def row(u, rec, **extra):
        note = (rec.get("review") or {}).get("note")
        det = " · ".join(x for x in (rec.get("details"), f"review: {note}" if note else "") if x)
        return rec["store"], dict(rec, url=u, details=det, **extra)

    fresh = [(u, rec) for u, rec in items.items() if rec["first_seen"] > since and not rec.get("baseline")]
    new = sorted((row(u, rec, found=watches.ago(rec["first_seen"])) for u, rec in fresh if W.accepted(rec)),
                 key=lambda x: (x[1].get("price") is None, x[1].get("price") or 0))
    rejected = sum(1 for _, rec in fresh if rec.get("review") and not W.accepted(rec))
    held = sum(1 for _, rec in fresh if "review" not in rec)
    drops = sorted((row(u, rec, was=f"${rec['prev_price']:,.2f} ({watches.ago(rec['dropped_at'])})")
                    for u, rec in items.items()
                    if rec.get("dropped_at", 0) > since and (rec.get("review") or {}).get("match")),
                   key=lambda x: x[1].get("price") or 0)
    gone_new = sum(1 for _, i in new if not W.is_current(w, i))
    out.append("")
    if not changes:
        pass
    elif new:
        out += [f"New listings since {watches.ago(since)} ({len(new)}):"] + table(new[:25], "found")
        if gone_new:
            out.append(f"({gone_new} of these were no longer listed on the latest check.)")
    else:
        out.append(f"No new listings since {watches.ago(since)}.")
    if changes and (rejected or held):
        out.append(f"({rejected} other new listings were judged not a match or reposts and skipped"
                   + (f"; {held} are waiting for review" if held else "") + ".)")
    if changes and drops:
        out += ["", f"Price drops ({len(drops)}):"] + table(drops[:15], "was")
    cur = sorted((row(u, rec) for u, rec in items.items()
                  if W.is_current(w, rec) and (rec.get("review") or {}).get("match", True)
                  and not (rec.get("review") or {}).get("repost_of")),
                 key=lambda x: (x[1].get("price") is None, x[1].get("price") or 0))
    if cur:
        out += ["", f"Cheapest currently listed ({min(top, len(cur))} of {len(cur)}):"] + table(cur[:top])
    return out


def _hunt_names() -> str:
    return ", ".join(f"{h['id']} ({h['label']})" for h in W.hunts()) or "none"


async def create_search(query, sites=None, every_hours=6, label=None, max_price=None, min_price=None,
                        condition="any", radius_miles=None, min_relevance=0.7, want=None, hunt=None,
                        recipients=None) -> str:
    params = {"query": query, "sites": _site_list(sites), "max_price": max_price, "min_price": min_price,
              "condition": condition or "any", "radius_miles": int(radius_miles) if radius_miles else None,
              "min_relevance": min_relevance}
    try:
        w = await W.create(params, (label or query)[:60], float(every_hours or 6),
                           want=(want or "").strip()[:600], hunt=(hunt or "").strip(), recipients=recipients)
    except ValueError as e:
        return f"watch_create FAILED: {e}"
    h = W.hunt_of(w)
    n = len(W.watches_in(h))
    out = [f"Added search {w['id']} to hunt {h['id']} \"{h['label']}\" ({n} search{'es' if n > 1 else ''} in it). "
           "It re-checks in the background; later, watch_report shows only new listings and price drops.", ""]
    out += _report(w, since=time.time(), top=15, changes=False)[1:]
    return "\n".join(out)


@mcp.tool()
async def watch_create(query: str, sites: list[str] | str | None = None, every_hours: float = 6,
                       hunt: str | None = None, want: str | None = None, label: str | None = None,
                       max_price: float | None = None, min_price: float | None = None,
                       condition: str = "any", radius_miles: int | None = None,
                       min_relevance: float = 0.7) -> str:
    """Save a search that re-runs automatically on a schedule (it keeps running when no chat is open).

    Use when the user wants to keep an eye on something: "watch for", "let me know if", "track the price of".
    Searches are grouped into hunts: one hunt per thing the user is after. Several query variations or site sets
    for the same goal MUST use the same `hunt` name; the hunt is one switch and one alert history in the user's
    Home Assistant, and a listing found by two of its searches is alerted only once. New listings are checked
    against `want` by an LLM before the user's phone is alerted. Call watch_manage(action="list") first to reuse
    an existing hunt.

    Args:
        query, sites, max_price, min_price, condition, radius_miles, min_relevance: same as shop_search.
        hunt: Short name of the overall goal, e.g. "KTM 300 dirt bike" or "JET 1236 lathe apron". Same name =
            same hunt. Use one search per model number (all model numbers in a query must appear in a title).
        want: One or two sentences describing exactly what counts as a match, written from the conversation,
            e.g. "A complete, running KTM 300 two-stroke dirt bike (XC, XC-W, TPI or TBI, any year). Not parts,
            gear, or other models." Applies to the whole hunt.
        every_hours: How often to re-check (default 6, minimum 1). Local marketplaces: 2-6; stores: 12-24.
        label: Short name for this particular search variation (default: the query).
    """
    return await create_search(query, sites, every_hours, label, max_price, min_price, condition,
                               radius_miles, min_relevance, want, hunt)


@mcp.tool()
async def watch_report(hunt: str | None = None, since_hours: float | None = None) -> str:
    """Report on hunts: new listings and price drops since the last report, plus the cheapest current ones.

    Use when the user asks for an update, "anything new?", or about something they asked you to watch.
    Each call marks the searches as reported, so the next report shows only newer changes.

    Args:
        hunt: One hunt (name or id like "h2"); omit for all hunts.
        since_hours: Look back this many hours instead of "since the last report".
    """
    hs = [W.hunt(hunt)] if hunt else W.hunts()
    if hunt and not hs[0]:
        return f"No hunt '{hunt}'. Existing: {_hunt_names()}"
    if not hs:
        return "There are no hunts. Create one with watch_create."
    out = []
    for h in hs:
        out += _describe_hunt(h)[:2] + [""]
        for w in W.watches_in(h):
            since = time.time() - since_hours * 3600 if since_hours else w["last_reported"]
            out += _report(w, since) + [""]
            await W.update(w["id"], mark_reported=True)
    return "\n".join(out)


@mcp.tool()
async def watch_manage(action: str, watch_id: str | None = None, hunt: str | None = None,
                       every_hours: float | None = None, want: str | None = None) -> str:
    """List or change hunts and their saved searches.

    Args:
        action: "list" (all hunts and their searches), "check_now" (re-run a search now, then report),
            "pause" / "resume" / "delete" / "set_interval" (one search, by watch_id), "delete_hunt" (a whole hunt
            and all its searches, also removes its Home Assistant switch), "move" (put search watch_id into
            hunt, e.g. to merge duplicates), "set_want" (change what a hunt counts as a match, with want).
        watch_id: The search to act on, e.g. "w14".
        hunt: Hunt name or id, for "delete_hunt" and "move".
        every_hours: New interval for "set_interval".
        want: New match description for "set_want".
    """
    action = action.strip().lower()
    if action == "list":
        out = []
        for h in W.hunts():
            out += _describe_hunt(h) + [""]
        return "\n".join(out) or "There are no hunts."
    if action == "delete_hunt":
        h = await W.delete_hunt(hunt or "", why="deleted from chat")
        return f"Deleted hunt {h['id']} ({h['label']}) and its searches." if h else f"No hunt '{hunt}'. Existing: {_hunt_names()}"
    if action == "set_want":
        h = W.hunt(hunt or "")
        if not h or not want:
            return f"set_want needs hunt and want. Existing hunts: {_hunt_names()}"
        await W.set_want(h["id"], want.strip()[:600])
        return f"Hunt {h['id']} ({h['label']}) now looks for: {want.strip()[:600]}"
    w = W.get(watch_id or "")
    if not w:
        return f"No search '{watch_id}'. Use watch_manage(action='list') to see them."
    if action == "move":
        if not hunt:
            return "move needs hunt (the hunt to move the search into)."
        h = await W.move(w["id"], hunt)
        return f"Moved {w['id']} into hunt {h['id']} ({h['label']})."
    if action == "delete":
        await W.delete(w["id"])
        return f"Deleted search {w['id']} ({w['label']})."
    if action in ("pause", "resume"):
        w = await W.update(w["id"], active=action == "resume")
        return _describe(w)
    if action == "set_interval":
        if not every_hours:
            return "set_interval needs every_hours."
        return _describe(await W.update(w["id"], every_hours=every_hours))
    if action == "check_now":
        since = w["last_reported"]
        await W.check(w["id"])
        await W.update(w["id"], mark_reported=True)
        return "\n".join(_report(W.get(w["id"]), since))
    return f"Unknown action '{action}'. Use list, check_now, pause, resume, delete, set_interval, delete_hunt, or move."


# ---------------------------------------------------------------- Home Assistant request box

async def _agent_tool(name: str, args: dict, recipients: list[dict] | None = None) -> str:
    if name == "shop_search":
        return await run_shop_search(args["query"], _site_list(args.get("sites")), max_price=args.get("max_price"),
                                     min_price=args.get("min_price"), condition=args.get("condition") or "any",
                                     radius_miles=args.get("radius_miles"), max_results=12)
    if name == "list_hunts":
        out = []
        for h in W.hunts():
            out += _describe_hunt(h)
        return "\n".join(out) or "No hunts yet."
    if name == "create_search":
        txt = await create_search(args["query"], args.get("sites"), args.get("every_hours") or 6, None,
                                  args.get("max_price"), args.get("min_price"), args.get("condition"),
                                  args.get("radius_miles"), 0.6, args.get("want"), args.get("hunt"), recipients)
        return txt[:2500]
    return f"unknown tool {name}"


async def request_loop():
    """Watch the Home Assistant request button; run the typed request through the local LLM."""
    if not (watches.ha.enabled() and review.enabled()):
        return
    last = None
    while True:
        await asyncio.sleep(5)
        try:
            st = await watches.ha.states()
        except Exception:
            continue
        pressed = (st.get(watches.ha.REQUEST_BUTTON) or {}).get("state")
        if last is None or pressed in (None, "unknown", "unavailable"):
            last = pressed if pressed not in ("unknown", "unavailable") else last or pressed
            continue
        if pressed == last:
            continue
        last = pressed
        text = ((st.get(watches.ha.REQUEST_TEXT) or {}).get("state") or "").strip()
        if not text or text in ("unknown", "unavailable"):
            await W.set_request_status("Type what to watch for in the box first.")
            continue
        # Whoever pressed the button gets the answer and owns the hunt's alerts (fallback: HA_NOTIFY).
        user_id = ((st.get(watches.ha.REQUEST_BUTTON) or {}).get("context") or {}).get("user_id")
        try:
            recipients = await watches.ha.recipients_for_user(user_id, st)
        except Exception as e:
            log.warning("could not look up who sent the request: %s", e)
            recipients = []
        who = recipients[0]["name"] if recipients else "unknown user (default phone)"
        logging.getLogger("shopsearch.requests").info("request from Home Assistant (%s): %s", who, text)
        await W.set_request_status(f"Working on: {text}"[:255])
        try:
            answer = await agent.run(text, KNOWN, lambda n, a: _agent_tool(n, a, recipients))
        except Exception as e:
            log.exception("request failed")
            answer = f"Sorry, that failed ({type(e).__name__}: {str(e)[:120]})."
        await W.set_request_status(answer)
        try:
            await watches.ha.service("input_text", "set_value", {"entity_id": watches.ha.REQUEST_TEXT, "value": ""})
            await watches.ha.notify("Shop watch request", answer, targets=[r["service"] for r in recipients] or None)
        except Exception as e:
            log.warning("could not report request result: %s", e)


# ---------------------------------------------------------------- REST mirrors

@mcp.custom_route("/health", methods=["GET"])
async def api_health(_: Request):
    return JSONResponse({"ok": True, "searxng": SEARXNG_URL, "default_sites": DEFAULT_SITES, "sites": list(sites.SITES),
                         "stores": H.summary()})


@mcp.custom_route("/api/search", methods=["GET"])
async def api_search(req: Request):
    q = req.query_params.get("q", "")
    site_list = [s for s in req.query_params.get("sites", ",".join(DEFAULT_SITES)).split(",") if s]

    def num(k):
        v = req.query_params.get(k)
        return float(v) if v else None

    txt = await run_shop_search(q, site_list, max_price=num("max_price"), min_price=num("min_price"),
                                condition=req.query_params.get("condition", "any"),
                                price_sort=req.query_params.get("price_sort") == "1",
                                max_results=int(req.query_params.get("max_results", 25)),
                                min_relevance=float(req.query_params.get("min_relevance", 0.7)),
                                radius_miles=int(num("radius_miles") or 0) or None)
    return PlainTextResponse(txt)


@mcp.custom_route("/api/fetch", methods=["GET"])
async def api_fetch(req: Request):
    return PlainTextResponse(await run_fetch_page(req.query_params["url"], int(req.query_params.get("max_chars", 6000)),
                                                  int(req.query_params.get("offset", 0)),
                                                  req.query_params.get("render_js") == "1"))


@mcp.custom_route("/api/web", methods=["GET"])
async def api_web(req: Request):
    return PlainTextResponse(await run_web_search(req.query_params["q"], req.query_params.get("site"),
                                                  int(req.query_params.get("n", 8))))


@mcp.custom_route("/api/watches", methods=["GET"])
async def api_watches(req: Request):
    wid = req.query_params.get("id")
    if wid:
        w = W.get(wid)
        return PlainTextResponse("\n".join(_report(w, w["last_reported"])) if w else "not found", status_code=200 if w else 404)
    out = []
    for h in W.hunts():
        out += _describe_hunt(h) + [""]
    return PlainTextResponse("\n".join(out) or "no hunts")


@mcp.custom_route("/api/import", methods=["POST"])
async def api_import(req: Request):
    try:
        n = await W.import_state(await req.json())
    except ValueError as e:
        return PlainTextResponse(str(e), status_code=409)
    return PlainTextResponse(f"imported {n} hunts")


def require_key(app):
    """ASGI wrapper: with API_KEY set, every request except /health must carry it as a Bearer token. The scheme
    is case-insensitive and stray spaces (from copy-paste) are ignored. Rejections are logged with the reason,
    because MCP clients such as Open WebUI only report that "verification failed"."""
    expected = API_KEY.strip().encode()
    seen: set[str] = set()

    async def guarded(scope, receive, send):
        if scope["type"] == "http" and scope["path"] != "/health":
            client = (scope.get("client") or ("?",))[0]
            if client not in seen:
                seen.add(client)
                log.info("first request from %s (%s %s)", client, scope["method"], scope["path"])
            header = dict(scope["headers"]).get(b"authorization", b"").strip()
            scheme, _, key = header.partition(b" ")
            if scheme.lower() != b"bearer" or not hmac.compare_digest(key.strip(), expected):
                why = "no Authorization header" if not header else \
                    f"not a Bearer token ({scheme[:20].decode(errors='replace')!r})" if scheme.lower() != b"bearer" else \
                    f"wrong key ({len(key.strip())} characters, expected {len(expected)})"
                log.warning("refused %s %s from %s: %s", scope["method"], scope["path"], client, why)
                await PlainTextResponse("missing or wrong API key", status_code=401)(scope, receive, send)
                return
        await app(scope, receive, send)
    return guarded if API_KEY else app


async def main():
    import uvicorn

    if not API_KEY:
        log.warning("no API key set: anyone who can reach port %d can use this server", PORT)
    app = require_key(mcp.streamable_http_app())
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="warning"))
    # uvicorn re-raises SIGTERM after shutting down, so the process ends "killed" (exit 143) and the Supervisor
    # marks a normal stop as an error. Handle the signals here instead and return normally.
    server.capture_signals = contextlib.nullcontext
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, server.handle_exit, sig, None)
    tasks = [asyncio.create_task(c) for c in (W.loop(), W.action_loop(), request_loop(), H.loop())]
    try:
        await server.serve()
    finally:
        for t in tasks:
            t.cancel()
        await F.close()  # else Chromium's transport is cleaned up after the loop closed (a traceback on every stop)


if __name__ == "__main__":
    asyncio.run(main())
