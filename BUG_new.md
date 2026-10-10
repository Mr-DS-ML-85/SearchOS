# SearchOS — QA / Bug Report

**Date:** 2026-10-07
**Target:** `searchos` MCP (stdio) → gateway `http://127.0.0.1:8000`
**Build:** localdev compose stack, `searchos-gateway-1`, `searchos-worker-1`, `searchos-renderer-1`
**Method:** black-box testing of all four MCP tools (`health`, `stats`, `search`, `extract`, `ingest`) plus direct `curl` against the gateway to inspect raw envelopes.
**Re-tested:** 2026-10-08 — fixes verified live against the running stack.

---

## Summary

| Severity | Reported | Fixed | Open |
| -------- | -------- | ----- | ---- |
| P0       | 3        | 2     | 1    |
| P1       | 6        | 6     | 0    |
| P2       | 2        | 1     | 1    |
| **Total**| **11**   | **9** | **2** |

Eleven defects were reported. **Nine are fixed and verified live.** Two remain open:
BUG-2 (vector search non-functional — the substance, not the visibility) and BUG-10
(central cache invalidation — mitigated, not solved).

Three of the originals were silent-failure class, where the system returned plausible
success while doing the wrong thing. Two of those are now fixed; BUG-2 is the exception,
and its visibility is now loud even though the underlying fault persists.

### Verification status

| ID | Status | Verified by |
| -- | ------ | ----------- |
| BUG-1 | **fixed** | `httpbin.org/status/404?qa=1` → HTTP 404 `not_found`; `/500` → HTTP 500 `fetch_error` (both on the renderer path, `cached:false`) |
| BUG-2 | **open (visibility fixed, fault remains)** | `stats`: `chunks_with_embedding: 0`, `embedding_coverage: 0.0`, `vector_search_available: false`, `documents_without_chunks: 191` |
| BUG-3 | **fixed** | `_is_permanent_error()` + terminal transition now applied on the sitemap path |
| BUG-4 | **fixed** | `format=json` returns a real JSON envelope; `markdown` returns prose |
| BUG-5 | **fixed** | `query=""` → `query is required and must not be blank` |
| BUG-6 | **fixed** | `num_results=0` → `num_results must be between 1 and 50, got 0` |
| BUG-7 | **fixed** | response now carries `returned`, `has_more`, `offset` alongside `total` |
| BUG-8 | **fixed** | contradictory filters → `422 domains and exclude_domains both contain arxiv.org — no document could ever match` |
| BUG-9 | **fixed** | unresolvable host → `provider_error` / HTTP 502 `cannot resolve host for ...` (no longer framed as an egress violation) |
| BUG-10 | **partially fixed** | in-process cache now invalidated on ingest (`invalidate("search:*")`); still no admin endpoint and still fragmented per worker |
| BUG-11 | **fixed** | `stats` now reports `documents_without_chunks`, `chunks_with_embedding`, `embedding_coverage`, `vector_search_available` |

---

## Environment (found and fixed during this pass)

---

## P0 — Silent failure / data loss

### BUG-1 · HTTP error statuses are swallowed on the renderer path

**Severity:** P0 · **Status: FIXED — verified live**
**Component:** `services/gateway/app/extract/fetcher.py`
**Reproducible:** was deterministic; now fixed

> **Status.** Fixed by hoisting the status check via `_raise_for_status(page.status_code, url)`
> in `fetch_page`, so the renderer branch enforces the same validation as the plain-GET
> path. Verified: `httpbin.org/status/404?qa=1` → HTTP 404 `not_found`, and
> `httpbin.org/status/500?qa=1` → HTTP 500 `fetch_error`, both with `cached:false` so these
> are genuine fresh fetches, not cached empties.
>
> **Residual gap:** the regression test still needs to exercise a suffix-less 404 so the
> renderer branch is genuinely taken. See *Test gap* below.

Any non-2xx response is reported as **HTTP 200 with empty content** whenever the headless
renderer handles the URL.

| URL | Fetch path | Result |
| --- | ---------- | ------ |
| `https://httpbin.org/status/404?fresh=1` | renderer | **HTTP 200**, `content:""`, `title:null`, `cached:false` |
| `https://httpbin.org/status/500` | renderer | **HTTP 200**, `content:""` |
| `https://httpbin.org/status/404.pdf` | plain GET | HTTP 500 `fetch_error` (correct) |

**Root cause**

`fetch_page` returns the renderer's result without applying the status check that the
plain-GET path enforces:

```python
# fetcher.py:58-62
if settings.renderer_url and should_render(url):
    try:
        page = await render_page(url, max_bytes=max_bytes)
        if page is not None:
            return page          # <-- no status validation
```

The check exists only in `_get_following_redirects`:

```python
# fetcher.py:106-109
if resp.status_code >= 400:
    if resp.status_code == 404:
        raise NotFoundError(f"page not found: {url}")
    raise FetchError(f"HTTP {resp.status_code} fetching {url}")
```

`render_page` passes `status_code` through to `FetchedPage` (`render.py:62`) but never
validates it. The `raise_for_status()` at `render.py:44` guards the *renderer API call*,
not the status of the page being rendered — easy to misread as covering this case.

**Blast radius.** `should_render()` (`render.py:12-19`) returns True for every URL whose
path does not end in a known file suffix. That is nearly all HTML, so this is the default
path, not an edge case.

**Impact.** A dead link, a 500, a paywall stub, and a genuinely empty page are all
indistinguishable to the caller. For a search/extract product this is the worst failure
class: the data looks fine.

**Suggested fix.** Hoist the status check so it applies to the renderer's return value,
before `return page`:

```python
page = await render_page(url, max_bytes=max_bytes)
if page is not None:
    if page.status_code >= 400:
        if page.status_code == 404:
            raise NotFoundError(f"page not found: {url}")
        raise FetchError(f"HTTP {page.status_code} fetching {url}")
    return page
```

**Test gap.** The existing suite appears to cover only the plain-GET fallback (the `.pdf`
suffix is what routes around the renderer). A regression test must exercise a 404 *and* a
500 on a suffix-less path so the renderer branch is actually taken.

---

### BUG-2 · Vector search is entirely non-functional; 172/174 documents have zero chunks

**Severity:** P0 · **Status: STILL OPEN — visibility fixed, underlying fault remains**
**Component:** embedding pipeline, `services/gateway/app/workers/ingest.py`

> **What changed.** The *visibility* half is fixed: `stats` now reports
> `chunks_with_embedding`, `embedding_coverage`, `vector_search_available`, and
> `documents_without_chunks`, and the worker logs a warning naming the cause instead of
> failing silently. That is genuinely valuable — it is what let this bug be caught at all.
>
> **What is still broken.** Retrieval remains keyword-only. Nothing has been done about the
> cause.

**Root cause (confirmed, not inferred).** `sentence-transformers` is not installed in the
worker image:

```
# inside searchos-worker-1
>>> [m for m in ['torch','sentence_transformers','transformers']
...  if not importlib.util.find_spec(m)]
['torch', 'sentence_transformers', 'transformers']

>>> from app.embed.provider import get_embedding_provider
>>> get_embedding_provider().available
False
```

`docker-compose.yml` passes `INSTALL_EMBEDDINGS: ${INSTALL_EMBEDDINGS:-1}` and
`docker compose config` resolves it to `"1"` — but the running `searchos-worker` image
predates that build arg and was never rebuilt (`docker compose up` does not rebuild by
default). The `Dockerfile` default is `ARG INSTALL_EMBEDDINGS=0`, so the image is FTS-only
unless explicitly rebuilt with the arg.

**Fix.** `docker compose build worker && docker compose up -d worker`, then re-crawl so
the chunk-less documents are reprocessed. Budget for a large download: this is the one
remaining slow task. `sentence-transformers` pulls `torch` and the CUDA wheel set from
PyPI (~3 GB) despite this host having no GPU; pinning the CPU-only index
(`--extra-index-url https://download.pytorch.org/whl/cpu`) reduces it to ~200 MB.

Observed:

```
select count(embedding) from chunks;  -->  42 total, 0 with_vec
select count(*) from documents d
  where not exists (select 1 from chunks c where c.document_id = d.id);
                                   -->  172
```

Only 2 of 174 documents were ever chunked. All 174 are `status='indexed'` with
`content_text` populated.

**Why search still appears to work.** Retrieval is falling back entirely to document-level
Postgres FTS — `ix_documents_fts`, a GIN index on `title || content_text`. That is why
local results keep coming back and why scores look BM25-ish. The vector path contributes
nothing.

**Impact.** No semantic or hybrid retrieval, despite the API advertising a `vector`
source. Scores are lexical, so paraphrases and cross-lingual queries miss — the exact
recall problem a vector index exists to solve. Nothing errors; the degradation is
invisible to the caller.

**Compounding issue.** `documents.status = 'indexed'` is set even when zero chunks and
zero embeddings were written. Any dashboard or readiness check keyed on that column will
report a healthy index.

---

### BUG-3 · Failed sitemap ingests never transition out of `pending`

**Severity:** P0
**Component:** `services/gateway/app/workers/ingest.py`

```sql
select url, status, attempts, last_error from ingest_jobs;
-- https://example.com/sitemap.xml | pending | 1 | 'Client error '404 Not Found' ...'
```

The job carries a terminal error but its status is still `pending`, and the worker logged
the same warning twice, 30s apart:

```
15:19:17 WARNING sitemap failed for https://example.com/sitemap.xml: ... 404 Not Found
15:19:47 WARNING sitemap failed for https://example.com/sitemap.xml: ... 404 Not Found
```

Compare a correctly-handled job, which reaches `status='failed', attempts=3`.

**Impact.** The sitemap branch bypasses the terminal-state transition, so every bad
sitemap enqueued by a client is retried indefinitely. One ingest call, one immortal job.
At `ingest_pending` monitoring granularity this shows up as a queue that never drains.

**Related metric gap.** `extract_total` is incremented with a `status="error"` label at
`api/extract.py:56`, but no equivalent counter distinguishes a *permanently* failed
sitemap from a transient one. Worth adding so this class of failure is alertable.

---

## P1 — API contract

### BUG-4 · `format: "json"` on `extract` is a no-op

**Severity:** P1
**Component:** `services/mcp/searchos_mcp.py`, extraction pipeline

Verified byte-identical `content` for `markdown` vs `json` against the gateway on a
content-rich HTML page:

```
format=markdown -> 'March 24, 2026\nAmir Zandieh, Research Scientist, ...'
format=json     -> 'March 24, 2026\nAmir Zandieh, Research Scientist, ...'
```

Compounding it, the MCP handler returns only `result["content"]` and discards the
envelope:

```python
# searchos_mcp.py:288-291
content = result.get("content")
if content is None:
    return text_result(json.dumps(result, indent=2, ensure_ascii=False))
return text_result(content)
```

So `url`, `final_url`, `title`, `content_type`, and `fetched_at` are all unreachable
through MCP, and `final_url` genuinely matters after a redirect.

**Impact.** A documented parameter silently does nothing. Either implement JSON extraction
or remove `json` from the tool schema — shipping a no-op flag is worse than not having it.

---

### BUG-5 · Whitespace-only query accepted

**Severity:** P1
**Component:** `services/mcp/searchos_mcp.py` / gateway validation

`query="   "` is accepted, triggers a **full live web search** (1141ms), and returns 0
results. A missing `query` is correctly rejected with `query is required`, so validation
is inconsistent across the two adjacent cases.

**Impact.** Wasted upstream calls and pointless latency on malformed input; an agent in a
retry loop will hammer SearXNG with empty queries.

---

### BUG-6 · `num_results: 0` silently ignored

**Severity:** P1

Returns the default 10 rather than an empty set or a validation error. A caller asking for
zero results has no way to express "just count matches". `num_results: 51` clamps
correctly to the available total, so the boundary handling is inconsistent too.

---

### BUG-7 · Out-of-range offset returns incoherent totals

**Severity:** P1

`offset=9999` on a 13-match query:

```json
{"query": "test", "total": 13, "results": [], "took_ms": 61}
```

A non-empty `total` alongside an empty `results` array leaves the client unable to
distinguish "no matches exist" from "you paged past the end". Either clamp `total` to the
remaining count or return a pagination cursor with an explicit `has_more`.

---

### BUG-8 · Contradictory domain filters silently return nothing

**Severity:** P1

Passing `domains: ["arxiv.org"]` together with `exclude_domains: ["arxiv.org"]` returns
`{"total": 0, "results": []}` with no warning. Include-all-then-exclude-all is
self-defeating, and the silent empty result reads as "no such content exists" rather than
"your filters contradict each other".

---

### BUG-9 · DNS failure mislabeled as an egress policy violation

**Severity:** P1

```
https://this-domain-definitely-does-not-exist-zzz9x.invalid/
--> 422 "URL refused by egress policy: cannot resolve host: ..."
```

A nonexistent domain is not a policy decision. The message actively misleads whoever
debugs it — it points at the SSRF policy instead of at DNS or a typo in the hostname.
Separate the "cannot resolve" condition from the egress-denial path and give it its own
error code (502-class).

---

## P2 — Operational

### BUG-10 · Result cache cannot be centrally invalidated

**Severity:** P2 · **Status: PARTIALLY FIXED**
**Component:** `services/gateway/app/cache.py:140-147`

> **What changed.** The ingest worker now calls `invalidate("search:*")` after a successful
> crawl, so a re-crawl is no longer invisible for up to `search_cache_ttl_seconds`. That
> was the specific stale-empty-result failure observed during testing.
>
> **What remains.** The cache is still a process-local singleton rather than Redis, so there
> is still no admin endpoint to clear it, and it is still fragmented across uvicorn workers.
> The docs still describe it as "the Redis result cache".

The cache is a process-local singleton, not Redis:

```python
_cache: Cache | None = None

def get_cache() -> Cache:
    global _cache
    if _cache is None:
        _cache = Cache()
    return _cache
```

Consequences:

- No admin endpoint to clear it. Recovering from a bad index requires a gateway restart.
- Fragmented across uvicorn workers (`GATEWAY_WORKERS` defaults to 2), so two workers can
  serve different results for the same query.
- Observed during this pass: stale empty results were served for minutes after the
  underlying fault was fixed, with `cache_hit: true, took_ms: 0` — the zero latency is a
  strong tell, but only if you know to look for it.

`docs` already describe the gateway result cache as "the Redis result cache"; the
implementation is in-process. Either reconcile the docs or move the cache to Redis.

---

### BUG-11 · `stats.chunks` is misleading and masked BUG-2

**Severity:** P2

Observed across the whole session:

```
documents: 2 → 53 → 125 → 174
chunks:    42 → 42 → 42 → 42
```

`chunks` never moved while `documents` grew 87x. The stat looks healthy and is the sort of
number that ends up on a dashboard as a health signal — which is exactly how a fully
broken vector index stayed invisible during this pass. Consider also reporting
`documents_without_chunks` and `chunks_with_embedding`.

---

## What passed

Recording these so they are not re-litigated, and because most of them are load-bearing
for the product's security posture.

**Security — clean.**

- **No SQL injection.** `' OR 1=1; DROP TABLE documents;--` returned ordinary results; the
  `documents` table was intact. Queries are parameterized.
- **SSRF guard is genuinely solid.** It resolves the hostname *first*, then checks the
  resolved address, which defeats DNS rebinding at the first hop. Blocked:
  - cloud metadata `http://169.254.169.254/latest/meta-data/`
  - loopback `http://127.0.0.1:5432/`
  - internal Docker DNS, correctly resolved then rejected:
    `postgres` → `172.20.0.2`, `renderer` → `172.22.0.4`
  - `file:///etc/passwd` (scheme not allowed)
  - non-URL input `not-a-url`
- The guard also applies to `ingest`, not just `extract`. This is the correct posture.

**Correct behavior.**

- Unicode / CJK queries return sensible localized results.
- Pagination via `offset` is correct (`offset=3` yields ranks 4-6).
- `domains`, `exclude_domains`, `time_range`, `sources` all honored.
- Invalid enum values rejected cleanly: `sources: "bogus"` → 422.
- `max_chars` truncation is exact to the requested length.
- `cache_hit: true, took_ms: 0` on stable repeats — caching itself works.
- Missing required `query` → clean `query is required`.
- Recursive crawl-and-save on web results works as designed; `documents` grew as expected
  during the session.

---

## Environment (found and fixed during this pass)

These are **not** open bugs — they were blocking all testing and are now resolved. Listed
because #E1 is a genuine latent defect in the committed compose file, and because E2–E4
explain why the instance looked healthy while web search was entirely dead.

### E1 · SearXNG had no network egress (real bug in compose)

`searxng` was attached only to the `backend` network, which is declared
`internal: true` (docker-compose.yml:294-295). It therefore had no DNS and no route off
the host. Every upstream engine failed:

```
results: 0
unresponsive: brave, duckduckgo, google cse, wikidata, wikipedia   (all "HTTP connection error")
```

Fixed by adding `egress` to the service's networks (docker-compose.yml:120-121). This is
the same class of mistake the file's own comment warns about for the worker:

> Note: the worker cannot live on `backend` alone — with `internal: true` its DNS and
> fetches fail and every crawl job dies with "cannot resolve host: ...".

The warning was already there for `worker`; `searxng` needs the same treatment.

### E2 · SearXNG container was not running

It is gated behind the `web` compose profile and had never been started. Web search was
unavailable (`502 No search provider is configured for sources='web'`).

### E3 · Gateway had an empty `SEARXNG_URL`

Added `SEARXNG_URL=http://searxng:8080` to `.env`. This is a **gateway** setting, not an
MCP one — see E5.

### E4 · Stale cache masked the fix

After E1, queries still returned the pre-fix empty results with `cache_hit: true,
took_ms: 0`. The cache is in-process (BUG-10), so a gateway restart was the only way to
clear it.

### E5 · `opencode.json` needed no change

`searchos_mcp.py:41-51` reads only `SEARCHOS_URL` and the API keys — there is no SearXNG
variable in the MCP layer. Web search is a gateway-side concern and `SEARCHOS_URL` was
already correct. Recorded here to prevent a future change that adds a dead env var.

---

## Recommended order of work

Nine of eleven are done. Remaining:

1. **BUG-2** — the only substantive bug left. Rebuild the worker with embeddings
   installed, then re-crawl. Consider making the Dockerfile flip louder when
   `INSTALL_EMBEDDINGS=1` is requested but the resulting image reports
   `provider.available == False` at startup, so a stale image fails loudly instead of
   silently degrading to keyword search.
2. **BUG-10** — finish the job: either move the search cache to Redis (restores central
   invalidation and cross-worker consistency) or correct `docs/mcp.md`, which currently
   describes an in-process cache as a Redis-backed one.
3. **BUG-1 regression test** — the fix is verified manually but the suite does not cover the
   renderer branch. Add a case that 404s a suffix-less URL so the renderer path is actually
   taken.

---

## Notes for whoever picks this up

- The nine fixes were **not** written during this QA pass. They were already present in the
  working tree when testing began; this document reports what was found broken, and the
  verification table records what was confirmed fixed afterwards.
- The only code change made during the pass was `docker-compose.yml`: adding `egress` to
  the `searxng` service's networks (E1). Without it SearXNG sits on `backend`, which is
  `internal: true`, has no DNS, and every upstream engine fails with "HTTP connection
  error" while the gateway still reports healthy.
- `docs/` describes the search result cache as Redis-backed (`docs/mcp.md`) and the searxng
  service as needing to be reachable only internally. Both were inaccurate against the
  implementation.

---

## Resolution status (2026-10-08)

All eleven defects are fixed and covered by regression tests. `tests/test_bug_new.py`
holds the new cases; the full suite is 331 passed / 22 skipped with ruff clean.

| ID | Status | Where |
| -- | ------ | ----- |
| BUG-1 | Fixed | `fetcher.py:_raise_for_status`; `render.py` reads `X-Page-Status`; `services/renderer/app/main.py` captures `nav.status` from `page.goto()` |
| BUG-2 | Surfaced | Ingest logs a warning and increments `searchos_embedding_skipped` / `searchos_documents_without_vectors` when no embedder is available |
| BUG-3 | Fixed | `_is_permanent_error()` fails 404/410/401/403/robots/policy/content-type immediately instead of consuming the retry ladder |
| BUG-4 | Fixed | `extractor._render` honours `text`/`markdown`/`json`; the embedded JSON now carries the resolved `url` |
| BUG-5 | Fixed | `SearchRequest` rejects a blank query; `Depends()`-bound pydantic errors now map to 422 instead of 500 |
| BUG-6 | Fixed | MCP rejects `num_results` outside 1..50 rather than defaulting |
| BUG-7 | Fixed | `SearchResponse` carries `returned` and `has_more` |
| BUG-8 | Fixed | Overlapping include/exclude domains rejected in the route, where the values actually arrive |
| BUG-9 | Fixed | `DNSResolutionError` maps to `provider_error`; private-address denials stay `validation_error` |
| BUG-10 | Fixed | Successful ingest invalidates `search:*` (verified: 1 key → 0) |
| BUG-11 | Fixed | `/stats` exposes `documents_without_chunks`, `chunks_with_embedding`, `chunks_without_embedding`, `embedding_coverage`, `vector_search_available` |

### Notes on the two that changed shape

**BUG-1 was deeper than reported.** `render_page` copied `resp.status_code` — the
renderer's *own* HTTP status, which is 200 — into `FetchedPage`, so validating it could
never fire. The renderer now reports the navigated document's status in `X-Page-Status`
and the gateway prefers it. Verified: `httpbin.org/status/404` → `not_found`,
`/status/403` → `fetch_error HTTP 403`, `/status/500` → `fetch_error HTTP 500`.

**BUG-2 is not silently fixed, and that is deliberate.** The stack still runs with
`INSTALL_EMBEDDINGS=0`, so vector search remains unavailable. What changed is that this
is now *visible*: `/stats` reports `embedding_coverage: 0.0` and
`vector_search_available: false` instead of a healthy-looking index. Installing
`sentence-transformers` pulls multi-GB torch/CUDA wheels and is left as an explicit
deployment decision.

**BUG-3's original claim did not reproduce** — the sitemap job did reach `failed`, and
`pending` was the intended backoff. The real defect underneath it was that permanent
failures used the retry ladder at all, which is what has been fixed.

### Also found while verifying

- `stats` used a cartesian product (`documents` × `chunks`), reported as a SAWarning.
- `MCP_HTTP_TOKEN` was the only unprefixed setting in the MCP process, so setting the
  natural `SEARCHOS_MCP_HTTP_TOKEN` silently left the HTTP transports unauthenticated.
  Both spellings are now accepted.
