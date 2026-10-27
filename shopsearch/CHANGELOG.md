# Changelog

## 1.2.1

- No more traceback in the log when the add-on stops.

## 1.2.0

- Before the review, new listings that could alert are opened: the model reads the seller's description and looks
  at the main photo (if it takes images), so "parts only" or "doesn't run" in the description is caught. eBay
  listings are read through the API.
- One notification per listing, with **Not interested** (drops it, rejects reposts of it, and teaches the hunt's
  reviewer) and **Stop hunt** buttons.
- The add-on now installs a prebuilt image from GitHub instead of building Chromium on your Home Assistant box.

## 1.1.0

- Store health: a daily test search per built-in store; your default phone hears when a store stops returning
  listings and when it works again. `GET /health` shows each store.
- eBay official API tested with real keys. New site `ebay-local`: eBay listings you can pick up within the
  pickup radius (needs the eBay keys).

## 1.0.1

- Review: a "false" answer given as text no longer counts as a match.
- Review notes keep model names such as K2 or N95 (the prompt's labels are now old-#/new-#).
- A hunt's alert de-dup list drops its oldest entries when full, not random ones.
- Listings that arrive while a review is running are reviewed within 10 minutes instead of at the next check.
- Each listing keeps its price changes (`prices`).
- Importing hunts recreates their Home Assistant switches instead of ending the hunts.
- Stopping the add-on is a clean exit (it showed as an error); quieter log.

## 1.0.0

- First release as a Home Assistant add-on. Talks to Home Assistant through the Supervisor (no access token).
- Personal settings (location, LLM, default phone, dashboard, assistant name) moved to the Configuration tab.
- API key for the MCP endpoint and REST API.
- `POST /api/import` loads hunts from a Docker install.
