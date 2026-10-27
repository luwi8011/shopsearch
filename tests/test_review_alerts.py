"""Review parsing (string booleans, label clean-up), alert de-dup trimming, and per-listing price history."""
import asyncio

import _harness as t

t.stub_ha()
import review  # noqa: E402
import watches  # noqa: E402

A = ("CL", t.item("JET 1236 apron", 300.0, "u-a"))


async def main():
    # A small model's "false" as a string must reject the listing.
    assert review._yes("false") is False and review._yes("False ") is False and review._yes(True) is True
    assert review._yes("true") is True and review._yes(None) is False
    # Real model names survive; only the prompt's labels are replaced.
    note = review._note("Same K2 skis as old-3, cheaper than new-2; N95 included")
    assert note == "Same K2 skis as the earlier listing, cheaper than another new listing; N95 included", note

    # Trimming the hunt's de-dup list keeps the newest keys.
    W = watches.Watches(t.fake_search([[], [A]]))
    W.sync_ha = t._noop
    w = await W.create({"query": "a", "sites": ["craigslist"]}, "a", 6, hunt="Trim")
    h = W.hunt_of(w)
    h["alerted"] = [f"old-{n}" for n in range(3000)]
    await W.check(w["id"])
    assert h["alerted"][-1] == "u-a" and "u-a" in h["alerted"] and "old-0" not in h["alerted"], h["alerted"][-3:]

    # Every price change is kept per listing.
    W._search = t.fake_search([[("CL", t.item("JET 1236 apron", 250.0, "u-a"))]])
    await W.check(w["id"])
    assert [p for _, p in w["items"]["u-a"]["prices"]] == [300.0, 250.0], w["items"]["u-a"]["prices"]
    print("test_review_alerts: OK")


asyncio.run(main())
