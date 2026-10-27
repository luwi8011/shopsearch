"""Home Assistant link for watches.

Everything shopsearch puts in Home Assistant is rebuilt from its own state, so it
cannot drift: render_dashboard() regenerates the "Shop Watches" section of one
dashboard and one sub-page per hunt, and the watch loop deletes any shop helper
that no live hunt owns.

- Each hunt (a goal with one or more searches) has an input_boolean switch, on
  while the hunt exists. Its tile opens the hunt's page; tapping the icon turns it
  off (with confirmation), which deletes the hunt and all its searches.
- Each hunt has a sensor (sensor.shop_hunt_<slug>) holding the alerts sent so far,
  shown on its page. It is pushed through the REST API, so it is re-pushed after a
  Home Assistant restart.
- A request box (input_text + input_button) lets the user type a watch request for
  the local LLM; a status sensor shows its answer.

Needs HA_URL and HA_TOKEN; as a Home Assistant add-on they point at the Supervisor proxy
(addon_options.py). HA_NOTIFY is the default notify service (e.g. mobile_app_pixel_8);
HA_DASHBOARD is the url_path of the dashboard that gets the "Shop Watches" section (empty: none).
"""

import json
import logging
import os

import httpx
import websockets

log = logging.getLogger("shopsearch.ha")

HA_URL = os.environ.get("HA_URL", "").rstrip("/")
HA_WS_URL = os.environ.get("HA_WS_URL") or HA_URL.replace("http", "ws", 1) + "/api/websocket"
HA_TOKEN = os.environ.get("HA_TOKEN", "")
ASSISTANT = os.environ.get("ASSISTANT_NAME", "Assistant")
HA_NOTIFY = os.environ.get("HA_NOTIFY", "")
HA_DASHBOARD = os.environ.get("HA_DASHBOARD", "")
SECTION_HEADING = "Shop Watches"
SUBVIEW_PREFIX = "shop-"
HUNT_PREFIX = "input_boolean.shop_hunt_"
OLD_PREFIX = "input_boolean.shop_watch_"   # switches made by the per-watch version
REQUEST_TEXT = "input_text.shop_watch_request"
REQUEST_BUTTON = "input_button.shop_watch_request_send"
STATUS_SENSOR = "sensor.shop_watch_request_status"


def enabled() -> bool:
    return bool(HA_URL and HA_TOKEN)


def _headers() -> dict:
    return {"Authorization": f"Bearer {HA_TOKEN}"}


async def _ws(*calls: dict) -> list:
    """Run WebSocket API calls on one authenticated connection; returns each call's result."""
    out = []
    async with websockets.connect(HA_WS_URL, max_size=8 * 1024 * 1024, open_timeout=15) as ws:
        await ws.recv()  # auth_required
        await ws.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))
        msg = json.loads(await ws.recv())
        if msg.get("type") != "auth_ok":
            raise RuntimeError(f"Home Assistant auth failed: {msg.get('message', msg.get('type'))}")
        for n, call in enumerate(calls, 1):
            await ws.send(json.dumps({"id": n, **call}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == n and msg.get("type") == "result":
                    break
            if not msg.get("success"):
                err = msg.get("error") or {}
                raise RuntimeError(f"{call['type']}: {err.get('code')} {err.get('message')}")
            out.append(msg.get("result"))
    return out


async def _rest(method: str, path: str, **kw) -> httpx.Response:
    async with httpx.AsyncClient(timeout=15) as c:
        return await c.request(method, f"{HA_URL}{path}", headers=_headers(), **kw)


async def service(domain: str, name: str, data: dict):
    r = await _rest("POST", f"/api/services/{domain}/{name}", json=data)
    r.raise_for_status()


async def running() -> bool:
    """True when Home Assistant is fully started (entities loaded). Raises when unreachable."""
    r = await _rest("GET", "/api/config")
    r.raise_for_status()
    return r.json().get("state") == "RUNNING"


async def states() -> dict[str, dict]:
    r = await _rest("GET", "/api/states")
    r.raise_for_status()
    return {s["entity_id"]: s for s in r.json()}


async def set_state(entity_id: str, state: str, attributes: dict):
    """Create or update a state-only entity (gone after a Home Assistant restart until pushed again)."""
    r = await _rest("POST", f"/api/states/{entity_id}", json={"state": state[:255], "attributes": attributes})
    r.raise_for_status()


async def remove_state(entity_id: str):
    await _rest("DELETE", f"/api/states/{entity_id}")


async def notify(title: str, message: str, url: str | None = None, targets: list[str] | None = None):
    """Send to each notify service in targets (default: HA_NOTIFY)."""
    data = {"title": title, "message": message}
    if url:
        # iOS opens "url", Android "clickAction"
        data["data"] = {"url": url, "clickAction": url, "group": "shopsearch"}
    for t in targets or ([HA_NOTIFY] if HA_NOTIFY else []):
        await service("notify", t, data)


async def recipients_for_user(user_id: str | None, st: dict) -> list[dict]:
    """The phones of the person linked to a Home Assistant user, as [{"service", "name"}]. Each Companion-app
    phone has device_tracker.<x> and notify.mobile_app_<x>. Empty when the user has no person or phone."""
    if not user_id:
        return []
    person = next((s for e, s in st.items() if e.startswith("person.")
                   and s["attributes"].get("user_id") == user_id), None)
    if not person:
        return []
    r = await _rest("GET", "/api/services")
    r.raise_for_status()
    notify_services = {n for d in r.json() if d["domain"] == "notify" for n in d["services"]}
    name = person["attributes"].get("friendly_name", person["entity_id"])
    out = []
    for tracker in person["attributes"].get("device_trackers") or []:
        svc = "mobile_app_" + tracker.split(".", 1)[1]
        if svc in notify_services:
            out.append({"service": svc, "name": name})
    return out


async def create_helper(domain: str, name: str, **fields) -> dict:
    """Create a UI helper (input_boolean, input_text, input_button). Returns {"helper_id", "entity_id"}."""
    (h,) = await _ws({"type": f"{domain}/create", "name": name[:100], **fields})
    return {"helper_id": h["id"], "entity_id": f"{domain}.{h['id']}"}


async def delete_helper(entity_id: str):
    domain, helper_id = entity_id.split(".", 1)
    try:
        await _ws({"type": f"{domain}/delete", f"{domain}_id": helper_id})
    except RuntimeError as e:
        if "not_found" not in str(e):
            raise


# ---------------------------------------------------------------- dashboard

def _confirm(label: str) -> dict:
    return {"text": f"Stop the {label} hunt? This deletes all of its saved searches."}


ALERTS_MD = """{%- set a = state_attr('SENSOR', 'alerts') or [] -%}
{%- if not a %}No alerts sent yet. New matches will show up here and on your phone.{% endif -%}
{%- for i in a %}
**[{{ i.title }}]({{ i.url }})**
{{ i.price }}{% if i.was %} (was {{ i.was }}){% endif %} · {{ i.where }} · {{ (i.t | as_datetime | as_local).strftime('%b %-d, %-I:%M %p') }}
{% if i.note %}→ {{ i.note }}{% endif %}
{% endfor %}"""


def _hunt_cards(h: dict) -> tuple[dict, dict]:
    """(tile for the main section, sub-view for the hunt's page)."""
    sw, slug, label = h["ha"]["entity_id"], h["slug"], h["label"]
    path = f"{SUBVIEW_PREFIX}{slug}"
    tile = {"type": "tile", "entity": sw, "name": label, "icon": "mdi:cart-heart",
            "tap_action": {"action": "navigate", "navigation_path": f"/{HA_DASHBOARD}/{path}"},
            "icon_tap_action": {"action": "toggle", "confirmation": _confirm(label)},
            "grid_options": {"columns": "full"}}
    searches = "\n".join(f"- {s}" for s in h.get("searches", [])) or "- (none)"
    view = {"title": label, "path": path, "subview": True, "back_path": f"/{HA_DASHBOARD}/{h.get('main_path', '')}",
            "type": "sections", "max_columns": 1, "sections": [{"type": "grid", "cards": [
                {"type": "heading", "heading": label, "icon": "mdi:cart-search"},
                {"type": "tile", "entity": sw, "name": "Hunt active (turn off to delete it)", "icon": "mdi:cart-heart",
                 "tap_action": {"action": "toggle", "confirmation": _confirm(label)},
                 "icon_tap_action": {"action": "toggle", "confirmation": _confirm(label)},
                 "grid_options": {"columns": "full"}},
                {"type": "markdown", "title": "Looking for",
                 "content": f"{h.get('want') or ''}\n\n**Searches**\n{searches}"
                            f"\n\n**Alerts go to:** {h.get('recipients_text') or 'default phone'}"},
                {"type": "markdown", "title": "Alerts sent", "content": ALERTS_MD.replace("SENSOR", h["sensor"])},
            ]}]}
    return tile, view


def _section(hunts: list[dict]) -> dict:
    cards = [
        {"type": "heading", "heading": SECTION_HEADING, "icon": "mdi:cart-search"},
        {"type": "entities", "entities": [{"entity": REQUEST_TEXT, "name": f"Ask {ASSISTANT} to watch for…"}],
         "grid_options": {"columns": "full"}},
        {"type": "tile", "entity": REQUEST_BUTTON, "name": f"Send to {ASSISTANT}", "icon": "mdi:send",
         "tap_action": {"action": "perform-action", "perform_action": "input_button.press",
                        "target": {"entity_id": REQUEST_BUTTON}},
         "grid_options": {"columns": 4}},
        {"type": "tile", "entity": STATUS_SENSOR, "name": ASSISTANT, "icon": "mdi:robot-outline",
         "grid_options": {"columns": 8}},
    ]
    if hunts:
        cards.append({"type": "heading", "heading_style": "subtitle",
                      "heading": "Tap a hunt to see what you've been sent. Tap its icon to stop it."})
    return {"type": "grid", "cards": cards}


async def render_dashboard(hunts: list[dict]):
    """Rebuild the Shop Watches section and the hunt pages; save only if something changed."""
    if not HA_DASHBOARD:
        return
    (cfg,) = await _ws({"type": "lovelace/config", "url_path": HA_DASHBOARD})
    before = json.dumps(cfg, sort_keys=True)
    main = cfg["views"][0]
    linked = [h for h in hunts if h.get("ha")]
    for h in linked:
        h["main_path"] = main.get("path", "0")
    tiles, views = zip(*[_hunt_cards(h) for h in linked]) if linked else ((), ())
    section = _section(linked)
    section["cards"] += list(tiles)

    secs = main.setdefault("sections", [])
    idx = next((n for n, s in enumerate(secs) if (s.get("cards") or [{}])[0].get("heading") == SECTION_HEADING), None)
    if idx is None:
        secs.append(section)
    else:
        secs[idx] = section
    cfg["views"] = [v for v in cfg["views"] if not str(v.get("path", "")).startswith(SUBVIEW_PREFIX)] + list(views)
    if json.dumps(cfg, sort_keys=True) != before:
        await _ws({"type": "lovelace/config/save", "url_path": HA_DASHBOARD, "config": cfg})
        log.info("dashboard %s updated (%d hunts)", HA_DASHBOARD, len(linked))
