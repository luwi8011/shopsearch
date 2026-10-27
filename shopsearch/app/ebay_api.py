"""eBay Browse API (official, free: 5,000 calls/day with a developer account).

Used instead of scraping eBay when EBAY_CLIENT_ID and EBAY_CLIENT_SECRET are set.
Get them at https://developer.ebay.com -> My Account -> Application Keys -> Production keyset.
"""

import base64
import os
import time

import httpx

CLIENT_ID = os.environ.get("EBAY_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("EBAY_CLIENT_SECRET", "")
ZIP = os.environ.get("HOME_ZIP", "")

_token: tuple[str, float] = ("", 0.0)


def enabled() -> bool:
    return bool(CLIENT_ID and CLIENT_SECRET)


async def _get_token(c: httpx.AsyncClient) -> str:
    global _token
    if _token[0] and time.time() < _token[1] - 60:
        return _token[0]
    auth = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    r = await c.post("https://api.ebay.com/identity/v1/oauth2/token",
                     headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
                     data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"})
    r.raise_for_status()
    d = r.json()
    _token = (d["access_token"], time.time() + int(d.get("expires_in", 7200)))
    return _token[0]


async def search(query: str, *, price_sort=False, min_price=None, max_price=None, condition=None, limit=50,
                 pickup_radius_mi: int | None = None) -> list[dict]:
    """pickup_radius_mi: only listings the seller lets you pick up within this many miles of HOME_ZIP."""
    filters = []
    if min_price is not None or max_price is not None:
        lo = f"{min_price:g}" if min_price is not None else ""
        hi = f"{max_price:g}" if max_price is not None else ""
        filters += [f"price:[{lo}..{hi}]", "priceCurrency:USD"]
    if condition == "new":
        filters.append("conditions:{NEW}")
    elif condition == "used":
        filters.append("conditions:{USED}")
    if pickup_radius_mi:
        if not ZIP:
            raise ValueError("local pickup needs a home ZIP code (home_zip)")
        filters += ["deliveryOptions:{SELLER_ARRANGED_LOCAL_PICKUP}", "pickupCountry:US", f"pickupPostalCode:{ZIP}",
                    f"pickupRadius:{pickup_radius_mi}", "pickupRadiusUnit:mi"]
    params = {"q": query, "limit": str(limit)}
    if filters:
        params["filter"] = ",".join(filters)
    if price_sort:
        params["sort"] = "price"
    headers = {"X-EBAY-C-MARKETPLACE-ID": "EBAY_US"}
    if ZIP:
        headers["X-EBAY-C-ENDUSERCTX"] = f"contextualLocation=country=US,zip={ZIP}"
    async with httpx.AsyncClient(timeout=25) as c:
        headers["Authorization"] = f"Bearer {await _get_token(c)}"
        r = await c.get("https://api.ebay.com/buy/browse/v1/item_summary/search", params=params, headers=headers)
        r.raise_for_status()
        data = r.json()
    out = []
    for it in data.get("itemSummaries", []):
        price = it.get("price") or it.get("currentBidPrice") or {}
        bits = [it.get("condition", "")]
        opts = it.get("buyingOptions") or []
        if "AUCTION" in opts:
            bid = (it.get("currentBidPrice") or {}).get("value")
            bits.append(f"AUCTION{' (bid $' + bid + ')' if bid else ''}, ends {it.get('itemEndDate', '')[:10]}")
        ship = (it.get("shippingOptions") or [{}])[0].get("shippingCost", {}).get("value")
        if ship is not None:
            bits.append("free shipping" if float(ship) == 0 else f"+${float(ship):.2f} shipping")
        loc = it.get("itemLocation") or {}
        if loc.get("postalCode") or loc.get("country"):
            bits.append(f"ships from {loc.get('postalCode', '')} {loc.get('country', '')}".strip())
        seller = it.get("seller") or {}
        if seller.get("feedbackPercentage"):
            bits.append(f"seller {seller['feedbackPercentage']}% ({seller.get('feedbackScore')})")
        out.append({
            "title": it.get("title", ""),
            "price": float(price["value"]) if price.get("value") else None,
            "price_text": f"${price.get('value')}" if price.get("value") else "",
            "url": f"https://www.ebay.com/itm/{it['legacyItemId']}" if it.get("legacyItemId") else it.get("itemWebUrl", ""),
            "details": " · ".join(b for b in bits if b),
        })
    return out
