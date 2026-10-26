"""Alert recipients: a hunt's alerts go to everyone who asked for it; otherwise the default phone."""
import asyncio

import _harness as t

t.stub_ha()
import watches  # noqa: E402

KARI = [{"service": "mobile_app_karis_iphone", "name": "Kari"}]
LUCAS = [{"service": "mobile_app_localhost", "name": "Lucas"}]
OLD = ("CL", t.item("thing", 5.0, "u-old"))
NEW = ("CL", t.item("thing 2", 4.0, "u-new"))


async def main():
    W = watches.Watches(t.fake_search([[OLD], [OLD], [OLD, NEW]]))
    W.sync_ha = t._noop
    w1 = await W.create({"query": "x", "sites": ["craigslist"]}, "x", 6, hunt="Shared", recipients=KARI)
    await W.create({"query": "y", "sites": ["craigslist"]}, "y", 6, hunt="Shared", recipients=LUCAS)
    W.hunt_of(w1)["ha"] = {"entity_id": "input_boolean.x"}
    await W.check(w1["id"])
    assert t.SENT[-1]["targets"] == ["mobile_app_karis_iphone", "mobile_app_localhost"], t.SENT

    t.SENT.clear()
    W2 = watches.Watches(t.fake_search([[OLD], [OLD, NEW]]))
    W2.sync_ha = t._noop
    w = await W2.create({"query": "z", "sites": ["craigslist"]}, "z", 6, hunt="From Open WebUI")
    W2.hunt_of(w)["ha"] = {"entity_id": "input_boolean.y"}
    await W2.check(w["id"])
    assert t.SENT[-1]["targets"] == ["mobile_app_default"], t.SENT
    print("test_routing: OK")


asyncio.run(main())
