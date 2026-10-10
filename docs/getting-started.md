# Getting Started

This guide walks you from an empty machine to a working SearchOS install with an MCP server your agents can talk to.

## 1. Prerequisites

- Docker + Docker Compose
- ~4 GB RAM for the compose stack (plus optional headless renderer)

## 2. Configure

```bash
git clone https://github.com/Mr-DS-ML-85/SearchOS.git searchos
cd searchos
cp .env.example .env
```

Generate a strong admin key and put it in `.env`:

```bash
python3 -c "import secrets; print('sk_' + secrets.token_urlsafe(32))"
# paste as BOOTSTRAP_ADMIN_API_KEY=sk_... in .env
```

## 3. Start the stack

```bash
docker compose up -d --build
```

This starts:

| Service | Host port | Notes |
|---|---|---|
| gateway | 8000 | FastAPI API (runs migrations) — bound to 127.0.0.1 |
| worker | — | ingest + crawler + snippet enrichment; metrics on 9101 (internal) |
| postgres | 5432 | pgvector, FTS index, jobs — bound to 127.0.0.1 |
| redis | 16379 | cache, rate limits, queues — bound to 127.0.0.1 |
| renderer | — | optional headless Chromium for JS pages; **not** published |
| grafana / prometheus | 3000 / 9090 | bound to 127.0.0.1 |

The renderer is deliberately not published to the host. `/render` accepts an
arbitrary URL, so publishing it would expose an open SSRF proxy into the
compose network. The gateway reaches it internally as `http://renderer:8010` and
authenticates with the shared `RENDERER_TOKEN`.

The default compose file now **requires** these to be set in `.env`:
`POSTGRES_PASSWORD`, `RENDERER_TOKEN`, `BOOTSTRAP_ADMIN_API_KEY`,
`GRAFANA_ADMIN_PASSWORD`, `SEARXNG_SECRET`. `BOOTSTRAP_ADMIN_API_KEY` must be at
least 16 characters and must not still start with `change-me` — the gateway
refuses to start otherwise rather than coming up silently locked out.

### Optional: SearXNG live-web fallback

```bash
docker compose --profile web up -d searxng
```

SearXNG is the primary web tier. Tavily joins the cascade with no credentials at all
(keyless access), so a fresh install already has a fallback; Exa and Jina add more depth
with a key. See [Web fallback chain](searxng.md).

To also serve the MCP endpoint over HTTP:

```bash
echo "MCP_HTTP_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')" >> .env
docker compose --profile mcp up -d
```

See [SearXNG fallback](searxng.md) for how it saves results into your index.

## 4. Create API keys

```bash
curl -s -X POST http://localhost:8000/v1/admin/keys \
  -H "Authorization: Bearer $BOOTSTRAP_ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name":"ci-bot","role":"user"}'
```

Save the returned `sk_...` (shown once).

## 5. Seed your index

```bash
# single page
curl -s -X POST http://localhost:8000/v1/admin/ingest \
  -H "Authorization: Bearer $BOOTSTRAP_ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://en.wikipedia.org/wiki/Vector_database","kind":"url"}'

# entire sitemap
curl -s -X POST http://localhost:8000/v1/admin/ingest \
  -H "Authorization: Bearer $BOOTSTRAP_ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com/sitemap.xml","kind":"sitemap"}'
```

The worker fetches (robots.txt-aware), extracts, chunks, embeds, indexes, and **follows nested same-domain links** up to `CRAWLER_MAX_DEPTH`.

## 6. Search

```bash
curl -s "http://localhost:8000/v1/search?q=vector+database&num_results=5" \
  -H "Authorization: Bearer sk_..."
```

Operators are supported: `site:example.com`, `-site:spam.com`, `filetype:pdf`,
`"exact phrase"`, and plain-term `OR`.

### Find images

Images indexed while crawling are searchable by what they depict, via CLIP:

```bash
curl -s -G "http://localhost:8000/v1/images/search" \
  --data-urlencode "q=a white arctic fox in snow" \
  -H "Authorization: Bearer sk_..."
```

Pass `image_url=` instead of `q=` to find images like another image. Both
directions are the same endpoint because CLIP embeds text and images into one
space.

Check `image_search_available` in the response — `false` means the `images`
extra (Pillow) is not installed, which is a degraded mode rather than an empty
index.

> Both endpoints lazy-load their model on first use, so the first query after a
> fresh start is slow (seconds) and later ones are fast. Weights are cached on
> the `modelcache` volume, so this cost is paid once per rebuild, not per query.

## 7. Connect an agent via MCP

```bash
cd services/mcp
python -m pip install -r requirements.txt

export SEARCHOS_API_KEY=sk_...            # user key
export SEARCHOS_ADMIN_KEY=$BOOTSTRAP_ADMIN_API_KEY   # only needed for ingest tool

# local stdio mode
python searchos_mcp.py --transport stdio

# remote mode (Streamable HTTP + legacy SSE)
python searchos_mcp.py --port 8099
```

Point Claude Desktop / Cursor / opencode / any MCP client at:

- modern clients → `http://localhost:8099/mcp`
- legacy clients → `http://localhost:8099/sse`

See [MCP server](mcp.md) for the full story, including the 2026-07-28 stateless spec.
