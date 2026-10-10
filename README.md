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
  <a href="docs/assets/badges/ci.svg"><img src="docs/assets/badges/ci.svg" alt="Tests 257 passed"/></a>
  <a href="docs/assets/badges/license.svg"><img src="docs/assets/badges/license.svg" alt="MIT"/></a>
</p>

<p align="center">
  <a href="https://searos.devforge.qzz.io"><b>Landing page</b></a> ·
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

SearchOS builds a production-grade, fully self-hosted search API from open-source components. This is the "Combined approach" applied to search: instead of picking one vendor, it studies what makes each search API good — Exa's semantic neural index, Tavily's clean LLM-ready content, SerpApi's rich SERP parameters, Jina's JS-rendered reading — then rebuilds those features yourself so you get the strengths without the vendor lock-in:

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
- **Cross-encoder reranking** — candidates are rescored by a real relevance model (`cross-encoder/ms-marco-MiniLM-L6-v2`), so good and bad matches no longer return near-identical scores. Results below a threshold are dropped and weak result sets are flagged `low_confidence`.
- **Web fallback cascade** — SearXNG is primary; when it fails, is rate-limited, or returns an empty SERP, the request falls through to **Tavily** (free, no account needed), **Exa** (via its hosted MCP server), then **Jina**. The first tier that answers wins, and an exhausted chain opens a cooldown so a request never pays every dead upstream's timeout.
- **Live web fallback that learns** — when local coverage is thin, SearXNG results are merged in, **saved into your index, and re-crawled**, so repeat queries are served instantly without re-hitting the web.
- **Continuous recursive crawler** — follows nested same-domain links (depth-limited), plus automatic **re-crawl of stale pages** so your index stays fresh.
- **Embedding backfill** — enabling embeddings on an existing corpus drains missing vectors in the background, so documents indexed while the embedder was unavailable are not stranded without semantic search.
- **LLM-ready extraction** — clean markdown/text/json from HTML, PDF, and JS-rendered pages (headless renderer), robots.txt-aware, with an SSRF guard that resolves each host before connecting.
- **MCP server** — expose search, extract, ingest, stats and health to any AI agent over **stdio, legacy HTTP+SSE, and the new stateless Streamable HTTP** (2026-07-28 spec). The HTTP transport is bearer-token protected; stdio is a local process.
- **Production ops** — Redis cache + rate limits, auth (hashed API keys), Prometheus/Grafana metrics, Alembic migrations, and network segmentation so data services have no route off the host.

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

Or run it as a container alongside the stack:

```bash
echo "MCP_HTTP_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')" >> .env
docker compose --profile mcp up -d
curl -s localhost:8099/healthz   # {"status":"ok","service":"searchos-mcp"}
```

`/mcp`, `/sse` and `/messages/` expose the full tool set including `ingest`, so the HTTP transports require `MCP_HTTP_TOKEN`. The server refuses to bind a non-loopback address without one. stdio is unaffected — it is a local process, not a socket.

Tools: `search`, `extract`, `ingest`, `stats`, `health`.

**Claude Code** speaks Streamable HTTP natively:

```bash
claude mcp add --scope user --transport http searchos \
  http://127.0.0.1:8099/mcp --header "Authorization: Bearer $MCP_HTTP_TOKEN"
claude mcp list    # shows "✔ Connected"
```

**Claude Desktop** accepts only the stdio shape and **deletes** entries it cannot parse, so back up its config before editing. It cannot launch `npx` (GUI apps get a minimal `PATH`, and npx's `#!/usr/bin/env node` shebang fails). Reaching `/mcp` from Desktop means bridging with a globally installed `mcp-remote` invoked through an absolute `node` path — both are spelled out, along with the failure modes, in [docs/mcp.md](docs/mcp.md).

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

1. `GET /v1/search?q=...` arrives. Redis cache lookup — 5 min TTL, or 30s for an empty/low-confidence response so a transient miss does not pin itself.
2. Query router fans out in parallel to `local` (FTS), `vector` (pgvector cosine), and `web` (the fallback cascade).
3. **SearXNG is skipped** when the local index has ≥ `SEARXNG_LOCAL_MIN_HITS` (default 3) fresh strict matches — otherwise live results are fetched.
4. Reciprocal Rank Fusion merges + **dedupes by normalized URL** (indexed copies preferred over web snippets).
5. **A cross-encoder rescores the fused candidates.** The previous reranker only multiplied the fused score by title/freshness boosts, so every candidate kept the same magnitude and nothing separated a good match from noise. Scores are now relevance logits — see *Reranking* below.
6. Results below `RERANK_MIN_SCORE` are dropped. If the best survivor is under `RERANK_LOW_CONFIDENCE_SCORE`, the response is flagged `low_confidence` so a caller can distinguish "the index does not cover this" from "here is the best of a weak set".
7. Web results are persisted to the index, and the top `SEARCH_ENRICH_TOP_N` URLs (default 15) are enqueued for snippet enrichment / full-page crawl.
8. Response includes `providers`, `degraded`, `top_score`, `low_confidence`, and timing.

> **Note on caching.** The search/extract result cache is **Redis**-backed (DB 1;
> rate-limit counters use DB 2, and the snippet queue shares DB 1), so it is
> shared across uvicorn workers rather than per-process. It is invalidated by
> pattern after every successful ingest, and can be flushed on demand:
>
> ```bash
> curl -X POST localhost:8000/v1/admin/cache/flush -H "Authorization: Bearer $ADMIN_KEY"
> # {"removed": 12, "cache_ttl_seconds": 300}
> ```
>
> Empty and low-confidence responses use the shorter
> `SEARCH_NEGATIVE_CACHE_TTL_SECONDS`, so a transient miss does not pin itself
> for the full TTL.

### Reranking and relevance thresholds

The fused reciprocal-rank score is a rank position, not a relevance measure. Left alone, a nonsense query and a precise one returned rows scored ~0.016 and ~0.017 — visually identical.

`cross-encoder/ms-marco-MiniLM-L6-v2` reads the query and document together and emits a relevance score. Measured on this codebase's corpus:

| Query class | Score range |
|---|---|
| relevant (`CAGRA GPU graph index`, `PostgreSQL autovacuum tuning`) | 4.94 – 9.34 |
| nonsense (`zxqvbnm flurgle obsidian kettle`) | ≤ 1.16 |

**Scores are unbounded logits, not probabilities.** A sigmoid was tried and rejected: it collapses that gap to 0.995–1.0 versus 0.76, far too narrow to cut on. `RERANK_MIN_SCORE` (3.0) and `RERANK_LOW_CONFIDENCE_SCORE` (4.5) sit in the measured gap and must be retuned if `RERANK_MODEL` changes.

If the cross-encoder cannot load, the heuristic reranker is used instead and **the thresholds are skipped** — heuristic scores cannot support one. Embedding and rerank models are loaded during gateway startup, so the first query does not pay for a cold load.

### Web fallback cascade

`sources=web` is a cascade, not SearXNG alone:

| Tier | Credential needed | Notes |
|---|---|---|
| SearXNG | — | primary |
| Tavily | **none** | `X-Tavily-Access-Mode: keyless`, identical response schema |
| Exa | **none** | hosted MCP at `mcp.exa.ai`; its REST API returns 402 without a funded key |
| Jina | key | `s.jina.ai` returns 401 without one |

A tier that answers with **zero results** does not stop the chain — it counts as a miss, because an empty SERP usually means an upstream rate limit rather than genuine absence of results. When every tier fails the chain opens a cooldown and returns immediately; one success clears it.

Each tier is also addressable on its own (`sources=searxng|tavily|exa|jina`), which errors rather than silently falling back, so an empty result means that upstream genuinely had nothing. See [docs/searxng.md](docs/searxng.md).

### Failure behavior

- A failing provider degrades the response (marked in `degraded`) instead of failing the request.
- **The web cascade degrades per tier.** A dead SearXNG is transparent: the next tier answers, and `degraded` names what failed.
- All network calls have timeouts; cache and DB are wrapped with graceful fallbacks. If the renderer is down, extraction falls back to plain HTTP fetch.
- `/health/ready` checks Postgres, Redis, and the renderer so orchestrators only route to healthy nodes.
- **Embeddings degrade loudly, not silently.** If the provider is unavailable, documents are still written with `status='indexed'` and no vectors, `/stats` reports `vector_search_available: false` and `embedding_coverage: 0.0`, and the worker logs why. Search falls back to keyword-only rather than failing.

## API reference

Full reference in [docs/api.md](docs/api.md).

### `GET /v1/search`

| Param | Type | Default | Description |
|---|---|---|---|
| `q` | string | — | search query (required) |
| `sources` | `all`\|`local`\|`vector`\|`web`\|`searxng`\|`tavily`\|`exa`\|`jina` | `all` | which providers to query; `web` is the whole cascade |
| `num_results` | int 1–50 | 10 | results to return |
| `language` | string | — | e.g. `en`, `de` |
| `country` | string | — | e.g. `us` |
| `time_range` | `day`\|`week`\|`month`\|`year`\|`all` | `all` | freshness filter |
| `domains` / `exclude_domains` | list[string] | — | domain restrictions |
| `offset` | int | 0 | pagination offset |
| `cache` | bool | `true` | enable the Redis result cache |

```json
{
  "query": "PostgreSQL autovacuum tuning",
  "results": [
    {
      "title": "PostgreSQL Autovacuum Tuning: 10 Key Parameters",
      "url": "https://example.com/postgres-autovacuum",
      "snippet": "Autovacuum is the single most common source of bloat...",
      "source": "web",
      "score": 9.34,
      "rank": 1,
      "published": null,
      "domain": "example.com"
    }
  ],
  "total": 15,
  "returned": 10,
  "has_more": true,
  "offset": 0,
  "took_ms": 190,
  "cache_hit": false,
  "degraded": [],
  "providers": ["local", "vector", "web"],
  "top_score": 9.34,
  "low_confidence": false,
  "min_score": 3.0
}
```

| Field | Meaning |
|---|---|
| `total` | matches surviving the threshold, before pagination |
| `returned` / `has_more` / `offset` | disambiguates "no matches" from "you paged past the end" |
| `top_score` | best relevance score in this page; `null` when nothing cleared the threshold |
| `low_confidence` | `true` when no result cleared `RERANK_LOW_CONFIDENCE_SCORE` — treat as a weak match, not an answer |
| `min_score` | the cutoff actually applied, so it is never a mystery why a result is missing |
| `degraded` | providers or cascade tiers that errored |

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
| `SEARXNG_URL` | — | live web fallback (optional; primary tier of the cascade) |
| `SEARXNG_LOCAL_MIN_HITS` | `3` | skip SearXNG when local strict matches ≥ this |
| `SEARXNG_LOCAL_TTL_SECONDS` | `3600` | local hits fresher than this skip the web |
| `TAVILY_API_KEY` / `TAVILY_KEYLESS` | — / `true` | cascade tier 2. Keyless needs no account; a key takes precedence |
| `EXA_API_KEY` / `EXA_MCP_ENABLED` | — / `true` | cascade tier 3 over hosted MCP. REST (`402`) needs a funded key |
| `JINA_API_KEY` | — | cascade tier 4; `s.jina.ai` returns 401 without one |
| `SEARCH_CASCADE_COOLDOWN_SECONDS` | `30` | pause live attempts after every tier fails |
| `CRAWLER_FOLLOW_LINKS` | `true` | follow nested links after ingest |
| `CRAWLER_MAX_DEPTH` | `2` | recursive crawl depth |
| `CRAWLER_FOLLOW_DOMAINS` | `same` | `same`\|`all`\|`none` |
| `CRAWLER_CONTINUOUS_ENABLED` | `true` | recrawl loop on |
| `CRAWLER_RECRAWL_AFTER_SECONDS` | `604800` | re-crawl pages older than this |
| `EMBEDDING_MODEL` | `BAAI/bge-small-en-v1.5` | local embeddings (384-dim) |
| `INSTALL_EMBEDDINGS` | `1` | **build arg.** Bakes torch into the gateway *and* worker images |
| `BACKFILL_ENABLED` | `true` | background loop fills missing embeddings |
| `BACKFILL_BATCH_SIZE` | `50` | documents per pass; drains continuously while work remains |
| `FUSION_MODE` / `RRF_K` | `rrf` / `60` | fusion algorithm |
| `RERANK_ENABLED` / `RERANK_TOP_K` | `true` / `20` | post-fusion rerank |
| `RERANK_MODEL` | `cross-encoder/ms-marco-MiniLM-L6-v2` | cross-encoder; empty disables semantic reranking |
| `RERANK_MIN_SCORE` | `3.0` | drop results below this **logit** (see *Reranking*) |
| `RERANK_LOW_CONFIDENCE_SCORE` | `4.5` | below this the response is flagged `low_confidence` |
| `RERANK_CANDIDATE_POOL` | `30` | candidates scored; the tail cannot reach the page |
| `SEARCH_ENRICH_TOP_N` | `15` | web results queued for full-text enrichment |
| `SEARCH_CACHE_TTL_SECONDS` | `300` | result cache TTL |
| `SEARCH_NEGATIVE_CACHE_TTL_SECONDS` | `30` | shorter TTL for empty/low-confidence answers |
| `RATE_LIMIT_*` | see above | per-key quotas |
| `BOOTSTRAP_ADMIN_API_KEY` | — | admin key seeded at startup |

MCP server settings: `SEARCHOS_URL`, `SEARCHOS_API_KEY`, `SEARCHOS_ADMIN_KEY`, `MCP_HOST`, `MCP_PORT`, `MCP_HTTP_TOKEN` (see [docs/mcp.md](docs/mcp.md)).

### Enabling embeddings

Vector search needs the embedder in **both** images. The gateway embeds the *query*; the worker embeds documents. Passing the build arg to only the worker lets the corpus gain vectors with no way to compare a query against them — the index looks healthy and `vector_search_available` stays `false` forever.

```bash
docker compose build && docker compose up -d
curl -s localhost:8000/stats | grep -E 'vector_search_available|embedding_coverage'
```

After enabling it on an existing corpus, the backfill loop drains missing vectors in the background. Watch it with:

```bash
docker compose logs -f worker | grep backfill
```

## Development

```bash
make test          # run unit tests (pytest)
make lint          # ruff check + format --check
make config        # validate compose, prometheus config/rules, dashboard JSON
make up            # docker compose up --build
make logs          # follow all logs
make restart       # restart gateway + worker
```

The unit suite runs with no infrastructure (fakes for Redis and HTTP, real
trafilatura extraction). Two further suites exercise the parts that only make
sense against real services — the FTS and pgvector SQL, and Redis key
semantics — and skip themselves unless you point them somewhere:

```
tests/
  test_fusion.py               # RRF correctness + URL dedupe
  test_orchestrator.py         # merge, cache, pagination, degrade, error paths
  test_extractor.py            # HTML/text/PDF extraction + content-type dispatch
  test_rerank.py               # rerank boosting
  test_chunker.py              # chunking + token budget enforcement
  test_ratelimit.py            # rate-limit windows + auth hash
  test_own_index.py            # URL normalization, renderer routing, embedding singleton
  test_security.py             # SSRF egress policy, CORS, settings validation, auth throttling
  test_robots.py               # RFC 9309 parsing: wildcards, UA groups, caching
  test_api_http.py             # HTTP contract: query-param binding, error envelope, 413/422
  test_mcp.py                  # MCP tool contracts, param handling, transport auth
  test_integration_db.py       # real Postgres: FTS/vector SQL, dedupe, upserts  [TEST_DATABASE_URL]
  test_integration_redis.py    # real Redis: INCR+EXPIRE atomicity, cache keys     [TEST_REDIS_URL]
  test_mcp_e2e.py              # real MCP client over stdio / SSE / Streamable HTTP  [TEST_MCP_*]
```

```bash
docker run -d --name searchos-test-pg -p 15432:5432 \
  -e POSTGRES_USER=searchos -e POSTGRES_PASSWORD=searchos -e POSTGRES_DB=searchos \
  pgvector/pgvector:pg16
docker run -d --name searchos-redis -p 16380:6379 redis:7-alpine
export TEST_DATABASE_URL=postgresql+asyncpg://searchos:searchos@127.0.0.1:15432/searchos
export TEST_REDIS_URL=redis://127.0.0.1:16380/0
make test
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
