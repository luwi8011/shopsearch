"""Turn a page's HTML into a short list of listings (title, price, link, details).

Order of attempts: a site recipe's CSS selectors when we have one, then schema.org
JSON-LD embedded in the page, then a generic heuristic that finds the repeated
"card" element around each price on the page.
"""

import json
import re
from collections import defaultdict
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode

from selectolax.lexbor import LexborHTMLParser

PRICE_RE = re.compile(r"(?:US\s?)?\$\s?(\d{1,3}(?:,\d{3})+|\d+)(\.\d{1,2})?")
NOISE = re.compile(
    r"^(add to (cart|wish ?list|compare)|compare|quick ?view|sponsored|ad|choose options|"
    r"select options|view details|shop now|opens in a new window or tab|save|wishlist|"
    r"price, product page|join prime|to get free delivery|or non-members get free delivery|"
    r"find more like this|see all similar items on ebay\.?|whatshot|local_shipping|rating:|"
    r"\d(\.\d)? out of 5 stars|list:|typical:|options:|more buying choices|\+\d+ other colors/patterns)$",
    re.I,
)
TITLE_JUNK = re.compile(r"\s*(Opens in a new window or tab|New Listing|Sponsored|(\d+ )?\d+ Reviews?)\s*$|^\s*(New Listing|Sponsored)\s*", re.I)
STOPWORDS = {"the", "a", "an", "for", "and", "or", "of", "with", "in", "to", "on", "new", "used", "cheap", "best"}


def parse_price(text: str | None) -> float | None:
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    m = PRICE_RE.search(text)
    if not m:
        try:
            return float(str(text).replace(",", "").strip())
        except ValueError:
            return None
    return float(m.group(1).replace(",", "") + (m.group(2) or ""))


def clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def tidy_url(url: str) -> str:
    """Strip tracking junk so links are short and stable in the model's context."""
    if not url:
        return url
    p = urlsplit(url)
    host = p.netloc.lower()
    if "amazon." in host:
        m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", p.path)
        if m:
            return f"https://www.amazon.com/dp/{m.group(1)}"
        if "/sspa/click" in p.path:
            q = dict(parse_qsl(p.query))
            if "url" in q:
                return tidy_url(urljoin("https://www.amazon.com", q["url"]))
    if "ebay." in host:
        m = re.search(r"/itm/(?:[^/]+/)?(\d{9,})", p.path)
        if m:
            return f"https://www.ebay.com/itm/{m.group(1)}"
    if "aliexpress." in host:
        m = re.search(r"/item/(\d+)\.html", p.path)
        if m:
            return f"https://www.aliexpress.us/item/{m.group(1)}.html"
    if "craigslist.org" in host or "facebook.com" in host:
        return urlunsplit((p.scheme, p.netloc, p.path, "", ""))
    keep = [(k, v) for k, v in parse_qsl(p.query) if not re.match(r"^(utm_|ref|_trk|itmmeta|hash|_sp|pf_rd|pd_rd|qid|sr|dib|crid|sprefix|keywords)", k)]
    if len(urlencode(keep)) > 100:
        # Long tracking strings waste the model's context; keep only id-like parameters.
        keep = [(k, v) for k, v in keep if re.search(r"(^|_)(id|pid|sku|item|product|p|variant)$", k, re.I) and len(v) < 40]
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(keep), ""))


def relevance(query: str, title: str) -> float:
    """Share of query words found in the title (0-1). Words with digits (model numbers like
    "ar15", "g19", "300", "m8") are treated as required: missing one scores 0. A region suffix on a
    model number (U6-IW-US, -EU, -UK) is dropped: stores often list just U6-IW."""
    query = re.sub(r"\b([a-z0-9]*\d[a-z0-9]*(?:-[a-z0-9]+)*)-(?:us|eu|uk|ca|au)\b", r"\1", query.lower())
    toks = [t for t in re.findall(r"[a-z0-9]+", query) if t not in STOPWORDS]
    if not toks:
        return 1.0
    squashed = re.sub(r"[^a-z0-9]", "", title.lower())
    words = set(re.findall(r"[a-z0-9]+", title.lower()))
    hit = 0
    for t in toks:
        ok = (t in words or (len(t) >= 3 and t in squashed)
              or (t.endswith("s") and t[:-1] in words) or (t + "s") in words)
        if ok:
            hit += 1
        elif re.search(r"\d", t):
            return 0.0
    return hit / len(toks)


# ---------------------------------------------------------------- JSON-LD

def _walk_ld(obj, out):
    if isinstance(obj, list):
        for x in obj:
            _walk_ld(x, out)
        return
    if not isinstance(obj, dict):
        return
    t = obj.get("@type")
    types = t if isinstance(t, list) else [t]
    if "Product" in types or "IndividualProduct" in types or "ProductGroup" in types:
        out.append(obj)
    for k in ("@graph", "itemListElement", "item", "mainEntity", "hasVariant"):
        if k in obj:
            _walk_ld(obj[k], out)


def _offer_price(offers):
    if isinstance(offers, list):
        prices = [p for p in (_offer_price(o) for o in offers) if p is not None]
        return min(prices) if prices else None
    if isinstance(offers, dict):
        for k in ("price", "lowPrice"):
            if offers.get(k) not in (None, ""):
                return parse_price(str(offers[k]))
        if "priceSpecification" in offers:
            return _offer_price(offers["priceSpecification"])
    return None


def jsonld_products(tree: LexborHTMLParser) -> list[dict]:
    found = []
    for s in tree.css('script[type="application/ld+json"]'):
        try:
            _walk_ld(json.loads(s.text(deep=True) or "null", strict=False), found)
        except Exception:
            continue
    return found


def product_details(html: str, url: str) -> dict:
    """Key facts from a single product page (JSON-LD first, then meta tags)."""
    tree = LexborHTMLParser(html)
    info = {}
    prods = jsonld_products(tree)
    if prods:
        p = prods[0]
        offers = p.get("offers") or {}
        o0 = offers[0] if isinstance(offers, list) and offers else offers if isinstance(offers, dict) else {}
        brand = p.get("brand")
        info = {
            "name": clean(p.get("name")),
            "price": _offer_price(offers),
            "currency": o0.get("priceCurrency") if isinstance(o0, dict) else None,
            "availability": str(o0.get("availability", "")).rsplit("/", 1)[-1] if isinstance(o0, dict) else None,
            "condition": str(o0.get("itemCondition", "")).rsplit("/", 1)[-1] if isinstance(o0, dict) else None,
            "brand": brand.get("name") if isinstance(brand, dict) else brand,
            "sku": p.get("sku") or p.get("mpn") or p.get("gtin12") or p.get("gtin13"),
        }
        rating = p.get("aggregateRating")
        if isinstance(rating, dict):
            info["rating"] = f'{rating.get("ratingValue")} ({rating.get("reviewCount") or rating.get("ratingCount")} reviews)'
    for prop in ("product:price:amount", "og:price:amount"):
        m = tree.css_first(f'meta[property="{prop}"]')
        if m and not info.get("price"):
            info["price"] = parse_price(m.attributes.get("content"))
    if not info.get("name"):
        og = tree.css_first('meta[property="og:title"]')
        title = tree.css_first("title")
        info["name"] = clean(og.attributes.get("content") if og else title.text() if title else "")
    if info.get("price") == 0:
        info["price"] = None
        info["price_note"] = "no price published (call/add-to-cart pricing, or out of stock)"
    return {k: v for k, v in info.items() if v not in (None, "", "None")}


def listing_page(html: str, max_chars: int = 1000) -> dict:
    """What a seller wrote about one listing, for the LLM review: {"text": description and attributes,
    "image": URL of the main photo or None}. Craigslist's posting body and attribute list, else the page's
    JSON-LD/meta description (Facebook and most stores put the description there), else the page text."""
    tree = LexborHTMLParser(html)
    parts = []
    attrs = [clean(a.text()) for a in tree.css(".attrgroup span")]
    if attrs:
        parts.append(" ".join(attrs))
    body = tree.css_first("#postingbody")
    if body is not None:
        for junk in body.css(".print-information, .print-qrcode-container"):
            junk.decompose()
        parts.append(clean(body.text(deep=True, separator=" ")).removeprefix("QR Code Link to This Post").strip())
    else:
        desc = next((clean(p.get("description")) for p in jsonld_products(tree) if p.get("description")), "")
        if not desc:
            m = tree.css_first('meta[property="og:description"]') or tree.css_first('meta[name="description"]')
            desc = clean(m.attributes.get("content")) if m else ""
        parts.append(desc or page_markdown(html)[:max_chars])
    img = tree.css_first('meta[property="og:image"]')
    return {"text": " | ".join(p for p in parts if p)[:max_chars],
            "image": (img.attributes.get("content") or None) if img else None}


# ---------------------------------------------------------------- recipe extraction

def _first_text(node, selectors: str | None) -> str:
    if not selectors:
        return ""
    for sel in selectors.split("||"):
        n = node.css_first(sel.strip())
        if n is not None:
            txt = n.attributes.get("data-price-amount") or n.attributes.get("content") or n.text(deep=True, separator=" ")
            if clean(txt):
                return clean(txt)
    return ""


def _details(card, title: str, price_text: str, limit: int = 110) -> str:
    bits = []
    for s in card.text(deep=True, separator="\n").split("\n"):
        s = clean(s)
        if not s or len(s) < 2 or s == title or s in price_text or NOISE.match(s) or s in bits:
            continue
        if s.lower() in title.lower():
            continue
        bits.append(s)
    out = " · ".join(bits)
    return out[:limit] + ("…" if len(out) > limit else "")


def recipe_listings(html: str, base_url: str, r: dict) -> list[dict]:
    tree = LexborHTMLParser(html)
    for bad in tree.css("script, style, svg, noscript"):
        bad.decompose()
    out = []
    for card in tree.css(r["card"]):
        title = _first_text(card, r.get("title"))
        if not title or (r.get("skip_title") and re.search(r["skip_title"], title, re.I)):
            continue
        price_text = _first_text(card, r.get("price"))
        link = None
        for sel in (r.get("link") or "a[href]").split("||"):
            a = card.css_first(sel.strip())
            if a is not None and a.attributes.get("href"):
                link = a.attributes["href"]
                break
        title = clean(TITLE_JUNK.sub(" ", title))
        item = {
            "title": title,
            "price": parse_price(price_text),
            "price_text": price_text,
            "url": tidy_url(urljoin(base_url, link)) if link else base_url,
            "details": _details(card, title, price_text),
        }
        if r.get("sponsored") and card.css_first(r["sponsored"]) is not None:
            item["details"] = "SPONSORED · " + item["details"]
        out.append(item)
    return out


# ---------------------------------------------------------------- generic extraction

def _sig(node) -> str:
    cls = " ".join(sorted(c for c in (node.attributes.get("class") or "").split() if not re.search(r"\d{3,}", c)))
    return f"{node.tag}.{cls}"


def generic_listings(html: str, base_url: str) -> list[dict]:
    tree = LexborHTMLParser(html)

    # 1) JSON-LD product lists are the most reliable when a site publishes them.
    prods = jsonld_products(tree)
    ld = []
    for p in prods:
        price = _offer_price(p.get("offers"))
        if p.get("name") and price is not None:
            ld.append({"title": clean(p["name"]), "price": price, "price_text": f"${price:,.2f}",
                       "url": tidy_url(urljoin(base_url, p.get("url") or "")) or base_url, "details": ""})
    if len(ld) >= 3 and sum(1 for x in ld if x["url"] != base_url) >= 0.8 * len(ld):
        return ld

    for bad in tree.css("script, style, svg, noscript, header, footer, nav"):
        bad.decompose()
    body = tree.body
    if body is None:
        return ld

    # 2) Find leaf-ish elements whose own text is a price, then the repeated
    #    ancestor ("card") that wraps one product each.
    price_nodes = []
    for n in body.traverse(include_text=False):
        deep = n.text(deep=True)
        if len(deep) > 200:
            continue
        deep = clean(deep)
        if len(deep) > 30 or not PRICE_RE.search(deep):
            continue
        # Keep only the innermost element that contains the price.
        kids = [k for k in n.iter(include_text=False)]
        if any(PRICE_RE.search(k.text(deep=True)) for k in kids):
            continue
        price_nodes.append(n)
    if len(price_nodes) < 2:
        return ld

    # For each element signature (tag + classes), collect the distinct ancestor
    # elements that hold a price. The product card is the OUTERMOST signature that
    # still has (nearly) one element per price group; the grid container has only
    # one element, a row wrapper about half as many.
    elems: dict[str, dict] = defaultdict(dict)
    depth_sum: dict[str, int] = defaultdict(int)
    for pn in price_nodes:
        a, d, seen = pn.parent, 1, set()
        while a is not None and a.tag not in ("body", "html") and d <= 12:
            s = _sig(a)
            if s not in seen:
                seen.add(s)
                if a.mem_id not in elems[s]:
                    elems[s][a.mem_id] = a
                    depth_sum[s] += d
            a, d = a.parent, d + 1
    def linked(s):
        e = elems[s].values()
        return sum(1 for x in e if x.tag == "a" or x.css_first("a[href]") is not None) >= 0.6 * len(e)

    sizes = {s: len(e) for s, e in elems.items() if len(e) >= 2 and linked(s)}
    if not sizes:
        return ld
    top = max(sizes.values())
    cands = [s for s, n in sizes.items() if n >= 0.8 * top]
    best = max(cands, key=lambda s: depth_sum[s] / sizes[s])
    cards = list(elems[best].values())

    price_ids = {pn.mem_id for pn in price_nodes}
    out = []
    for c in cards:
        links = ([c] if c.tag == "a" and c.attributes.get("href") else []) + [
            x for x in c.css("a[href]") if not (x.attributes.get("href") or "#").startswith(("#", "javascript"))]
        cands = [clean(x.text(deep=True, separator=" ")) or clean(x.attributes.get("title")) for x in links]
        for h in c.css("h1, h2, h3, h4, h5, [class*=title], [class*=name], [itemprop=name]"):
            cands.append(clean(h.text(deep=True, separator=" ")))
        img = c.css_first("img[alt]")
        if img is not None:
            cands.append(clean(img.attributes.get("alt")))
        # A product name is the longest text that is not just a price or a badge.
        cands = [PRICE_RE.sub("", t).strip(" -·|") for t in cands if t]
        cands = [t for t in cands if len(t) >= 6 and not NOISE.match(t)]
        if not cands:
            continue
        shortest_full = sorted(cands, key=len)
        title = max(cands, key=len)
        # ...but if the longest one is the whole card text, prefer a shorter one it contains.
        for t in shortest_full:
            if len(t) >= 15 and t in title and len(title) > 2.5 * len(t):
                title = t
                break
        # Current price: the first price element that isn't shipping, savings, a list/was
        # price, or a free-shipping threshold.
        price, all_prices = None, []
        for n in c.traverse(include_text=False):
            if n.mem_id not in price_ids:
                continue
            txt = clean(n.text(deep=True))
            p = parse_price(txt)
            if p is None:
                continue
            all_prices.append(p)
            # Look at the words just before and after the price, within the card.
            scope = n.parent.parent if n.parent is not None and n.parent.parent is not None else n
            stext = clean(scope.text(deep=True))
            at = stext.find(txt)
            before = (stext[max(0, at - 28):at].lower() if at >= 0 else "") + " " + txt.lower().split("$")[0]
            after = stext[at + len(txt):at + len(txt) + 22].lower() if at >= 0 else ""
            struck = False
            a, d = n, 0
            while a is not None and d < 4:
                if a.tag in ("del", "s", "strike") or re.search(r"strike|old-price|was-price|list-price|regular-price|msrp|compare",
                                                                 a.attributes.get("class") or "", re.I):
                    struck = True
                    break
                a, d = a.parent, d + 1
            if price is None and not struck and not re.search(r"was|list|regular|typical|msrp|compare|save|reg\.?|\+|on orders|over", before) \
                    and not re.search(r"^\s*(deliver|shipping|off\b|/ ?(oz|ounce|count|ea|ct))", after):
                price = p
        if price is None and all_prices:
            price = all_prices[0]
        if price is None:
            continue
        ptxt = " / ".join(dict.fromkeys(f"${p:,.2f}" for p in all_prices))
        href = None
        for x in links:
            lt = clean(x.text(deep=True, separator=" "))
            if lt and (lt in title or title in lt):
                href = x.attributes["href"]
                break
        href = href or (links[0].attributes["href"] if links else None)
        out.append({
            "title": clean(TITLE_JUNK.sub(" ", title))[:160],
            "price": price,
            "price_text": ptxt,
            "url": tidy_url(urljoin(base_url, href)) if href else base_url,
            "details": _details(c, title, ptxt),
        })
    return out if len(out) >= len(ld) else ld


# ---------------------------------------------------------------- facebook marketplace

def facebook_listings(html: str) -> list[dict]:
    dec = json.JSONDecoder()
    out, seen = [], set()
    for m in re.finditer(r'"listing":(\{"__typename":"GroupCommerceProductItem")', html):
        try:
            obj, _ = dec.raw_decode(html, m.start(1))
        except Exception:
            continue
        lid = obj.get("id")
        if not lid or lid in seen or obj.get("is_sold") or obj.get("is_hidden"):
            continue
        seen.add(lid)
        price = (obj.get("listing_price") or {})
        geo = ((obj.get("location") or {}).get("reverse_geocode") or {})
        loc = ", ".join(x for x in (geo.get("city"), geo.get("state")) if x)
        bits = [loc] if loc else []
        if obj.get("is_pending"):
            bits.append("PENDING")
        strike = obj.get("strikethrough_price") or {}
        if strike.get("formatted_amount"):
            bits.append(f'was {strike["formatted_amount"]}')
        if obj.get("delivery_types") and obj["delivery_types"] != ["IN_PERSON"]:
            bits.append("ships")
        out.append({
            "title": clean(obj.get("marketplace_listing_title") or obj.get("custom_title") or ""),
            "price": parse_price(price.get("amount")),
            "price_text": price.get("formatted_amount", ""),
            "url": f"https://www.facebook.com/marketplace/item/{lid}/",
            "details": " · ".join(bits),
        })
    return out


# ---------------------------------------------------------------- walmart

def walmart_listings(html: str, base_url: str) -> list[dict]:
    """Walmart's search results from the page's __NEXT_DATA__ JSON. The HTML cards are not enough: a search
    with a single result has no repeated card for the generic extractor to find. Only the first item stack
    is read; later ones are "Other options to consider", not results."""
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    try:
        stacks = json.loads(m.group(1))["props"]["pageProps"]["initialData"]["searchResult"]["itemStacks"]
    except Exception:
        return []
    out = []
    for it in (stacks[0].get("items") or []) if stacks else []:
        if it.get("__typename") != "Product" or not it.get("name"):
            continue
        price = it.get("price")
        info = it.get("priceInfo") or {}
        bits = ["SPONSORED"] if it.get("isSponsoredFlag") else []
        if it.get("sellerName") and it["sellerName"] != "Walmart.com":
            bits.append(f'sold by {it["sellerName"]}')
        if info.get("wasPrice"):
            bits.append(f'was {info["wasPrice"]}')
        avail = (it.get("availabilityStatusV2") or {}).get("display")
        if avail and avail != "In stock":
            bits.append(avail)
        out.append({
            "title": clean(it["name"]),
            "price": float(price) if price else None,
            "price_text": info.get("linePriceDisplay") or info.get("linePrice") or (f"${price:,.2f}" if price else ""),
            "url": tidy_url(urljoin(base_url, it.get("canonicalUrl") or f'/ip/{it.get("usItemId")}')),
            "details": " · ".join(bits),
        })
    return out


# ---------------------------------------------------------------- page text

def page_markdown(html: str) -> str:
    """Main content of a page as compact Markdown (menus, headers, footers and images removed)."""
    from scrapling.parser import Selector
    from scrapling.core.shell import Convertor

    tree = LexborHTMLParser(html)
    for bad in tree.css("script, style, noscript, svg, header, nav, footer, aside, iframe, form[role=search], "
                        "[role=navigation], [role=banner], [role=contentinfo], [aria-hidden=true], .sr-only, "
                        "[class*=megamenu], [class*=mega-menu], [id*=menu], [class*=breadcrumb], [class*=cookie]"):
        bad.decompose()
    main = None
    for sel in ("main", "[role=main]", "#maincontent", "#main-content", "#main", "#content", "article"):
        m = tree.css_first(sel)
        if m is not None and len(m.text(deep=True)) > 500:
            main = m
            break
    node = main or tree.body or tree.root
    page = Selector(node.html or "")
    page = Convertor._sanitize_for_ai(Convertor._strip_noise_tags(page))
    md = Convertor._convert_to_markdown(page.html_content)
    md = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", md)                 # images
    md = re.sub(r"\[([^\]]*)\]\((?:#|javascript)[^)]*\)", r"\1", md)  # in-page links
    md = re.sub(r"[ \t]+\n", "\n", md)
    return re.sub(r"\n{3,}", "\n\n", md).strip()
