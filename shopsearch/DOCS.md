# Shop Search

Price research and deal hunts for a self-hosted LLM. Serves MCP tools (`shop_search`, `web_search`,
`fetch_page`, hunts) to Open WebUI or any MCP client on port 8765, re-runs saved searches on a schedule,
has your model review new listings, and sends the good ones to your phone.

## Before you start

- An OpenAI-compatible LLM endpoint (Ollama, llama.cpp, vLLM, LM Studio, …). Without one, every new listing
  alerts unreviewed and the request box is off.
- The Home Assistant Companion app on your phone, for alerts.
- Optional: a SearXNG server for `web_search`.
- 1–3 GB of free memory while the stealth browser runs.

## Settings

| Setting | What to enter |
|---|---|
| API key | Any long random string. Required by the MCP endpoint and the REST API; port 8765 is open to your network. |
| LLM URL / model | e.g. `http://192.168.1.20:11434/v1` and the model name. |
| Assistant name | What the dashboard calls the model ("Send to …"). |
| About you | A sentence for the model: where you live, how far you drive for pickup, what you buy. |
| Home ZIP | Center of the Craigslist pickup radius; also used for eBay shipping estimates. |
| Craigslist site | The subdomain of your Craigslist site (`denver` for denver.craigslist.org). |
| Facebook Marketplace location | The slug in Marketplace URLs (`facebook.com/marketplace/<slug>/search`). |
| Pickup radius | Default miles for Craigslist and Facebook; the model raises it for big or rare items. |
| Default sites | Searched when the model names none. |
| Default phone | Notify service without `notify.` (e.g. `mobile_app_pixel_8`), for hunts nobody owns. |
| Dashboard | URL path of a dashboard (sections view) that gets a "Shop Watches" section and a page per hunt. Empty leaves dashboards alone. The add-on rewrites that section and the `shop-*` pages; don't edit them by hand. |
| SearXNG URL | Enables `web_search`. |
| eBay client ID / secret | Production keys from developer.ebay.com; eBay then uses the official API instead of scraping. |
| Browser pages | Pages the stealth browser loads at once (default 3). |

## Connect Open WebUI

Admin Panel → Settings → External Tools → add an MCP (Streamable HTTP) connection to
`http://<home-assistant-ip>:8765/mcp` with Bearer auth and your API key. Enable it for a model.

## Using it

- In a chat: "find the cheapest JET 1236 lathe apron", "watch Craigslist and Facebook for a KTM 300 under $5000".
- On the dashboard: type a request in the box and press Send. The answer arrives on your phone within a few
  minutes, and the hunt gets a tile and a page.
- To stop a hunt, turn its switch off (tap the tile's icon).

## Moving from a Docker install

Stop the old container first (two copies would fight over the same switches), then post its `watches.json`
while this add-on has no searches yet:

    curl -X POST -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
         --data @watches.json http://<home-assistant-ip>:8765/api/import

Hunts keep their switches, alert history and listings.

## Logs

The add-on's Log tab. Hunt, Home Assistant and request events also go to `shopsearch.log` in the add-on's data.
