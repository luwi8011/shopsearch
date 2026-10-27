"""LLM review of a watch's new listings before anything is sent to the phone.

Keyword matching lets through parts, gear and look-alikes ("KTM 300 exhaust pipe"
for a KTM 300 bike), and marketplaces are full of reposts. The local LLM reads
each new listing against the watch's plain-language goal ("want") and the
listings already accepted, and answers per listing: a real match or not, and
whether it is a repost of one already seen.
"""

import json
import logging
import os
import re

import httpx

log = logging.getLogger("shopsearch.review")

LLM_URL = os.environ.get("LLM_URL", "").rstrip("/")          # OpenAI-compatible, e.g. http://192.168.1.20:11434/v1
LLM_MODEL = os.environ.get("LLM_MODEL", "")
LLM_TIMEOUT_S = int(os.environ.get("LLM_TIMEOUT_S", "300"))
BATCH = 20

PROMPT = """You screen marketplace and store listings for a shopper. Decide for each NEW listing whether it is something the shopper actually wants.

What the shopper wants:
{want}
{limits}
Listings already accepted earlier (for spotting reposts of the same item):
{known}

NEW listings to judge:
{new}

For each NEW listing decide:
- "match": true only if the listing is the item described above (right product/model, a whole item rather than parts, accessories, gear, "wanted" posts or services, unless the shopper asked for those). Judge from the title, price and details; an implausibly low price for a whole item usually means parts or a scam.
- "repost_of": the number (K#) of an accepted listing that is very likely the SAME physical item reposted (same model/year/description, same area, similar price), else null.
- "note": one short sentence (max 20 words) for the shopper comparing this listing to what they want: how well it fits (model, year, completeness, condition), the price against their limits or typical value, and any red flag. Shown in a phone notification, so be concrete; no filler. Never mention the K#/N# labels; say "the earlier listing" instead.

Reply with only JSON: {{"results": [{{"id": "N1", "match": true, "repost_of": null, "note": "..."}}, ...]}}"""


def enabled() -> bool:
    return bool(LLM_URL and LLM_MODEL)


def _line(i: dict) -> str:
    price = f"${i['price']:,.0f}" if i.get("price") is not None else "no price"
    det = f" | {i['details']}" if i.get("details") else ""
    return f"{price} | {i['title'][:140]} | {i['store']}{det}"


def _parse(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    m = re.search(r"\{.*\}", text, flags=re.S)
    data = json.loads(m.group(0)) if m else {}
    return {str(r.get("id")): r for r in data.get("results", []) if isinstance(r, dict)}


async def review(want: str, limits: str, new: list[dict], known: list[dict]) -> list[dict]:
    """new/known: listing dicts (title, price, store, details). Returns one verdict per new listing,
    in order: {"match": bool, "repost_of": index into known or None, "note": str}. Raises on LLM failure."""
    out: list[dict] = []
    known_txt = "\n".join(f"K{n}: {_line(k)}" for n, k in enumerate(known[:40], 1)) or "(none yet)"
    for start in range(0, len(new), BATCH):
        chunk = new[start:start + BATCH]
        prompt = PROMPT.format(want=want, limits=limits, known=known_txt,
                               new="\n".join(f"N{n}: {_line(i)}" for n, i in enumerate(chunk, 1)))
        async with httpx.AsyncClient(timeout=LLM_TIMEOUT_S) as c:
            r = await c.post(f"{LLM_URL}/chat/completions", json={
                "model": LLM_MODEL, "temperature": 0, "max_tokens": 8000,
                "messages": [{"role": "user", "content": prompt}]})
            r.raise_for_status()
        verdicts = _parse(r.json()["choices"][0]["message"].get("content") or "")
        if not verdicts:
            raise ValueError("LLM reply had no usable JSON")
        for n in range(1, len(chunk) + 1):
            v = verdicts.get(f"N{n}") or verdicts.get(str(n))
            if v is None:
                raise ValueError(f"LLM reply skipped listing N{n}")
            k = v.get("repost_of")
            k = int(re.sub(r"\D", "", str(k)) or 0) if k not in (None, "", "null") else 0
            out.append({"match": bool(v.get("match")), "repost_of": k - 1 if 0 < k <= len(known[:40]) else None,
                        "note": re.sub(r"\b[KN]\d+\b", "the earlier listing", str(v.get("note") or ""))[:200]})
    return out
