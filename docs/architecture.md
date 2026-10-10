# Architecture

SearchOS is a set of cooperating services that together replace a third-party search API.

```
┌─────────────────────────────── SearchOS Gateway (FastAPI) ───────────────────────────────┐
│  /v1/search   →  router  →  local FTS + vector + web cascade  →  RRF  →  cross-encoder  │
│  /v1/extract  →  robots.txt check  →  render→GET→proxify → trafilatura/pypdf → clean text │
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
3. **snippet** — enrich the top `SEARCH_ENRICH_TOP_N` result URLs (default 15) of each search with full-page content. Enrichment goes through the same `index_document()` path as ingest (language, tsvector, SimHash, chunks, vectors) and clears the snippet flag. It used to replace `content_text` only, leaving chunks and vectors describing the 160-char snippet, and the next SearXNG save of that URL reverted it to the snippet.
4. **backfill** — chunks and embeds documents that have none, fills missing vectors, and re-indexes any document still flagged as a snippet whose content is longer than a snippet (the damage the old enrichment left). Documents with empty content are skipped so they cannot stall a batch.

All page fetches (ingest, recrawl, enrichment) use the same fetcher as `/v1/extract`, including its Proxify fallback when configured.

### renderer (`services/renderer`)
Optional Playwright/headless-Chromium service for SPA / heavy-JS pages. Falls back to plain HTTP fetch when down.

### Proxify (optional, separate project)
[Proxify](https://github.com/Mr-DS-ML-85/proxify) runs as its own compose project; `docker-compose.proxify.yml` joins the gateway and worker to its network. SearchOS uses it for three things: Google web results (a cascade tier), Google Images, and as the last-resort page fetcher when the renderer and a direct GET both fail with a retryable error. Every request opts out of Proxify's captcha solver. See [SearXNG fallback → Google through Proxify](searxng.md#google-through-proxify).

### postgres (pgvector)
`documents`, `chunks` (vector embeddings), `api_keys`, `ingest_jobs`, usage. GIN FTS index (english config) + HNSW vector index. Migrations via Alembic on gateway startup.

Two tables back the newer retrieval subsystems:

| Table | Purpose | Notes |
|---|---|---|
| `images` | one CLIP-embedded image per URL | `vector(512)` plus a 64-bit `phash` for dedupe. `document_id` is `ON DELETE SET NULL` — an image outlives the page it was found on, since the image URL is the durable identifier. The hash is stored signed: Postgres `BIGINT` is signed, so an unsigned 64-bit value with the top bit set is rejected as out of range. |
| `chunk_tokens` | one ColBERT vector **per token** | `vector(128)`, primary key `(chunk_id, position)`. 256 tokens/chunk ≈ 128 KB; measured at 392 MB for the whole corpus, against 10.9 GB for unprojected ColBERT. |

`documents` also carries a 64-bit `simhash` for near-duplicate collapsing at query time, and a per-language tsvector (`lang`) so a German document is not stemmed by English rules.

### Model weights
CLIP (~600 MB), the cross-encoder and the text embedder are cached on the `modelcache` volume shared by gateway and worker. Without it every rebuild re-pulls ~800 MB and the first query after a restart pays ~310 s; with it, ~1.5 s.

ColBERT is warmed at startup only when `COLBERT_ENABLED=true`; otherwise it loads on first use, and a per-query `colbert=true` pays that load once per uvicorn worker (measured: 3.9s first query, 0.45s after). CLIP is never *downloaded* at startup either, but it is **warmed** at startup when its weights are already in the cache: lazy loading made the first image query on each uvicorn worker pay 4–7s (15s for image-to-image).

### redis
Search/extract result cache (DB 1), rate-limit counters (DB 2), snippet queue.

### Web providers (SearXNG optional, profile `web`)
Live web fallback as a **cascade**: SearXNG → Google (with Proxify) → Tavily → Exa → Jina. The first tier that returns results wins; a tier answering with zero results counts as a miss and the next is tried, since an empty SERP usually means rate limiting. When every tier fails the cascade opens a cooldown so a request does not pay each dead upstream's timeout.

Tavily works with no account via `X-Tavily-Access-Mode: keyless`. Exa is reached through its hosted MCP server (`mcp.exa.ai`) because its REST API returns 402 without a funded key. Jina requires a key. Results from **every** web tier are saved into `documents` immediately as snippets (`source='web'`, `meta.provider` names the tier), deduplicated by normalized URL and never over a crawled page; the top `SEARCH_ENRICH_TOP_N` result URLs are then enriched into full documents. See [Result persistence](searxng.md#result-persistence).

### federated peers (`FEDERATED_ENDPOINTS`)
Other SearchOS instances queried alongside the local one. Results carry `source: federated:<peer>` and are merged with RRF, so a URL several independent indexes return outranks one only a single index found. By default peers are asked for their own index; `FEDERATED_PEER_SOURCES=all` also has them search the web on every query. Separately, a `federated_web` tier at the end of the web cascade has peers run the web search when this instance's own web tiers are rate-limited, so a group of self-hosters can pool web quota. Requests between peers carry `X-SearchOS-Federated-Hop`, and a gateway receiving one does not federate again, so a query cannot bounce between peers.

Peers are ordinary remote gateways: **their URLs resolve from inside this gateway's container**, so `127.0.0.1:8001` refers to the gateway itself rather than the host. A peer that errors is recorded in `degraded` and then skipped without a live call for 30 s, so one unreachable peer cannot add its timeout to every query.

Peer results are only useful if their title and snippet survive the trip — the local cross-encoder scores text, so a bare URL is unscoreable and gets dropped below `RERANK_MIN_SCORE`.

## Search pipeline

1. `GET /v1/search` → Redis cache lookup (TTL `SEARCH_CACHE_TTL_SECONDS`).
2. Router fans out to providers in parallel:
   - `local` — FTS with `ts_rank_cd`, AND-then-OR fallback query builder.
   - `vector` — pgvector cosine similarity (when an embedding model is configured), one row per document by its best chunk, ranked by similarity. It used to keep the first N documents *by UUID*, and to ignore the domain, time and language filters.

   Domain, time and language filters are applied by every provider. `language` matches the detected document language (`fr` → `french`); `country` only refines the web locale.
   - `web` — the cascade, **skipped** when local strict matches ≥ `SEARXNG_LOCAL_MIN_HITS` and fresh (`SEARXNG_LOCAL_TTL_SECONDS`).
   - `searxng` / `google` / `tavily` / `exa` / `jina` — pin one tier; errors instead of falling back.
   - `federated` — configured peers' indexes (or their whole pipeline with `FEDERATED_PEER_SOURCES=all`), merged with RRF. Excluded on a request that came from a peer.
3. Reciprocal Rank Fusion merges ranked lists; **dedupes by normalized URL**, preferring indexed copies (local/vector) over live web snippets.
4. A **cross-encoder** (`RERANK_MODEL`) rescores the top `RERANK_CANDIDATE_POOL` candidates. RRF yields a rank position, not a relevance measure — without this step good and nonsense queries returned near-identical scores. Title/freshness/domain boosts remain as the fallback when no cross-encoder is installed.
5. If `COLBERT_ENABLED`, the top `COLBERT_TOP_N` are **rescored by late interaction (MaxSim)** against their best-matching chunk's token vectors and blended into the score at `COLBERT_WEIGHT`. It runs *after* the cross-encoder rather than instead of it, and the final sort uses the blended value so `score` decreases with `rank`. Candidates with no token vectors are kept in their existing order rather than dropped — a missing vector is an indexing gap, not evidence of irrelevance.
6. Results below `RERANK_MIN_SCORE` are dropped; a best score under `RERANK_LOW_CONFIDENCE_SCORE` sets `low_confidence`. Near-duplicates (SimHash) are collapsed here.
7. Results from every web provider are persisted as deduplicated snippet documents (`app/search/web_persist.py`); top `SEARCH_ENRICH_TOP_N` URLs enqueued for snippet enrichment.
8. Response: `providers`, `degraded`, `top_score`, `low_confidence`, timing, `cache_hit`.

A semantic cache sits in front of all of this: a rephrased query whose embedding is close to a cached one resolves to that entry's result (~18s → ~10–20ms). Entries are scoped by source and by every filter that changes the answer (domains, language, country, time range, operators, ColBERT), so an answer recorded for `sources=all` can never satisfy a `sources=federated` request, and an unfiltered answer can never satisfy a `domains=` one. Until this release the scope was `sources` alone.

> **Verify A/B changes with the cache flushed.** A cached response will happily serve you the previous ranking and a suspiciously fast `took_ms`:
>
> ```bash
> curl -X POST localhost:8000/v1/admin/cache/flush -H "Authorization: Bearer $ADMIN_KEY"
> ```

## Failure behavior

- Providers degrade individually (listed in `degraded`) instead of failing the request.
- The web cascade degrades per tier: a dead SearXNG is transparent because the next tier answers.
- Missing embeddings degrade loudly: `/stats` reports `vector_search_available` and
  `embedding_coverage`, and the worker logs why. A `backfill` loop fills gaps in the background.
- Page fetches degrade renderer → direct GET → Proxify (when configured). 404, robots.txt and egress-policy refusals are final; they are never retried through a proxy.
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
        ├──▶ index_chunk_tokens: ColBERT per-token vectors   [COLBERT_ENABLED]
        │
        ▼
  upsert document + chunks → status=indexed
        │
        ▼
  index_page_images: rank <img> by declared size, drop SVG/   [images extra]
  icons, fetch survivors, CLIP-embed → images
        │
        ▼
  _discover_and_enqueue: extract <a href>, dedupe against
  indexed docs + active jobs, enqueue depth+1 (same-domain policy)
```

Both new steps are best-effort and non-fatal: a page whose images cannot be fetched or embedded still indexes as a document.

## MCP server

`services/mcp/searchos_mcp.py` is a thin wrapper over the gateway API exposing 5 tools (`search`, `extract`, `ingest`, `stats`, `health`) over all transports. See [MCP server](mcp.md).
