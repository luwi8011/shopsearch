"""Shared setup: a temp data dir, Home Assistant switched off/stubbed, and a fake search.

Import this before any app module. Tests run inside the container:
    docker cp tests shopsearch:/tmp/tests && docker exec shopsearch python /tmp/tests/<test>.py
"""
import os
import sys
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["HA_URL"] = ""          # never touch the real Home Assistant
if not os.environ.get("TEST_LIVE_LLM"):
    os.environ["LLM_URL"] = ""     # unit tests: no LLM review (every listing counts as a match)
sys.path.insert(0, os.environ.get("APP_DIR", "/app"))

import ha  # noqa: E402

SENT = []


async def _capture(title, message, url=None, targets=None, tag=None, actions=None):
    SENT.append({"title": title, "message": message, "url": url, "targets": targets, "tag": tag, "actions": actions})


async def _noop(*a, **k):
    pass


def stub_ha():
    """Pretend HA is connected; capture notifications instead of sending them."""
    ha.enabled = lambda: True
    ha.HA_NOTIFY = "mobile_app_default"
    ha.notify, ha.set_state, ha.clear_notification = _capture, _noop, _noop


def fake_search(rounds):
    """rounds: list of row lists [(store, item)]; call N returns rounds[min(N, last)]."""
    n = {"c": 0}

    async def search(params):
        rows = rounds[min(n["c"], len(rounds) - 1)]
        n["c"] += 1
        return rows, ["- ok"], [{"name": s, "items": [i for st, i in rows if st == s], "error": ""}
                                for s in {st for st, _ in rows}]
    return search


def item(title, price, url, details=""):
    return {"title": title, "price": price, "url": url, "details": details}
