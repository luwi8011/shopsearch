# shopsearch

Price research and deal hunting for a self-hosted LLM. It searches stores and marketplaces directly,
returns real listings sorted by price, and re-runs saved searches ("hunts") on a schedule. Your local
model reviews each new listing before a phone alert goes out through Home Assistant.

- An MCP server for Open WebUI (or any MCP client): `shop_search`, `web_search`, `fetch_page`, and the
  hunt tools `watch_create`, `watch_report`, `watch_manage`.
- A Home Assistant add-on: a switch and a page per hunt, a request box where you type what to watch for,
  and phone notifications with the model's one-line take on each listing.

Docs: [shopsearch/DOCS.md](shopsearch/DOCS.md) (install and settings) · [docs/HANDOFF.md](docs/HANDOFF.md)
(architecture and decisions) · [docs/HOME_ASSISTANT.md](docs/HOME_ASSISTANT.md) · [docs/ROADMAP.md](docs/ROADMAP.md)
· [tests/README.md](tests/README.md)

## Install

**Home Assistant OS:** open the app (add-on) store under Settings → Apps, then ⋮ → Repositories, add
`https://github.com/luwi8011/shopsearch`, and install **Shop Search**. Fill in the Configuration tab
(at least an API key, your LLM and your location) and start it. Details in [shopsearch/DOCS.md](shopsearch/DOCS.md).

**Plain Docker** (no HA OS): copy `.env.example` to `.env`, fill it in, and run `docker compose up -d --build`.
Home Assistant is optional there (`HA_URL` + a long-lived token).

Requirements: an OpenAI-compatible LLM endpoint (Ollama, llama.cpp, vLLM, LM Studio, …) for listing review
and the request box; optionally SearXNG for `web_search`. About 1–3 GB of free memory for the stealth browser.

## Connect Open WebUI

Admin Panel → Settings → External Tools → add a connection of type MCP (Streamable HTTP):
URL `http://<home-assistant-ip>:8765/mcp`, auth Bearer, key = the add-on's API key. Then enable the tool for a
model (or per chat from `+` → Tools). Open WebUI prefixes tool names with the connection id, so the model sees
e.g. `shop_shop_search`.

Test without the LLM (`$KEY` is the API key):

    curl -H "Authorization: Bearer $KEY" 'http://<ip>:8765/api/search?q=ar15+lower+parts+kit&sites=psa,amazon'
    curl -H "Authorization: Bearer $KEY" 'http://<ip>:8765/api/fetch?url=https://...'
    curl -H "Authorization: Bearer $KEY" 'http://<ip>:8765/api/watches'

## How fetching works (app/fetcher.py)

1. Plain HTTP with a real browser's TLS fingerprint (curl_cffi; Safari, then Chrome). About 1 s per page. On a
   403 it loads the homepage first for cookies and retries (eBay needs this).
2. A stealth Chromium (Scrapling 0.5 / patchright) that runs JavaScript and solves Cloudflare challenges.
   Takes 8 to 20 s. Palmetto State Armory needs this on every request.

The method that worked for each domain is saved in `tiers.json`. Requests to one domain are spaced at least
2.5 s apart and pages are cached for 10 minutes. Bursts of requests are what get a home IP flagged.

## Sites (app/sites.py)

Built in: amazon, ebay, psa, dahlonega, mxlocker, craigslist and facebook (local pickup around your ZIP /
Marketplace location, default radius from the settings; the model can pass `radius_miles` per search;
Facebook logged out shows about 10 to 24 newest listings), ebay-local (eBay listings you can pick up within
that radius; needs the eBay API keys), walmart, newegg, gunbroker, aliexpress.

Store health (app/health.py): once a day each built-in store gets a test search that always has results. Two
empty ones in a row send "Shop Search: <store> stopped working" to the default phone, so a layout change or a
block doesn't leave hunts quietly finding nothing; you get "works again" when it recovers. `GET /health` shows
each store's state.

Any other store: pass its domain. The search URL is found from the homepage (schema.org SearchAction, the
search form, or Shopify/Woo/Magento/BigCommerce defaults) and saved in `learned_sites.json`. If that fails,
it falls back to a `site:` web search plus product pages. To add a store permanently, add an entry with just
a `search` URL; CSS selectors are optional because the generic extractor finds product cards on its own.

Condition filter (`condition` new/used): eBay, Craigslist (used = like new to fair), Facebook. Retail stores
ignore it.

## Hunts (app/watches.py)

- `watch_create` saves a search and re-runs it every N hours (default 6, min 1), whether or not a chat is
  open. All searches for one goal share a hunt name and a `want` (the match description, set by the first
  search). A listing found by two searches of a hunt alerts once.
- The first run of a search is a baseline and never alerts. Later runs: new listings and price drops.
- LLM review (app/review.py): before any alert, the model checks each new listing against `want` (a real
  match, not parts, gear, "wanted" posts or other models) and whether it reposts a listing already seen;
  a cheaper repost alerts as a price drop. New listings that could alert are opened first: the model also reads
  the seller's description and looks at the main photo (when it takes images), so "parts only" or "doesn't run"
  is caught. It writes a one-sentence comparison shown in the alert. Listings wait until a review succeeds;
  failed reviews retry every 10 minutes.
- Each alert is its own phone notification with **Not interested** (the listing is dropped, never alerts again,
  and becomes an example of what you don't want for that hunt's reviewer) and **Stop hunt**.
- `watch_report` shows what changed since the last report; `watch_manage` lists, pauses, resumes, deletes,
  re-runs, moves searches between hunts and sets `want`.

## Home Assistant

Everything is rebuilt from the add-on's own state by `Watches.sync_ha()`; see
[docs/HOME_ASSISTANT.md](docs/HOME_ASSISTANT.md).

- Each hunt has a switch `input_boolean.shop_hunt_<slug>`. With a dashboard set in the settings, it gets a
  "Shop Watches" section with a tile per hunt and a page per hunt (goal, searches, alerts sent). Turning a
  hunt's switch off deletes the hunt.
- Request box: type into `input_text.shop_watch_request` and press Send. The model tries a few searches,
  creates a hunt, and answers on your phone. This takes a few minutes, because every new search's first
  results are reviewed.
- Alerts go to the person who pressed Send (HA user → person → phone), others who add to the hunt, and
  otherwise the default phone from the settings.

## Known limits

- **eBay** (Akamai) blocks scraping from a home IP after bursts of requests. With free eBay developer keys
  (settings `ebay_client_id`/`ebay_client_secret`, Production keyset; in eBay's portal pick the "not persisting
  eBay data" opt-out for account-deletion notices) eBay uses the official Browse API instead (5,000 calls/day).
- **Home Depot / Lowe's** (Akamai) refuse both HTTP and the headless browser.
- **Amazon** bans most firearm parts, so gun-part searches there return accessories.
- **Facebook Marketplace** without login shows only the first batch of results.
- Scraping is against some sites' terms of service. Keep the polite defaults, prefer official APIs where they
  exist, and use it for your own shopping.
