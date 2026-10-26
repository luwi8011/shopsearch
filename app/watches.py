"""Saved searches ("watches") that re-run on a schedule, grouped into hunts.

A hunt is one goal ("JET 1236 lathe apron") with one or more searches (variations
of the query, different sites). Each watch stores the listings it has seen, keyed
by URL, with when each was first seen and its price history. New listings are
judged by the local LLM against the hunt's goal (review.py) before any alert, and
a listing found by two searches of the same hunt is alerted once.

Home Assistant (ha.py, when HA_URL and HA_TOKEN are set): each hunt has a switch
and a page listing the alerts sent; switching it off deletes the hunt. Everything
in Home Assistant is reconciled from this state by sync_ha().
"""

import asyncio
import datetime
import json
import logging
import os
import random
import re
import time

import ha
import review

log = logging.getLogger("shopsearch.watch")

DATA_DIR = os.environ.get("DATA_DIR", "/data")
STORE = os.path.join(DATA_DIR, "watches.json")
MAX_WATCHES = int(os.environ.get("MAX_WATCHES", "30"))
MIN_EVERY_H = 1.0
FORGET_AFTER_S = 30 * 86400  # drop listings not seen for 30 days
KEEP_ALERTS = 40             # alerts kept per hunt (shown on its Home Assistant page)

HA_POLL_S = 30          # how often the Home Assistant switches are checked
HA_TIDY_S = 600         # how often orphan helpers are removed and missing ones created
MISSING_POLLS = 3       # a switch must be missing this many polls in a row (HA running) to end its hunt
REVIEW_RETRY_S = 600    # retry an LLM review that failed (model down or busy) this often


def ago(t: float | None) -> str:
    if not t:
        return "never"
    s = time.time() - t
    if s < 0:
        s = -s
        return "in " + _span(s)
    return _span(s) + " ago"


def _span(s: float) -> str:
    if s < 90:
        return "1 min"
    if s < 5400:
        return f"{round(s / 60)} min"
    if s < 2 * 86400:
        return f"{s / 3600:.0f} h"
    return f"{s / 86400:.0f} days"


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:50] or "hunt"


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


class Watches:
    def __init__(self, search):
        """search: async (params: dict) -> (rows [(store, item)], status lines, raw results)."""
        self._search = search
        self._lock = asyncio.Lock()      # guards self.data and the file
        self._ha_lock = asyncio.Lock()   # one Home Assistant reconcile/change at a time
        self._running: set[str] = set()
        self._reviewing: set[str] = set()
        self._last_tidy = 0.0
        try:
            with open(STORE) as f:
                self.data = json.load(f)
        except FileNotFoundError:
            self.data = {"next_id": 1, "watches": {}}
        self.data.setdefault("hunts", {})
        self.data.setdefault("next_hunt", 1)
        self._migrate()

    def _migrate(self):
        """Older files had one switch per watch: give each such watch its own hunt."""
        for w in self.data["watches"].values():
            if w.get("hunt") in self.data["hunts"]:
                continue
            h = self._new_hunt(w["label"], w.get("want", ""))
            h["ha"] = w.pop("ha", None)
            h["created"] = w["created"]
            w["hunt"] = h["id"]
            w.pop("ha_tried", None), w.pop("ha_error", None)
        self._save()

    def _save(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = STORE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=1)
        os.replace(tmp, STORE)

    # ---------------------------------------------------------------- lookups

    def get(self, wid: str) -> dict | None:
        wid = (wid or "").strip().lower()
        if wid.isdigit():
            wid = "w" + wid
        return self.data["watches"].get(wid)

    def all(self) -> list[dict]:
        return list(self.data["watches"].values())

    def hunts(self) -> list[dict]:
        return list(self.data["hunts"].values())

    def hunt(self, ref: str) -> dict | None:
        """A hunt by id ("h3") or by name (case/punctuation-insensitive)."""
        ref = (ref or "").strip()
        if ref.lower() in self.data["hunts"]:
            return self.data["hunts"][ref.lower()]
        n = _norm(ref)
        return next((h for h in self.hunts() if _norm(h["label"]) == n), None)

    def hunt_of(self, w: dict) -> dict:
        return self.data["hunts"][w["hunt"]]

    def watches_in(self, h: dict) -> list[dict]:
        return [w for w in self.all() if w["hunt"] == h["id"]]

    def _new_hunt(self, label: str, want: str) -> dict:
        hid = f"h{self.data['next_hunt']}"
        self.data["next_hunt"] += 1
        slugs = {x["slug"] for x in self.hunts()}
        slug, n = slugify(label), 2
        while slug in slugs:
            slug, n = f"{slugify(label)}_{n}", n + 1
        h = {"id": hid, "label": label, "slug": slug, "want": want, "created": time.time(), "ha": None,
             "alerts": [], "alerted": [], "missing": 0}
        self.data["hunts"][hid] = h
        return h

    # ---------------------------------------------------------------- create / delete

    async def create(self, params: dict, label: str, every_hours: float, want: str = "",
                     hunt: str = "", recipients: list[dict] | None = None) -> dict:
        """Add a search. hunt: an existing hunt's name or id to add this search to; otherwise a new hunt
        named `hunt` (or `label`) is started. recipients: phones ({"service", "name"}) that get this hunt's
        alerts, added to the hunt's list; a hunt without any uses HA_NOTIFY."""
        async with self._lock:
            if len(self.data["watches"]) >= MAX_WATCHES:
                raise ValueError(f"already {MAX_WATCHES} searches; delete one first")
            h = self.hunt(hunt or label)
            if h is None:
                h = self._new_hunt((hunt or label)[:60], want or params["query"])
            elif not h.get("want") or h["want"] == params["query"]:
                h["want"] = want or h.get("want", "")
            # An existing hunt keeps its goal; a variation's wording must not narrow it (use set_want to change).
            for r in recipients or []:
                if r["service"] not in {x["service"] for x in h.setdefault("recipients", [])}:
                    h["recipients"].append(r)
            wid = f"w{self.data['next_id']}"
            self.data["next_id"] += 1
            now = time.time()
            w = {"id": wid, "hunt": h["id"], "label": label, "params": params,
                 "every_hours": max(MIN_EVERY_H, every_hours),
                 "active": True, "created": now, "last_check": None, "next_check": now,
                 "last_reported": now, "last_status": [], "store_ok": {}, "items": {}, "checks": []}
            self.data["watches"][wid] = w
            self._save()
        log.info("watch %s created in hunt %s (%s): %s", wid, h["id"], h["label"], params)
        await self.check(wid)
        await self.sync_ha(force=True)
        return w

    async def delete(self, wid: str) -> bool:
        """Delete one search; the hunt goes too when it was the last one."""
        async with self._lock:
            w = self.get(wid)
            if not w:
                return False
            del self.data["watches"][w["id"]]
            h = self.data["hunts"].get(w["hunt"])
            if h and not self.watches_in(h):
                del self.data["hunts"][h["id"]]
            self._save()
        log.info("watch %s deleted", w["id"])
        await self.sync_ha(force=True)
        return True

    async def delete_hunt(self, ref: str, why: str = "") -> dict | None:
        async with self._lock:
            h = self.hunt(ref)
            if not h:
                return None
            for w in self.watches_in(h):
                del self.data["watches"][w["id"]]
            del self.data["hunts"][h["id"]]
            self._save()
        log.info("hunt %s (%s) deleted%s", h["id"], h["label"], f": {why}" if why else "")
        await self.sync_ha(force=True)
        return h

    async def move(self, wid: str, hunt: str) -> dict | None:
        """Move a search into another (or a new) hunt."""
        async with self._lock:
            w = self.get(wid)
            if not w:
                return None
            old = self.data["hunts"][w["hunt"]]
            h = self.hunt(hunt) or self._new_hunt(hunt[:60], old.get("want", ""))
            w["hunt"] = h["id"]
            if not self.watches_in(old):
                h["alerts"] = sorted(h["alerts"] + old["alerts"], key=lambda a: a["t"], reverse=True)[:KEEP_ALERTS]
                h["alerted"] = list(dict.fromkeys(h["alerted"] + old["alerted"]))
                del self.data["hunts"][old["id"]]
            self._save()
        log.info("watch %s moved to hunt %s (%s)", wid, h["id"], h["label"])
        await self.sync_ha(force=True)
        return h

    async def set_want(self, hid: str, want: str):
        async with self._lock:
            self.data["hunts"][hid]["want"] = want
            self._save()
        await self.sync_ha(force=True)  # the hunt's page shows it

    async def update(self, wid: str, **changes) -> dict | None:
        async with self._lock:
            w = self.get(wid)
            if not w:
                return None
            if "every_hours" in changes:
                w["every_hours"] = max(MIN_EVERY_H, float(changes["every_hours"]))
                w["next_check"] = (w["last_check"] or time.time()) + w["every_hours"] * 3600
            if "active" in changes:
                w["active"] = bool(changes["active"])
                if w["active"] and w["next_check"] < time.time():
                    w["next_check"] = time.time()
            if changes.get("mark_reported"):
                w["last_reported"] = time.time()
            self._save()
            return w

    # ---------------------------------------------------------------- Home Assistant

    async def sync_ha(self, force: bool = False):
        """Make Home Assistant match our hunts: end hunts whose switch was turned off, create missing switches,
        delete switches no hunt owns, refresh sensors and the dashboard."""
        if not ha.enabled():
            return
        async with self._ha_lock:
            try:
                if not await ha.running():
                    return  # starting up: entities may not be loaded yet, so "missing" means nothing
                st = await ha.states()
            except Exception as e:
                log.debug("Home Assistant unreachable: %s", e)
                return
            changed = force
            for h in self.hunts():
                link = h.get("ha")
                if not link:
                    continue
                s = (st.get(link["entity_id"]) or {}).get("state")
                if s == "off":
                    await self._end_hunt(h, "switch turned off in Home Assistant")
                    changed = True
                elif s is None:
                    h["missing"] = h.get("missing", 0) + 1
                    if h["missing"] >= MISSING_POLLS:
                        await self._end_hunt(h, "switch deleted in Home Assistant")
                        changed = True
                else:
                    h["missing"] = 0

            tidy = force or time.time() - self._last_tidy > HA_TIDY_S
            if tidy:
                self._last_tidy = time.time()
                changed |= await self._tidy(st)
            if changed:
                try:
                    await ha.render_dashboard(self.hunts_for_ha())
                except Exception as e:
                    log.warning("dashboard update failed: %s", e)
            async with self._lock:
                self._save()

    async def _end_hunt(self, h: dict, why: str):
        async with self._lock:
            for w in self.watches_in(h):
                del self.data["watches"][w["id"]]
            self.data["hunts"].pop(h["id"], None)
            self._save()
        log.info("hunt %s (%s) ended: %s", h["id"], h["label"], why)
        try:
            await ha.delete_helper(h["ha"]["entity_id"])
            await ha.remove_state(f"sensor.shop_hunt_{h['slug']}")
        except Exception as e:
            log.warning("cleanup of hunt %s in Home Assistant failed (will retry): %s", h["id"], e)

    async def _tidy(self, st: dict) -> bool:
        """Create missing helpers/sensors, delete orphans. Returns True if anything changed."""
        changed = False
        owned = {h["ha"]["entity_id"] for h in self.hunts() if h.get("ha")}
        for eid in [e for e in st if e.startswith((ha.HUNT_PREFIX, ha.OLD_PREFIX)) and e not in owned]:
            try:
                await ha.delete_helper(eid)
                log.info("deleted orphan switch %s", eid)
                changed = True
            except Exception as e:
                log.warning("could not delete orphan %s: %s", eid, e)
        live_sensors = {f"sensor.shop_hunt_{h['slug']}" for h in self.hunts()}
        for eid in [e for e in st if e.startswith("sensor.shop_hunt_") and e not in live_sensors]:
            await ha.remove_state(eid)
        for h in self.hunts():
            if not h.get("ha"):
                try:
                    h["ha"] = await ha.create_helper("input_boolean", f"Shop hunt {h['label']}", icon="mdi:cart-heart")
                    await ha.service("input_boolean", "turn_on", {"entity_id": h["ha"]["entity_id"]})
                    h["missing"] = 0
                    log.info("hunt %s: created switch %s", h["id"], h["ha"]["entity_id"])
                    changed = True
                except Exception as e:
                    log.warning("hunt %s: switch not created (will retry): %s", h["id"], e)
            await self._push_alerts(h)
        for eid, name, domain, fields in ((ha.REQUEST_TEXT, "Shop watch request", "input_text", {"max": 255}),
                                          (ha.REQUEST_BUTTON, "Shop watch request send", "input_button", {})):
            if eid not in st:
                try:
                    await ha.create_helper(domain, name, icon="mdi:cart-search", **fields)
                    changed = True
                except Exception as e:
                    log.warning("could not create %s: %s", eid, e)
        if ha.STATUS_SENSOR not in st:
            await ha.set_state(ha.STATUS_SENSOR, self.data.get("request_status") or "Ready",
                               {"friendly_name": "Shop watch request status", "icon": "mdi:robot-outline"})
        return changed

    def hunts_for_ha(self) -> list[dict]:
        out = []
        for h in self.hunts():
            ws = self.watches_in(h)
            searches = [f'"{w["params"]["query"]}" on {", ".join(w["params"]["sites"])}, every {w["every_hours"]:g} h'
                        + ("" if w["active"] else " (paused)") for w in ws]
            names = sorted({r["name"] for r in h.get("recipients", [])})
            out.append(dict(h, searches=searches, sensor=f"sensor.shop_hunt_{h['slug']}",
                            recipients_text=", ".join(names)))
        return out

    async def _push_alerts(self, h: dict):
        a = h.get("alerts", [])
        try:
            await ha.set_state(f"sensor.shop_hunt_{h['slug']}", f"{len(a)} alerts",
                               {"friendly_name": f"{h['label']} alerts", "icon": "mdi:cart-heart", "alerts": a[:25]})
        except Exception as e:
            log.warning("hunt %s: alert sensor not updated: %s", h["id"], e)

    async def set_request_status(self, text: str):
        self.data["request_status"] = text[:255]
        async with self._lock:
            self._save()
        if ha.enabled():
            try:
                await ha.set_state(ha.STATUS_SENSOR, text,
                                   {"friendly_name": "Shop watch request status", "icon": "mdi:robot-outline"})
            except Exception as e:
                log.warning("status sensor not updated: %s", e)

    # ---------------------------------------------------------------- checking

    async def check(self, wid: str) -> dict:
        """Run one watch now and record what changed. Returns {"new": [...], "drops": [...], "errors": [...]}."""
        w = self.get(wid)
        if not w or w["id"] in self._running:
            return {"new": [], "drops": [], "errors": []}
        self._running.add(w["id"])
        try:
            t = time.time()
            rows, status, results = await self._search(w["params"])
        except Exception as e:
            log.exception("watch %s failed", wid)
            rows, status, results = [], [f"- check FAILED: {type(e).__name__}: {e}"], []
        finally:
            self._running.discard(w["id"])

        async with self._lock:
            w = self.get(wid)
            if not w:  # deleted while running
                return {"new": [], "drops": [], "errors": []}
            baseline = w["last_check"] is None
            new, drops = [], []
            for r in results:
                if r["items"]:  # a site that failed this time keeps its listings as "still listed"
                    w["store_ok"][r["name"]] = t
            for store, i in rows:
                url, price = i["url"], i.get("price")
                old = w["items"].get(url)
                if old is None:
                    rec = {"title": i["title"], "store": store, "details": i.get("details", ""), "price": price,
                           "first_seen": t, "last_seen": t, "baseline": baseline}
                    w["items"][url] = rec
                    if not baseline:
                        new.append((url, rec))
                    continue
                old.update(title=i["title"], details=i.get("details", ""), last_seen=t)
                if price is not None and old.get("price") is not None and price < old["price"] - 0.005:
                    old["prev_price"], old["dropped_at"], old["drop_pending"] = old["price"], t, True
                    drops.append((url, old))
                old["price"] = price
            for url in [u for u, rec in w["items"].items() if t - rec["last_seen"] > FORGET_AFTER_S]:
                del w["items"][url]
            errors = [s for s in status if "FAILED" in s]
            w["last_check"] = t
            w["next_check"] = t + w["every_hours"] * 3600 * random.uniform(0.9, 1.1)
            w["last_status"] = status
            w["checks"] = (w["checks"] + [{"t": t, "listed": len(rows), "new": len(new), "drops": len(drops),
                                            "failed": len(errors)}])[-30:]
            self._save()
        log.info("watch %s: %d listed, %d new, %d cheaper, %d sites failed", wid, len(rows), len(new), len(drops), len(errors))
        await self._review_and_notify(w)
        return {"new": new, "drops": drops, "errors": errors}

    @staticmethod
    def accepted(rec: dict) -> bool:
        """The LLM judged it a real match (or review is off) and it is not a repost of an earlier listing."""
        v = rec.get("review")
        return bool(v and v["match"] and not v.get("repost_of"))

    def _limits(self, w: dict) -> str:
        p = w["params"]
        bits = []
        if p.get("max_price") is not None:
            bits.append(f"max price ${p['max_price']:g}")
        if p.get("min_price") is not None:
            bits.append(f"min price ${p['min_price']:g}")
        if p.get("condition") not in (None, "", "any"):
            bits.append(f"condition: {p['condition']}")
        return ("Search limits already applied: " + ", ".join(bits) + "\n") if bits else ""

    async def _review_and_notify(self, w: dict):
        """Have the LLM judge every listing it has not judged yet, then alert on accepted new listings and
        price drops. Listings wait (no alert) until a review succeeds; a failed review is retried later.
        Reposts are judged against everything accepted in the whole hunt."""
        if w["id"] in self._reviewing:
            return
        self._reviewing.add(w["id"])
        h, new, drops = None, [], []
        try:
            h = self.data["hunts"].get(w["hunt"])
            if h is None:  # hunt ended while this check ran
                return
            items = w["items"]
            pending = [(u, rec) for u, rec in items.items() if "review" not in rec]
            if pending and review.enabled():
                pool = {}
                for x in self.watches_in(h):
                    for u, rec in x["items"].items():
                        if self.accepted(rec):
                            pool[u] = rec
                known = sorted(pool.items(), key=lambda x: -x[1]["first_seen"])[:40]
                w["review_tried"] = time.time()
                try:
                    verdicts = await review.review(h.get("want") or w["params"]["query"], self._limits(w),
                                                   [rec for _, rec in pending], [rec for _, rec in known])
                except Exception as e:
                    w["review_error"] = f"{type(e).__name__}: {e}"[:200]
                    log.warning("watch %s: review of %d listings failed, will retry: %s", w["id"], len(pending), e)
                    verdicts = None
                if verdicts:
                    w["review_error"] = ""
                    for (u, rec), v in zip(pending, verdicts):
                        orig = known[v["repost_of"]] if v["repost_of"] is not None else None
                        rec["review"] = {"match": v["match"], "note": v["note"], "repost_of": orig[0] if orig else None}
                        # A repost at a lower price is really a price drop on the original listing.
                        if orig and rec.get("price") is not None and orig[1].get("price") is not None \
                                and rec["price"] < orig[1]["price"] - 0.005 and not rec.get("baseline"):
                            rec.update(prev_price=orig[1]["price"], dropped_at=rec["first_seen"], drop_pending=True)
            elif pending:
                for _, rec in pending:
                    rec["review"] = {"match": True, "note": "", "repost_of": None, "unreviewed": True}

            new, drops = [], []
            alerted = set(h["alerted"])
            for u, rec in items.items():
                if "review" not in rec:
                    continue
                if not rec.get("alerted"):
                    rec["alerted"] = True
                    if rec.get("baseline"):
                        alerted.add(u)
                    elif self.accepted(rec) and u not in alerted:  # another search of this hunt may have sent it
                        new.append((u, rec))
                        alerted.add(u)
                        rec.pop("drop_pending", None)
                if rec.pop("drop_pending", False) and rec["review"]["match"]:
                    key = f"drop:{u}:{rec.get('price')}"  # the same drop seen by two searches alerts once
                    if key not in alerted:
                        drops.append((u, rec))
                        alerted.add(key)
            h["alerted"] = list(alerted)[-3000:]
            async with self._lock:
                if self.get(w["id"]):
                    self._save()
        finally:
            self._reviewing.discard(w["id"])
        if h is not None and (new or drops):
            log.info("hunt %s via watch %s: alerting %d new, %d cheaper", h["id"], w["id"], len(new), len(drops))
            await self._notify(h, new, drops)

    def is_current(self, w: dict, rec: dict) -> bool:
        """Still listed: seen on the store's most recent successful check."""
        return rec["last_seen"] >= w["store_ok"].get(rec["store"], 0)

    async def loop(self):
        """Run due watches one at a time, forever, and keep Home Assistant in sync."""
        await asyncio.sleep(20)
        while True:
            try:
                await self.sync_ha()
            except Exception:
                log.exception("Home Assistant sync error")
            try:
                due = sorted((w for w in self.all() if w["active"] and w["next_check"] <= time.time()),
                             key=lambda w: w["next_check"])
                for w in due:
                    await self.check(w["id"])
                    await asyncio.sleep(10)
                for w in self.all():  # listings whose LLM review failed earlier
                    if w.get("review_error") and time.time() - w.get("review_tried", 0) > REVIEW_RETRY_S:
                        await self._review_and_notify(w)
            except Exception:
                log.exception("watch loop error")
            await asyncio.sleep(HA_POLL_S)

    # ---------------------------------------------------------------- alerts

    async def _notify(self, h: dict, new: list, drops: list):
        """Record the alert on the hunt's page and send one phone notification: each listing with the
        LLM's comparison under it."""
        def money(p):
            return f"${p:,.0f}" if p is not None else "No price"

        def where(rec):
            return ", ".join(x for x in (rec.get("details", "").split(" · ")[0], rec["store"]) if x)

        now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        entries = [{"t": now, "title": rec["title"][:120], "url": u, "price": money(rec.get("price")), "was": "",
                    "where": where(rec), "note": (rec.get("review") or {}).get("note", "")} for u, rec in new]
        entries += [{"t": now, "title": rec["title"][:120], "url": u, "price": money(rec["price"]),
                     "was": money(rec["prev_price"]), "where": where(rec),
                     "note": (rec.get("review") or {}).get("note", "")} for u, rec in drops]
        async with self._lock:
            h["alerts"] = (entries + h.get("alerts", []))[:KEEP_ALERTS]
            self._save()
        if not (h.get("ha") and ha.enabled()):
            return
        await self._push_alerts(h)
        targets = [r["service"] for r in h.get("recipients", [])] or ([ha.HA_NOTIFY] if ha.HA_NOTIFY else [])
        if not targets:
            return

        def entry(e: dict) -> str:
            price = e["price"] + (f" (was {e['was']})" if e["was"] else "")
            return f"{price} · {e['title'][:70]} ({e['where']})" + (f"\n→ {e['note']}" if e["note"] else "")

        lines = [entry(e) for e in entries[:3]]
        if len(entries) > 3:
            lines.append(f"+{len(entries) - 3} more on the hunt's page in Home Assistant")
        parts = []
        if new:
            parts.append(f"{len(new)} new match" + ("es" if len(new) > 1 else ""))
        if drops:
            parts.append(f"{len(drops)} price drop" + ("s" if len(drops) > 1 else ""))
        try:
            await ha.notify(f"{h['label']}: {', '.join(parts)}", "\n\n".join(lines), entries[0]["url"], targets)
        except Exception as e:
            log.warning("Home Assistant notify failed: %s", e)
