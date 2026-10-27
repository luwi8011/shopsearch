# Home Assistant integration

Code: `shopsearch/app/ha.py` (client and dashboard renderer) and `Watches.sync_ha()` in `shopsearch/app/watches.py`.

## Connection

- As an add-on: `addon_options.py` points `HA_URL` at `http://supervisor/core` and `HA_WS_URL` at
  `ws://supervisor/core/websocket`, with the add-on's `SUPERVISOR_TOKEN` (`homeassistant_api: true` in
  `config.yaml`). No user token is needed.
- As plain Docker: `HA_URL` plus `HA_TOKEN`, a long-lived access token (Profile → Security). It must belong to
  an admin, because creating helpers and saving dashboards are admin-only.
- `HA_DASHBOARD` (add-on option `dashboard`): url_path of a storage-mode dashboard whose first view uses
  sections. Empty: no dashboard edits.
- `HA_NOTIFY` (option `notify_service`): the default phone, e.g. `mobile_app_pixel_8`.
- `ASSISTANT_NAME` (option `assistant_name`): how the dashboard names the model ("Send to …").
- Uses the REST API (states, services, config) and the WebSocket API (helper create/delete,
  `lovelace/config` and `lovelace/config/save`).

## What shopsearch creates and owns

| Entity / object | Kind | Purpose |
|---|---|---|
| `input_boolean.shop_hunt_<slug>` | UI helper | One per hunt. On while the hunt exists. Turning it off (with confirmation) deletes the hunt and all its searches within ~30 s |
| `sensor.shop_hunt_<slug>` | REST-pushed state (no unique_id) | State is "N alerts". Attribute `alerts` holds up to 25 entries {t (ISO UTC), title, url, price, was, where, note}. Re-pushed after HA restarts |
| `input_text.shop_watch_request` | UI helper (max 255) | The request box ("Ask … to watch for…") |
| `input_button.shop_watch_request_send` | UI helper | "Send to …". shopsearch polls its state every 5 s |
| `sensor.shop_watch_request_status` | REST-pushed state | Status of the last request ("Working on: …", then the model's one-line answer) |
| Section "Shop Watches" in view 0 of the dashboard | Dashboard | Heading, request box, Send button, status tile, then one tile per hunt |
| Sub-views `shop-<slug>` in the dashboard | Dashboard | A page per hunt: switch tile, "Looking for" plus searches plus "Alerts go to", and alert history (markdown template over the sensor) |

`ha.render_dashboard()` regenerates the section and every `shop-*` sub-view from current state and
saves only if something changed. It finds its section by the heading text "Shop Watches", so
don't hand-edit that section: edits are overwritten. Other sections are untouched.

Legacy prefix `input_boolean.shop_watch_` (from the per-watch design) is treated as orphaned and deleted
automatically.

## Reconciliation (`Watches.sync_ha`, every 30 s; full tidy every 10 min or on any change)

1. If HA isn't `RUNNING` (from `/api/config`), do nothing.
2. A hunt switch that is `off` ends the hunt: its searches are deleted, then the helper and sensor.
3. A hunt switch that is missing ends the hunt only after 3 consecutive polls while HA is running.
4. Tidy:
   - Delete any `shop_hunt_`/`shop_watch_` switch, and any `sensor.shop_hunt_*`, that no hunt owns.
   - Create missing hunt switches (turned on).
   - Re-push hunt sensors.
   - Create the request helpers and the status sensor if missing.
5. Re-render the dashboard if anything changed.

Only `sync_ha()` creates switches, under `_ha_lock`. This fixed a duplicate-switch bug. Two shopsearch
instances on one Home Assistant would delete each other's switches as orphans: run only one.

## Notifications

- One notification per alerted listing (at most 3 per check, then one "+N more"): title "<hunt>: new match" or
  "<hunt>: price drop", body `$price · title (location, store)` with `→ LLM comparison note`. Tapping opens the
  listing (`data.url` for iOS, `clickAction` for Android); `tag` is `shop_<listing id>` (sha1 of the URL, 10 hex).
- Buttons: **Not interested** (`SHOPSEARCH_NO_<hunt id>_<listing id>`) and **Stop hunt**
  (`SHOPSEARCH_STOP_<hunt id>`). shopsearch keeps a WebSocket subscription to `mobile_app_notification_action`
  events. Not interested marks the listing rejected (no new or price-drop alerts, reposts of it are rejected too),
  adds it to the hunt's `rejected` examples for the reviewer (last 20), marks it on the hunt page and clears the
  notification. Stop hunt deletes the hunt like turning its switch off.
- Store health alerts ("<store> stopped working" / "works again") go to `HA_NOTIFY`.
- Recipients per hunt (`hunt.recipients` = [{service, name}]):
  - The person who pressed Send: HA user id (from the button state's `context.user_id`) →
    `person.*` with that `user_id` → its `device_trackers` → `notify.mobile_app_<tracker object id>`,
    if that service exists.
  - Anyone who adds to an existing hunt is added to its recipients.
  - Hunts from Open WebUI, and users without a phone, fall back to `HA_NOTIFY`.

## Request box flow

1. The user types in `input_text.shop_watch_request` and presses Send.
2. `request_loop()` (server.py) sees the button's state timestamp change. It reads the text and
   resolves the requester's phones.
3. The status sensor becomes "Working on: …".
4. `agent.run()` lets the LLM call `shop_search` (1–3 trials), `list_hunts` and `create_search` (≤4 per
   hunt, same hunt name). It finishes with one sentence.
5. Status is set to the answer, the request box is cleared, and the answer is sent to the requester's phone.

Typical duration is 3–7 minutes with a mid-size local model, because the first run of each new search is
reviewed by the LLM.
