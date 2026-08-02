# SearchOS

A production-grade, fully self-hosted search API and web index built on open-source components. No third-party search vendors, no per-query fees, no rate-limit ceilings, no data leaving your infra. This is the "Apple approach" applied to search: instead of picking one vendor, it studies what makes each search API good — Exa's semantic neural index, Tavily's clean LLM-ready content, SerpApi's rich SERP parameters, Jina's JS-rendered reading — then rebuilds those features yourself so you get the strengths without the vendor lock-in:

```
┌─────────────────────────────── SearchOS Gateway (FastAPI) ───────────────────────────────┐
│  /v1/search   →  query router  →  fan-out to own-index providers  →  RRF fusion  →  rank  │
│  /v1/extract  →  robots.txt check  →  render/fetch  →  trafilatura/pypdf  →  clean text   │
│  /v1/admin/ingest · /v1/admin/keys · /health · /metrics · /stats                          │
└───────────────────────────────┬───────────────────────────────┬──────────────────────────┘
                                │                               │
                     ┌──────────▼─────────┐           ┌──────────▼──────────┐
                     │   Renderer (SPA)   │           │   Your Own Index    │
                     │  headless Chromium │           │  Postgres + pgvector│
                     │  renders heavy-JS  │───▶  crawler/index ──▶  FTS + embeddings
                     └────────────────────┘           └──────────┬──────────┘
                                                                  │  ingest worker
                                                          ┌───────▼───────┐
                                                          │  Crawler/PDF  │
                                                          │  extractor    │
                                                          └───────────────┘
```

## Why this exists

Search APIs like SerpApi, Tavily, Exa, and Jina each have real strengths (Google-grade relevance, LLM-ready output, semantic search, free content grounding). But they share two structural problems: **cost scales linearly with usage** and **you are limited by someone else's quotas and ToS**. This project keeps the ideas and removes the problems by self-hosting every layer:

| Layer | SerpApi | Tavily | Exa | Jina | SearchOS |
|---|---|---|---|---|---|
| Cost | $25+/mo, per-search | credits, per-search | per-search | tokens | **free, self-hosted** |
| Rate limits | hourly caps | credits | plan caps | RPM/TPM | **your own quotas** |
| Privacy | vendor sees queries | vendor sees queries | vendor sees queries | vendor sees queries | **your infra only** |
| Result source | Google | own index | neural index | own index | **your own crawler + index** |
| LLM-ready output | no | yes | yes | yes | **yes (clean text/markdown)** |
| Content extraction | no | yes | yes | yes | **yes (robots-respecting)** |
| Geo/local | yes | no | no | no | **via your own index + SERP params** |
| Own your data | no | no | no | no | **yes (full index export)** |

## Architecture

### Components

- **gateway** (`services/gateway/app`) — FastAPI async service. Query routing, RRF fusion, reranking, caching, auth, rate limiting, extraction.
- **worker** — separate process from the same image. Consumes `ingest_jobs`, crawls URLs/sitemaps, renders and chunks content, embeds, and indexes it into your own search index. Also enriches result snippets in the background.
- **renderer** (`services/renderer`) — optional headless-Chromium (Playwright) service. Renders SPAs and heavy-JS pages to real DOM so the extractor gets clean, complete content.
- **postgres (pgvector)** — documents, chunks (with vector embeddings), API keys, ingest jobs, usage logs. GIN FTS index + HNSW vector index.
- **redis** — search/extract result cache, rate-limit counters, snippet queue.
- **prometheus + grafana** — metrics, alerts, dashboards.

### Search pipeline

1. `GET /v1/search?q=...` arrives.
2. Cache lookup (Redis, 5 min TTL). Hit → instant response.
3. Query router fans out to your own-index providers in parallel:
   - `local` — your Postgres index (FTS, `ts_rank_cd`).
   - `vector` — your Postgres index via pgvector cosine similarity (enabled when `sentence-transformers` model is present).
4. Reciprocal Rank Fusion merges per-provider ranked lists (dedupe by URL).
5. Reranker boosts title/phrase matches, known-good domains, freshness.
6. Response includes `providers`, `degraded` (which providers failed), and timing.
7. Top-5 result URLs are enqueued for snippet enrichment so future searches return richer snippets from your own cache.

### Failure behavior

- A failing provider degrades the response (marked in `degraded`) instead of failing the request — a failed FTS query still returns vector results, and vice versa.
- All network calls have timeouts; cache and DB are wrapped with graceful fallbacks. If the headless renderer is down, extraction falls back to a plain HTTP fetch.
- `/health/ready` checks Postgres, Redis, and the renderer (when configured) so orchestrators only route traffic to a healthy node.

## Quick start

Prerequisites: Docker + Docker Compose.

```bash
cp .env.example .env

# generate a real admin key
python3 -c "import secrets; print('sk_' + secrets.token_urlsafe(32))"

# paste it into .env as BOOTSTRAP_ADMIN_API_KEY=sk_...
docker compose up -d --build
```

That builds the gateway image (with Alembic migrations auto-run on startup), starts the renderer, Postgres+pgvector, Redis, Prometheus, and Grafana. The gateway is at `http://localhost:8000`.

### Create an API key

The admin key from `.env` is bootstrapped on first start. Use it to create user keys:

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

### Extract a URL to clean content

```bash
curl -s -X POST http://localhost:8000/v1/extract \
  -H "Authorization: Bearer sk_..." \
  -H "Content-Type: application/json" \
  -d '{"url":"https://en.wikipedia.org/wiki/Web_search_engine"}'
```

### Ingest content into your private index

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

The worker picks these up, fetches (respecting robots.txt), extracts with trafilatura (or pypdf for PDFs), chunks, embeds, and indexes. Once indexed, your private FTS + vector results merge into every `/v1/search`.

## API reference

### `GET /v1/search`

| Param | Type | Default | Description |
|---|---|---|---|
| `q` | string | — | search query (required) |
| `sources` | `all`\|`local`\|`vector` | `all` | which providers to query |
| `num_results` | int 1–50 | 10 | results to return |
| `language` | string | — | e.g. `en`, `de` |
| `country` | string | — | e.g. `us` |
| `time_range` | `day`\|`week`\|`month`\|`year`\|`all` | `all` | freshness filter |
| `domains` | list[string] | — | restrict to domains |
| `exclude_domains` | list[string] | — | exclude domains |
| `offset` | int | 0 | pagination offset |
| `cache` | bool | `true` | enable Redis result cache |

Response:

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
      "domain": "example.com",
      "favicon": "..."
    }
  ],
  "total": 10,
  "took_ms": 312,
  "cache_hit": false,
  "degraded": [],
  "providers": ["local", "vector"]
}
```

### `POST /v1/extract`

| Body | Type | Default | Description |
|---|---|---|---|
| `url` | string | — | page to fetch (required) |
| `format` | `markdown`\|`text`\|`json` | `markdown` | output shape |
| `max_chars` | int | — | truncate content |

Respects robots.txt (configurable), follows redirects, handles HTML (trafilatura), PDF (pypdf), and plain text. Results cached for 1 hour.

### `POST /v1/admin/ingest` (admin)
Enqueue a URL or sitemap for crawling/indexing.

### `POST /v1/admin/keys` (admin)
Create user/admin API keys. `GET /v1/admin/keys` lists them (never returns raw keys).

### Ops
- `GET /health/live` — process alive.
- `GET /health/ready` — Postgres/Redis/renderer health for orchestrators.
- `GET /metrics` — Prometheus metrics (`searchos_*`).
- `GET /stats` — documents, chunks, keys, pending jobs.

## Authentication & rate limiting

- Every request needs `Authorization: Bearer <key>` or `X-API-Key: <key>` (unless `AUTH_ENABLED=false`).
- Keys are stored as SHA-256 hashes; the raw key is returned exactly once at creation.
- Roles: `user` (search/extract) and `admin` (ingest, key management).
- Rate limits per key, enforced with Redis fixed-window counters:
  - per minute (`RATE_LIMIT_PER_MINUTE`, default 60)
  - per hour (`RATE_LIMIT_PER_HOUR`, default 600)
  - per day (`RATE_LIMIT_PER_DAY`, default 5000)
- `429` responses include `Retry-After`. The bootstrap admin key comes from `BOOTSTRAP_ADMIN_API_KEY` in `.env`.

## Configuration

Everything is environment-driven (`.env`). Key variables:

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://...@postgres:5432/searchos` | async Postgres |
| `REDIS_URL` | `redis://redis:6379/0` | Redis (cache DB 1, rate-limit DB 2) |
| `RENDERER_URL` | `http://renderer:8010` | headless Chromium for SPA/heavy-JS pages |
| `LOCAL_INDEX_ENABLED` | `true` | include your own index in search |
| `EMBEDDING_MODEL` | `BAAI/bge-small-en-v1.5` | local embeddings (384-dim) |
| `FUSION_MODE` | `rrf` | fusion algorithm |
| `RRF_K` | `60` | RRF smoothing constant |
| `RERANK_ENABLED` / `RERANK_TOP_K` | `true` / `20` | post-fusion rerank |
| `SEARCH_CACHE_TTL_SECONDS` | `300` | result cache TTL |
| `EXTRACT_CACHE_TTL_SECONDS` | `3600` | extract cache TTL |
| `FETCH_RESPECT_ROBOTS` | `true` | honor robots.txt |
| `EXTRACT_MAX_BYTES` | `5242880` | page size cap |
| `RATE_LIMIT_*` | see above | per-key quotas |
| `BOOTSTRAP_ADMIN_API_KEY` | — | admin key seeded at startup |
| `WORKER_*` | see above | worker batching/polling |

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
  test_fusion.py          # RRF correctness
  test_orchestrator.py    # merge, cache, degrade, error paths
  test_extractor.py       # HTML/text/PDF extraction
  test_rerank.py          # rerank boosting
  test_chunker.py         # chunking/token estimation
  test_ratelimit.py       # rate-limit windows + auth hash
  test_own_index.py       # URL normalization, renderer routing, embedding singleton
```

Postgres-backed behavior (FTS, vectors) requires the compose stack.

## Operations

- **Scaling**: stateless gateway → add replicas behind a load balancer; worker is horizontally scalable too (jobs use `SELECT ... FOR UPDATE SKIP LOCKED` semantics via status polling).
- **Migrations**: Alembic runs automatically on gateway startup (`alembic upgrade head`). Manual: `docker compose exec gateway alembic upgrade head`.
- **Backups**: `pgdata`, `redisdata`, `promdata` volumes. Use `pg_dump` on the postgres volume.
- **Monitoring**: Grafana `http://localhost:3000` (admin/admin), dashboard "SearchOS Overview". Prometheus alerts cover gateway down, 5xx spikes, provider failures, p95 latency, queue backlog, rate-limit rejection storms.
- **Logs**: structured single-line logs with request IDs propagated via `X-Request-Id`.

## Roadmap

- Scheduled re-crawl and freshness scoring over your own index.
- Streaming responses and typed structured-output queries.
- More SERP features ported into the own index: geo/local relevance, site: operators, autocomplete.
- Per-provider retry/backoff policies and per-site crawl-rate throttling in the worker.

## License

MIT.
