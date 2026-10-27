# shopsearch: handoff

A price-research and deal-watching service for a self-hosted LLM. It searches stores and
marketplaces directly, returns real listings sorted by price, and runs saved searches ("hunts")
on a schedule. A local LLM reviews new listings before a phone alert goes out through
Home Assistant.

Built October 2026 in Claude Code / omp sessions. This file is the map. `docs/HOME_ASSISTANT.md` covers the
Home Assistant side and `docs/ROADMAP.md` the open ideas. Notes about one particular install (hosts, people)
belong in a git-ignored `LOCAL.md`, never in the repo: it is public.

## How it runs

| Piece | Notes |
|---|---|
| shopsearch | A Home Assistant OS add-on (`shopsearch/`, installed from this repo, which is also the add-on repository via `repository.yaml`), or plain Docker (`docker-compose.yml`). Listens on port 8765 |
| Settings | Add-on: Configuration tab → `/data/options.json`, turned into environment variables by `app/addon_options.py` before anything else is imported. Docker: `.env` |
| Home Assistant | Add-on: the Supervisor proxy (`http://supervisor/core`, `ws://supervisor/core/websocket`) with `SUPERVISOR_TOKEN` (`homeassistant_api: true`). Docker: `HA_URL` + a long-lived token |
| LLM | Any OpenAI-compatible endpoint (`LLM_URL`, `LLM_MODEL`), used by the reviewer and the request agent |
| SearXNG | Optional, for `web_search` |
| MCP client | Open WebUI or any MCP client, Streamable HTTP at `/mcp`, `Authorization: Bearer <API_KEY>` |

Persistent state lives in `/data` (the add-on's data folder, included in HA backups except the browser
profile and logs): `watches.json` (hunts, searches, every listing seen, alert history), `tiers.json` (which
fetch method works per site), `learned_sites.json` (auto-discovered search URLs), `browser-profile/`
(Chromium cookies), `health.json` (daily store test results) and `shopsearch.log` (watch, Home Assistant, agent
and store-health events, rotated).

Releasing: bump `version` in `shopsearch/config.yaml`, add a `shopsearch/CHANGELOG.md` entry, push `work`. The
"Add-on image" GitHub Actions workflow builds `ghcr.io/luwi8011/shopsearch-amd64:<version>` (never rebuilding a
published version). Once it has succeeded, fast-forward `main`; Home Assistant then offers the update and pulls
the image (`image:` in config.yaml). Moving `main` before the image exists makes the update fail.

## Architecture

```
Open WebUI ──MCP (streamable HTTP, /mcp)──┐
Home Assistant request "Send" button ────┤  (polled every 5 s)
                                          ▼
                               app/server.py  (FastMCP + REST mirrors + background tasks)
             ┌────────────────────┬───────┴─────────┬──────────────────────┐
     shop_search / fetch    web_search         watches.py              agent.py
             │               (SearXNG)      (hunts, scheduler)   (HA request → LLM tool loop)
     sites.py + fetcher.py                   │        │
     + extract.py + ebay_api.py         review.py    ha.py ──REST/WebSocket──► Home Assistant
                                       (LLM judge)   (switches, pages, notify)
```

### Files (shopsearch/app/)

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
  preferred tier, `no_results` regex for the store's own "nothing matched" page) and the build of search URLs:
  - `no_results` lets a search report "the site found nothing" instead of FAILED, and keeps the suggested
    products Newegg/Walmart put on that page out of the results. A store without it can't tell an empty search
    from a layout the extractor no longer reads.
  - Filters: price, condition, Craigslist postal/radius, Facebook location/radius.
  - Unknown stores: their search URL is found by discovery (schema.org SearchAction, GET search forms,
    Shopify/Woo/Magento/BigCommerce defaults) and cached.
- **extract.py**: turns pages into listings, in three layers: recipe selectors, JSON-LD, then a generic
  "repeated card with a price and a link" heuristic, plus custom Facebook and Walmart (`__NEXT_DATA__` JSON;
  the generic layer can't see a single-result page) parsers.
  - It skips struck-through, list, was and save prices and shipping amounts.
  - `relevance()`: the share of query words in the title, with words that contain digits mandatory and a
    region suffix on a model number (U6-IW-US) optional.
  - `product_details()` for product pages, and `page_markdown()` for size-capped page text.
- **ebay_api.py**: eBay Browse API client (OAuth client credentials), used for `ebay` when keys are set and
  always for `ebay-local` (pickup filters: `deliveryOptions:{SELLER_ARRANGED_LOCAL_PICKUP}` + pickup ZIP/radius).
  Always sends `buyingOptions:{FIXED_PRICE|AUCTION}`: by default the API drops an auction once it has a bid.
- **health.py**: daily canary search per built-in store (`canary` in sites.py); two empty results in a row notify
  the default phone, recovery notifies again. State in `health.json`, shown by `GET /health`.
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
- **Local settings** (add-on options): `home_zip`, `craigslist_site`, `facebook_location`,
  `local_radius_miles`.
  - Craigslist: the configured site with `postal=<zip>&search_distance=<radius>`, which also returns
    neighbouring Craigslist sites.
  - Facebook: the configured location slug, radius in km.
  - `radius_miles` per search overrides the default.
  - The request agent's "About the user" comes from `about_user` (falls back to the ZIP).
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
- **eBay scraping** (Akamai): it hard-blocked scripted access from the home IP after a burst of ~20 test requests on
  2026-10-22, and it is intermittently blocked since. With API keys set, eBay goes through the official API instead
  (working since 2026-10-27).
- **Home Depot, Lowe's** (Akamai): they refuse even the browser, so they were removed from the built-in list.
- **primaryarms.com**: search URL discovery fails.

## Open WebUI integration

- Admin Panel → Settings → External Tools: type MCP (Streamable HTTP), url `http://<host>:8765/mcp`, Bearer auth
  with the API key. The connection id (e.g. `shop`) becomes the tool-name prefix.
- Attach it to a model by default (`model.meta.toolIds = ["server:mcp:<id>"]`), or enable it per chat via `+` → Tools.

## Testing

- REST (add `-H "Authorization: Bearer $KEY"`): `/api/search?q=ktm+300&sites=craigslist,facebook&radius_miles=60`,
  `/api/fetch?url=...`, `/api/web?q=...`, and `/api/watches` (`?id=w14` for a report that is not marked read).
- `tests/`: scripts that run against fakes and a temp data dir, locally or in the container (see tests/README.md).
- The server also runs locally (`DATA_DIR=<tmp> python shopsearch/app/server.py`, with an `options.json` in
  `DATA_DIR`); only the browser tier needs `patchright install chromium`.
- **Do not burst-test eBay or other Akamai sites from a home network.** That is how eBay got blocked.

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
