# Handover — SearchOS

**Date:** 2026-10-10
**Branch:** `main` · **HEAD:** `153587c` · pushed to origin
**Tests:** `295 passed, 78 skipped` · `ruff check` + `ruff format --check` clean

Written for Claude Code. Read this before changing anything.

---

## 1. What you are picking up

Search, image search (both directions), ColBERT, federation, MCP and the eval
harness are all in place — §2 lists what was re-verified at handover.

I fixed several real bugs and **failed to close one**. The open item is a Tavily
behaviour I could not reproduce on demand. Everything else in this document is
verified and committed.

### Open bug: pinned `sources=tavily` intermittently returns 0 results

**Symptom.** `GET /v1/search?q=python asyncio event loop internals&sources=tavily`
sometimes returns `results: []` with `degraded: []`, `total: 0`,
`top_score: null` and `low_confidence: true`. Other times the same query returns
7–10 results. `sources=all` (cascade) is not affected.

**What I ruled out, with evidence — do not re-investigate these:**

| Suspect | Ruled out how |
|---|---|
| Wrong request body to Tavily | Replayed the exact gateway body (`max_results:10, search_depth:'basic', include_answer:false, include_raw_content:false`) directly → **200, 10 results, `response_time` 1.25s** |
| Provider misconfiguration / missing key | Keyless header used correctly; direct call returns 10 |
| Wrong provider instance registered | `build_orchestrator().select_providers('tavily')` → `p.search(...)` returns **10 results, `raw={'empty': False, 'upstream_results': 10}`** |
| Reranker threshold dropping them | Set `RERANK_ENABLED=0` → still 0 |
| Semantic cache poisoning | Reproduced with cache flushed each time |
| Domain filter | No domains requested |

**So: the provider produces results, and they are lost between
`ProviderResult` and the `SearchResponse`.** The loss is *not* in
`app/search/providers/tavily.py`.

**Where to look next** (I ran out of budget here, in order of likelihood):

1. `services/gateway/app/search/orchestrator.py` — the block between
   `provider_results = await asyncio.gather(...)` and
   `response = SearchResponse(...)`. Specifically the RRF fusion call
   `reciprocal_rank_fusion(by_provider)`. Check whether a result list of one
   provider survives `by_provider` → `fused` intact. Confirm `total=0` really
   originates there rather than being overwritten.
2. The `if degraded and not any(by_provider.values())` branch just after the
   provider loop — it raises `ProviderError` when everything is empty, but note
   it only runs when `degraded` is non-empty. With `degraded=[]` it cannot fire,
   so check what produces `low_confidence=True` with zero results instead.
3. **Most likely remaining suspect:** the async gap. `await` on the HTTP client
   yields, and `await asyncio.gather` means the response object is built later.
   If Tavily's connection is being reused/reset between the request and the read,
   `payload.get("results")` could be `None` on a 200 — which would hit
   `items = payload.get("results") or []` and silently yield zero results with no
   error. **This would produce exactly the observed symptom**: 200, no error,
   empty list. Consider logging `len(payload.get("results") or [])` and the HTTP
   status inside the provider's success path and see whether the empty case ever
   reaches it.

**Why it is intermittent:** the keyless tier is fair-use capped per IP and
degrades quietly rather than returning 429. Empty 200s cluster with cap pressure.
Setting `TAVILY_API_KEY` should make it deterministic — worth testing first to
confirm the diagnosis, even though we do not have a key.

---

## 2. Verified working right now (re-checked at handover)

### Image search — both directions, via REST *and* MCP

Implemented in `babff2a`. CLIP puts images and text in one vector space, so both
directions are the same operation against the same index.

`GET /v1/images/search?q=<text>` — **text → image**
`GET /v1/images/search?image_url=<url>` — **image → image**
MCP tool: **`search_images`** (args `query` and/or `image_url`; the gateway's own
parameter is `q`, the MCP tool renames it for consistency with `search`).

Re-verified at handover:

```
text -> image
  "a white arctic fox in snow"                  -> 0.375 Vulpes_lagopus_in_Iceland
                                                    0.346 Arctic_fox
  "a portrait painting of a woman with a subtle smile"
                                                 -> 0.310 Mona_Lisa
                                                    0.306 Isleworthml
  "sailing yacht on the ocean"  (via MCP)        -> 0.308 Ahmanson_Cup_Regatta_yacht

image -> image  (query = a Vulpes lagopus photo)
  the query image itself                          1.000
  Arctic_fox_6375652473                           0.871
  Alopex_lagopus_cub_summer_coat                  0.824
```

Index state: **34 images, 34 embedded**. A self-match at exactly 1.000 is the
sanity check that the two directions share one space.

Note for the record: an external agent previously reported image search as
"not verifiable — there's no image parameter in the search tool". That was true
before `4bf0c1d`, which added the `search_images` tool. It is a separate endpoint
by design, not a parameter of `search`.

First image query after a fresh start is slow (seconds) — CLIP is lazy-loaded on
first use, and weights are cached on the `modelcache` volume so this is paid once
per rebuild, not per query. `image_search_available: false` in the response means
the `images` extra (Pillow) is missing; that is a degraded mode, not an empty
index.

### ColBERT toggle (for A/B from an agent)

`GET /v1/search?...&colbert=true|false` and the same flag on the MCP `search`
tool. Overrides the server default per query. Verified end-to-end: **266 ms off
vs 3298 ms on**, ordering changes. **It is off by default and measured as a
quality regression — see §5.**

---

## 3. Fixed in the last session (all committed)

### `153587c` — IndexError 500s
`SemanticCache.lookup` grew a 4th return element (`miss reason`) but two of six
return paths still returned 3. The orchestrator indexes `[3]` on every miss, so
any query whose nearest cached entry fell below threshold raised `IndexError`
→ HTTP 500, intermittently. Guarded by
`TestSemanticCacheReturnShape::test_every_return_path_has_four_elements`, which
walks the AST rather than trusting manual updates.

**Lesson: if you widen a tuple return, grep every `return` in that function.**

### Pre-existing test failure
`test_stats_uptime_is_not_hardcoded_zero` failed whenever the test Postgres was
down — the `client` fixture only wraps app lifespan, so `ConnectionRefusedError`
surfaced in the test body instead of skipping. Confirmed pre-existing by
stashing. Fixed with an explicit guard.

### `e1e7d2c` — semantic threshold
`SEMANTIC_CACHE_THRESHOLD` was `0.92`, above the paraphrase band, rejecting real
paraphrases. Measured on this corpus:

| Class | Similarity |
|---|---|
| paraphrase (should hit) | 0.9477 · 0.9506 · 0.9103 · **0.8625** |
| related, different question | 0.6891 · 0.6782 · 0.6100 · 0.5628 |

Now `0.80`. **Do not replace the embedding with the cross-encoder** — measured and
worse: ms-marco scores topical relevance, not question equivalence, so
`postgres dead rows cleanup ~ postgres autovacuum tuning guide` scored **7.61**
(highest of any pair) while a true paraphrase scored 5.50.

### `bdec5a9` — semantic miss diagnostics
Added `semantic_cache_miss_reason` (`below_threshold`, `target_expired`, `empty`,
`no_embedding`, `disabled`, `store_unavailable`) and
`semantic_cache_runner_up_*`. A miss can report a similarity *above* threshold
because the entry is a reference to an evicted response; the reason field is what
makes that readable.

### `88acba7` — web tiers queried twice
`sources=all` fanned out to the cascade *and* each of its tiers, so every live-web
request ran twice (`web` ~6s beside `searxng` ~3.2s). Now `all` = local, vector,
web, federated; tier names remain reachable via `sources=tavily` etc.

### `84c12bb` — Tavily keyless 429
Tavily uses two envelopes: keyless puts `code`/`window`/`retry_after_seconds`/
`next_actions` in the **body**; keyed uses `detail` + `Retry-After` header. Both
parsed. The cascade cooldown now honours `Retry-After` (a 429 is a rate limit,
not an outage).

### `babff2a`, `c2b5812`, `770a380` — image search, ColBERT, MCP wiring
See `git log`. **Note: `770a380`'s ColBERT measured as a regression**
(nDCG@10 0.6331 off vs 0.5760 at weight 30) and is off by default.

---

## 4. Environment notes that will bite you

- **Containers hold baked-in code.** Host edits are *not* mounted. Always
  `docker compose build gateway worker && docker compose up -d`. The MCP service
  needs `docker cp services/mcp/searchos_mcp.py searchos-mcp-1:/srv/mcp/searchos_mcp.py`
  followed by restart — the container path is `/srv/mcp`, not `/app`.
- **Config must be wired through compose.** A setting in `.env` reaches the
  container only if it is also listed under the service's `environment:`.
  `COLBERT_WEIGHT` and `FEDERATED_API_KEY` were both missing for exactly this
  reason.
- **`str.replace()` in a heredoc silently no-ops** when the target does not match
  exactly. This bit me three times. Prefer the Edit tool, which errors loudly.
- **Disk is ~95% full.** Reclaim before large work:
  `docker image prune -f` (~10GB reclaimable).
- **`einops` is required by the Nomic embedder** (used by the discarded
  knowledge-graph work) and is installed in the Dockerfile only.
- The federated test peer (`searchos-peer`, `searchos-peer-worker`) is running
  against its own database `searchos_peer`. It is **not** part of compose — it was
  created with `docker run`, so `docker compose down` will not stop it.

---

## 5. Test commands

```bash
cd services/gateway && uv run --extra test python -m pytest ../../tests -q
cd /run/media/irfan/models/SearchOS && uv run ruff check services tests tools
```

Optional integration suites (skip themselves without these):

```bash
export TEST_DATABASE_URL=postgresql+asyncpg://searchos:searchos@127.0.0.1:15432/searchos
export TEST_REDIS_URL=redis://127.0.0.1:16380/0
```

Retrieval benchmark — **not yet validated end-to-end by a maintainer**; it has
been run and produced sane numbers:

```bash
python tools/eval_retrieval.py --api-key sk_... --sources local
```

---

## 6. Things I would not trust without re-checking

- **ColBERT's blend weight (30)** was calibrated from score distributions, not
  from the benchmark. The benchmark then showed it *regresses* quality. Off by
  default; do not re-enable on the strength of the earlier commit message.
- **`semantic_cache_max_age_seconds` (330)** is clamped to
  `search_cache_ttl_seconds + 30`. Paraphrase caching therefore does not survive
  a gap beyond ~5 minutes. That is deliberate — see `bdec5a9`.
- **The knowledge graph was discarded.** Nomic-based entity resolution worked
  (0.956 vs 0.302 separation), but candidate extraction produced junk entities
  (`ORDER BY`, `Use ALTER TABLE`). It needs LLM-based extraction, not regex.
  Schema and modules were removed; `docker compose exec postgres` still has the
  tables dropped and alembic stamped at `0009`.

---

## 7. Conventions

- Commit author: `Mr-DS-ML-85 <gojmanday@gmail.com>`. No AI co-author trailers.
- Commits already pushed are never amended — add a follow-up commit.
- Prefer measuring over asserting. Several claims in this project's history were
  wrong until something was actually measured (ColBERT, the semantic threshold,
  the domain filter); both "it works" and "it's broken" have been wrong here.
- When a doc claim is written, verify it against the running instance. That
  caught four wrong claims in this session, including one that would have leaked
  other tenants' query history.
