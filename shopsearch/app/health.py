"""Store health: a daily test search per built-in store, so a store that stops returning listings
(a layout change, or a block) gets noticed instead of hunts on it quietly finding nothing.

Every store in sites.SITES with a `canary` query is searched about once a day, one at a time. A search
with no listings is repeated after RETRY_S; two in a row mark the store as failing and send one
notification to the default phone. The first success after that sends "works again".
State lives in health.json; GET /health shows it.
"""

import asyncio
import json
import logging
import os
import random
import time

import ha
import sites

log = logging.getLogger("shopsearch.health")

DATA_DIR = os.environ.get("DATA_DIR", "/data")
STORE = os.path.join(DATA_DIR, "health.json")
EVERY_S = 86400
RETRY_S = 2 * 3600
FAILS_TO_ALERT = 2
START_DELAY_S = 600   # let the hunts run first after a restart
SPACING_S = 60        # between two stores' test searches


class Health:
    def __init__(self, search):
        """search: async (site_key, query) -> result dict with "items" and "error" (server._search_site)."""
        self._search = search
        try:
            with open(STORE) as f:
                self.data: dict[str, dict] = json.load(f)
        except FileNotFoundError:
            self.data = {}

    def _save(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = STORE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=1)
        os.replace(tmp, STORE)

    def stores(self) -> dict[str, str]:
        """{site key: canary query} for the stores that get checked."""
        return {k: r["canary"] for k, r in sites.SITES.items() if r.get("canary")}

    def summary(self) -> dict[str, dict]:
        return {k: {"ok": not s.get("failing"), "last_ok": s.get("last_ok"), "fails": s.get("fails", 0),
                    "error": s.get("error", "")} for k, s in self.data.items() if k in self.stores()}

    async def check(self, key: str) -> dict:
        query = self.stores()[key]
        res = await self._search(key, query)
        t = time.time()
        s = self.data.setdefault(key, {})
        s["last_check"] = t
        name = sites.SITES[key]["name"]
        if res["items"]:
            if s.get("failing"):
                log.info("store %s works again", key)
                await self._notify(f"Shop Search: {name} works again",
                                   f"The daily test search finds listings on {name} again.")
            s.update(failing=False, fails=0, error="", last_ok=t, next=t + EVERY_S * random.uniform(0.95, 1.05))
        else:
            s["fails"] = s.get("fails", 0) + 1
            s["error"] = res.get("error") or "no listings"
            log.warning("store %s: test search %r found nothing (%d in a row): %s", key, query, s["fails"], s["error"])
            if s["fails"] >= FAILS_TO_ALERT and not s.get("failing"):
                s["failing"] = True
                await self._notify(f"Shop Search: {name} stopped working",
                                   f'The test search "{query}" found no listings twice in a row ({s["error"]}). '
                                   f"Searches and hunts on {name} miss everything until this is fixed.")
            s["next"] = t + (RETRY_S if s["fails"] < FAILS_TO_ALERT else EVERY_S)
        self._save()
        return s

    async def _notify(self, title: str, message: str):
        if not (ha.enabled() and ha.HA_NOTIFY):
            return
        try:
            await ha.notify(title, message)
        except Exception as e:
            log.warning("store health notification failed: %s", e)

    async def loop(self):
        await asyncio.sleep(START_DELAY_S)
        while True:
            try:
                due = [k for k in self.stores() if self.data.get(k, {}).get("next", 0) <= time.time()]
                for key in due:
                    await self.check(key)
                    await asyncio.sleep(SPACING_S)
            except Exception:
                log.exception("store health loop error")
            await asyncio.sleep(SPACING_S)
