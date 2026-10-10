# SearchOS MCP Server

MCP (Model Context Protocol) server exposing SearchOS to AI agents, on all three transports,
built on the official `mcp` Python SDK **2.0.0** (supports the modern stateless 2026-07-28 spec
*and* the classic handshake era).

## Tools

| Tool            | Calls                          | Description |
|-----------------|--------------------------------|-------------|
| `search`        | `GET /v1/search`               | FTS + vector + web cascade; `sources="google"` pins Google (needs Proxify). Returns `next_cursor` for paging |
| `search_images` | `GET /v1/images/search`        | CLIP text→image (`query`) or image→image (`image_url`); `source="google"` for Google Images |
| `extract`       | `POST /v1/extract`             | fetch + clean a page; `fetched_via` says whether Proxify served it |
| `suggest`       | `GET /v1/suggest`              | prefix completions |
| `popular`       | `GET /v1/popular`              | most-run queries |
| `recent`        | `GET /v1/recent`               | recently (re)crawled documents |
| `ingest`        | `POST /v1/admin/ingest`        | seed URL / sitemap (admin key) |
| `cache_flush`   | `POST /v1/admin/cache/flush`   | drop cached results (admin key) |
| `stats`         | `GET /stats`                   | index, queue and embedding-coverage stats |
| `health`        | `GET /health`                  | liveness/readiness |

## Run

```bash
pip install -r requirements.txt

export SEARCHOS_API_KEY=sk_...            # user key
export SEARCHOS_ADMIN_KEY=sk_...          # admin key (only needed for ingest and cache_flush)

# stdio mode
python searchos_mcp.py --transport stdio

# HTTP server (Streamable HTTP + SSE on one port)
python searchos_mcp.py --port 8099
```

## Transports

- Streamable HTTP (stateless, 2026-07-28 spec): `POST /mcp`
- Legacy HTTP + SSE: `GET /sse`, `POST /messages`
- stdio

A stdio server keeps the code it started with: after editing `searchos_mcp.py`,
restart it from the host app and open a new session (sessions cache the tool list).

## Client configs

See [`examples/`](examples/) for Claude Desktop, Cursor, and stateless-Streamable-HTTP configs.
Full docs: [`docs/mcp.md`](../../docs/mcp.md).
