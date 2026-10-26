# Home Assistant integration

Home Assistant 2026.10.1 at `http://192.168.0.161:8123` (also `homeassistant.local`), timezone America/Denver.
Code: `app/ha.py` (client and dashboard renderer) and `Watches.sync_ha()` in `app/watches.py`.

## Connection

- `HA_URL=http://192.168.0.161:8123` (docker-compose.yml)
- `HA_TOKEN`: a long-lived access token in `.env`, created by the user (Lucas) under Profile → Security.
  It is a full-admin token. A dedicated limited user would be better (see ROADMAP).
- `HA_DASHBOARD=ha-tools`: the "Tools" dashboard (storage mode, sections view, path `tools`).
- `HA_NOTIFY=mobile_app_localhost`: the default phone (Lucas's Android; the Companion app registered
  it as "localhost").
- Uses the REST API (states, services, config) and the WebSocket API (helper create/delete,
  `lovelace/config` and `lovelace/config/save`).
- Python dependency: `websockets` (installed in its own Dockerfile layer).

## What shopsearch creates and owns

| Entity / object | Kind | Purpose |
|---|---|---|
| `input_boolean.shop_hunt_<slug>` | UI helper | One per hunt. On while the hunt exists. Turning it off (with confirmation) deletes the hunt and all its searches within ~30 s |
| `sensor.shop_hunt_<slug>` | REST-pushed state (no unique_id) | State is "N alerts". Attribute `alerts` holds up to 25 entries {t (ISO UTC), title, url, price, was, where, note}. Re-pushed after HA restarts |
| `input_text.shop_watch_request` | UI helper (max 255) | The request box ("Ask Qwen to watch for…") |
| `input_button.shop_watch_request_send` | UI helper | "Send to Qwen". shopsearch polls its state every 5 s |
| `sensor.shop_watch_request_status` | REST-pushed state | Status of the last request ("Working on: …", then Qwen's one-line answer) |
| Section "Shop Watches" in view 0 of `ha-tools` | Dashboard | Heading, request box, Send button, status tile, then one tile per hunt |
| Sub-views `shop-<slug>` in `ha-tools` | Dashboard | A page per hunt: switch tile, "Looking for" plus searches plus "Alerts go to", and alert history (markdown template over the sensor) |

`ha.render_dashboard()` regenerates the section and every `shop-*` sub-view from current state and
saves only if something changed. It finds its section by the heading text "Shop Watches", so
don't hand-edit that section: edits are overwritten. Other sections are untouched.

Legacy prefix `input_boolean.shop_watch_` (from the per-watch design before 2026-10-26) is treated as
orphaned and deleted automatically.

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

Only `sync_ha()` creates switches, under `_ha_lock`. This fixed the 2026-10-26 duplicate-switch bug.

## Notifications

- Format: title "<hunt>: N new matches, M price drops". The body lists up to 3 entries, each
  `$price · title (location, store)` with `→ LLM comparison note` under it. Tapping opens the first
  listing (`data.url` for iOS, `clickAction` for Android).
- Recipients per hunt (`hunt.recipients` = [{service, name}]):
  - The person who pressed "Send to Qwen": HA user id (from the button state's `context.user_id`) →
    `person.*` with that `user_id` → its `device_trackers` → `notify.mobile_app_<tracker object id>`,
    if that service exists.
  - Anyone who adds to an existing hunt is added to its recipients.
  - Hunts from Open WebUI, and users without a phone (Guest), fall back to `HA_NOTIFY`.
- People at the time of writing:

| Person | HA user_id | Tracker | Notify service |
|---|---|---|---|
| Lucas Wilson | 6cec0d0a3be44752b151f433d61e0665 | device_tracker.localhost | mobile_app_localhost |
| Kari | ef48d2076e20488c884466857e840872 | device_tracker.karis_iphone | mobile_app_karis_iphone |
| Guest | 272e845136c7449dbbc67e2eca196e8c | none | (fallback) |
| Home Maintenance | none | none | n/a |

Other notify targets that exist: `mobile_app_kitchen_tablet`, `lg_webos_tv_lk5700pua`, `notify.notify`.

## Request box flow

1. The user types in `input_text.shop_watch_request` and presses Send.
2. `request_loop()` (server.py) sees the button's state timestamp change. It reads the text and
   resolves the requester's phones.
3. The status sensor becomes "Working on: …".
4. `agent.run()` lets the LLM call `shop_search` (1–3 trials), `list_hunts` and `create_search` (≤4 per
   hunt, same hunt name). It finishes with one sentence.
5. Status is set to the answer, the request box is cleared, and the answer is sent to the requester's phone.

Typical duration is 3–7 minutes, because the first run of each new search is reviewed by the LLM.

## Also on this HA instance (context, not used by shopsearch)

- The `ha-mcp` MCP server (homeassistant-ai) is reachable for Claude via a claude.ai connector. It is also
  registered in Open WebUI as a tool server with id `ha`. Its webhook URL doubles as a credential, so
  keep it out of git.
- The Tools dashboard's other sections: Poppy's feeder, feeding schedule, RF remote learning,
  learned RF commands, HVAC filter, door lock batteries, dashboard theme.
