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
(cd services/gateway && python worker_entrypoint.py) &                   # worker
```

There is no `app/workers/worker.py`; the entrypoint is
`services/gateway/worker_entrypoint.py`. The gateway owns schema migrations
(`alembic upgrade head` in its container command). The worker skips them unless
`WORKER_RUN_MIGRATIONS=true`, so two processes never race on DDL.

## Tests

```bash
python -m pytest -q tests
```

The unit suite runs with no infrastructure and uses fakes:

```bash
python -m pytest -q tests
```

Two suites need real services and skip themselves when the env vars are unset.
Point them at throwaway instances to exercise the actual SQL and Redis
semantics — the FTS/vector queries, rate-limit atomicity and cache-key isolation
have no meaningful unit doubles:

```bash
docker run -d --name searchos-test-pg -p 15432:5432 \
  -e POSTGRES_USER=searchos -e POSTGRES_PASSWORD=searchos -e POSTGRES_DB=searchos \
  pgvector/pgvector:pg16
docker run -d --name searchos-redis -p 16380:6379 redis:7-alpine

export TEST_DATABASE_URL=postgresql+asyncpg://searchos:searchos@127.0.0.1:15432/searchos
export TEST_REDIS_URL=redis://127.0.0.1:16380/0
python -m pytest -q tests
```

> The integration fixture **TRUNCATEs** `documents`, `chunks`, `api_keys` and
> `ingest_jobs` before every test. Point `TEST_DATABASE_URL` only at a throwaway
> database like the container above, never at the stack's own Postgres.


MCP protocol tests drive the real SDK against a live gateway on all three
transports (stdio, legacy HTTP+SSE, Streamable HTTP):

```bash
uvicorn app.main:app --app-dir services/gateway --port 18002 &
SEARCHOS_URL=http://127.0.0.1:18002 SEARCHOS_API_KEY=sk_... \
  python services/mcp/searchos_mcp.py --transport http --port 18100 &

export TEST_MCP_GATEWAY_URL=http://127.0.0.1:18002
export TEST_MCP_API_KEY=sk_...
export TEST_MCP_SERVER_URL=http://127.0.0.1:18100
export TEST_MCP_HTTP_TOKEN=...
python -m pytest -q tests/test_mcp_e2e.py
```

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
