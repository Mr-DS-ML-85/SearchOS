<p align="center">
  <img src="docs/assets/banner.png" alt="SearchOS" width="100%" />
</p>

<p align="center">
  <b>Self-hosted search API for AI agents.</b><br/>
  Your own FTS + vector index, live web fallback, continuous recursive crawler, and an MCP server — no vendor lock-in, no per-query fees, no data leaving your infra.
</p>

<p align="center">
  <a href="docs/assets/badges/python.svg"><img src="docs/assets/badges/python.svg" alt="Python 3.13"/></a>
  <a href="docs/assets/badges/fastapi.svg"><img src="docs/assets/badges/fastapi.svg" alt="FastAPI gateway"/></a>
  <a href="docs/assets/badges/mcp.svg"><img src="docs/assets/badges/mcp.svg" alt="MCP 3 transports"/></a>
  <a href="docs/assets/badges/postgres.svg"><img src="docs/assets/badges/postgres.svg" alt="PostgreSQL pgvector + HNSW"/></a>
  <a href="docs/assets/badges/redis.svg"><img src="docs/assets/badges/redis.svg" alt="Redis cache + rate limits"/></a>
  <a href="docs/assets/badges/searxng.svg"><img src="docs/assets/badges/searxng.svg" alt="SearXNG web fallback"/></a>
  <a href="docs/assets/badges/crawler.svg"><img src="docs/assets/badges/crawler.svg" alt="Crawler recursive + recrawl"/></a>
  <a href="docs/assets/badges/docker.svg"><img src="docs/assets/badges/docker.svg" alt="Docker compose"/></a>
  <a href="docs/assets/badges/alembic.svg"><img src="docs/assets/badges/alembic.svg" alt="Alembic migrations"/></a>
  <a href="docs/assets/badges/grafana.svg"><img src="docs/assets/badges/grafana.svg" alt="Grafana metrics"/></a>
  <a href="docs/assets/badges/ci.svg"><img src="docs/assets/badges/ci.svg" alt="Tests 34 passed"/></a>
  <a href="docs/assets/badges/license.svg"><img src="docs/assets/badges/license.svg" alt="MIT"/></a>
</p>

<p align="center">
  <a href="https://your-org.github.io/searchos"><b>Landing page</b></a> ·
  <a href="docs/getting-started.md">Getting started</a> ·
  <a href="docs/architecture.md">Architecture</a> ·
  <a href="docs/api.md">API reference</a> ·
  <a href="docs/searxng.md">SearXNG fallback</a> ·
  <a href="docs/crawler.md">Crawler</a> ·
  <a href="docs/mcp.md">MCP server</a> ·
  <a href="docs/deployment.md">Deployment</a> ·
  <a href="docs/development.md">Development</a>
</p>

---

SearchOS builds a production-grade, fully self-hosted search API from open-source components. This is the "Apple approach" applied to search: instead of picking one vendor, it studies what makes each search API good — Exa's semantic neural index, Tavily's clean LLM-ready content, SerpApi's rich SERP parameters, Jina's JS-rendered reading — then rebuilds those features yourself so you get the strengths without the vendor lock-in:

| Layer | SerpApi | Tavily | Exa | Jina | SearchOS |
|---|---|---|---|---|---|
| Cost | $25+/mo, per-search | credits, per-search | per-search | tokens | **free, self-hosted** |
| Rate limits | hourly caps | credits | plan caps | RPM/TPM | **your own quotas** |
| Privacy | vendor sees queries | vendor sees queries | vendor sees queries | vendor sees queries | **your infra only** |
| Result source | Google | own index | neural index | own index | **your own crawler + index** |
| Live web fallback | yes | — | — | — | **yes (SearXNG, self-cached)** |
| LLM-ready output | no | yes | yes | yes | **yes (clean text/markdown)** |
| Content extraction | no | yes | yes | yes | **yes (robots-respecting)** |
| MCP server | — | — | — | — | **yes (stdio + SSE + Streamable HTTP)** |
| Own your data | no | no | no | no | **yes (full index export)** |

## Features

- **Own search index** — Postgres FTS (`ts_rank_cd`, english config) + pgvector HNSW embeddings, fused with Reciprocal Rank Fusion.
- **Live web fallback that learns** — when local coverage is thin, SearXNG results are merged in, **saved into your index, and re-crawled**, so repeat queries are served instantly without re-hitting the web.
- **Continuous recursive crawler** — follows nested same-domain links (depth-limited), plus automatic **re-crawl of stale pages** so your index stays fresh.
- **LLM-ready extraction** — clean markdown/text from HTML, PDF, and JS-rendered pages (headless renderer), robots.txt-aware.
- **MCP server** — expose search, extract, ingest, stats and health to any AI agent over **stdio, legacy HTTP+SSE, and the new stateless Streamable HTTP** (2026-07-28 spec).
- **Production ops** — Redis cache + rate limits, auth (hashed API keys), Prometheus/Grafana metrics, Alembic migrations.

## Quick start

Prerequisites: Docker + Docker Compose.

```bash
cp .env.example .env

# generate a real admin key
python3 -c "import secrets; print('sk_' + secrets.token_urlsafe(32))"

# paste it into .env as BOOTSTRAP_ADMIN_API_KEY=sk_...
docker compose up -d --build

# optionally enable the SearXNG live-web fallback
docker compose --profile web up -d searxng
```

The gateway is at `http://localhost:8000` (Alembic migrations auto-run on startup).

### Create an API key

```bash
curl -s -X POST http://localhost:8000/v1/admin/keys \
  -H "Authorization: Bearer $BOOTSTRAP_ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name":"ci-bot","role":"user"}'
```

Returns `{"api_key":"sk_...","name":"ci-bot","role":"user"}` — store it securely (only shown once).

### Search

```bash
curl -s "http://localhost:8000/v1/search?q=how+do+search+engines+work&num_results=5" \
  -H "Authorization: Bearer sk_..."
```

When local coverage is thin the response also includes live `web` results that get saved + crawled into your index. See [docs/searxng.md](docs/searxng.md) for how that decision is made.

### Extract a URL to clean content

```bash
curl -s -X POST http://localhost:8000/v1/extract \
  -H "Authorization: Bearer sk_..." \
  -H "Content-Type: application/json" \
  -d '{"url":"https://en.wikipedia.org/wiki/Web_search_engine"}'
```

### Ingest content (recursive crawl + reindex)

```bash
curl -s -X POST http://localhost:8000/v1/admin/ingest \
  -H "Authorization: Bearer $BOOTSTRAP_ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com/docs","kind":"url"}'

# or seed from a sitemap
curl -s -X POST http://localhost:8000/v1/admin/ingest \
  -H "Authorization: Bearer $BOOTSTRAP_ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com/sitemap.xml","kind":"sitemap"}'
```

The worker fetches (robots-aware), extracts, chunks, embeds and indexes; it then **follows nested same-domain links** up to `CRAWLER_MAX_DEPTH`. Stale documents are re-crawled automatically (see [docs/crawler.md](docs/crawler.md)).

### MCP server

```bash
cd services/mcp
pip install -r requirements.txt

# stdio (Claude Desktop / Cursor / opencode)
SEARCHOS_API_KEY=sk_... python searchos_mcp.py --transport stdio

# remote: Streamable HTTP (/mcp) + legacy SSE (/sse), stateless per 2026-07-28 spec
SEARCHOS_API_KEY=sk_... python searchos_mcp.py --port 8099
```

Point your agent at `http://localhost:8099/mcp` (modern clients) or `/sse` (legacy clients). Tools: `search`, `extract`, `ingest`, `stats`, `health`. See [docs/mcp.md](docs/mcp.md).

## Architecture

```
┌─────────────────────────────── SearchOS Gateway (FastAPI) ───────────────────────────────┐
│  /v1/search   →  query router  →  local FTS + vector  +  SearXNG fallback  →  RRF  rank  │
│  /v1/extract  →  robots.txt check  →  render/fetch  →  trafilatura/pypdf  →  clean text   │
│  /v1/admin/ingest · /v1/admin/keys · /health · /metrics · /stats                           │
└───────────────┬───────────────────────────────┬──────────────────────────────────────────┘
                │                               │
     ┌──────────▼─────────┐           ┌──────────▼──────────┐
     │  Renderer (SPA)    │           │   Your Own Index    │
     │  headless Chromium │           │  Postgres + pgvector│
     │  renders heavy-JS  │───▶  worker ──▶ FTS + embeddings
     └────────────────────┘           └──────┬───────┬──────┘
                                             │       │
                                       crawler     recrawler
                                   (follow links) (stale pages)
                                             │
                                    ┌────────▼────────┐
                                    │  SearXNG (web)  │──▶ results saved into index
                                    └─────────────────┘
```

### Search pipeline

1. `GET /v1/search?q=...` arrives. Redis cache lookup (5 min TTL).
2. Query router fans out in parallel to `local` (FTS), `vector` (pgvector cosine), and `web` (SearXNG).
3. **SearXNG is skipped** when the local index has ≥ `SEARXNG_LOCAL_MIN_HITS` (default 3) fresh strict matches — otherwise live results are fetched.
4. Reciprocal Rank Fusion merges + **dedupes by normalized URL** (indexed copies preferred over web snippets).
5. Reranker boosts title/phrase matches, known-good domains, freshness.
6. Web results are persisted to the index, and the top-5 URLs are enqueued for snippet enrichment / full-page crawl.
7. Response includes `providers`, `degraded`, and timing.

### Failure behavior

- A failing provider degrades the response (marked in `degraded`) instead of failing the request.
- All network calls have timeouts; cache and DB are wrapped with graceful fallbacks. If the renderer is down, extraction falls back to plain HTTP fetch.
- `/health/ready` checks Postgres, Redis, and the renderer so orchestrators only route to healthy nodes.

## API reference

Full reference in [docs/api.md](docs/api.md).

### `GET /v1/search`

| Param | Type | Default | Description |
|---|---|---|---|
| `q` | string | — | search query (required) |
| `sources` | `all`\|`local`\|`vector`\|`web` | `all` | which providers to query |
| `num_results` | int 1–50 | 10 | results to return |
| `language` | string | — | e.g. `en`, `de` |
| `country` | string | — | e.g. `us` |
| `time_range` | `day`\|`week`\|`month`\|`year`\|`all` | `all` | freshness filter |
| `domains` / `exclude_domains` | list[string] | — | domain restrictions |
| `offset` | int | 0 | pagination offset |
| `cache` | bool | `true` | enable Redis result cache |

```json
{
  "query": "how do search engines work",
  "results": [
    {
      "title": "Web search engine - Wikipedia",
      "url": "https://en.wikipedia.org/wiki/Web_search_engine",
      "snippet": "A web search engine or Internet search engine is a software system...",
      "source": "local",
      "score": 0.0164,
      "rank": 1,
      "published": null,
      "domain": "en.wikipedia.org"
    }
  ],
  "total": 10,
  "took_ms": 312,
  "cache_hit": false,
  "degraded": [],
  "providers": ["local", "vector", "web"]
}
```

### `POST /v1/extract`

| Body | Type | Default | Description |
|---|---|---|---|
| `url` | string | — | page to fetch (required) |
| `format` | `markdown`\|`text`\|`json` | `markdown` | output shape |
| `max_chars` | int | — | truncate content |

Respects robots.txt, follows redirects, handles HTML (trafilatura), PDF (pypdf), and plain text. Results cached 1 hour.

### Admin + ops

- `POST /v1/admin/ingest` (admin) — enqueue a URL or sitemap for crawling/indexing.
- `POST /v1/admin/keys` / `GET /v1/admin/keys` (admin) — create/list API keys.
- `GET /health/live`, `GET /health/ready` — process / dependency health.
- `GET /metrics` — Prometheus metrics (`searchos_*`).
- `GET /stats` — documents, chunks, keys, pending jobs.

## Authentication & rate limiting

- Every request needs `Authorization: Bearer <key>` or `X-API-Key: <key>` (unless `AUTH_ENABLED=false`).
- Keys stored as SHA-256 hashes; raw key returned once at creation. Roles: `user` and `admin`.
- Redis fixed-window rate limits: per-minute / per-hour / per-day (`RATE_LIMIT_*`). `429` includes `Retry-After`.

## Configuration

Everything is environment-driven (`.env`). Key variables:

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://...@postgres:5432/searchos` | async Postgres |
| `REDIS_URL` | `redis://redis:6379/0` | Redis (cache DB 1, rate-limit DB 2) |
| `RENDERER_URL` | `http://renderer:8010` | headless Chromium for SPA/heavy-JS pages |
| `SEARXNG_URL` | — | live web fallback (optional) |
| `SEARXNG_LOCAL_MIN_HITS` | `3` | skip SearXNG when local strict matches ≥ this |
| `SEARXNG_LOCAL_TTL_SECONDS` | `3600` | local hits fresher than this skip the web |
| `CRAWLER_FOLLOW_LINKS` | `true` | follow nested links after ingest |
| `CRAWLER_MAX_DEPTH` | `2` | recursive crawl depth |
| `CRAWLER_FOLLOW_DOMAINS` | `same` | `same`\|`all`\|`none` |
| `CRAWLER_CONTINUOUS_ENABLED` | `true` | recrawl loop on |
| `CRAWLER_RECRAWL_AFTER_SECONDS` | `604800` | re-crawl pages older than this |
| `EMBEDDING_MODEL` | `BAAI/bge-small-en-v1.5` | local embeddings (384-dim) |
| `FUSION_MODE` / `RRF_K` | `rrf` / `60` | fusion algorithm |
| `RERANK_ENABLED` / `RERANK_TOP_K` | `true` / `20` | post-fusion rerank |
| `SEARCH_CACHE_TTL_SECONDS` | `300` | result cache TTL |
| `RATE_LIMIT_*` | see above | per-key quotas |
| `BOOTSTRAP_ADMIN_API_KEY` | — | admin key seeded at startup |

MCP server settings: `SEARCHOS_URL`, `SEARCHOS_API_KEY`, `SEARCHOS_ADMIN_KEY`, `MCP_HOST`, `MCP_PORT` (see [docs/mcp.md](docs/mcp.md)).

## Development

```bash
make test          # run unit tests (pytest)
make lint          # compile-check all modules
make up            # docker compose up --build
make logs          # follow all logs
make restart       # restart gateway + worker
```

Tests run against mocked HTTP (respx), an in-memory fake Redis, and real trafilatura extraction. Postgres-backed behavior (FTS, vectors) requires the compose stack.

```
tests/
  test_fusion.py          # RRF correctness + URL dedupe
  test_orchestrator.py    # merge, cache, degrade, error paths
  test_extractor.py       # HTML/text/PDF extraction
  test_rerank.py          # rerank boosting
  test_chunker.py         # chunking/token estimation
  test_ratelimit.py       # rate-limit windows + auth hash
  test_own_index.py       # URL normalization, renderer routing, embedding singleton
```

Assets are generated from `tools/`:

```bash
python tools/gen_logo.py       # docs/assets/logo*.svg
python tools/gen_badges.py     # docs/assets/badges/*.svg (lucide-style)
python tools/gen_banner.py     # docs/assets/banner.svg + .png
```

## Operations

- **Scaling**: stateless gateway → add replicas behind a load balancer; workers scale horizontally (jobs use `FOR UPDATE SKIP LOCKED` polling).
- **Migrations**: Alembic runs automatically on gateway startup. Manual: `docker compose exec gateway alembic upgrade head`.
- **Backups**: `pgdata`, `redisdata`, `promdata` volumes; `pg_dump` on postgres.
- **Monitoring**: Grafana `http://localhost:3000` (admin/admin), dashboard "SearchOS Overview". Prometheus alerts: gateway down, 5xx spikes, provider failures, p95 latency, queue backlog, rate-limit storms.
- **Logs**: structured single-line logs with request IDs (`X-Request-Id`).

## Roadmap

- Scheduled re-crawl and freshness scoring over your own index.
- Streaming responses and typed structured-output queries.
- More SERP features ported into the own index: geo/local relevance, site: operators, autocomplete.
- Per-provider retry/backoff policies and per-site crawl-rate throttling in the worker.

## License

MIT.
