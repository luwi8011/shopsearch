"""Turns a watch request typed in Home Assistant into a hunt, using the local LLM.

The model gets three tools: shop_search (to try queries and see what comes back),
list_hunts, and create_search (adds a scheduled search to a hunt). It runs a short
tool loop and ends with a one-sentence summary for the user.
"""

import json
import logging
import time

import httpx

import review

log = logging.getLogger("shopsearch.agent")

MAX_STEPS = 10

SYSTEM = """You set up price watches for the user from a short request they typed into Home Assistant. They will not see this conversation, only your final one-sentence summary on their phone.

About the user: lives in Erie, Colorado (ZIP 80516). Usually drives up to ~30 minutes for local pickup (craigslist/facebook radius_miles 25) but goes farther for big or rare items (raise radius_miles to 60-150 for vehicles, machinery, rare parts). They buy new and used.

How to work:
1. Pick sites that actually sell this kind of item. Known sites: {sites}. Local/used: craigslist, facebook. Any other store works by domain.
2. Every word containing a digit in a query (model numbers like 2853-20, ar15, 1236) must appear in a listing's title or the listing is dropped, so put at most ONE model number in a query; make a separate search per model number instead. Try 1-3 shop_search calls to find query wording that returns the right listings (titles as sellers write them, model numbers). Look at what comes back.
3. Create ONE hunt for the request: call create_search once per useful variation (different wording, model numbers, or site sets), all with the same `hunt` name, max 4 searches. Write `want` precisely for the whole hunt (not one site or variation): what counts as a match and what does not (e.g. whole item vs parts). It is set by the first search of a hunt; later searches keep it. Use every_hours 4-6 for craigslist/facebook, 12-24 for stores. Put price limits the user gave into max_price/min_price.
4. If list_hunts shows a hunt for the same goal already, add searches to it (same hunt name) instead of starting another.
5. Finish with ONE plain sentence (max 30 words): the hunt name, what it watches, and anything notable you saw (e.g. current cheapest). No markdown.
If the request is not a shopping watch request, create nothing and reply with one sentence saying so."""

TOOLS = [
    {"type": "function", "function": {
        "name": "shop_search", "description": "Search stores now and see current listings (price-sorted table).",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}, "sites": {"type": "array", "items": {"type": "string"}},
            "max_price": {"type": "number"}, "min_price": {"type": "number"},
            "condition": {"type": "string", "enum": ["any", "new", "used"]},
            "radius_miles": {"type": "integer"}}, "required": ["query", "sites"]}}},
    {"type": "function", "function": {
        "name": "list_hunts", "description": "List the user's existing hunts and their searches.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "create_search",
        "description": "Add a scheduled search to a hunt (creates the hunt if the name is new). Alerts the user's "
                       "phone about new matching listings.",
        "parameters": {"type": "object", "properties": {
            "hunt": {"type": "string", "description": "Short hunt name shown to the user, e.g. 'KTM 300 dirt bike'"},
            "want": {"type": "string", "description": "1-2 sentences: exactly what counts as a match and what does not"},
            "query": {"type": "string"}, "sites": {"type": "array", "items": {"type": "string"}},
            "max_price": {"type": "number"}, "min_price": {"type": "number"},
            "condition": {"type": "string", "enum": ["any", "new", "used"]},
            "radius_miles": {"type": "integer"}, "every_hours": {"type": "number"}},
            "required": ["hunt", "want", "query", "sites"]}}},
]


async def run(request: str, sites: str, call_tool) -> str:
    """call_tool: async (name, args) -> str. Returns the model's final summary."""
    msgs = [{"role": "system", "content": SYSTEM.format(sites=sites)},
            {"role": "user", "content": request}]
    for step in range(MAX_STEPS):
        t = time.time()
        async with httpx.AsyncClient(timeout=review.LLM_TIMEOUT_S) as c:
            r = await c.post(f"{review.LLM_URL}/chat/completions", json={
                "model": review.LLM_MODEL, "messages": msgs, "tools": TOOLS, "temperature": 0.3, "max_tokens": 6000})
            r.raise_for_status()
        msg = r.json()["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        log.info("request step %d (%.0fs): %d tool calls", step + 1, time.time() - t, len(calls))
        if not calls:
            return (msg.get("content") or "").strip() or "Done."
        msgs.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for tc in calls:
            fn = tc["function"]
            try:
                args = json.loads(fn.get("arguments") or "{}")
                out = await call_tool(fn["name"], args)
            except Exception as e:
                out = f"ERROR: {type(e).__name__}: {e}"
            log.info("request tool %s(%s) -> %d chars", fn["name"], (fn.get("arguments") or "")[:200], len(out))
            msgs.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": out[:6000]})
    return "Stopped after too many steps; check the Shop Watches section for what was created."
