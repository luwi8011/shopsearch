# Roadmap and recommendations

Ideas collected on 2026-10-26. ★ marks the highest-value items. Done since: Home Assistant add-on, personal
settings moved to add-on options, API key on the MCP server.

## Pending from earlier work

- **eBay official API.** `app/ebay_api.py` is written but untested.
  - An App ID exists (Production keyset, in `.env`), but no Cert ID/secret yet.
  - The eBay developer portal requires a "Marketplace Account Deletion" endpoint before it unlocks the
    keyset. The intended path is the opt-out toggle ("Not persisting eBay data"), because this app
    stores no eBay user data, only public listings.
  - Once keys work: test it, then add a local-pickup filter (pickupPostalCode/pickupRadius), which
    matters for vehicles and machinery.
  - Until then, eBay scraping is intermittently blocked by Akamai. Consider leaving eBay out of
    frequent hunts.
- **Hunt pages** in Home Assistant were verified only through template rendering, not visually on a phone.

## Making it usable by other people

- ★ **A Home Assistant integration next to the add-on** (HACS or bundled) for real entities with unique_ids
  instead of REST-pushed sensors and dashboard rewriting, plus a card users add themselves. The add-on keeps
  doing the scraping (HA's own Python can't run the stealth browser). Removes the most fragile part of the design.
- ★ **A `doctor` check** that tests search, LLM, HA and each store and reports problems plainly (on start, in
  the log, and as an API route).
- **aarch64 build** (Raspberry Pi and other ARM boxes): patchright ships ARM Chromium, but it is untested and
  memory is tight there.
- **Publish a prebuilt image** (GHCR) so installs and updates don't build Chromium on the HA box.
- **Works with any OpenAI-compatible LLM** (Ollama, LM Studio, vLLM, cloud). Mostly config already; needs prompt
  testing on smaller models.
- **Honest docs about scraping.** Cover terms of service, polite rates, and preferring official APIs.

## Better results

- ★ **Open each new listing before review.** Fetch the listing page, plus photos (the model has vision),
  so issues mentioned only in descriptions get caught ("doesn't run", "parts only").
- ★ **True total price.** Add shipping, estimate tax, or show distance/drive time for local pickup.
- **Price history and "is this a good deal?".** Each listing keeps its price changes (`prices`, since 1.0.1)
  until 30 days after it was last seen; listings that sold or vanished are dropped then. Add per-hunt charts,
  keep gone listings' last prices, and add sold-price data (eBay sold listings) so the reviewer can say
  "$400 below typical".
- **Store health checks.** Run a daily known search per store and flag when a layout change silently
  drops results to zero.
- **Saved HTML fixtures per store** for automated extraction tests.
- **Better Facebook coverage.** Logged out, it sees only ~15–24 listings. Options: login cookies, or more
  frequent checks of a newest-first search.
- **More sources:** OfferUp, KSL Classifieds, Mercari, GunBroker auctions with end times,
  r/gundeals, and manufacturer outlets.

## Better alerts

- ★ **Actionable notifications:** "Not interested", "Stop this hunt", "Show similar". Feed rejections back
  into the review prompt as examples so the reviewer learns the user's taste.
- **Quiet hours, and an optional daily digest** for low-priority hunts.
- **Hunts that end themselves:** "stop after I buy one", "end in 30 days".
- **Auction-ending-soon alerts**, and a target-price trigger.

## Quality of life

- A small web UI for people without Home Assistant (view and edit hunts).
- Edit a hunt in place (price, radius, interval) instead of delete-and-recreate.
- Export a hunt's results to CSV, or a bill of materials for a build (a hunt per part, with a running total).
- Request progress in HA ("searching eBay… reviewing 23 listings…") instead of a single "Working on…".
- `watch_report` grouped per hunt, with listings merged across its searches.
