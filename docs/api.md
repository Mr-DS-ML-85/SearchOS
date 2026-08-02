# API Reference

All endpoints accept `Authorization: Bearer <key>` or `X-API-Key: <key>`.

## `GET /v1/search`

| Param | Type | Default | Description |
|---|---|---|---|
| `q` | string | — | search query (required) |
| `sources` | `all` \| `local` \| `vector` \| `web` | `all` | providers to query |
| `num_results` | int 1–50 | 10 | results to return |
| `language` | string | — | e.g. `en`, `de` |
| `country` | string | — | e.g. `us` |
| `time_range` | `day` \| `week` \| `month` \| `year` \| `all` | `all` | freshness filter |
| `domains` | comma list | — | restrict to domains |
| `exclude_domains` | comma list | — | exclude domains |
| `offset` | int | 0 | pagination offset |
| `cache` | bool | `true` | Redis result cache |

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
      "score": 0.0217,
      "rank": 1,
      "published": null,
      "domain": "en.wikipedia.org"
    }
  ],
  "total": 4,
  "took_ms": 176,
  "cache_hit": false,
  "degraded": [],
  "providers": ["local", "web"]
}
```

Behavioral notes:

- `sources=web` forces a live SearXNG query (bypasses the local-skip heuristic).
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

## Ops endpoints

| Endpoint | Purpose |
|---|---|
| `GET /health` | overall status |
| `GET /health/live` | process alive |
| `GET /health/ready` | Postgres / Redis / renderer readiness |
| `GET /metrics` | Prometheus `searchos_*` metrics |
| `GET /stats` | `{documents, chunks, api_keys, ingest_pending, uptime_seconds}` |

## Errors

- `401` — missing/invalid key.
- `403` — insufficient role.
- `429` — rate limited (`Retry-After` header).
- `4xx/5xx` — JSON `{"code": "...", "message": "...", "details": null}`.

## MCP

The same functionality is exposed to agents as MCP tools (`search`, `extract`, `ingest`, `stats`, `health`) — see [MCP server](mcp.md).
