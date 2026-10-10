# Deployment

SearchOS runs on a single host with Docker Compose (dev/prod small), or split across hosts for scale.

## Single host (recommended)

```bash
cp .env.example .env   # set BOOTSTRAP_ADMIN_API_KEY
docker compose --profile web up -d --build
docker compose ps
```

### Embeddings (optional, ~3 GB)

Embeddings use `sentence-transformers` (model: `BAAI/bge-small-en-v1.5`, 384-dim) with
pgvector cosine search. They are **opt-in per image** to keep the base small:

```bash
# Enable — default. Builds torch into both images.
docker compose build && docker compose up -d

# Disable — keyword-only (adds ~3 GB back to each image)
INSTALL_EMBEDDINGS=0 docker compose build && docker compose up -d
```

**Both the gateway and the worker need it.** The worker embeds documents; the gateway
embeds the *query*. Passing the arg to only the worker lets the corpus gain vectors with
no way to compare a query against them — search silently stays lexical, `documents` keep
`status='indexed'`, and `vector_search_available` stays `false` with nothing in the
response to explain why.

Verify rather than assume:

```bash
curl -s localhost:8000/stats | grep -E 'vector_search_available|embedding_coverage|chunks_without_embedding'
```

`vector_search_available: false` with `chunks_without_embedding: 0` means the gateway
image is stale — rebuild it, not just the worker.

On an existing corpus, enabling this does not retroactively embed what is already
indexed. The worker's `backfill` loop drains the gap in the background:

```bash
docker compose logs -f worker | grep backfill
```

Tear down without losing data: `docker compose down` (keep volumes). Full reset: `docker compose down -v`.

## Health gates

```bash
curl -s http://localhost:8000/health/ready
```

`/health/ready` always returns HTTP 200 and reports component state in the
`status` and `services` fields — it does **not** block traffic. There is no
middleware that gates requests on readiness. If you need fail-fast behaviour,
have your client check `status == "ok"`; a gateway with Postgres down returns
200 from the probes and then fails on the first real query.

`/health/live` is the process-level liveness probe used by the Docker
`HEALTHCHECK`. `/stats` and `/metrics` require an API key when
`RATE_LIMIT_HEALTH_ENDPOINTS=true` (the default) because `/stats` runs `COUNT(*)`
over tables that can hold millions of rows.

## Backups

Postgres volume is the only state you can't rebuild.

```bash
docker compose exec postgres pg_dump -U searchos searchos > searchos-$(date +%F).sql
```

WAL/periodic dumps and offsite copies are strongly recommended.

## Scaling

- **Workers**: bump `docker compose scale worker=N`. Jobs are claimed with `SELECT ... FOR UPDATE SKIP LOCKED`, so N workers split the queue safely.
- **Gateway**: stateless — run multiple behind a load balancer; sessions live in Redis (cache + rate limits) and Postgres.
- **Renderer**: scale the Chromium pool independently.
- **SearXNG**: run multiple instances behind a load balancer and point `SEARXNG_URL` at the LB for higher web-fallback throughput.

## Reverse proxy (TLS)

Terminate TLS in front of the gateway, e.g. Caddy:

```
searchos.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

## Metrics & monitoring

- Prometheus scrapes `GET /metrics` (default `:8000/metrics`).
- Grafana ships in the compose stack at `:3000` (admin/admin — change it).
- Dashboards expose `searchos_documents_indexed_total`, ingest queue depth, latency histograms, and cache hit rate.

## Common failure modes

| Symptom | Cause / fix |
|---|---|
| `/health/ready` not ready | Postgres/Redis down or wrong host — check `DATABASE_URL` / `REDIS_URL` |
| SearXNG 429s | you hit a live instance repeatedly; SearXNG saves results so re-queries go local |
| worker idle with pending jobs | restart worker; check `worker.log` for claim errors |
| renderer missing | ingest falls back to plain fetch; install the renderer for heavy-JS sites |
| cold-start search latency | first SearXNG hit is slow (2–3s); subsequent same-query hits are instant + cached |

## Local dev (no Docker hostnames)

Config defaults point at Docker service names (`postgres`, `redis`) which don't resolve on the host. For local runs, pass explicit env:

```bash
export DATABASE_URL=postgresql+asyncpg://searchos:searchos@localhost:5432/searchos
export REDIS_URL=redis://localhost:6379/0
export SEARXNG_URL=http://localhost:8080
export BOOTSTRAP_ADMIN_API_KEY=sk_...
```
