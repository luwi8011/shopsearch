"""LIVE: the local LLM judges made-up KTM listings (needs LLM_URL/LLM_MODEL, i.e. the real container env).
Checks parts/wanted/other-model rejection, repost-as-price-drop, and prints the phone alert text."""
import asyncio
import os

os.environ["TEST_LIVE_LLM"] = "1"
import _harness as t  # noqa: E402

assert os.environ.get("LLM_URL"), "set LLM_URL and LLM_MODEL (or run inside the container)"
t.stub_ha()
import watches  # noqa: E402

FB = "Facebook Marketplace"
base = [(FB, t.item("2019 KTM 300 XCW TPI", 3900.0, "u1", "Longmont, CO")),
        (FB, t.item("KTM 300 radiator guards", 60.0, "u2", "Erie, CO"))]
later = base + [(FB, t.item("2017 KTM 300 XC, fresh top end, plated", 3700.0, "u3", "Boulder, CO")),
                (FB, t.item("KTM 300 XC exhaust pipe FMF", 150.0, "u4", "Thornton, CO")),
                (FB, t.item("WANTED: KTM 300 any condition", 1.0, "u5", "Denver, CO")),
                (FB, t.item("2019 KTM 300 XC-W TPI must sell this week", 3500.0, "u6", "Longmont, CO")),
                (FB, t.item("2016 KTM 250 SX", 3200.0, "u7", "Brighton, CO"))]


async def main():
    W = watches.Watches(t.fake_search([base, later]))
    W.sync_ha = t._noop
    w = await W.create({"query": "ktm 300", "sites": ["facebook"], "max_price": 4000}, "KTM", 6,
                       want="A complete, running KTM 300 two-stroke dirt bike (XC, XC-W, TPI or TBI, any year). "
                            "Not parts, gear, or other models.", hunt="KTM dirt bike")
    W.hunt_of(w)["ha"] = {"entity_id": "input_boolean.x"}
    await W.check(w["id"])
    v = {u: r["review"] for u, r in W.get(w["id"])["items"].items()}
    for u, r in v.items():
        print(f"  {u}: match={r['match']} repost_of={r['repost_of']} | {r['note']}")
    assert v["u3"]["match"] and not v["u3"]["repost_of"], "2017 XC should be accepted"
    assert not any(v[u]["match"] and not v[u]["repost_of"] for u in ("u2", "u4", "u5", "u7")), "should be rejected"
    assert v["u6"]["repost_of"] == "u1", "must-sell 2019 should be a repost of u1"
    print("\nALERT:", t.SENT[-1]["title"], "\n" + t.SENT[-1]["message"])
    print("test_review_live: OK")


asyncio.run(main())
