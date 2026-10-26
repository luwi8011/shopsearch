# shopsearch: handoff

A price-research and deal-watching service for a self-hosted LLM. It searches stores and
marketplaces directly, returns real listings sorted by price, and runs saved searches ("hunts")
on a schedule. A local LLM reviews new listings before a phone alert goes out through
Home Assistant.

Built 2026-10-22 to 2026-10-26 on the `llm` VM, in Claude Code sessions. This file is the map.
`docs/HOME_ASSISTANT.md` covers the Home Assistant side and `docs/ROADMAP.md` the open ideas.

## Where it runs today

| Piece | Where | Notes |
|---|---|---|
| shopsearch (this repo) | Docker on the `llm` VM, 192.168.0.252, container `shopsearch` | Listens on `127.0.0.1:8765` only. `docker compose up -d --build` after edits |
| Open WebUI 0.12.3 | Same VM, Docker, host networking, port 8080 | Connects to shopsearch over MCP |
| LLM: Strata serving `qwen3.8-flash-next-iq3_xxs` | Same VM, `systemctl --user` service `strata`, `0.0.0.0:11434/v1` (OpenAI-compatible, no key) | Used by Open WebUI and by shopsearch's reviewer and request agent |
| SearXNG | Docker VM 192.168.0.60:8080 | Used by `web_search`; needed no changes |
| Home Assistant 2026.10.1 | 192.168.0.161:8123 (`homeassistant.local`) | See docs/HOME_ASSISTANT.md |

Persistent state lives in `./data` (git-ignored): `watches.json` (hunts, searches, every listing
seen, alert history), `tiers.json` (which fetch method works per site), `learned_sites.json`
(auto-discovered search URLs), `browser-profile/` (Chromium cookies) and `shopsearch.log`
(watch, Home Assistant and agent events, rotated, survives rebuilds). Docker's own log is lost on
every rebuild, so check `shopsearch.log` first.

Secrets live in `./.env` (git-ignored; template in `.env.example`): `HA_TOKEN`, and the optional
eBay keys.

## Architecture

```
Open WebUI ──MCP (streamable HTTP, /mcp)──┐
Home Assistant "Send to Qwen" button ─────┤  (polled every 5 s)
                                          ▼
                               app/server.py  (FastMCP + REST mirrors + background tasks)
             ┌────────────────────┬───────┴─────────┬──────────────────────┐
     shop_search / fetch    web_search         watches.py              agent.py
             │               (SearXNG)      (hunts, scheduler)   (HA request → LLM tool loop)
     sites.py + fetcher.py                   │        │
     + extract.py + ebay_api.py         review.py    ha.py ──REST/WebSocket──► Home Assistant
                                       (LLM judge)   (switches, pages, notify)
```

### Files (app/)

- **server.py**: entry point. Holds the MCP tools (`shop_search`, `web_search`, `fetch_page`,
  `watch_create`, `watch_report`, `watch_manage`), REST mirrors (`/health`, `/api/search`,
  `/api/fetch`, `/api/web`, `/api/watches`) and the HA request poller `request_loop()`.
  `main()` runs uvicorn plus the background tasks on one event loop.
- **fetcher.py**: tiered page fetching.
  - Tier 1: curl_cffi with TLS impersonation, Safari then Chrome, plus a homepage cookie warm-up retry on a 403.
  - Tier 2: Scrapling 0.5 `AsyncStealthySession` (patchright Chromium, solves Cloudflare challenges).
  - The tier that worked is remembered per domain.
  - Politeness: at least 2.5 s between requests to one domain, and a 10-minute page cache.
- **sites.py**: store "recipes" (search URL template, optional CSS selectors, price-sort parameter,
  preferred tier) and the build of search URLs:
  - Filters: price, condition, Craigslist postal/radius, Facebook location/radius.
  - Unknown stores: their search URL is found by discovery (schema.org SearchAction, GET search forms,
    Shopify/Woo/Magento/BigCommerce defaults) and cached.
- **extract.py**: turns pages into listings, in three layers: recipe selectors, JSON-LD, then a generic
  "repeated card with a price and a link" heuristic, plus a custom Facebook parser.
  - It skips struck-through, list, was and save prices and shipping amounts.
  - `relevance()`: the share of query words in the title, with words that contain digits mandatory.
  - `product_details()` for product pages, and `page_markdown()` for size-capped page text.
- **ebay_api.py**: eBay Browse API client (OAuth client credentials). Inactive and untested; it needs keys.
- **watches.py**: hunts and searches, the scheduler loop, change detection (new listings and price
  drops), review gating, alert recording and `sync_ha()`, which makes Home Assistant match the state.
- **review.py**: the LLM judge. For each new listing it decides match or no match, whether it is a
  repost of an accepted listing, and writes a one-line note comparing it to the hunt's `want`.
- **agent.py**: the request-box agent. It runs a tool loop (shop_search, list_hunts, create_search)
  that turns a typed request into a hunt.
- **ha.py**: the Home Assistant client: REST, WebSocket, helpers, notify, recipients and the dashboard renderer.

### Key behaviours and decisions

- **Hunts.** A hunt is one goal with one or more searches (query or site variations).
  - Each hunt has one HA switch, one page, one alert history and one `want`.
  - `want` is set by the hunt's first search; later searches don't change it (use `set_want`).
  - A listing found by two searches alerts once, and repost detection spans the whole hunt.
- **Alerts.**
  - The first run of a search is a baseline and never alerts.
  - Later runs: new listings, and drops in price, are reviewed by the LLM. Only accepted, non-repost listings alert.
  - A repost at a lower price alerts as a price drop.
  - Listings wait, with no alert, until a review succeeds; failed reviews retry every 10 minutes.
- **Relevance rule.** Every query word containing a digit must appear in the title. So use one model
  number per query, never "2853-20 2850-20" in one query. This is in the tool descriptions and the agent prompt.
- **Local defaults.**
  - Erie, CO, ZIP 80516.
  - Craigslist: the `denver` site with `postal=80516&search_distance=25`, which also returns
    neighbouring Craigslist sites such as Boulder.
  - Facebook: slug `erie-co`, radius in km.
  - `radius_miles` per search overrides the 25-mile default.
- **Condition filter:** eBay (`LH_ItemCondition`), Craigslist (`condition=10` new; 20–50 used), and
  Facebook (`itemCondition`). Retail sites ignore it.
- **MCP SDK pinned to `mcp==1.30.0`.** Version 2.x changed the API.
- **Open WebUI tool naming:** it prefixes MCP tools with the connection id, so the model sees
  `shop_shop_search`, `shop_watch_create` and so on.

## Status of each site (last tested 2026-10-22 to 2026-10-26)

**Working:**
- Amazon: http, ~1 s. It bans most firearm parts, so gun-part searches return only accessories.
- Palmetto State Armory: browser tier, Cloudflare, 8–18 s.
- Dahlonega Armory and MX Locker.
- Craigslist and Facebook Marketplace (logged out, first ~15–24 listings).
- Walmart, Newegg, AliExpress and GunBroker (browser).
- Auto-discovered: Brownells and MidwayUSA.

**Blocked or broken:**
- **eBay** (Akamai): it hard-blocked scripted access from the home IP after a burst of ~20 test requests on
  2026-10-22, and it is intermittently blocked since. The durable fix is the official API (see ROADMAP).
- **Home Depot, Lowe's** (Akamai): they refuse even the browser, so they were removed from the built-in list.
- **primaryarms.com**: search URL discovery fails.

## Open WebUI integration

- MCP connection is stored in the Open WebUI DB (`/home/lucas/llm/open-webui/webui.db`, key
  `tool_server.connections`): url `http://127.0.0.1:8765/mcp`, type `mcp`, auth none, `info.id` `shop`,
  name "Shop Search". It was written via `docker exec -i open-webui python -` because the DB is root-owned,
  then `docker compose restart open-webui`. DB backups are `webui.db.bak-<epoch>`.
- Attached by default to model `qwen3.8-flash-next-iq3_xxs` (`model.meta.toolIds = ["server:mcp:shop"]`).
  Other models enable it per chat via `+` → Tools.

## Testing

- REST: `curl 'http://127.0.0.1:8765/api/search?q=ktm+300&sites=craigslist,facebook&radius_miles=60'`,
  `/api/fetch?url=...`, `/api/web?q=...`, and `/api/watches` (`?id=w14` for a report that is not marked read).
- `tests/`: scripts that run inside the container against fakes and a temp data dir (see tests/README.md).
- **Do not burst-test eBay or other Akamai sites from this network.** That is how eBay got blocked.

## Lessons learned (keep these)

- FlareSolverr and Byparr only get past bot checks; they don't extract anything. The tiered
  curl_cffi → stealth-browser fetcher plus extraction was the useful part.
- Bot managers score IP plus fingerprint plus burst rate. Pacing and caching matter more than tricks.
- **Duplicate HA switches (fixed 2026-10-26).** The cause was a race: watch creation and the background
  loop both created a switch. Now only `sync_ha()` creates switches, under one lock, and it deletes any
  shop switch that no hunt owns.
- **Never treat "entity missing" as "user deleted it" while HA is starting.** A hunt ends only when its
  switch is `off`, or missing for 3 polls while `/api/config` reports `RUNNING`.
- **Local-model tool failures tend to be silent.** When a model "forgets" or stops, read the actual tool
  calls in the Open WebUI `chat` table before blaming the model.
