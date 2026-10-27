"""Notification buttons: "Not interested" rejects the listing (no more alerts for it, an example for the
reviewer, marked on the hunt page); "Stop hunt" ends the hunt."""
import asyncio

import _harness as t

t.stub_ha()
import watches  # noqa: E402

A = ("CL", t.item("JET 1236 apron", 300.0, "u-a"))
B = ("CL", t.item("JET 1236 apron, rusty", 250.0, "u-b"))


async def main():
    W = watches.Watches(t.fake_search([[A], [A, B], [A, ("CL", t.item("JET 1236 apron, rusty", 200.0, "u-b"))]]))
    W.sync_ha = t._noop
    w = await W.create({"query": "a", "sites": ["craigslist"]}, "a", 6, hunt="Apron")
    h = W.hunt_of(w)
    h["ha"] = {"entity_id": "input_boolean.x"}
    await W.check(w["id"])
    (sent,) = t.SENT
    assert sent["url"] == "u-b" and sent["tag"] == f"shop_{watches.listing_id('u-b')}", sent
    no, stop = (a["action"] for a in sent["actions"])

    await W.on_action(no)
    assert w["items"]["u-b"]["review"]["rejected"] and not W.accepted(w["items"]["u-b"])
    assert h["rejected"][-1]["title"] == "JET 1236 apron, rusty"
    assert h["alerts"][0]["rejected"] is True
    t.SENT.clear()
    await W.check(w["id"])  # u-b is now cheaper: no price-drop alert for a rejected listing
    assert t.SENT == [], t.SENT

    await W.on_action(stop)
    assert not W.hunts() and not W.all()
    print("test_buttons: OK")


asyncio.run(main())
