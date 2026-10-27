# shopsearch

Price-research MCP server and deal watcher for a self-hosted LLM, shipped as a Home Assistant OS add-on
(`shopsearch/`; this repo is also the add-on repository). Start with `docs/HANDOFF.md` (architecture,
deployment, decisions), `docs/HOME_ASSISTANT.md` (everything it creates in HA) and `docs/ROADMAP.md` (open work).

**The repo is public.** Never commit hosts, people, tokens or other install details; they go in the
git-ignored `LOCAL.md` (read it first if it exists: it says where this install runs and how to reach it).

## Run and release

- The add-on's code is `shopsearch/app/`. GitHub Actions (`.github/workflows/addon-image.yml`) builds
  `shopsearch/Dockerfile` into `ghcr.io/luwi8011/shopsearch-amd64:<version>`; Home Assistant pulls that image.
- Release: bump `version` in `shopsearch/config.yaml` (a published version is never rebuilt), add to
  `shopsearch/CHANGELOG.md`, push `work` (ws-publish), wait for the "Add-on image" run to succeed, then fast-forward
  `main` and update the add-on in HA (Supervisor store reload + update). Logs: the add-on's Log tab or
  `GET /api/hassio/addons/<slug>/logs` with an admin token; events also go to `/data/shopsearch.log`.
- Plain Docker: `docker compose up -d --build` with `.env` (template `.env.example`).

## Test

- Unit-style scripts in `tests/` run against fakes, locally or in the container (see tests/README.md).
- The whole server runs locally for REST/MCP checks (see docs/HANDOFF.md, Testing).
- Don't burst-test eBay, Home Depot, Lowe's or other Akamai-protected sites from a home network.
  Repeated rapid requests got a home IP blocked by eBay.

## Conventions

- Python 3.12, asyncio throughout. Keep `mcp==1.30.0` pinned (2.x changed the API).
- New settings: an add-on option in `config.yaml` (+ `translations/en.yaml`), mapped to an environment
  variable in `addon_options.py`, read by the module that uses it, and listed in `.env.example`.
- Every Home Assistant change goes through `Watches.sync_ha()`. Never create helpers elsewhere (duplicate bug).
- Match the existing style: module docstrings explain the why, few inline comments, f-strings, no type-heavy
  abstractions.
- MCP tool descriptions are prompts for a small local model, so keep them short and concrete, and
  state rules the model can't infer (e.g. one model number per query).
