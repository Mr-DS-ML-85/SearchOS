<p align="center">
  <img src="docs/assets/banner.png" alt="SearchOS" width="100%" />
</p>

<p align="center">
  <b>Self-hosted search API for AI agents.</b><br/>
  Your own FTS + vector index, image search, late-interaction ranking, federated peers, live web fallback, continuous recursive crawler, and an MCP server — no vendor lock-in, no per-query fees, no data leaving your infra.
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
  <a href="docs/assets/badges/ci.svg"><img src="docs/assets/badges/ci.svg" alt="Tests 276 passed"/></a>
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
| Image / multimodal search | — | — | limited | limited | **yes (CLIP, text→image + image→image)** |
| Own your data | no | no | no | no | **yes (full index export)** |

## Features

- **Own search index** — Postgres FTS (`ts_rank_cd`, english config) + pgvector HNSW embeddings, fused with Reciprocal Rank Fusion.
- **Cross-encoder reranking** — candidates are rescored by a real relevance model (`cross-encoder/ms-marco-MiniLM-L6-v2`), so good and bad matches no longer return near-identical scores. Results below a threshold are dropped and weak result sets are flagged `low_confidence`.
- **Image and multimodal search** — CLIP embeds images and text into one vector space, so `/v1/images/search` finds images by *what they depict* from a text query, or finds images like another image. Images are discovered while crawling; icons, SVGs and undersized assets are filtered out.
- **Late-interaction ranking (ColBERT)** — optional, token-level MaxSim scoring blended with the cross-encoder. A single pooled vector per chunk can only express "how similar are the centroids"; MaxSim asks how much of the query is covered at token level. Off by default because it costs ~128KB per chunk.
- **Federated search** — query several SearchOS instances at once and merge with RRF, so a URL several independent indexes return scores higher than one only a single index found. Dead peers are skipped with a cooldown instead of adding their timeout to every query.
- **Web fallback cascade** — SearXNG is primary; when it fails, is rate-limited, or returns an empty SERP, the request falls through to **Tavily** (free, no account needed), **Exa** (via its hosted MCP server), then **Jina**. The first tier that answers wins, and an exhausted chain opens a cooldown so a request never pays every dead upstream's timeout.
- **Live web fallback that learns** — when local coverage is thin, SearXNG results are merged in, **saved into your index, and re-crawled**, so repeat queries are served instantly without re-hitting the web.
- **Semantic query cache** — a rephrased query ("consensus in distributed systems" → "how does paxos agree") resolves through the cache instead of re-running the pipeline: ~18s became ~10–20ms. Scoped by source and filters so a cached whole-corpus answer can never satisfy a pinned-provider query.
- **Search operators** — `site:`, `-site:`, `filetype:`, quoted phrases, and plain-term `OR`. Domain restrictions from operators and from the `domains=` / `exclude_domains=` parameters are resolved **centrally** and re-applied to every provider, not just the local index — a tier that ignored them returned dictionary-definition sites for `site:wikipedia.org`, which looks like an answer rather than a filter failure.
- **Query-aware snippets** — the snippet is the passage around your query terms, not the first paragraph of the page. A sliding window is scored against the query and the best-scoring one is returned, so a result shows the sentence that made it a match instead of the site's boilerplate intro. Falls back to the leading window when nothing matches, and to an empty snippet when there is no content.
- **Near-duplicate detection** — a 64-bit SimHash per document collapses syndicated copies at query time, so a corpus that re-publishes one press release does not return it six times.
- **Continuous recursive crawler** — follows nested same-domain links (depth-limited), plus automatic **re-crawl of stale pages** so your index stays fresh.
- **Crawl politeness** — the worker fetches one job at a time with no delay between requests to the same host, so a recursive crawl of a docs site fires requests as fast as the network allows — which is how a crawler gets blocked, and blocking one origin can take out the whole seed. Every outbound fetch passes a per-origin gate that honours `robots.txt`'s `Crawl-delay` when declared and `CRAWLER_MIN_DELAY_SECONDS` otherwise, so an origin that omits the directive is still throttled. The schedule lives in **Redis**, so all workers share it and a restart cannot reset an origin's window and immediately re-burst against it.
- **Per-language full-text search** — a second GIN FTS index using the `simple` configuration covers languages whose stemmers Postgres does not ship, alongside the english-configured index. Documents are language-detected and stored in a per-language tsvector, so a German document is not matched by English stemming rules. Detection is **script-first, stopwords-second**: a Unicode range identifies Bengali or Japanese outright, and stopword voting is only trusted for Latin script, which is genuinely ambiguous. Pure-stopword heuristics mislabelled 218 of 742 English documents as Bengali.
- **Embedding backfill** — enabling embeddings on an existing corpus drains missing vectors in the background, so documents indexed while the embedder was unavailable are not stranded without semantic search.
- **LLM-ready extraction** — clean markdown/text/json from HTML, PDF, and JS-rendered pages (headless renderer), robots.txt-aware, with an SSRF guard that resolves each host before connecting.
- **MCP server** — expose search, extract, ingest, stats and health to any AI agent over **stdio, legacy HTTP+SSE, and the new stateless Streamable HTTP** (2026-07-28 spec). The HTTP transport is bearer-token protected; stdio is a local process.
- **Production ops** — Redis cache + rate limits, auth (hashed API keys), Prometheus/Grafana metrics, Alembic migrations, and network segmentation so data services have no route off the host.
- **Reproducible evaluation** — `tools/eval_retrieval.py` scores the index against a judged query set with nDCG@10, MRR@10, recall@50 and an abstention check, so retrieval changes can be measured rather than argued about.

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

`/mcp`, `/sse` and `/messages/` expose all nine tools including `ingest`, so the HTTP transports require `MCP_HTTP_TOKEN`. The server refuses to bind a non-loopback address without one. stdio is unaffected — it is a local process, not a socket.

Tools: `search`, `extract`, `ingest`, `stats`, `health`, `search_images`, `suggest`, `popular`, `recent`.

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

### Late interaction (ColBERT, optional)

A single pooled vector per chunk has to compress a whole passage into one point in space, so relevance reduces to "how similar are the centroids". ColBERT keeps **one vector per token** and scores with MaxSim — for each query token take the best-matching document token, then sum — which asks how much of the query is *covered*.

Storage is the reason it is off by default. Measured on this codebase's corpus:

| Configuration | per chunk | corpus total |
|---|---|---|
| `colbertv2.0`, 512 tokens × 768d fp32 | 1536 KB | 10.9 GB |
| projected, 256 tokens × 128d fp32 | 128 KB | **0.9 GB** |

So tokens are projected to 128 dimensions using ColBERT's own trained head (transformers reports that head as an unexpected key, so it is read from the checkpoint directly — a random projection would produce plausible scores from vectors that no longer share a space) and capped at 256 tokens. fp16 would halve the figure again but the pinned `pgvector-python` only exposes `HalfVector` as a value wrapper, not a column type.

MaxSim runs **after** the cross-encoder, blended into the score, and the sort uses the blended value — so `score` always decreases with `rank`. Reordering without rescoring left the top result carrying the *lowest* score, which breaks any caller filtering on `score`.

`COLBERT_WEIGHT=30` is calibrated, not guessed: MaxSim lands around 0.06–0.09 (it is divided by query token count) while cross-encoder logits are 3–6, so at the original weight of 2 the ColBERT term moved a score by ~0.16 and never changed a single ordering.

### Federated search

Query several instances at once and merge with RRF, so a URL that several independent indexes return scores higher than one only a single index found. Results carry `source: federated:<peer>`.

```bash
FEDERATED_ENDPOINTS=team-a=http://peer-a:8000,shared=http://shared:8000
```

**Peer URLs resolve from inside the gateway container**, so `127.0.0.1:8001` is the gateway itself, not the host — use a compose service name or `host.docker.internal`. A peer that errors is recorded in `degraded` and then skipped without a live call for 30s, so one dead peer cannot add its timeout to every query.

### Image search

CLIP embeds images and text into one space, so text→image and image→image are the same cosine search rather than separate machinery.

Images are discovered while crawling, and picking them well matters more than it sounds: a page's first dozen `<img>` tags are its logo, edit shields and badges, not its photograph. Candidates are therefore ranked by **declared size** rather than document order, and SVG and icon-named assets are dropped — Wikimedia serves icons as `<name>.svg.png`, so a raster-looking suffix does not mean it is a photo.

Requires the `images` extra (Pillow). CLIP is lazy-loaded on the first image query, not at startup, so deployments that never search images do not pay a ~600MB download in their boot path.

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
| `sources` | `all`\|`local`\|`vector`\|`web`\|`searxng`\|`tavily`\|`exa`\|`jina`\|`federated` | `all` | which providers to query; `web` is the whole cascade, the others pin one upstream |
| `num_results` | int 1–50 | 10 | results to return |
| `language` | string | — | e.g. `en`, `de` |
| `country` | string | — | e.g. `us` |
| `time_range` | `day`\|`week`\|`month`\|`year`\|`all` | `all` | freshness filter |
| `domains` / `exclude_domains` | list[string] | — | domain restrictions |
| `offset` / `cursor` | int / string | 0 | pagination; prefer `cursor` for deep pages (see below) |
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

### `GET /v1/images/search`

| Param | Type | Default | Description |
|---|---|---|---|
| `q` | string | — | text query — text→image search |
| `image_url` | string | — | reference image — image→image search |
| `limit` | int 1–50 | 10 | images to return |

Provide `q` **or** `image_url`. CLIP puts images and text in one vector space, so both directions are the same operation rather than separate machinery. Image URLs are fetched through the same egress guard as every other outbound request, so this is not an SSRF path.

```bash
curl -s -G localhost:8000/v1/images/search \
  --data-urlencode "q=a white arctic fox in snow" \
  -H "Authorization: Bearer sk_..."
```

Check `image_search_available` in the response — `false` means the `images` extra (Pillow) is not installed, which is a degraded mode rather than an empty index.

### `GET /v1/suggest`, `GET /v1/popular`, `GET /v1/recent`

`/v1/suggest?q=...` returns prefix completions from indexed titles, for search-as-you-type.

`/v1/popular` returns the most-run queries, from the live Redis query counter — so it reflects current traffic, not history. `/v1/recent` returns documents the crawler indexed or re-crawled in the last `hours` (default 24): the recrawler already knows what changed, and this exposes it as a "what's new" feed that `search` cannot answer.

Both require authentication, but neither is scoped to the calling key — they are corpus-wide, so treat them as shared state.

### Pagination

Use `cursor` from a previous response rather than `offset` for anything past the first few pages. Postgres makes `OFFSET` walk and discard every skipped row, so page 50 costs roughly 50× page 1. `has_more` disambiguates "no matches" from "you paged past the end".

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
| `COLBERT_ENABLED` | `0` | late-interaction reranking; needs the token index populated |
| `COLBERT_MODEL` | `colbert-ir/colbertv2.0` | late-interaction model |
| `COLBERT_TOP_N` | `50` | candidates rescored by MaxSim |
| `COLBERT_WEIGHT` | `30.0` | blend weight vs the cross-encoder (calibrated — see below) |
| `IMAGE_MIN_DIMENSION` | `128` | smaller images (icons, tracking pixels) are not indexed |
| `IMAGE_MAX_PER_DOCUMENT` | `12` | images embedded per crawled page |
| `FEDERATED_ENDPOINTS` | — | comma-separated `name=url` peers, e.g. `team-a=http://peer-a:8000` |
| `FEDERATED_API_KEY` | — | bearer token sent to peers |
| `DEDUPE_NEAR_DUPLICATES` | `true` | collapse SimHash near-duplicates at query time |
| `SEMANTIC_CACHE_*` | see `.env.example` | paraphrase-aware query cache |
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

## Evaluation

Retrieval changes are easy to argue about and hard to measure. `tools/eval_retrieval.py` scores the index against a judged query set so a change can be evaluated rather than eyeballed:

```bash
# score the index against the judged query set
python tools/eval_retrieval.py --api-key sk_...

# compare two runs, which is how a retrieval change should be argued for
python tools/eval_retrieval.py --json before.json
python tools/eval_retrieval.py --baseline before.json --json after.json
```

Flags: `--set` (which query set), `--sources` (pin providers), `--k`, `--timeout`, `--gateway`.

Each judged query carries its relevant URLs and whether the system is expected to *abstain*. Abstention matters as much as ranking: a search engine that confidently returns three loosely-related documents for a nonsense query is worse than one that returns none, and a metric that only rewards hits cannot see that difference.

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
- **Backups**: `pgdata`, `redisdata`, `promdata`, `modelcache` volumes; `pg_dump` on postgres.
- **Model weights** live on the `modelcache` volume. Without it every rebuild re-pulls ~800MB and the first query after a restart pays ~310s of download; with it, ~1.5s.
- **Monitoring**: Grafana `http://localhost:3000` (admin/admin), dashboard "SearchOS Overview". Prometheus alerts: gateway down, 5xx spikes, provider failures, p95 latency, queue backlog, rate-limit storms.
- **Logs**: structured single-line logs with request IDs (`X-Request-Id`).

## Roadmap

- **Knowledge graph** — entity and relation extraction over the crawled corpus, with graph traversal alongside the existing search.
- Per-token storage in fp16 (halves the ColBERT index) once `pgvector-python` exposes `HalfVector` as a column type.
- Scheduled re-crawl and freshness scoring over your own index.
- Streaming responses and typed structured-output queries.
- Geo/local relevance, and more SERP features ported into the own index.
- Per-provider retry/backoff policies and per-site crawl-rate throttling in the worker.

## License

MIT.
