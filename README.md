# shopsearch

Price-research tools for the local LLM (Open WebUI + Strata), served as an MCP server, with scheduled hunts and Home Assistant alerts.

Docs: [docs/HANDOFF.md](docs/HANDOFF.md) (architecture, deployment, decisions) · [docs/HOME_ASSISTANT.md](docs/HOME_ASSISTANT.md) · [docs/ROADMAP.md](docs/ROADMAP.md) · [tests/README.md](tests/README.md)

- Open WebUI connection: **Shop Search** (`http://127.0.0.1:8765/mcp`, id `shop`). Attached by default to the qwen3.8-flash-next model (tool id `server:mcp:shop`); for other models enable it per chat from `+` → Tools.
- Tools: `shop_search` (one price-sorted table across stores), `web_search` (SearXNG at 192.168.0.60:8080), `fetch_page` (any page, product facts first, size-capped), and the watch tools below.
- Rebuild after editing `app/`: `docker compose up -d --build`
- Test without the LLM:
  - `curl 'http://127.0.0.1:8765/api/search?q=ar15+lower+parts+kit&sites=psa,dahlonega,amazon'`
  - `curl 'http://127.0.0.1:8765/api/fetch?url=https://...'`
  - `curl 'http://127.0.0.1:8765/api/web?q=...'`
- Logs: `docker logs -f shopsearch`

## How fetching works (app/fetcher.py)

1. Plain HTTP with a real browser's TLS fingerprint (curl_cffi; Safari, then Chrome). About 1 s per page. On a 403 it loads the homepage first for cookies and retries (eBay needs this).
2. A stealth Chromium (Scrapling 0.5 / patchright) that runs JavaScript and solves Cloudflare challenges. Takes 8 to 20 s. PSA needs this on every request.

The method that worked for each domain is saved in `data/tiers.json`. Requests to one domain are spaced at least 2.5 s apart (`MIN_INTERVAL_S`), and pages are cached for 10 minutes (`CACHE_TTL_S`). Bursts of requests are what get the home IP flagged.

## Sites (app/sites.py)

Built in: amazon, ebay, psa, dahlonega, mxlocker, craigslist and facebook (local to Erie, CO: `HOME_ZIP` 80516 and `FB_LOCATION` erie-co, default radius `LOCAL_RADIUS_MI` 25 mi; the model can pass `radius_miles` per search; Craigslist's radius covers neighbouring CL sites such as Boulder; Facebook logged out shows about 10 to 24 newest listings), walmart, newegg, gunbroker, aliexpress.
Any other store: pass its domain. The search URL is found from the homepage (schema.org SearchAction, the search form, or Shopify/Woo/Magento/BigCommerce defaults) and saved in `data/learned_sites.json`. If that fails, it falls back to a `site:` web search plus product pages.
To add a store permanently, add an entry with just a `search` URL. CSS selectors are optional, because the generic extractor finds product cards on its own.

## Watches (scheduled searches, app/watches.py)

`watch_create` saves a shop_search and re-runs it every N hours (default 6, min 1) in this container's background loop, whether or not any chat is open. Watches run one at a time. Each listing is remembered by URL, with when it was first seen and its price history.
`watch_report` shows new listings and price drops since the last report, plus the cheapest ones still listed. It then marks the watch as reported, so asking "any updates?" in any chat the next day gives only what changed.
`watch_manage` lists, pauses, resumes, deletes, re-runs (`check_now`) or changes the interval of a watch.
State lives in `data/watches.json`. Read-only view: `curl http://127.0.0.1:8765/api/watches` (add `?id=w1` for a report that does not mark it read).
LLM review (app/review.py): before any alert, the local model (Strata, `LLM_URL`/`LLM_MODEL`) checks each new listing against the watch's `want` text, which Qwen writes when it creates the watch. It decides whether the listing is a real match (not parts, gear, "wanted" posts or other models) and whether it is a repost of a listing already accepted. A cheaper repost alerts as a price drop. It also writes a one-sentence comparison, which appears under each item in the phone alert and in reports. Listings wait until a review succeeds; failed reviews retry every 10 minutes. Rejected listings are hidden from reports, which show only a count.

Hunts: searches are grouped into hunts, one per goal. All query variations for the same thing share a `hunt` name and a `want` (the match description, set by the first search). A listing found by two searches of a hunt alerts once, and repost detection spans the whole hunt. `watch_manage` can `move` a search between hunts, `delete_hunt`, and `set_want`.

Home Assistant (app/ha.py, HA at 192.168.0.161, token `HA_TOKEN` in `.env`). Everything is rebuilt from `data/watches.json` by `Watches.sync_ha()`:
- Each hunt has a switch `input_boolean.shop_hunt_<slug>`, shown as a tile in the "Shop Watches" section of the Tools dashboard (`HA_DASHBOARD=ha-tools`). Tapping the tile opens the hunt's page (sub-view `shop-<slug>`), which shows its goal, its searches, and every alert sent. The alerts come from `sensor.shop_hunt_<slug>`, a REST-pushed sensor that is re-pushed after an HA restart. Tapping the icon turns the hunt off after a confirmation, and the hunt and all its searches are deleted within about 30 s.
- Request box: type into `input_text.shop_watch_request` and press "Send to Qwen" (`input_button.shop_watch_request_send`). app/agent.py runs the local LLM with shop_search, list_hunts and create_search tools. It creates a hunt and puts its one-sentence answer in `sensor.shop_watch_request_status` and on the phone. This takes a few minutes, because every new search's first results are reviewed.
- Self-healing: switches are created only inside `sync_ha` (under one lock). Any `shop_hunt_`/`shop_watch_` switch that no hunt owns is deleted. A hunt ends only when its switch is off, or missing for 3 polls while HA reports RUNNING.
- Who gets alerts: the person who pressed "Send to Qwen" gets Qwen's answer and owns the hunt's alerts. Their HA user maps to a person, then to `device_tracker.<x>`, then to `notify.mobile_app_<x>`. Anyone who adds to an existing hunt is added to its recipients. Hunts from Open WebUI, or from an account with no phone (e.g. Guest), go to `HA_NOTIFY` (`mobile_app_localhost`, Lucas). Each hunt's page shows who gets its alerts.
- Event log: `data/shopsearch.log` (survives rebuilds).

Condition filter (`condition` new/used): eBay, Craigslist (used = like new to fair), Facebook. Retail stores ignore it.

## Known limits

- **eBay** (Akamai) blocks scripted access from the home IP after bursts of requests. Durable fix: free eBay developer keys. Set `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` (Production keyset from developer.ebay.com) in docker-compose.yml, and eBay then uses the official Browse API (5,000 calls/day). That code path (`app/ebay_api.py`) is untested until keys exist.
- **Home Depot / Lowe's** (Akamai) refuse both HTTP and the headless browser, so they are not in the built-in list.
- **Amazon** does not sell most firearm parts (it bans them), so gun-part searches there return accessories.
- **Facebook Marketplace** without login shows only the first batch of results. Logging in would mean storing Facebook cookies; not done.
