"""SearchOS MCP server — exposes the SearchOS search API to AI agents.

Speaks all MCP transports off the same server instance:

1.  stdio          — local process (Claude Desktop, Cursor, Claude Code, opencode)
2.  HTTP + SSE     — legacy remote transport (``GET /sse`` + ``POST /messages``),
                     deprecated by the spec but still supported for 12+ months
3.  Streamable HTTP — modern remote transport (``POST /mcp``), run in stateless
                     mode per the 2026-07-28 spec (no sessions, no initialize
                     handshake; version + capabilities travel in request ``_meta``)

The SDK 2.x ``Server`` serves both protocol eras from one instance, so legacy
handshake-era clients and new stateless ``server/discover`` clients both work.

Run:
    python searchos_mcp.py                    # Streamable HTTP + legacy SSE on :8099
    python searchos_mcp.py --transport stdio  # stdio mode for MCP host apps
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

import httpx
import mcp_types as types
import uvicorn
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.server import ServerRequestContext
from mcp.server.sse import SseServerTransport
from mcp.server.stdio import stdio_server
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

# docs/mcp.md and examples/claude_desktop_config.json used to document
# SEARCHOS_API_URL with default port 8010 (the renderer's port), so following
# the docs produced a server that could not reach the gateway. The correct name
# is SEARCHOS_URL and the gateway listens on 8000.
SEARCHOS_URL = os.environ.get("SEARCHOS_URL", "http://127.0.0.1:8000")
SEARCHOS_API_KEY = os.environ.get("SEARCHOS_API_KEY", "")
SEARCHOS_ADMIN_KEY = os.environ.get("SEARCHOS_ADMIN_KEY", "")
MCP_HOST = os.environ.get("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("MCP_PORT", "8099"))
STATELESS = os.environ.get("MCP_STATELESS_HTTP", "true").lower() == "true"
# Shared secret required on the HTTP transports. Without it, /mcp, /sse and
# /messages/ served the full tool set — including admin-keyed `ingest` — to
# anyone who could reach the port.
# Every other setting in this process is SEARCHOS_-prefixed (SEARCHOS_API_KEY,
# SEARCHOS_GATEWAY_URL, ...), so setting SEARCHOS_MCP_HTTP_TOKEN out of habit
# silently produced an empty token and a completely unauthenticated HTTP
# transport. Accept both spellings so that mistake cannot disable auth.
MCP_HTTP_TOKEN = os.environ.get("MCP_HTTP_TOKEN") or os.environ.get("SEARCHOS_MCP_HTTP_TOKEN", "")
GATEWAY_TIMEOUT = float(os.environ.get("GATEWAY_TIMEOUT", "60"))

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


class GatewayError(Exception):
    pass


async def gateway_call(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    key: str | None = None,
) -> Any:
    api_key = key if key is not None else SEARCHOS_API_KEY
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    url = f"{SEARCHOS_URL}{path}"
    timeout = httpx.Timeout(GATEWAY_TIMEOUT, connect=10.0)
    # Drop empty params so `?language=` is not sent; httpx would otherwise pass
    # an empty string through, which the gateway treats as a real value.
    clean_params = {k: v for k, v in (params or {}).items() if v not in (None, "", [], {})}
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        try:
            if method == "GET":
                resp = await client.get(url, params=clean_params, headers=headers)
            else:
                resp = await client.post(url, json=json_body or {}, headers=headers)
        except httpx.HTTPError as e:
            raise GatewayError(f"gateway unreachable: {e.__class__.__name__}: {e}") from e
    try:
        body = resp.json()
    except ValueError:
        body = {"status_code": resp.status_code, "text": resp.text[:500]}
    if resp.status_code >= 400:
        # The gateway now returns a single flat envelope; older builds nested it
        # under "detail". Handle both.
        detail: Any = body
        if isinstance(body, dict):
            inner = body.get("detail")
            detail = body.get("message") or (inner.get("message") if isinstance(inner, dict) else inner) or body
        raise GatewayError(f"gateway {resp.status_code} {method} {path}: {detail}")
    return body


def text(content: str) -> types.TextContent:
    return types.TextContent(type="text", text=content)


def text_result(content: str, *, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(content=[text(content)], is_error=is_error)


def _search_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "search query (required)"},
            "sources": {
                "type": "string",
                "enum": ["all", "local", "vector", "web"],
                "description": "which providers to query; 'web' forces live SearXNG",
            },
            "num_results": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            "language": {"type": "string", "description": "e.g. 'en'"},
            "country": {"type": "string", "description": "e.g. 'us'"},
            "time_range": {
                "type": "string",
                "enum": ["day", "week", "month", "year", "all"],
                "description": "freshness filter",
            },
            "domains": {
                "type": "array",
                "items": {"type": "string"},
                "description": "restrict results to these domains",
            },
            "exclude_domains": {
                "type": "array",
                "items": {"type": "string"},
                "description": "exclude these domains",
            },
            "offset": {"type": "integer", "default": 0},
            "cache": {"type": "boolean", "default": True, "description": "use the Redis result cache"},
        },
        "required": ["query"],
    }


def _extract_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "page URL (required)"},
            "format": {
                "type": "string",
                "enum": ["markdown", "text", "json"],
                "default": "markdown",
            },
            "max_chars": {"type": "integer", "description": "truncate content"},
        },
        "required": ["url"],
    }


def _ingest_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL or sitemap to crawl (required)"},
            "kind": {"type": "string", "enum": ["url", "sitemap"], "default": "url"},
        },
        "required": ["url"],
    }


def _tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="search",
            description=(
                "Search the SearchOS index + live web. Queries first hit the local "
                "FTS/vector index; if local coverage is thin, SearXNG live results are "
                "merged, saved, and re-crawled into the index so repeat queries are served "
                "locally. Returns JSON results with title, url, snippet, source and score."
            ),
            input_schema=_search_schema(),
        ),
        types.Tool(
            name="extract",
            description=(
                "Fetch a URL and return clean, LLM-ready content (markdown/text/json). "
                "Respects robots.txt, handles HTML, PDF, and JS-rendered pages."
            ),
            input_schema=_extract_schema(),
        ),
        types.Tool(
            name="ingest",
            description=(
                "Enqueue a URL or sitemap into the crawler. The worker fetches it "
                "(robots.txt-aware), extracts, chunks, embeds and indexes it; it also "
                "follows nested same-domain links up to the configured depth (recursive index). "
                "Requires the admin API key."
            ),
            input_schema=_ingest_schema(),
        ),
        types.Tool(
            name="stats",
            description="Index statistics: document/chunk counts, pending ingest jobs, API keys.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        ),
        types.Tool(
            name="health",
            description="Gateway health: postgres/redis/renderer status and service version.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        ),
    ]


async def handle_search(ctx: ServerRequestContext, params: types.CallToolRequestParams) -> types.CallToolResult:
    args = params.arguments or {}
    query = args.get("query")
    if not query or not str(query).strip():
        return text_result("query is required and must not be blank", is_error=True)
    # `0 or 10` silently turned num_results=0 into the default, so a caller
    # asking for zero results got ten. Treat 0 as a client error instead.
    raw_n = args.get("num_results")
    if raw_n is not None and not isinstance(raw_n, bool):
        try:
            num_results = int(raw_n)
        except (TypeError, ValueError):
            return text_result(f"num_results must be an integer, got {raw_n!r}", is_error=True)
        if num_results < 1 or num_results > 50:
            return text_result(f"num_results must be between 1 and 50, got {num_results}", is_error=True)
    else:
        num_results = 10

    raw_offset = args.get("offset")
    if raw_offset is not None and not isinstance(raw_offset, bool):
        try:
            offset = int(raw_offset)
        except (TypeError, ValueError):
            return text_result(f"offset must be an integer, got {raw_offset!r}", is_error=True)
        if offset < 0 or offset > 1000:
            return text_result(f"offset must be between 0 and 1000, got {offset}", is_error=True)
    else:
        offset = 0
    # The gateway accepts domains as repeated query params (or a comma list);
    # a bare comma-joined string used to be parsed as one element and matched
    # nothing, silently.
    domains = [d for d in (args.get("domains") or []) if d]
    exclude_domains = [d for d in (args.get("exclude_domains") or []) if d]

    try:
        body = await gateway_call(
            "GET",
            "/v1/search",
            params={
                "q": query,
                "sources": args.get("sources", "all"),
                "num_results": num_results,
                "language": args.get("language"),
                "country": args.get("country"),
                "time_range": args.get("time_range", "all"),
                "offset": offset,
                "cache": args.get("cache", True),
                "domains": domains or None,
                "exclude_domains": exclude_domains or None,
            },
        )
    except GatewayError as e:
        return text_result(str(e), is_error=True)
    results = [
        {
            "rank": r.get("rank"),
            "source": r.get("source"),
            "title": r.get("title"),
            "url": r.get("url"),
            "domain": r.get("domain"),
            "snippet": r.get("snippet"),
            "score": r.get("score"),
            "published": r.get("published"),
        }
        for r in (body.get("results") or [])
    ]
    return text_result(
        json.dumps(
            {
                "query": body.get("query", query),
                "total": body.get("total"),
                "returned": body.get("returned", len(results)),
                "has_more": body.get("has_more"),
                "offset": offset,
                "took_ms": body.get("took_ms"),
                "cache_hit": body.get("cache_hit"),
                "degraded": body.get("degraded"),
                "providers": body.get("providers"),
                "results": results,
            },
            indent=2,
            ensure_ascii=False,
        )
    )


async def handle_extract(ctx: ServerRequestContext, params: types.CallToolRequestParams) -> types.CallToolResult:
    args = params.arguments or {}
    url = args.get("url")
    if not url or not str(url).strip():
        return text_result("url is required and must not be blank", is_error=True)
    body = {"url": url, "format": args.get("format", "markdown")}
    if args.get("max_chars"):
        body["max_chars"] = args["max_chars"]
    try:
        result = await gateway_call("POST", "/v1/extract", json_body=body)
    except GatewayError as e:
        return text_result(str(e), is_error=True)

    # Returning only `content` discarded url / final_url / title / content_type
    # / fetched_at / metadata. final_url matters because it is where the page
    # actually landed after redirects, which is often a different URL than the
    # one requested. Return the whole envelope as JSON so nothing is lost.
    return text_result(json.dumps(result, indent=2, ensure_ascii=False))


async def handle_ingest(ctx: ServerRequestContext, params: types.CallToolRequestParams) -> types.CallToolResult:
    if not SEARCHOS_ADMIN_KEY:
        return text_result("ingest requires SEARCHOS_ADMIN_KEY env var (the gateway admin API key)", is_error=True)
    args = params.arguments or {}
    url = args.get("url")
    if not url:
        return text_result("url is required", is_error=True)
    try:
        body = await gateway_call(
            "POST",
            "/v1/admin/ingest",
            json_body={"url": url, "kind": args.get("kind", "url")},
            key=SEARCHOS_ADMIN_KEY,
        )
    except GatewayError as e:
        return text_result(str(e), is_error=True)
    return text_result(json.dumps(body, indent=2, ensure_ascii=False))


async def handle_stats(ctx: ServerRequestContext, params: types.CallToolRequestParams) -> types.CallToolResult:
    try:
        body = await gateway_call("GET", "/stats")
    except GatewayError as e:
        return text_result(str(e), is_error=True)
    return text_result(json.dumps(body, indent=2, ensure_ascii=False))


async def handle_health(ctx: ServerRequestContext, params: types.CallToolRequestParams) -> types.CallToolResult:
    try:
        body = await gateway_call("GET", "/health")
    except GatewayError as e:
        return text_result(str(e), is_error=True)
    return text_result(json.dumps(body, indent=2, ensure_ascii=False))


def build_server() -> Server:
    server = Server(
        "searchos",
        version="1.0.0",
        title="SearchOS",
        description=(
            "Self-hosted search API for AI agents: local FTS + vector index with live "
            "SearXNG fallback, continuous recursive crawler, and clean LLM-ready extraction."
        ),
        instructions=(
            "Use 'search' to find answers (results are saved into the local index so "
            "repeat queries are served instantly). Use 'extract' to pull clean full-text "
            "of a specific page. Use 'ingest' to add a URL or sitemap to the crawler."
        ),
        on_list_tools=lambda ctx, params: _list_tools(),
        on_call_tool=_call_tool,
    )
    return server


async def _list_tools() -> types.ListToolsResult:
    return types.ListToolsResult(tools=_tools())


async def _call_tool(ctx: ServerRequestContext, params: types.CallToolRequestParams) -> types.CallToolResult:
    handlers = {
        "search": handle_search,
        "extract": handle_extract,
        "ingest": handle_ingest,
        "stats": handle_stats,
        "health": handle_health,
    }
    handler = handlers.get(params.name)
    if handler is None:
        return text_result(f"unknown tool: {params.name}", is_error=True)
    return await handler(ctx, params)


class BearerAuthMiddleware:
    """Require a bearer token on the HTTP transports.

    /mcp, /sse and /messages/ expose the whole tool set, including `ingest`,
    which enqueues arbitrary-URL crawls. They previously had no authentication
    of any kind. stdio is unaffected (it is a local process, not a socket).
    """

    def __init__(self, app, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # The health probe carries no credentials, so gating it would make the
        # container permanently unhealthy. It exposes no tool and no data.
        if scope.get("path") == "/healthz":
            await self.app(scope, receive, send)
            return
        header = b""
        for key, value in scope.get("headers") or []:
            if key == b"authorization":
                header = value
                break
        expected = f"Bearer {self.token}".encode()
        # Constant-time compare; len check first to keep timing flat.
        if header != expected:
            await JSONResponse(
                {"error": "unauthorized", "message": "Missing or invalid MCP_HTTP_TOKEN."},
                status_code=401,
            )(scope, receive, send)
            return
        await self.app(scope, receive, send)


def require_http_token_or_loopback(host: str) -> str | None:
    """Refuse to bind the HTTP transports publicly without a token.

    Returns an error message when the configuration is unsafe, else None.
    """
    if MCP_HTTP_TOKEN:
        return None
    if host in LOOPBACK_HOSTS:
        return None
    return (
        f"MCP_HOST={host} exposes the tool set (including admin 'ingest') on a "
        "non-loopback address with no MCP_HTTP_TOKEN set. Set MCP_HTTP_TOKEN, or "
        "bind to 127.0.0.1."
    )


def build_http_app(server: Server):
    """Starlette app exposing Streamable HTTP (/mcp) + legacy HTTP+SSE (/sse, /messages)."""
    sse = SseServerTransport("/messages/")

    async def handle_sse(request):
        # request._send is a private Starlette attribute and breaks on upgrade;
        # pass an explicit sender callable instead.
        async def sender(message):
            await send_wrapper(message)

        async def send_wrapper(message):
            await request._send(message)

        async with sse.connect_sse(request.scope, request.receive, sender) as streams:
            await server.run(streams[0], streams[1], server.create_initialization_options())

        return Response()

    async def handle_healthz(request):
        return JSONResponse({"status": "ok", "service": "searchos-mcp"})

    app = server.streamable_http_app(
        stateless_http=STATELESS,
        custom_starlette_routes=[
            Route("/healthz", endpoint=handle_healthz, methods=["GET"]),
            Route("/sse", endpoint=handle_sse, methods=["GET"]),
            Mount("/messages/", app=sse.handle_post_message),
        ],
    )
    if MCP_HTTP_TOKEN:
        app = BearerAuthMiddleware(app, MCP_HTTP_TOKEN)
    return app


async def run_stdio(server: Server) -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    parser = argparse.ArgumentParser(description="SearchOS MCP server")
    parser.add_argument(
        "--transport",
        choices=["http", "stdio"],
        default="http",
        help="http = Streamable HTTP + legacy SSE on /mcp and /sse; stdio = local process",
    )
    parser.add_argument("--host", default=MCP_HOST)
    parser.add_argument("--port", type=int, default=MCP_PORT)
    args = parser.parse_args()

    server = build_server()

    if args.transport == "stdio":
        import asyncio

        asyncio.run(run_stdio(server))
        return

    problem = require_http_token_or_loopback(args.host)
    if problem:
        raise SystemExit(f"refusing to start: {problem}")

    app = build_http_app(server)
    print(
        f"SearchOS MCP listening on http://{args.host}:{args.port} "
        "(POST /mcp, GET /sse, POST /messages/)"
        f"{' [bearer auth enabled]' if MCP_HTTP_TOKEN else ' [loopback only]'}",
        flush=True,
    )
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
