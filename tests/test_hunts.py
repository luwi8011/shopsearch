"""Hunts: grouping by name, one alert per listing across a hunt's searches, merge, delete."""
import asyncio

import _harness as t

t.stub_ha()
import watches  # noqa: E402

A = ("CL", t.item("JET 1236 apron", 300.0, "u-a"))
B = ("CL", t.item("JET 1236 apron, new listing", 250.0, "u-b"))


async def main():
    # searches 1-2 are baselines (A only); from call 3 on, B appears in both searches
    W = watches.Watches(t.fake_search([[A], [A], [A, B]]))
    W.sync_ha = t._noop
    w1 = await W.create({"query": "a", "sites": ["craigslist"]}, "var 1", 6, want="apron", hunt="Lathe apron")
    w2 = await W.create({"query": "b", "sites": ["craigslist"]}, "var 2", 6, want="ignored", hunt="lathe  APRON")
    h = W.hunt_of(w1)
    assert w1["hunt"] == w2["hunt"], "same hunt name (any case/spacing) must group"
    assert h["want"] == "apron", "a later search must not overwrite the hunt's want"
    h["ha"] = {"entity_id": "input_boolean.x"}
    await W.check(w1["id"])
    await W.check(w2["id"])
    assert len(t.SENT) == 1 and "u-b" in t.SENT[0]["url"], f"expected one alert for u-b, got {t.SENT}"
    w3 = await W.create({"query": "c", "sites": ["craigslist"]}, "other", 6, hunt="Other")
    await W.move(w3["id"], "Lathe apron")
    assert len(W.hunts()) == 1 and len(W.watches_in(h)) == 3, "move must merge and drop the empty hunt"
    # a cheaper relist seen by both searches: one price-drop alert, not two
    for w in (w1, w2):
        w["items"]["u-b"].update(price=300.0)
    W._search = t.fake_search([[A, B]])  # B back at 250 -> drop
    t.SENT.clear()
    await W.check(w1["id"])
    await W.check(w2["id"])
    assert len(t.SENT) == 1 and "price drop" in t.SENT[0]["title"], f"expected one drop alert, got {t.SENT}"
    await W.delete_hunt("Lathe apron")
    assert not W.hunts() and not W.all()
    print("test_hunts: OK")


asyncio.run(main())
