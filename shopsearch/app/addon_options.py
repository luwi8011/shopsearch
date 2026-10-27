"""Home Assistant add-on settings: /data/options.json becomes environment variables.

Every module reads its settings from os.environ when it is imported, so server.py calls apply()
before importing them. Run as plain Docker (no options.json) the environment is used as is.
Inside the add-on, Home Assistant is reached through the Supervisor proxy with the add-on's own
token, so no long-lived access token is needed.
"""

import json
import os

OPTIONS_FILE = os.path.join(os.environ.get("DATA_DIR", "/data"), "options.json")

# add-on option -> environment variable
ENV = {
    "llm_url": "LLM_URL",
    "llm_model": "LLM_MODEL",
    "assistant_name": "ASSISTANT_NAME",
    "about_user": "USER_PROFILE",
    "searxng_url": "SEARXNG_URL",
    "default_sites": "DEFAULT_SITES",
    "home_zip": "HOME_ZIP",
    "craigslist_site": "CRAIGSLIST_SITE",
    "facebook_location": "FB_LOCATION",
    "local_radius_miles": "LOCAL_RADIUS_MI",
    "notify_service": "HA_NOTIFY",
    "dashboard": "HA_DASHBOARD",
    "api_key": "API_KEY",
    "ebay_client_id": "EBAY_CLIENT_ID",
    "ebay_client_secret": "EBAY_CLIENT_SECRET",
    "browser_pages": "BROWSER_PAGES",
}


def apply():
    try:
        with open(OPTIONS_FILE) as f:
            opts = json.load(f)
    except FileNotFoundError:
        opts = {}
    for key, var in ENV.items():
        v = opts.get(key)
        if isinstance(v, list):
            v = ",".join(str(x) for x in v)
        if v not in (None, ""):
            os.environ[var] = str(v)
    token = os.environ.get("SUPERVISOR_TOKEN")
    if token and not os.environ.get("HA_URL"):
        os.environ["HA_URL"] = "http://supervisor/core"
        os.environ["HA_WS_URL"] = "ws://supervisor/core/websocket"
        os.environ["HA_TOKEN"] = token
