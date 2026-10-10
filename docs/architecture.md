# Architecture

SearchOS is a set of cooperating services that together replace a third-party search API.

```
┌─────────────────────────────── SearchOS Gateway (FastAPI) ───────────────────────────────┐
│  /v1/search   →  router  →  local FTS + vector + web cascade  →  RRF  →  cross-encoder  │
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
                                    │ web cascade     │──▶ results saved into index
                                    └─────────────────┘
```

## Components

### gateway (`services/gateway/app`)
FastAPI async service. Owns the API surface, query routing, fusion, reranking, Redis caching, auth, rate limiting, and extraction endpoints.

### worker (same image, separate process)
Consumes `ingest_jobs` from Postgres (SKIP-LOCKED polling). Three concurrent loops:

1. **ingest** — fetch → robots check → extract → chunk → embed → index, then discover nested links (recursive crawl).
2. **recrawler** — every `CRAWLER_CONTINUOUS_INTERVAL_SECONDS`, re-enqueue `indexed` documents older than `CRAWLER_RECRAWL_AFTER_SECONDS` (oldest first).
3. **snippet** — enrich the top `SEARCH_ENRICH_TOP_N` result URLs (default 15) of each search with full-page content.

### renderer (`services/renderer`)
Optional Playwright/headless-Chromium service for SPA / heavy-JS pages. Falls back to plain HTTP fetch when down.

### postgres (pgvector)
`documents`, `chunks` (vector embeddings), `api_keys`, `ingest_jobs`, usage. GIN FTS index (english config) + HNSW vector index. Migrations via Alembic on gateway startup.

### redis
Search/extract result cache (DB 1), rate-limit counters (DB 2), snippet queue.

### Web providers (SearXNG optional, profile `web`)
Live web fallback as a **cascade**: SearXNG → Tavily → Exa → Jina. The first tier that returns results wins; a tier answering with zero results counts as a miss and the next is tried, since an empty SERP usually means rate limiting. When every tier fails the cascade opens a cooldown so a request does not pay each dead upstream's timeout.

Tavily works with no account via `X-Tavily-Access-Mode: keyless`. Exa is reached through its hosted MCP server (`mcp.exa.ai`) because its REST API returns 402 without a funded key. Jina requires a key. Every result is **persisted into `documents`** (`source='web'`) and re-crawled, so the index learns regardless of which tier served it.

## Search pipeline

1. `GET /v1/search` → Redis cache lookup (TTL `SEARCH_CACHE_TTL_SECONDS`).
2. Router fans out to providers in parallel:
   - `local` — FTS with `ts_rank_cd`, AND-then-OR fallback query builder.
   - `vector` — pgvector cosine similarity (when an embedding model is configured).
   - `web` — the cascade, **skipped** when local strict matches ≥ `SEARXNG_LOCAL_MIN_HITS` and fresh (`SEARXNG_LOCAL_TTL_SECONDS`).
   - `searxng` / `tavily` / `exa` / `jina` — pin one tier; errors instead of falling back.
3. Reciprocal Rank Fusion merges ranked lists; **dedupes by normalized URL**, preferring indexed copies (local/vector) over live web snippets.
4. A **cross-encoder** (`RERANK_MODEL`) rescores the top `RERANK_CANDIDATE_POOL` candidates. RRF yields a rank position, not a relevance measure — without this step good and nonsense queries returned near-identical scores. Title/freshness/domain boosts remain as the fallback when no cross-encoder is installed.
5. Results below `RERANK_MIN_SCORE` are dropped; a best score under `RERANK_LOW_CONFIDENCE_SCORE` sets `low_confidence`.
6. Web results are persisted; top `SEARCH_ENRICH_TOP_N` URLs enqueued for snippet enrichment.
7. Response: `providers`, `degraded`, `top_score`, `low_confidence`, timing, `cache_hit`.

## Failure behavior

- Providers degrade individually (listed in `degraded`) instead of failing the request.
- The web cascade degrades per tier: a dead SearXNG is transparent because the next tier answers.
- Missing embeddings degrade loudly: `/stats` reports `vector_search_available` and
  `embedding_coverage`, and the worker logs why. A `backfill` loop fills gaps in the background.
- Network calls have timeouts; cache/DB wrapped with graceful fallbacks.
- `/health/ready` gates traffic on Postgres, Redis, and renderer health.

## Data flow for ingest

```
POST /v1/admin/ingest {url}
        │
        ▼
  ingest_jobs (pending)
        │  worker claims (SKIP LOCKED)
        ▼
  fetch_page → robots.txt allowed?
        │
        ▼
  extract_page (trafilatura / pypdf / renderer)
        │
        ▼
  chunk_text → embed (bge-small-en-v1.5, local)
        │
        ▼
  upsert document + chunks → status=indexed
        │
        ▼
  _discover_and_enqueue: extract <a href>, dedupe against
  indexed docs + active jobs, enqueue depth+1 (same-domain policy)
```

## MCP server

`services/mcp/searchos_mcp.py` is a thin wrapper over the gateway API exposing 5 tools (`search`, `extract`, `ingest`, `stats`, `health`) over all transports. See [MCP server](mcp.md).
