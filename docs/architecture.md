# Architecture

SearchOS is a set of cooperating services that together replace a third-party search API.

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

## Components

### gateway (`services/gateway/app`)
FastAPI async service. Owns the API surface, query routing, fusion, reranking, Redis caching, auth, rate limiting, and extraction endpoints.

### worker (same image, separate process)
Consumes `ingest_jobs` from Postgres (SKIP-LOCKED polling). Three concurrent loops:

1. **ingest** — fetch → robots check → extract → chunk → embed → index, then discover nested links (recursive crawl).
2. **recrawler** — every `CRAWLER_CONTINUOUS_INTERVAL_SECONDS`, re-enqueue `indexed` documents older than `CRAWLER_RECRAWL_AFTER_SECONDS` (oldest first).
3. **snippet** — enrich the top-5 result URLs of each search with full-page content.

### renderer (`services/renderer`)
Optional Playwright/headless-Chromium service for SPA / heavy-JS pages. Falls back to plain HTTP fetch when down.

### postgres (pgvector)
`documents`, `chunks` (vector embeddings), `api_keys`, `ingest_jobs`, usage. GIN FTS index (english config) + HNSW vector index. Migrations via Alembic on gateway startup.

### redis
Search/extract result cache (DB 1), rate-limit counters (DB 2), snippet queue.

### SearXNG (optional, profile `web`)
Live web fallback. Every result it returns is **persisted into `documents`** (`source='web'`) and re-crawled, so the index learns.

## Search pipeline

1. `GET /v1/search` → Redis cache lookup (TTL `SEARCH_CACHE_TTL_SECONDS`).
2. Router fans out to providers in parallel:
   - `local` — FTS with `ts_rank_cd`, AND-then-OR fallback query builder.
   - `vector` — pgvector cosine similarity (when an embedding model is configured).
   - `web` — SearXNG, **skipped** when local strict matches ≥ `SEARXNG_LOCAL_MIN_HITS` and fresh (`SEARXNG_LOCAL_TTL_SECONDS`).
3. Reciprocal Rank Fusion merges ranked lists; **dedupes by normalized URL**, preferring indexed copies (local/vector) over live web snippets.
4. Reranker boosts title/phrase matches, known-good domains, freshness.
5. Web results are persisted; top-5 URLs enqueued for snippet enrichment.
6. Response: `providers`, `degraded`, timing, `cache_hit`.

## Failure behavior

- Providers degrade individually (listed in `degraded`) instead of failing the request.
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
