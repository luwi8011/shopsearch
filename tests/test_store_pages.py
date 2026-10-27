"""Store result pages: a store's own "no results" page is reported as an empty search (not a failure, and its
suggested products are not counted), a single-result Walmart page is read, and an unreadable page still fails."""
import asyncio
import json

import _harness as t  # noqa: F401  (temp data dir, no HA)
import server  # noqa: E402
from fetcher import Page  # noqa: E402

PAGES: dict[str, str] = {}
BROWSER = []


async def fake_fetch(url, mode="auto", prefer=None, wait_selector=None):
    if mode == "browser":
        BROWSER.append(url)
    html = next(h for k, h in PAGES.items() if k in url)
    return Page(url=url, status=200, html=html, tier=mode, seconds=0.1)


def walmart(stacks):
    data = {"props": {"pageProps": {"initialData": {"searchResult": {
        "aggregatedCount": len(stacks[0]) if stacks else 0,
        "itemStacks": [{"items": [{"__typename": "Product", "name": n, "price": p, "canonicalUrl": f"/ip/x/{i}"}
                                  for i, (n, p) in enumerate(s)]} for s in stacks]}}}}}
    return (f'<html><body><script id="__NEXT_DATA__" type="application/json">{json.dumps(data, separators=(",", ":"))}'
            '</script></body></html>')


def newegg_card(n):
    return f'<div class="item-cell"><a class="item-title" href="/p/{n}">Suggested gadget {n}</a><li class="price-current">$9.99</li></div>'


def craigslist(titles):
    lis = "".join(f'<li class="cl-static-search-result" title="{x}"><a href="https://x.craigslist.org/{n}">'
                  f'<div class="title">{x}</div><div class="price">$50</div></a></li>' for n, x in enumerate(titles))
    return (f'<html><body><ol class="cl-static-search-results">\n<li class="cl-static-hub-links">\n<div>see also</div>\n'
            f'</li>\n{lis}\n</ol></body></html>')


async def status_of(site, query):
    rows, status, results = await server.collect(query, [site])
    return rows, status[0], results[0]


async def main():
    server.F.fetch = fake_fetch

    # One result, searched by the regional SKU the store doesn't print; "Other options" aren't results.
    PAGES["walmart.com"] = walmart([[("Ubiquiti Networks U6 In-Wall (U6-IW) Access Point", 257.9)],
                                    [("Ubiquiti U6-IW Wall Plate", 9.0), ("Ubiquiti U6 Pro Access Point", 149.0)]])
    rows, line, _ = await status_of("walmart", "U6-IW-US")
    assert [(r[1]["title"], r[1]["price"]) for r in rows] == [("Ubiquiti Networks U6 In-Wall (U6-IW) Access Point", 257.9)], rows
    assert rows[0][1]["url"].endswith("/ip/x/0"), rows

    PAGES["walmart.com"] = walmart([[]])
    rows, line, res = await status_of("walmart", "nothing here")
    assert not rows and res["empty"] and "FAILED" not in line, line

    PAGES["walmart.com"] = "<html><body><p>Robot or human?</p></body></html>"
    BROWSER.clear()
    rows, line, res = await status_of("walmart", "anything")
    assert "FAILED" in line and not res["empty"] and BROWSER, line

    PAGES["newegg.com"] = ("<html><body><p>We have found 0 items that match &quot;zzzqqq&quot;.</p>"
                           + "".join(newegg_card(n) for n in range(36)) + "</body></html>")
    BROWSER.clear()
    rows, line, res = await status_of("newegg", "zzzqqq")
    assert not rows and res["empty"] and not res["items"] and not BROWSER, (line, BROWSER)
    assert "36" not in line and "FAILED" not in line, line

    PAGES["craigslist.org"] = craigslist([])
    rows, line, res = await status_of("craigslist", "zzzqqq")
    assert res["empty"] and "FAILED" not in line, line

    PAGES["craigslist.org"] = craigslist(["Ubiquiti access point"])
    rows, line, res = await status_of("craigslist", "ubiquiti access point")
    assert not res["empty"] and [r[1]["title"] for r in rows] == ["Ubiquiti access point"], (line, rows)

    print("test_store_pages: OK")


asyncio.run(main())
