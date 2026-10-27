"""Known stores ("recipes") and search-URL discovery for stores we have never seen.

A recipe needs only a search URL. CSS selectors are optional: without them the
generic extractor in extract.py finds the product cards on its own. Add a store
by adding an entry here (or just pass its domain to search_site, which discovers
the search URL and caches it in /data/learned_sites.json).
"""

import json
import logging
import os
import re
from urllib.parse import quote_plus, urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser

log = logging.getLogger("shopsearch.sites")

CRAIGSLIST_SITE = os.environ.get("CRAIGSLIST_SITE", "denver")
FB_LOCATION = os.environ.get("FB_LOCATION", "denver")
# Local pickup area: Craigslist searches this many miles around HOME_ZIP (across
# neighbouring Craigslist sites too); Facebook uses the same radius around FB_LOCATION.
HOME_ZIP = os.environ.get("HOME_ZIP", "")
LOCAL_RADIUS_MI = int(os.environ.get("LOCAL_RADIUS_MI", "25"))
DATA_DIR = os.environ.get("DATA_DIR", "/data")
LEARNED_FILE = os.path.join(DATA_DIR, "learned_sites.json")

# {q} is the URL-encoded query. {min}/{max} are filled only when given.
# canary: a search that always has listings, run daily by health.py to notice a store that stops working.
# no_results: a regex that matches the page only when the store itself says nothing matched. Without it, an
# empty search can't be told from a layout the extractor no longer reads; Newegg and Walmart also fill a
# no-results page with suggested products, which would otherwise be counted as results.
SITES: dict[str, dict] = {
    "amazon": {
        "name": "Amazon", "domain": "amazon.com",
        "search": "https://www.amazon.com/s?k={q}",
        "price_sort": "&s=price-asc-rank",
        "card": 'div[data-component-type="s-search-result"]',
        "title": '[data-cy="title-recipe"] h2||h2',
        "price": ".a-price:not(.a-text-price) .a-offscreen||.a-price .a-offscreen",
        "link": 'a[href*="/dp/"]||a.a-link-normal',
        "sponsored": '.puis-sponsored-label-text, a[aria-label*="Sponsored"]',
        "kind": "retail", "canary": "usb c cable",
    },
    "ebay": {
        "name": "eBay", "domain": "ebay.com",
        "search": "https://www.ebay.com/sch/i.html?_nkw={q}{ebay_filters}",
        "price_sort": "&_sop=15",
        "card": "li.s-card||li.s-item",
        "title": ".s-card__title||.s-item__title",
        "price": ".s-card__price||.s-item__price",
        "link": "a.s-card__link||a.s-item__link||a[href*='/itm/']",
        "skip_title": r"^Shop on eBay",
        "kind": "marketplace", "canary": "usb c cable",
    },
    # Official API only (needs EBAY_CLIENT_ID/SECRET): listings the seller lets you pick up within
    # radius_miles of HOME_ZIP. No canary: an empty result can be genuine in a small area.
    "ebay-local": {
        "name": "eBay local pickup", "domain": "ebay.com", "api": "ebay", "pickup": True,
        "kind": "local",
    },
    "psa": {
        "name": "Palmetto State Armory", "domain": "palmettostatearmory.com",
        "search": "https://palmettostatearmory.com/catalogsearch/result/?q={q}",
        "price_sort": "&product_list_order=price",
        "card": "li.product-item",
        "title": ".product-item-link",
        "price": '[data-price-type="finalPrice"]||.price',
        "link": "a.product-item-link",
        "prefer": "browser",  # Cloudflare challenge on every request
        "kind": "retail", "canary": "magazine",
    },
    "dahlonega": {
        "name": "Dahlonega Armory", "domain": "dahlonegaarmory.com",
        "search": "https://www.dahlonegaarmory.com/search?q={q}",
        "card": ".product-card",
        "title": ".product-title",
        "price": ".product-price",
        "link": "a[href]",
        "kind": "retail", "canary": "trigger",
    },
    "mxlocker": {
        "name": "MX Locker", "domain": "mxlocker.com",
        "search": "https://mxlocker.com/search?keyword={q}",
        "kind": "marketplace", "canary": "helmet",
    },
    "craigslist": {
        "name": "Craigslist", "domain": "craigslist.org",
        "search": "https://{cl_site}.craigslist.org/search/sss?query={q}{cl_filters}",
        "price_sort": "&sort=priceasc",
        "card": "li.cl-static-search-result",
        "title": ".title",
        "price": ".price",
        "link": "a[href]",
        "kind": "local", "canary": "chair",
        "no_results": r'(?s)<ol class="cl-static-search-results">\s*(?:<li class="cl-static-hub-links">(?:(?!<li\b).)*)?</ol>',
    },
    "facebook": {
        "name": "Facebook Marketplace", "domain": "facebook.com",
        "search": "https://www.facebook.com/marketplace/{fb_loc}/search?query={q}{fb_filters}",
        "price_sort": "&sortBy=price_ascend",
        "parser": "facebook",
        "prefer": "http:safari",
        "kind": "local", "canary": "chair",
        "no_results": r'"__typename":"MarketplaceSearchFeedNoResults"',
    },
    "walmart": {
        "name": "Walmart", "domain": "walmart.com",
        "search": "https://www.walmart.com/search?q={q}",
        "price_sort": "&sort=price_low",
        "parser": "walmart",
        "kind": "retail", "canary": "usb c cable",
        "no_results": r'"aggregatedCount":0[,}]',
    },
    "newegg": {
        "name": "Newegg", "domain": "newegg.com",
        "search": "https://www.newegg.com/p/pl?d={q}",
        "price_sort": "&Order=1",
        "card": ".item-cell",
        "title": ".item-title",
        "price": ".price-current",
        "link": "a.item-title",
        "kind": "retail", "canary": "usb c cable",
        "no_results": r"We have found 0 items that match",
    },
    "gunbroker": {
        "name": "GunBroker", "domain": "gunbroker.com",
        "search": "https://www.gunbroker.com/All/search?Keywords={q}",
        "price_sort": "&Sort=4",
        "kind": "marketplace", "canary": "magazine",
    },
    "aliexpress": {
        "name": "AliExpress", "domain": "aliexpress.us",
        "search": "https://www.aliexpress.us/w/wholesale-{q_dash}.html",
        "price_sort": "?sortType=price_asc",
        "kind": "retail", "canary": "usb c cable",
    },
}

ALIASES = {
    "palmetto": "psa", "palmettostatearmory": "psa", "palmetto state armory": "psa",
    "dahlonega armory": "dahlonega", "mx locker": "mxlocker", "fb": "facebook",
    "marketplace": "facebook", "facebook marketplace": "facebook", "cl": "craigslist",
    "ali": "aliexpress", "ebay local": "ebay-local", "ebay local pickup": "ebay-local",
}


def resolve(site: str) -> tuple[str, dict] | None:
    """Map a site key, alias, domain or URL to (key, recipe)."""
    s = site.strip().lower()
    s = ALIASES.get(s, s)
    if s in SITES:
        return s, SITES[s]
    host = urlsplit(s if "://" in s else "https://" + s).hostname or s
    host = host[4:] if host.startswith("www.") else host
    for k, r in SITES.items():
        if host == r["domain"] or host.endswith("." + r["domain"]):
            return k, r
    learned = _load_learned()
    if host in learned:
        return host, learned[host]
    return None


def build_url(recipe: dict, query: str, *, price_sort: bool = False,
              min_price: float | None = None, max_price: float | None = None,
              condition: str | None = None, radius_mi: int | None = None) -> str:
    radius_mi = radius_mi or LOCAL_RADIUS_MI
    ebay = ""
    if min_price is not None:
        ebay += f"&_udlo={min_price:g}"
    if max_price is not None:
        ebay += f"&_udhi={max_price:g}"
    if condition == "new":
        ebay += "&LH_ItemCondition=1000"
    elif condition == "used":
        ebay += "&LH_ItemCondition=3000"
    cl = ""
    if min_price is not None:
        cl += f"&min_price={min_price:g}"
    if max_price is not None:
        cl += f"&max_price={max_price:g}"
    if condition == "new":
        cl += "&condition=10"
    elif condition == "used":
        cl += "&condition=20&condition=30&condition=40&condition=50"  # like new .. fair; not salvage
    if HOME_ZIP:
        cl += f"&postal={HOME_ZIP}&search_distance={radius_mi}"
    fb = ""
    if min_price is not None:
        fb += f"&minPrice={int(min_price)}"
    if max_price is not None:
        fb += f"&maxPrice={int(max_price)}"
    fb += f"&radius={round(radius_mi * 1.609)}"
    if condition == "new":
        fb += "&itemCondition=new"
    elif condition == "used":
        fb += "&itemCondition=used_like_new%2Cused_good%2Cused_fair"
    url = recipe["search"].format(
        q=quote_plus(query), q_path=quote_plus(query).replace("+", "%20"),
        q_dash=re.sub(r"[^a-zA-Z0-9]+", "-", query).strip("-"),
        ebay_filters=ebay, cl_site=CRAIGSLIST_SITE, cl_filters=cl,
        fb_loc=FB_LOCATION, fb_filters=fb,
    )
    if price_sort and recipe.get("price_sort"):
        url += recipe["price_sort"]
    return url


# ---------------------------------------------------------------- discovery

def _load_learned() -> dict:
    try:
        with open(LEARNED_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_learned(host: str, recipe: dict):
    data = _load_learned()
    data[host] = recipe
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LEARNED_FILE, "w") as f:
            json.dump(data, f, indent=1, sort_keys=True)
    except Exception as e:
        log.warning("could not save learned site: %s", e)


SEARCH_PARAM_NAMES = ("q", "s", "query", "search", "keyword", "keywords", "k", "search_query", "searchTerm", "term", "w", "text")


def discover_templates(home_html: str, home_url: str) -> list[str]:
    """Candidate search URL templates (with {q}) found on a store's homepage, best first."""
    out: list[str] = []
    tree = LexborHTMLParser(home_html)

    # schema.org SearchAction, e.g. "https://x.com/search?keyword={search_term_string}"
    for s in tree.css('script[type="application/ld+json"]'):
        txt = s.text(deep=True) or ""
        for m in re.finditer(r'"(?:target|urlTemplate)"\s*:\s*"([^"]*\{search_term_string\}[^"]*)"', txt):
            out.append(urljoin(home_url, m.group(1).replace("\\/", "/").replace("{search_term_string}", "{q}")))

    # <form> with a search box
    for form in tree.css("form"):
        inputs = form.css("input")
        names = [i.attributes.get("name") for i in inputs]
        box = None
        for i in inputs:
            n, t = i.attributes.get("name"), (i.attributes.get("type") or "text").lower()
            if n and (t == "search" or n in SEARCH_PARAM_NAMES) and t not in ("hidden", "submit", "email", "password"):
                box = n
                break
        if not box or "password" in str(names):
            continue
        method = (form.attributes.get("method") or "get").lower()
        if method != "get":
            continue
        action = urljoin(home_url, form.attributes.get("action") or "/")
        extra = ""
        for i in inputs:
            if (i.attributes.get("type") or "").lower() == "hidden" and i.attributes.get("name") in ("post_type", "type"):
                extra += f"&{i.attributes['name']}={quote_plus(i.attributes.get('value') or '')}"
        out.append(f"{action}{'&' if '?' in action else '?'}{box}={{q}}{extra}")

    # Platform defaults
    low = home_html[:400000].lower()
    root = f"{urlsplit(home_url).scheme}://{urlsplit(home_url).netloc}"
    if "cdn.shopify.com" in low or "shopify.theme" in low:
        out.append(root + "/search?q={q}&type=product")
    if "woocommerce" in low or "wp-content" in low:
        out.append(root + "/?s={q}&post_type=product")
    if "magento" in low or "mage/" in low or "catalogsearch" in low:
        out.append(root + "/catalogsearch/result/?q={q}")
    if "bigcommerce" in low:
        out.append(root + "/search.php?search_query={q}")
    out += [root + "/search?q={q}", root + "/search?query={q}"]
    seen, uniq = set(), []
    for u in out:
        if u not in seen and urlsplit(u).hostname and urlsplit(u).hostname.endswith(urlsplit(root).hostname.removeprefix("www.")):
            seen.add(u)
            uniq.append(u)
    return uniq[:6]
