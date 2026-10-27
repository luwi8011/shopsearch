"""Store health: one alert after two failed test searches in a row, none while it stays down, one when it recovers."""
import asyncio

import _harness as t

t.stub_ha()
import health  # noqa: E402

OK = {"items": [{"title": "USB-C cable", "price": 5.0}], "error": ""}
EMPTY = {"items": [], "error": "page loaded but no product listings were found on it"}


async def main():
    answers = [OK, EMPTY, EMPTY, EMPTY, OK, OK]

    async def search(key, query):
        assert query == "usb c cable"
        return answers.pop(0)

    H = health.Health(search)
    titles = []
    for _ in range(6):
        t.SENT.clear()
        s = await H.check("walmart")
        titles += [m["title"] for m in t.SENT]
    assert titles == ["Shop Search: Walmart stopped working", "Shop Search: Walmart works again"], titles
    assert s["fails"] == 0 and not s["failing"]
    assert H.summary()["walmart"]["ok"] is True
    print("test_health: OK")


asyncio.run(main())
