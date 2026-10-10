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

When SearXNG returns results for a query:

1. Results are returned to the caller (mixed with local results, deduped by normalized URL — see below).
2. The top-5 result URLs are enqueued for **snippet enrichment**, and the page itself is persisted as `source='web'` in `documents` (with metadata `{"searxng_snippet": true, "engine": "<engine>"}`).
3. The worker may fully crawl a web result, upgrading it to a first-class indexed document.

The next time you search for the same thing, the answers come from **your** index, and SearXNG is not contacted again (rate-limiter friendly).

```
query "milvus cognee"  →  SearXNG (took 3000ms, src=web)  →  persisted
re-query "milvus cognee"  →  3 results, all src=local, no network hit
```

## Dedup across providers

`services/gateway/app/search/fusion.py` merges provider results by **normalized URL** (lowercased host with any `www.` prefix and default port removed, path without a trailing slash; query string and fragment dropped):

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

## Notes

- SearXNG instances rate-limit agents that send identical queries repeatedly — the persistence + local-skip behavior exists specifically to avoid that.
- Keep SearXNG settings unchanged in `searxng/settings.yml` (`limiter: false`, json format enabled).
