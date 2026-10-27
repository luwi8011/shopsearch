"""LLM review of a watch's new listings before anything is sent to the phone.

Keyword matching lets through parts, gear and look-alikes ("KTM 300 exhaust pipe"
for a KTM 300 bike), and marketplaces are full of reposts. The local LLM reads
each new listing against the watch's plain-language goal ("want") and the
listings seen earlier, and answers per listing: a real match or not, and
whether it is a repost of one already seen.

New listings that can alert come with the seller's description ("Page:") and,
when the model takes images, their main photo, so "parts only" or "doesn't run"
in the description is caught. Listings the shopper marked "Not interested" are
shown as examples of what they don't want.
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
Listings seen earlier (for spotting reposts of the same item):
{known}
{rejected}
NEW listings to judge:
{new}

Some NEW listings include the seller's own description ("Page:") and a photo. Use them: "parts only", "not running", "needs engine", "for parts or repair", "wanted", or a photo of something other than the whole item mean it is not a match unless the shopper asked for that. Mention a real problem from the description or photo in the note.

For each NEW listing decide:
- "match": true only if the listing is the item described above (right product/model, a whole item rather than parts, accessories, gear, "wanted" posts or services, unless the shopper asked for those). Judge from the title, price and details; an implausibly low price for a whole item usually means parts or a scam.
- "repost_of": the label (old-#) of an accepted listing that is very likely the SAME physical item reposted (same model/year/description, same area, similar price), else null.
- "note": one short sentence (max 20 words) for the shopper comparing this listing to what they want: how well it fits (model, year, completeness, condition), the price against their limits or typical value, and any red flag. Shown in a phone notification, so be concrete; no filler. Never mention the old-#/new-# labels; say "the earlier listing" instead.

Reply with only JSON: {{"results": [{{"id": "new-1", "match": true, "repost_of": null, "note": "..."}}, ...]}}"""


def enabled() -> bool:
    return bool(LLM_URL and LLM_MODEL)


def _yes(v) -> bool:
    """Small models sometimes answer "false" as a string, and bool("false") is True."""
    return v is True or str(v).strip().lower() in ("true", "yes", "1")


def _note(text: str) -> str:
    """The note without the prompt's labels. They look like old-3/new-2 so that real model names
    (K2 skis, N95 masks) are never mistaken for them."""
    text = re.sub(r"\bold-\d+\b", "the earlier listing", text, flags=re.I)
    return re.sub(r"\bnew-\d+\b", "another new listing", text, flags=re.I)[:200]


def enabled() -> bool:
    return bool(LLM_URL and LLM_MODEL)


def _line(i: dict) -> str:
    price = f"${i['price']:,.0f}" if i.get("price") is not None else "no price"
    det = f" | {i['details']}" if i.get("details") else ""
    page = f"\n   Page: {i['page']}" if i.get("page") else ""
    return f"{price} | {i['title'][:140]} | {i['store']}{det}{page}"


def _parse(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    m = re.search(r"\{.*\}", text, flags=re.S)
    data = json.loads(m.group(0)) if m else {}
    return {str(r.get("id")).strip().lower(): r for r in data.get("results", []) if isinstance(r, dict)}


async def review(want: str, limits: str, new: list[dict], known: list[dict], photos: list[str | None] | None = None,
                 rejected: list[dict] | None = None) -> list[dict]:
    """new/known: listing dicts (title, price, store, details, page). photos: per new listing, an image data URL
    or None. rejected: listings the shopper turned down. Returns one verdict per new listing, in order:
    {"match": bool, "repost_of": index into known or None, "note": str}. Raises on LLM failure."""
    out: list[dict] = []
    photos = photos or [None] * len(new)
    known_txt = "\n".join(f"old-{n}: {_line(dict(k, page=''))}" for n, k in enumerate(known[:40], 1)) or "(none yet)"
    rejected_txt = ("\nThe shopper said \"not interested\" to these; treat listings like them as not a match:\n"
                    + "\n".join(f"- {_line(dict(x, page=''))}" for x in rejected[-15:]) + "\n") if rejected else ""
    for start in range(0, len(new), BATCH):
        chunk, chunk_photos = new[start:start + BATCH], photos[start:start + BATCH]
        prompt = PROMPT.format(want=want, limits=limits, known=known_txt, rejected=rejected_txt,
                               new="\n".join(f"new-{n}: {_line(i)}" for n, i in enumerate(chunk, 1)))
        content = [{"type": "text", "text": prompt}]
        for n, p in enumerate(chunk_photos, 1):
            if p:
                content += [{"type": "text", "text": f"Photo of new-{n}:"}, {"type": "image_url", "image_url": {"url": p}}]
        try:
            text = await _ask(content if len(content) > 1 else prompt)
        except httpx.HTTPStatusError:
            if len(content) == 1:
                raise
            log.warning("LLM refused the photos (no image input?); reviewing from text only")
            text = await _ask(prompt)
        verdicts = _parse(text)
        if not verdicts:
            raise ValueError("LLM reply had no usable JSON")
        for n in range(1, len(chunk) + 1):
            v = verdicts.get(f"new-{n}") or verdicts.get(str(n))
            if v is None:
                raise ValueError(f"LLM reply skipped listing new-{n}")
            k = v.get("repost_of")
            k = int(re.sub(r"\D", "", str(k)) or 0) if k not in (None, "", "null") else 0
            out.append({"match": _yes(v.get("match")), "repost_of": k - 1 if 0 < k <= len(known[:40]) else None,
                        "note": _note(str(v.get("note") or ""))})
    return out


async def _ask(content) -> str:
    async with httpx.AsyncClient(timeout=LLM_TIMEOUT_S) as c:
        r = await c.post(f"{LLM_URL}/chat/completions", json={
            "model": LLM_MODEL, "temperature": 0, "max_tokens": 8000,
            "messages": [{"role": "user", "content": content}]})
        r.raise_for_status()
    return r.json()["choices"][0]["message"].get("content") or ""
