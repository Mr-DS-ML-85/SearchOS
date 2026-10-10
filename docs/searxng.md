# SearXNG Web Fallback

SearXNG gives SearchOS a **live web fallback** that doubles as an index *seed*: every web result is saved into your local index, so the more you search, the more you own.

## Enabling

```bash
docker compose --profile web up -d searxng
```

Check `http://localhost:8080`. On first run SearXNG generates a secret key; add it to `docker-compose.yml` (environment `SEARXNG_SECRET`).

## Activation heuristic

SearchOS only falls back to the web when local coverage is thin. Two knobs:

| Variable | Default | Meaning |
|---|---|---|
| `SEARXNG_LOCAL_MIN_HITS` | `3` | if strict local results ≥ this, skip SearXNG |
| `SEARXNG_LOCAL_TTL_SECONDS` | `3600` | local results older than this no longer suppress SearXNG |

So a cold query (or one about a niche topic the index doesn't cover) hits SearXNG, while a query the index already answers well never touches the network.

## Result persistence

When **any** web provider returns results (SearXNG, Google, Tavily, Exa or Jina,
whether reached through the `web` cascade or pinned):

1. Results are returned to the caller (mixed with local results, deduped by normalized URL — see below).
2. Every result is saved as a snippet document (`source='web'`, `meta` =
   `{"searxng_snippet": true, "provider": "<tier>", "engine": "<engine>"}`; the
   flag keeps its historical name). This happens once, in the orchestrator
   (`app/search/web_persist.py`). It used to happen inside the SearXNG provider
   only, so a query answered by any other tier taught the index nothing.
3. The top `SEARCH_ENRICH_TOP_N` result URLs (default 15) are enqueued for **snippet enrichment**, which fetches each page and indexes it fully (chunks, vectors, language, SimHash) and clears the snippet flag.
4. Snippet documents not reached by enrichment get chunks and vectors from the backfill loop.

Federated results are **not** saved: they come from another SearchOS index, which owns them.

### Dedupe when saving

1. **Within one response**, by normalized URL. Two providers, or one provider
   twice, returning `https://www.example.com/a/` and `https://example.com/a` save
   one row. (A single duplicate used to fail the whole write: Postgres rejects an
   `INSERT … ON CONFLICT DO UPDATE` that touches the same row twice.)
2. **Against the index**, by the unique `normalized_url`:
   - a crawled, ingested or enriched page is **never** overwritten by a snippet;
   - an existing snippet is updated, keeping the **longer** text, so a richer
     upstream's snippet is not replaced by a terser one; `meta.provider` records
     the latest provider.
3. **Near-duplicate content** at different URLs (syndicated copies, mirrors) is
   collapsed at query time by SimHash once pages are enriched. Snippets are too
   short for SimHash to be meaningful, so they are deduped by URL only.

Check what was saved:

```sql
select meta->>'provider' as provider, count(*)
from documents where meta->>'searxng_snippet' = 'true' group by 1;
```

The next time you search for the same thing, the answers come from **your** index, and SearXNG is not contacted again (rate-limiter friendly).

```
query "milvus cognee"  →  SearXNG (took 3000ms, src=web)  →  persisted
re-query "milvus cognee"  →  3 results, all src=local, no network hit
```

## Dedup across providers

`services/gateway/app/search/fusion.py` merges provider results by **normalized URL**, the same key the index uses (`normalize_url` in `app/workers/ingest.py`):

- host lowercased, `www.` and default ports removed; scheme kept (an http-only site may differ from its https twin);
- path without a trailing slash; fragment dropped;
- query string **kept**, minus tracking parameters (`utm_*`, `gclid`, `dclid`, `fbclid`, `msclkid`, `yclid`, `igshid`, `mc_cid`, `mc_eid`, `_hsenc`, `_hsmi`, `ref_src`), sorted.

The query used to be dropped entirely, which merged distinct pages: every
`help.salesforce.com/s/articleView?id=…` article, every Hacker News `item?id=`
and every YouTube `watch?v=` became one document (the second overwrote the
first) and one search result. Migration `0010` re-keyed the 33 affected rows
and queued them for recrawl, since an overwritten row's content could belong to
a different URL than its own.

Effects:

- duplicate URLs → single entry
- an **indexed** (local/vector) copy is preferred over the live web snippet
- one web page that appears from 3 engines → one result

## Testing

```bash
# force live web
curl -s "http://localhost:8000/v1/search?q=milvus+cognee&sources=web&cache=false" -H "Authorization: Bearer $KEY"

# local only (should not touch SearXNG)
curl -s "http://localhost:8000/v1/search?q=milvus+cognee&sources=local&cache=false" -H "Authorization: Bearer $KEY"
```

Verify in Postgres:

```sql
select count(*) from documents where source = 'web';
select meta from documents where source = 'web' limit 3;
```

## Web fallback chain

`sources=web` is served by a cascade, not by SearXNG alone. Tiers are tried in
order and the first that answers wins:

1. **SearXNG** — primary
2. **Google** — only when `PROXIFY_URL` is set; see *Google through Proxify* below
3. **Tavily** — free without any account via `X-Tavily-Access-Mode: keyless`
4. **Exa** — no key needed over its hosted MCP server; the REST API returns 402 without a funded key
5. **Jina** — requires a key; `s.jina.ai` returns 401 without one
6. **federated_web** — only with `FEDERATED_ENDPOINTS` and `FEDERATED_WEB_FALLBACK` (default on): peers run the web search when every tier above is failing or rate-limited, pooling a group's web quota. Skipped on requests that themselves came from a peer. See [Pooling web search across peers](api.md#pooling-web-search-across-peers-rate-limits).

Exa is reached over its hosted **MCP** server (`mcp.exa.ai/mcp`), not its REST
API: REST returns `402 x402 payment-required` without a funded key, while the
MCP endpoint answers anonymously under a rate limit. Set `EXA_API_KEY` with
`EXA_USE_MCP=false` to use REST instead.

A tier that answers with zero results does not stop the chain: it counts as a
miss and the next tier is tried, since an empty SERP is usually a rate limit
rather than a genuine "nothing exists".

Concurrent searches run the chain in parallel. The cascade's lock used to wrap
the whole tier loop, so every live-web search in the process waited for the one
ahead of it; it now guards only the cooldown check.

Each tier the chain tried gets its own `provider_latency_ms` entry, measured
inside the cascade, so `web 16.3s` reads as e.g. `searxng 6012ms` (failed) +
`google 10240ms`. A tier that failed inside the chain used to be reported as
`0ms`, which reads as "failed instantly" even after a full timeout.

When every tier fails the cascade opens a cooldown
(`SEARCH_CASCADE_COOLDOWN_SECONDS`, default 30) and returns immediately for that
window instead of paying the timeout of each dead tier. A single successful
search clears it, so a recovered service is used again at once.

Only SearXNG and a configured key add a tier, so a stock install behaves
exactly as before. `TAVILY_KEYLESS` is the one that changes that default: it puts
a credential-free commercial tier in the chain out of the box. Set it to `false`
to require a Tavily key.

Search responses carry which tier answered, under `raw` in the provider path:

```bash
docker compose logs gateway | grep "web served by"
# web served by tavily after 2 tier(s): rust tokio async runtime scheduler
```

## Selecting one upstream

Each tier is addressable by name, so you can test or pin a single provider
without waiting for the primary to fail:

```bash
curl -s "http://localhost:8000/v1/search?q=tokio&sources=tavily&cache=false" -H "Authorization: Bearer $KEY"
curl -s "http://localhost:8000/v1/search?q=tokio&sources=exa&cache=false"     -H "Authorization: Bearer $KEY"
curl -s "http://localhost:8000/v1/search?q=tokio&sources=searxng&cache=false" -H "Authorization: Bearer $KEY"
```

Accepted values are `all`, `local`, `vector`, `web` (the cascade), `federated`,
and the tier names (`searxng`, `google`, `tavily`, `exa`, `jina`). A tier that is not configured returns an error rather than
silently falling back, so an empty result means the upstream genuinely had
none — `degraded` in the response names any tier that errored.

SearXNG intermittently returns **zero results** with no error when its own
upstream engines are suspended or rate-limited; `docker compose logs searxng`
shows the suspensions. That is exactly the case the cascade exists for: `web`
treats an empty answer as a miss and descends to the next tier.

## Google through Proxify

[Proxify](https://github.com/Mr-DS-ML-85/proxify) is a self-hosted fetch gateway
that runs a headful Chrome in a virtual display, carrying the operator's own
browser session. With `PROXIFY_URL` set, SearchOS uses it for Google web search
(a cascade tier, pinnable as `sources=google`), Google Images
(`/v1/images/search?source=google`), and as the fallback page fetcher (see the
API reference, `POST /v1/extract`).

How a Google query runs:

1. The results page is fetched in Proxify's VM browser (`PROXIFY_BROWSER`,
   default `gui_chrome`). `language`, `country`, `time_range` and the domain
   filters become `hl`, `gl`, `tbs=qdr:*` and `site:` / `-site:` operators.
2. Google now emits result links as `/goto?url=<token>`, and the token is
   encrypted: the destination only exists as the redirect target. Each kept link
   is followed once through Proxify's cheap `simple` strategy
   (`PROXIFY_RESOLVE_BROWSER`), 5 at a time. A link that does not resolve is
   dropped rather than returned as a google.com URL.

Measured: ~10.5s for the results page, ~1s per link resolution in parallel,
about 12–16s per query in total. That is why Google sits **after** SearXNG by
default; `GOOGLE_CASCADE_POSITION=first` puts it first.

**No captcha solving.** Proxify's `/fetch` runs its captcha bridge on a challenge
page unless the request carries `X-Orchestrator-Challenge-Solve: skip`. Every
request SearchOS sends carries it, and a challenge or `/sorry/` page is reported
as a failed tier, so the cascade moves on. Nothing retries a blocked page. Note
that Proxify currently forwards that header to the target as well.

### Enabling

Proxify runs as its own compose project on its own network, so the gateway and
worker join that network through an opt-in overlay:

```bash
docker compose -f docker-compose.yml -f docker-compose.proxify.yml up -d
```

The overlay sets `PROXIFY_URL=http://proxy-orchestrator:8081` and joins
`proxy-orchestrator_default` (override with `PROXIFY_URL` / `PROXIFY_NETWORK`).
Proxify must already be running, because the network is external. A plain
`docker compose up` leaves Google and the fetch fallback disabled.

| Variable | Default | Purpose |
|---|---|---|
| `PROXIFY_URL` | — | Proxify API; empty disables Google and the fetch fallback |
| `PROXIFY_BROWSER` | `gui_chrome` | strategy for Google results pages |
| `PROXIFY_RESOLVE_BROWSER` | `simple` | strategy for following `/goto` links |
| `PROXIFY_TIMEOUT_SECONDS` | `45` | budget for a results page |
| `PROXIFY_RESOLVE_CONCURRENCY` | `5` | parallel link resolutions |
| `GOOGLE_CASCADE_POSITION` | `after_searxng` | or `first` |
| `PROXIFY_FETCH_FALLBACK` | `true` | retry failed page fetches through Proxify |
| `PROXIFY_FETCH_BROWSER` | — (auto) | strategy for fallback fetches; empty lets Proxify escalate |
| `PROXIFY_FETCH_TIMEOUT_SECONDS` | `45` | budget for a fallback fetch |

## Notes

- SearXNG instances rate-limit agents that send identical queries repeatedly — the persistence + local-skip behavior exists specifically to avoid that.
- Keep SearXNG settings unchanged in `searxng/settings.yml` (`limiter: false`, json format enabled).
