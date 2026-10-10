# API Reference

All endpoints accept `Authorization: Bearer <key>` or `X-API-Key: <key>`.

## `GET /v1/search`

| Param | Type | Default | Description |
|---|---|---|---|
| `q` | string | — | search query (required) |
| `sources` | `all` \| `local` \| `vector` \| `web` \| `searxng` \| `tavily` \| `exa` \| `jina` | `all` | providers to query. `web` is the whole fallback cascade; the other four pin one upstream |
| `num_results` | int 1–50 | 10 | results to return |
| `language` | string | — | e.g. `en`, `de` |
| `country` | string | — | e.g. `us` |
| `time_range` | `day` \| `week` \| `month` \| `year` \| `all` | `all` | freshness filter |
| `domains` | repeated or comma list | — | restrict to these domains |
| `exclude_domains` | repeated or comma list | — | exclude these domains |
| `offset` | int 0–1000 | 0 | pagination offset |
| `cache` | bool | `true` | enable the Redis result cache |

Both list parameters accept `?domains=a.com&domains=b.com` and
`?domains=a.com,b.com` interchangeably. They are matched as substrings of the
stored URL with `LIKE` metacharacters escaped, so `domains=%` matches nothing.

Example:

```bash
curl -s "http://localhost:8000/v1/search?q=hnsw+gpu&num_results=5&sources=all" \
  -H "Authorization: Bearer sk_..."
```

```json
{
  "query": "hnsw gpu",
  "results": [
    {
      "title": "Hierarchical navigable small world - Wikipedia",
      "url": "https://en.wikipedia.org/wiki/Hierarchical_navigable_small_world",
      "snippet": "HNSW is an approximate nearest-neighbor search method...",
      "source": "local",
      "score": 8.42,
      "rank": 1,
      "published": null,
      "domain": "en.wikipedia.org"
    }
  ],
  "total": 4,
  "returned": 4,
  "has_more": false,
  "offset": 0,
  "took_ms": 176,
  "cache_hit": false,
  "degraded": [],
  "providers": ["local", "web"],
  "top_score": 8.42,
  "low_confidence": false,
  "min_score": 3.0
}
```

### Relevance scoring

`score` is a **cross-encoder relevance logit**, not a probability and not a
rank. It is unbounded: measured on this corpus, relevant queries scored
4.94–9.34 and nonsense scored ≤ 1.16. Do not normalise it or assume 0–1 — a
sigmoid collapses that separation to a range too narrow to threshold.

| Field | Meaning |
|---|---|
| `total` | matches surviving `min_score`, before pagination |
| `returned` / `has_more` / `offset` | disambiguates "no matches" from "paged past the end" |
| `top_score` | best score in this page; `null` when nothing cleared the threshold |
| `low_confidence` | `true` when no result cleared the low-confidence bar |
| `min_score` | the cutoff actually applied |
| `semantic_cache_hit` | the answer was reused from a near-identical earlier query |
| `semantic_cache_match` / `semantic_cache_similarity` | which query was reused, and how similar |
| `degraded` | providers or cascade tiers that errored |

When `low_confidence` is `true` the corpus most likely does not cover the
query. Treat the results as a weak match rather than an answer.

Behavioral notes:

- Pagination is applied once. `offset` selects the page; there are no gaps
  between pages and an offset past the end returns `200` with an empty `results`
  array, not a 404.
- `time_range` filters the local and vector providers by publish time (falling
  back to fetch time) as well as SearXNG.
- `freshness` is accepted for compatibility but currently has no effect.
- A query with no matches returns `200` with an empty `results` array. A `404`
  `no_results` is reserved for a genuinely unknown resource, and a `502`
  `provider_error` means every configured provider failed.


- `sources=web` forces a live web query (bypasses the local-skip heuristic) and routes through the
  SearXNG → Tavily → Exa → Jina cascade. `sources=searxng|tavily|exa|jina` pins one tier and
  **errors rather than silently falling back**, so an empty result means that upstream genuinely
  had nothing. See [searxng.md](searxng.md).
- `cache=false` bypasses the Redis cache — use when testing the live pipeline.
- When `providers` includes `web` but all results are `local`, SearXNG was skipped (local coverage sufficient).

## `POST /v1/extract`

| Body | Type | Default | Description |
|---|---|---|---|
| `url` | string | — | page to fetch (required) |
| `format` | `markdown` \| `text` \| `json` | `markdown` | output shape |
| `max_chars` | int | — | truncate content |

```bash
curl -s -X POST http://localhost:8000/v1/extract \
  -H "Authorization: Bearer sk_..." \
  -H "Content-Type: application/json" \
  -d '{"url":"https://en.wikipedia.org/wiki/Web_search_engine","format":"markdown"}'
```

Returns clean, LLM-ready content. Respects robots.txt (`FETCH_RESPECT_ROBOTS`), follows redirects, handles HTML (trafilatura), PDF (pypdf), and plain text. Cached 1 hour.

## `POST /v1/admin/ingest` (admin)

| Body | Type | Default | Description |
|---|---|---|---|
| `url` | string | — | URL or sitemap (required) |
| `kind` | `url` \| `sitemap` | `url` | source kind |

```bash
curl -s -X POST http://localhost:8000/v1/admin/ingest \
  -H "Authorization: Bearer $BOOTSTRAP_ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com/docs","kind":"url"}'
```

Returns `{"job_id": "...", "url": "...", "kind": "url", "status": "pending"}`.

## `POST /v1/admin/keys` (admin)

```bash
curl -s -X POST http://localhost:8000/v1/admin/keys \
  -H "Authorization: Bearer $BOOTSTRAP_ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name":"ci-bot","role":"user"}'
```

`GET /v1/admin/keys` lists keys (hashes only). Roles: `user`, `admin`.

## Discovery endpoints

| Endpoint | Purpose |
|---|---|
| `GET /v1/suggest?q=tok&limit=8` | Query completions, merged from the corpus and the query log |
| `GET /v1/popular?limit=10` | Most-run queries from the Redis query counter |
| `GET /v1/recent?hours=24&limit=20` | Documents the crawler indexed or re-crawled recently |

`/v1/recent` is what a "what's new" feed needs; the recrawl loop already knows
what changed, and `search` cannot express it.

All three require authentication but are **corpus-wide**, not scoped to the
calling key — treat their output as shared state rather than per-user history.

## Image and multimodal search

CLIP embeds images and text into one vector space, so an image can be found by
what it *depicts* rather than by its filename or alt text.

```bash
curl -G /v1/images/search --data-urlencode "q=a white arctic fox in snow"
curl -G /v1/images/search --data-urlencode "image_url=https://example.com/photo.jpg"
```

`q` is text-to-image; `image_url` is image-to-image. Both take the same endpoint
because CLIP makes them the same operation — a cosine search against image
vectors. Image URLs are fetched through the same egress guard as every other
outbound request, so this is not an SSRF path.

Images are discovered on indexed pages automatically: the crawler takes the
largest-declared `<img>` candidates (not the first N, which on a typical page is
the logo and edit shields), skips SVG and icon assets, drops anything below
`image_min_dimension`, and embeds what survives.

Requires the `images` extra (Pillow). `image_search_available` in the response is
`false` when it is not installed — that is a degraded mode, not an empty index,
so check it before concluding there are no images.

## Late interaction (ColBERT)

Off by default (`COLBERT_ENABLED`). Set it and reindex: the feature scores
queries against per-token vectors, so an index with none has nothing to score.

A single pooled vector per chunk has to compress a whole passage into one point
in space. MaxSim keeps one vector per token and scores each query token against
its best matching document token, then sums — relevance becomes "how much of the
query is covered" rather than "how similar are the centroids".

Storage is the reason it is optional. Measured on this corpus:

    colbertv2.0, 512 tokens x 768d     1536 KB/chunk   10.9 GB
    projected, 256 tokens x 128d         128 KB/chunk    0.9 GB

So tokens are projected to 128 dimensions with ColBERT's own trained head and
capped at 256 tokens. `COLBERT_TOP_N` bounds how many candidates are rescored —
MaxSim is a Python matrix product per chunk, and rescoring the corpus to reorder
one page of results costs far more than the ordering is worth.

Scores are blended with the cross-encoder (`COLBERT_WEIGHT`) rather than
replacing them, and the sort uses the blended value, so `score` always decreases
with `rank`.

**Measured outcome: a regression, so this is off by default.** Against the judged
query set, nDCG@10 is 0.6331 with ColBERT off, 0.6235 at weight 5, and 0.5760 at
weight 30 — with recall unchanged and p50 latency roughly tripling. MaxSim is
reordering results without retrieving anything the cross-encoder had missed. Do
not enable it expecting better ranking without re-running the benchmark against
your own corpus.

## Federated search

Queries several SearchOS instances at once, for a team that wants its private
corpus searched together with a shared one.

```bash
FEDERATED_ENDPOINTS=team-a=http://peer-a:8000,shared=http://shared:8000
```

**Peer URLs are resolved from inside the gateway container.** `127.0.0.1:8001`
means the gateway itself, not the host — use a compose service name or
`host.docker.internal`.

Results carry `source: federated:<peer>` so callers can see which index answered,
and merge with Reciprocal Rank Fusion, so a URL that several peers return scores
higher than one only a single peer found. A peer that fails is skipped and
recorded in `degraded`, and is then skipped without a live call for 30 seconds so
one dead peer cannot add its timeout to every query.

## Search operators

Accepted inline in `q`, stripped before anything reaches the index:

| Operator | Effect |
|---|---|
| `site:example.com` | restrict to a host (repeatable, OR-ed) |
| `-site:example.com` | exclude a host |
| `filetype:pdf` | restrict by URL extension (soft: extensionless URLs are kept) |
| `"exact phrase"` | require the phrase; reported back in `operators_applied` |
| `-word` | drop a term from the free text |
| `OR` | require either side (plain terms only) |

Operators are merged with the `domains` / `exclude_domains` parameters rather
than replacing them. `operators_applied` in the response echoes what was parsed.

## Caching

Two layers. The exact-match cache keys on the full query and survives process
restarts. The semantic cache embeds the query and reuses a recent answer for a
*differently worded* but near-identical question, which is the common case for
agents that rephrase.

Measured with `bge-small-en-v1.5`: paraphrases of an answered query score
0.945–0.958 and are reused; a different topic scores ~0.50 and is not. The
threshold is `SEMANTIC_CACHE_THRESHOLD`, default 0.92 — high enough that a
merely-related question cannot borrow an unrelated answer. Verified that
"rust tokio async runtime" and "rust tokio runtime" share an answer (0.945) while
"vector database search" does not.

Responses served from the semantic cache carry `semantic_cache_hit`,
`semantic_cache_match` and `semantic_cache_similarity` so a caller can tell a
reused answer from a freshly retrieved one. Empty and low-confidence answers are
never remembered — a weak answer is the one most likely to be wrong.

## Pagination

`offset` is capped and works, but costs grow linearly because OFFSET makes
Postgres walk and discard every skipped row. For deep paging pass the
`next_cursor` from a previous response back as `cursor`; an invalid cursor is a
422 rather than a silent fallback to page 1.

## Ops endpoints

| Endpoint | Purpose |
|---|---|
| `GET /health` | overall status |
| `GET /health/live` | process alive |
| `GET /health/ready` | Postgres / Redis / renderer readiness |
| `GET /metrics` | Prometheus `searchos_*` metrics |
| `GET /stats` | `{documents, chunks, api_keys, ingest_pending, uptime_seconds}` |

## Errors

Every error response uses one envelope:

```json
{"code": "...", "message": "...", "details": null}
```

- `401` — missing/invalid key (includes a `WWW-Authenticate` header).
- `403` — insufficient role.
- `413` — request body over `MAX_REQUEST_BODY_BYTES`.
- `422` — validation failure, including URLs refused by the egress policy.
- `429` — rate limited, with a `Retry-After` header.
- `502` — all search providers failed.
- `503` — database unavailable.
- `500` — unhandled error; the body carries a `request_id` that also appears in
  the `X-Request-Id` header and the gateway logs.

## MCP

The same functionality is exposed to agents as MCP tools (`search`, `extract`, `ingest`, `stats`, `health`) — see [MCP server](mcp.md).
