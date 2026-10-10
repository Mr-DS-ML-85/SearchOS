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

The two Claude clients differ in ways that are not obvious from the failure
messages, so they get separate sections.

| | stdio | Streamable HTTP `/mcp` |
|---|---|---|
| Claude Code (CLI) | yes | yes, natively |
| Claude Desktop | yes | needs a bridge — see below |
| Cursor / opencode | yes | varies by version |

### Claude Code (CLI)

Claude Code speaks Streamable HTTP directly, so no bridge is needed. Use the CLI
rather than hand-editing `~/.claude.json` — it keeps the file consistent and it
prints a connection check.

```bash
# Streamable HTTP, with the bearer token
claude mcp add --scope user --transport http searchos \
  http://127.0.0.1:8099/mcp \
  --header "Authorization: Bearer $MCP_HTTP_TOKEN"

# or stdio, which needs no token and no running container
claude mcp add --scope user searchos -- \
  /path/to/services/mcp/searchos_mcp.py --transport stdio

claude mcp list        # shows "✔ Connected" once it can reach the server
```

`--scope` is `local` (this project only, the default), `user` (all your
projects, stored in `~/.claude.json`), or `project` (checked-in `.mcp.json`,
shared with collaborators). Multiple `--header` flags are allowed; the bearer
token is the only one SearchOS needs.

If the container is not running, `claude mcp list` reports the server as failed
rather than silently omitting it, which makes a stopped container easy to tell
apart from a bad key.

### Claude Desktop — `claude_desktop_config.json`

Location: `~/.config/Claude/claude_desktop_config.json` on Linux,
`~/Library/Application Support/Claude/` on macOS.

**Desktop accepts only the stdio shape** (`command` + `args` + `env`). It does
not accept the `{"type": "http", "url": ...}` form, and it does not just
report the problem — it **deletes the entry from the config file**, so a
mistake takes the working server with it. Back the file up before editing.

The simplest configuration needs nothing but Python:

```json
{
  "mcpServers": {
    "searchos": {
      "command": "/path/to/.venv/bin/python",
      "args": ["/path/to/services/mcp/searchos_mcp.py", "--transport", "stdio"],
      "env": {
        "SEARCHOS_URL": "http://127.0.0.1:8000",
        "SEARCHOS_API_KEY": "sk_...",
        "SEARCHOS_ADMIN_KEY": "sk_..."
      }
    }
  }
}
```

`SEARCHOS_ADMIN_KEY` is what enables the `ingest` tool; without it that one tool
returns an explanatory error while the rest work.

#### Reaching `/mcp` from Desktop

To use the container's HTTP endpoint rather than stdio, bridge it with
[`mcp-remote`](https://www.npmjs.com/package/mcp-remote):

```bash
npm install -g mcp-remote
```

```json
{
  "mcpServers": {
    "searchos": {
      "command": "/home/you/.nvm/versions/node/vXX/bin/node",
      "args": [
        "/home/you/.nvm/versions/node/vXX/lib/node_modules/mcp-remote/dist/proxy.js",
        "http://127.0.0.1:8099/mcp",
        "--header", "Authorization: Bearer sk_..."
      ],
      "env": {
        "SEARCHOS_URL": "http://127.0.0.1:8000",
        "SEARCHOS_API_KEY": "sk_...",
        "SEARCHOS_ADMIN_KEY": "sk_..."
      }
    }
  }
}
```

Three traps, all of which show up as an immediate "Failed":

- **Do not launch it with `npx`.** `npx` is a symlink whose shebang is
  `#!/usr/bin/env node`. Desktop launches GUI apps with a minimal `PATH` that
  omits your Node installation, so npx dies instantly with
  `env: 'node': No such file or directory` — even when `command` points at npx's
  absolute path, because the *interpreter* is still resolved through `PATH`.
  Invoke `node` by absolute path instead.
- **Install `mcp-remote` globally first.** `npx -y mcp-remote@latest` refetches
  the package on every launch, which can exceed Desktop's connection timeout and
  reads as a connection failure.
- **Both paths are pinned to one Node version.** Upgrading Node or running
  `nvm use` elsewhere breaks the entry silently. Update `command` and the
  `proxy.js` argument together.

Desktop rewrites this file whenever it dislikes an entry, so after restarting it
is worth confirming the `searchos` block is still present before assuming the
server is down.

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

Claude Code reads these `url`-shaped entries directly; Claude Desktop does not,
and will drop them.

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
