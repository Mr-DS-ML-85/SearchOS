# MCP Server

`services/mcp/searchos_mcp.py` exposes SearchOS to AI agents as MCP tools, on **all three transports**, and is built against the official `mcp` Python SDK **2.0.0** — which supports the modern **stateless** protocol (2026-07-28) *and* the classic handshake era.

## Tools

| Tool | Calls | Description |
|---|---|---|
| `search` | `GET /v1/search` | full search pipeline (FTS + vector + web fallback); `sources="google"` pins Google via Proxify |
| `extract` | `POST /v1/extract` | fetch + clean a page (markdown/text/json); falls back to Proxify when configured, reported as `fetched_via` |
| `ingest` | `POST /v1/admin/ingest` | seed a URL or sitemap (needs admin key) |
| `stats` | `GET /stats` | index + queue + uptime stats |
| `health` | `GET /health` | liveness/readiness |
| `search_images` | `GET /v1/images/search` | text-to-image (`query`) or image-to-image (`image_url`) via CLIP; `source="google"` for Google Images via Proxify |
| `suggest` | `GET /v1/suggest` | prefix completions from indexed titles |
| `popular` | `GET /v1/popular` | most-run queries from the live Redis counter |
| `recent` | `GET /v1/recent` | documents the crawler indexed or re-crawled recently |
| `cache_flush` | `POST /v1/admin/cache/flush` | drop cached results so a fresh ingest is visible at once (needs admin key) |

Four gateway routes are deliberately **not** exposed as tools:

| Route | Why not |
|---|---|
| `/health/live`, `/health/ready` | orchestrator probes; `/health` covers an agent |
| `/metrics` | Prometheus scrape endpoint, not agent-facing |
| `/v1/admin/keys` | key minting is a privilege an agent should not self-grant |

Everything else is wrapped. `ingest` and `cache_flush` both act on the index and
both refuse to run without `SEARCHOS_ADMIN_KEY`; `cache_flush` in particular
discards every cached response, so an unauthenticated caller could otherwise
force re-queries across the whole corpus.

The MCP server is a **translator**, not a second API: the gateway exposes no MCP
endpoint itself. Every tool above is a thin wrapper over one REST route, so
anything added to the gateway needs adding here too or agents cannot reach it.

### Parity with the REST API

Every gateway route is either an MCP tool or deliberately excluded (the table
above), and every query or body parameter of a wrapped route is accepted by its
tool. `freshness` is the one omission: it is an alias of `time_range`, which the
tool takes. Auth headers come from the server's configured keys.

The `search` tool passes the gateway's response through unchanged, results
included, so an MCP caller sees exactly the fields an API caller does
(`next_cursor`, `min_score`, `cache_type`, the semantic-cache diagnostics,
per-result `favicon` and `raw`, …). It used to build its output from a
hand-kept whitelist, which fell behind the gateway field by field: `next_cursor`
was dropped, so `has_more: true` came with no way to ask for the next page.

`tests/test_api_mcp_parity.py` derives all of this from the gateway's OpenAPI
schema and fails when a new route, parameter or response field is not reachable
through MCP, so the two cannot drift silently again.

### Paging

Page by passing `next_cursor` back as `cursor`; combining `cursor` with `offset`
is an error, and `offset` is `null` on a cursor page because the cursor carries
its own.

`search_images` takes exactly one of `query` and `image_url`. The schema cannot
say so: Anthropic's tool API rejects a top-level `anyOf`, so the handler enforces
it and names the missing or conflicting argument. Its result carries `source`,
`min_score` and `below_min_score` (see the API reference).

### Reloading after a change

A stdio MCP server is a process the host app spawned; it keeps the code it
started with. After editing `searchos_mcp.py`, restart the server from the app
(or restart the app) **and** open a new session, because a session also caches
the tool list. Restarting the `searchos-mcp` container only reloads the HTTP
transport. `ps -eo pid,lstart,args | grep searchos_mcp` shows when each running
copy started.

The `search` tool's `sources` enum is asserted in `tests/test_mcp.py` against the
gateway's own `SearchSource`, because it previously drifted: MCP listed four
values while the gateway accepted nine (ten now, with `google`), and an agent asking for
`sources="federated"` received a validation error instead of results.

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

# or stdio, which needs no MCP token and no MCP container. The gateway keys go
# in -e; the script has no shebang, so run it with the venv's python.
claude mcp add --scope user searchos \
  -e SEARCHOS_URL=http://127.0.0.1:8000 \
  -e SEARCHOS_API_KEY=sk_user_... \
  -e SEARCHOS_ADMIN_KEY=sk_admin_... \
  -- /path/to/.venv/bin/python /path/to/services/mcp/searchos_mcp.py --transport stdio

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

`SEARCHOS_ADMIN_KEY` is what enables `ingest` and `cache_flush`; without it
those two tools return an explanatory error while the rest work. See
[API keys](#api-keys).

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
- `tools/list`, `tools/call` (all ten tools) work over both eras

## Env vars

| Variable | Default | Purpose |
|---|---|---|
| `SEARCHOS_URL` | `http://127.0.0.1:8000` | gateway base URL |
| `SEARCHOS_API_KEY` | — | gateway key sent by every non-admin tool: `search`, `search_images`, `extract`, `suggest`, `popular`, `recent`, `stats`, `health` |
| `SEARCHOS_ADMIN_KEY` | — | admin gateway key, used **only** by `ingest` and `cache_flush` |
| `MCP_HOST` | `127.0.0.1` | bind address for the HTTP transports |
| `MCP_PORT` | `8099` | port for the HTTP transports |
| `MCP_HTTP_TOKEN` | — | bearer token required by `/mcp`, `/sse`, `/messages/` |
| `MCP_STATELESS_HTTP` | `true` | run Streamable HTTP without sessions |
| `GATEWAY_TIMEOUT` | `60` | seconds before a gateway call is abandoned |

`ingest` and `cache_flush` return an explicit error if no admin key is set; the
other tools keep working.

### API keys

The MCP server holds two **gateway** keys and sends them on the agent's behalf;
the agent never sees them.

| Key | Env var | Role | Used by |
|---|---|---|---|
| user key | `SEARCHOS_API_KEY` | `user` | `search`, `search_images`, `extract`, `suggest`, `popular`, `recent`, `stats`, `health` |
| admin key | `SEARCHOS_ADMIN_KEY` | `admin` | `ingest`, `cache_flush` |

Mint a user key with the admin key (the raw key is returned once):

```bash
curl -s -X POST http://localhost:8000/v1/admin/keys \
  -H "Authorization: Bearer $BOOTSTRAP_ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name":"mcp-agent","role":"user"}'
```

Give the MCP server a **user** key for `SEARCHOS_API_KEY`, and set
`SEARCHOS_ADMIN_KEY` only if agents should be able to crawl and flush the cache.
Leaving it unset disables exactly those two tools.

**Where the keys go depends on the transport:**

- **stdio**: the client spawns the server, so the keys go in the client's
  config: `-e` flags for `claude mcp add`, or the `env` block in
  `claude_desktop_config.json` (both shown below).
- **HTTP (`/mcp`, `/sse`)**: the keys live in the MCP **server's** environment.
  The client sends only `Authorization: Bearer $MCP_HTTP_TOKEN`, which admits it
  to the MCP server; it is not a gateway key.

**Docker Compose default:** `docker-compose.yml` sets
`SEARCHOS_API_KEY=${SEARCHOS_API_KEY:-${BOOTSTRAP_ADMIN_API_KEY}}` and
`SEARCHOS_ADMIN_KEY=${BOOTSTRAP_ADMIN_API_KEY}`. With no `SEARCHOS_API_KEY` in
`.env`, the MCP container therefore runs *every* tool with the bootstrap admin
key. For least privilege, put a minted user key in `.env` as
`SEARCHOS_API_KEY`; the admin key is then used only for `ingest` and
`cache_flush`.

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
