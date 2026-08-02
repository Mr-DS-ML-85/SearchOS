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

| Service | Port | Notes |
|---|---|---|
| gateway | 8000 | FastAPI API (migrations auto-run) |
| worker | — | ingest + crawler + snippet enrichment |
| postgres | 5432 | pgvector, FTS index, jobs |
| redis | 6379 | cache, rate limits, queues |
| renderer | 8040 | optional headless Chromium for JS pages |
| grafana / prometheus | 3000 / 9090 | metrics |

### Optional: SearXNG live-web fallback

```bash
docker compose --profile web up -d searxng
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
