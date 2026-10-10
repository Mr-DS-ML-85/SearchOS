# MCP Server

`services/mcp/searchos_mcp.py` exposes SearchOS to AI agents as MCP tools, on **all three transports**, and is built against the official `mcp` Python SDK **2.0.0** — which supports the modern **stateless** protocol (2026-07-28) *and* the classic handshake era.

## Tools

| Tool | Calls | Description |
|---|---|---|
| `search` | `GET /v1/search` | full search pipeline (FTS + vector + web fallback) |
| `extract` | `POST /v1/extract` | fetch + clean a page (markdown/text/json) |
| `ingest` | `POST /v1/admin/ingest` | seed a URL or sitemap (needs admin key) |
| `stats` | `GET /stats` | index + queue + uptime stats |
| `health` | `GET /health` | liveness/readiness |

## Running

```bash
cd services/mcp
pip install -r requirements.txt

# 1. stdio — point Claude Desktop / Cursor / opencode at the command
export SEARCHOS_API_KEY=sk_...  SEARCHOS_ADMIN_KEY=sk_...
python searchos_mcp.py --transport stdio

# 2. HTTP server — all transports on one port
python searchos_mcp.py --port 8099
```

### Transport map on `:8099`

| Transport | Endpoint | Clients |
|---|---|---|
| Streamable HTTP (stateless) | `POST /mcp` | modern clients (2026-07-28 spec) |
| HTTP + SSE (legacy) | `GET /sse` + `POST /messages?session_id=...` | older MCP clients |
| stdio | `python searchos_mcp.py --transport stdio` | local agents |

## Client configs

### Claude Desktop — `claude_desktop_config.json`

```json
{
  "mcpServers": {
    "searchos": {
      "command": "python",
      "args": ["/path/to/services/mcp/searchos_mcp.py", "--transport", "stdio"],
      "env": { "SEARCHOS_API_KEY": "sk_..." }
    }
  }
}
```

### Cursor — `.mcp.json` (remote, SSE)

```json
{
  "mcpServers": {
    "searchos": {
      "url": "http://localhost:8099/sse"
    }
  }
}
```

### Modern client (stateless Streamable HTTP)

```json
{
  "mcpServers": {
    "searchos": {
      "url": "http://localhost:8099/mcp"
    }
  }
}
```

## The 2026-07-28 stateless spec

MCP's July 2026 spec revision made the protocol stateless over HTTP (full changelog: modelcontextprotocol.io):

- `initialize` / `notifications/initialized` **removed**
- sessions and `Mcp-Session-Id` **removed** from Streamable HTTP
- new `server/discover` probe returns capabilities + version negotiation
- every result carries `resultType` (`complete` | `input_required`)
- SSE resumability removed; legacy HTTP+SSE stays for a 12-month deprecation window

SDK 2.0's `Server` runs a dual-era loop, so one instance serves both handshake-era and modern clients. Verified live:

- `POST /mcp` → `server/discover` → `result_type="complete"`, `supported_versions=["2026-07-28"]`
- `tools/list`, `tools/call` (search/health/stats/extract) work over both eras

## Env vars

| Variable | Default | Purpose |
|---|---|---|
| `SEARCHOS_URL` | `http://127.0.0.1:8000` | gateway base URL |
| `SEARCHOS_API_KEY` | — | user key for `search`/`extract`/`stats`/`health` |
| `SEARCHOS_ADMIN_KEY` | — | admin key; only needed for `ingest` tool |
| `MCP_HOST` | `127.0.0.1` | bind address for the HTTP transports |
| `MCP_PORT` | `8099` | port for the HTTP transports |
| `MCP_HTTP_TOKEN` | — | bearer token required by `/mcp`, `/sse`, `/messages/` |
| `MCP_STATELESS_HTTP` | `true` | run Streamable HTTP without sessions |
| `GATEWAY_TIMEOUT` | `60` | seconds before a gateway call is abandoned |

The `ingest` tool returns an explicit error if no admin key is set.

### Authentication on the HTTP transports

`/mcp`, `/sse` and `/messages/` expose the full tool set, including `ingest`,
which enqueues arbitrary-URL crawls. They require `Authorization: Bearer
$MCP_HTTP_TOKEN` when it is set.

Binding to a non-loopback `MCP_HOST` **without** `MCP_HTTP_TOKEN` is refused at
startup. To enable remote agents, set both:

```bash
export MCP_HOST=0.0.0.0
export MCP_HTTP_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
```

stdio transport is unaffected — it is a local process, not a socket.

### Naming note

The gateway base URL variable is `SEARCHOS_URL`. An earlier revision of this
document called it `SEARCHOS_API_URL`, which the server never read, so a
following setup silently pointed the MCP server at the wrong address.

See also: [API reference](api.md) · [deployment](deployment.md)
