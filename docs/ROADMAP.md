# Roadmap and recommendations

Ideas collected on 2026-10-26, not yet started. ★ marks the highest-value items. The suggested order
is: (1) fetch each listing before review, (2) notification actions that train the reviewer, and
(3) remove the hard-coded personal settings. The first two improve the tool now; the third is the gate
for anyone else using it.

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

- ★ **Make the hard-coded personal settings configurable.** These are:
  - Erie/80516 and Denver Craigslist.
  - Facebook's `erie-co` slug.
  - The default phone.
  - Strata's URL and the model name.
  - `ha-tools`.
  - "Qwen" in user-facing text.
  - The user profile in `agent.SYSTEM`.

  Move them all to config with defaults, plus a setup check.
- ★ **A real Home Assistant custom integration (HACS)** instead of REST-pushed sensors and dashboard
  rewriting. It would give real entities with unique_ids that survive restarts, a config flow and a
  card users add themselves. It removes the most fragile part of the design.
- ★ **One-command install.** Publish the image, keep `.env.example` and add a `doctor` command that tests
  search, LLM, HA and each store and reports problems plainly.
- **Works with any OpenAI-compatible LLM** (Ollama, LM Studio, vLLM, cloud). Mostly config, plus prompt
  testing on smaller models.
- **Security.**
  - The MCP server has no auth (it is safe only because it binds to localhost). Add an API key.
  - Document a dedicated, limited HA user instead of a full-admin token.
- **Honest docs about scraping.** Cover terms of service, polite rates, and preferring official APIs.

## Better results

- ★ **Open each new listing before review.** Fetch the listing page, plus photos (the model has vision),
  so issues mentioned only in descriptions get caught ("doesn't run", "parts only").
- ★ **True total price.** Add shipping, estimate tax, or show distance/drive time for local pickup.
- **Price history and "is this a good deal?".** All prices seen are already stored. Add per-hunt charts
  and sold-price data (eBay sold listings) so the reviewer can say "$400 below typical".
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
