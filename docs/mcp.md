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
| `SEARCHOS_API_URL` | `http://localhost:8000` | gateway base URL |
| `SEARCHOS_API_KEY` | — | user key for `search`/`extract`/`stats`/`health` |
| `SEARCHOS_ADMIN_KEY` | — | admin key; only needed for `ingest` tool |

The `ingest` tool returns an explicit error if no admin key is set.
