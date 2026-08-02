# Development

## Layout

```
services/
  gateway/app/           FastAPI service
    config.py            env-driven settings
    api/                 routers (search, extract, ingest, keys, health, stats)
    search/              providers (local, vector, searxng) + fusion + orchestrator
    workers/             ingest, recrawler, snippet loops
    core/                auth, rate limiting, db, redis, logging
    models.py            SQLAlchemy models (Document, Chunk, ApiKey, IngestJob)
    migrations/          Alembic
  mcp/                   MCP server (stdio + HTTP + SSE)
  renderer/              headless Chromium extractor
searxng/                 SearXNG compose profile + settings
tools/                   code-generated branding (logo, badges, banner)
tests/                   pytest suite
docs/                    this documentation + GitHub Pages landing page
```

## Running locally

Prereqs: a running Postgres (with pgvector) + Redis, and the deps in `requirements.txt`.

```bash
# from the repo root
export DATABASE_URL=postgresql+asyncpg://searchos:searchos@localhost:5432/searchos
export REDIS_URL=redis://localhost:6379/0
export SEARXNG_URL=http://localhost:8080
export BOOTSTRAP_ADMIN_API_KEY=sk_...

uvicorn app.main:app --app-dir services/gateway --port 8000 --reload &   # gateway
python services/gateway/app/workers/worker.py &                          # worker
```

## Tests

```bash
python -m pytest -q tests
```

34 tests covering search routing, providers, fusion dedup, admin ingest, keys, rate limiting, and MCP tool wiring. Tests use a scratch Postgres DB (env `TEST_DATABASE_URL` if you need to override).

## Codegen tools

Branding assets are generated, not hand-edited:

```bash
python tools/gen_logo.py        # logo.svg, logo-mark.svg, logo-wordmark.svg
python tools/gen_badges.py      # docs/assets/badges/*.svg (12)
python tools/gen_banner.py      # docs/assets/banner.svg + banner.png
```

Requires `rsvg-convert` for the PNG step.

## Conventions

- Async everywhere in the gateway (asyncpg + SQLAlchemy 2.0 async).
- Lazy-loading a SQLAlchemy relationship in an async handler is a crash — use `selectinload` in the query (see the reindex fix in `workers/ingest.py`).
- Never swallow provider failures — return them in the `degraded` list.
- Add a test alongside any behavior change.

## Git workflow

- Repo protects the code: commits are gated on the 34-test suite passing.
- Message style: lowercase, imperative, scoped (`searxng: cast meta to jsonb`, `mcp: add streamable http transport`).
