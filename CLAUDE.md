# shopsearch

Price-research MCP server and deal watcher for a self-hosted LLM (Open WebUI + Strata), with
Home Assistant alerts. Start with `docs/HANDOFF.md` (architecture, deployment, decisions),
`docs/HOME_ASSISTANT.md` (everything it creates in HA) and `docs/ROADMAP.md` (open work).

## Run

- Deployed as Docker on the `llm` VM (192.168.0.252): `docker compose up -d --build` from the repo root.
  The code is copied into the image, so every change needs a rebuild.
- Secrets go in `.env` (template `.env.example`); never commit them. Runtime state is in `data/` (git-ignored).
- Event log: `data/shopsearch.log`. Docker logs are lost on rebuild.

## Test

- Quick checks against the running service: `curl 'http://127.0.0.1:8765/api/search?q=...&sites=craigslist,facebook'`,
  `/api/watches`, `/health`.
- Unit-style scripts in `tests/` run inside the container with fakes (see tests/README.md).
- Don't burst-test eBay, Home Depot, Lowe's or other Akamai-protected sites from the home network.
  Repeated rapid requests got the home IP blocked by eBay.

## Conventions

- Python 3.12, asyncio throughout. Keep `mcp==1.30.0` pinned (2.x changed the API).
- Every Home Assistant change goes through `Watches.sync_ha()`. Never create helpers elsewhere (duplicate bug).
- Match the existing style: module docstrings explain the why, few inline comments, f-strings, no type-heavy
  abstractions.
- MCP tool descriptions are prompts for a small local model, so keep them short and concrete, and
  state rules the model can't infer (e.g. one model number per query).
