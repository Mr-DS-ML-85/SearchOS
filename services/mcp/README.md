# SearchOS MCP Server

MCP (Model Context Protocol) server exposing SearchOS to AI agents, on all three transports,
built on the official `mcp` Python SDK **2.0.0** (supports the modern stateless 2026-07-28 spec
*and* the classic handshake era).

## Tools

| Tool      | Calls                    | Description                        |
|-----------|--------------------------|------------------------------------|
| `search`  | `GET /v1/search`         | FTS + vector + web fallback search |
| `extract` | `POST /v1/extract`       | fetch + clean a page               |
| `ingest`  | `POST /v1/admin/ingest`  | seed URL / sitemap (admin key)     |
| `stats`   | `GET /stats`             | index + queue stats                |
| `health`  | `GET /health`            | liveness/readiness                 |

## Run

```bash
pip install -r requirements.txt

export SEARCHOS_API_KEY=sk_...            # user key
export SEARCHOS_ADMIN_KEY=sk_...          # admin key (only needed for ingest)

# stdio mode
python searchos_mcp.py --transport stdio

# HTTP server (Streamable HTTP + SSE on one port)
python searchos_mcp.py --port 8099
```

## Transports

- Streamable HTTP (stateless, 2026-07-28 spec): `POST /mcp`
- Legacy HTTP + SSE: `GET /sse`, `POST /messages`
- stdio

## Client configs

See [`examples/`](examples/) for Claude Desktop, Cursor, and stateless-Streamable-HTTP configs.
Full docs: [`docs/mcp.md`](../../docs/mcp.md).
