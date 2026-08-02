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
from starlette.routing import Mount, Route
from starlette.types import Scope, Receive, Send

from mcp.server.lowlevel import Server
from mcp.server.lowlevel.server import ServerRequestContext
from mcp.server.sse import SseServerTransport
from mcp.server.stdio import stdio_server

SEARCHOS_URL = os.environ.get("SEARCHOS_URL", "http://127.0.0.1:8010")
SEARCHOS_API_KEY = os.environ.get("SEARCHOS_API_KEY", "")
SEARCHOS_ADMIN_KEY = os.environ.get("SEARCHOS_ADMIN_KEY", "")
MCP_HOST = os.environ.get("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("MCP_PORT", "8099"))
STATELESS = os.environ.get("MCP_STATELESS_HTTP", "true").lower() == "true"


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
    timeout = httpx.Timeout(60.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        try:
            if method == "GET":
                resp = await client.get(url, params=params, headers=headers)
            else:
                resp = await client.post(url, json=json_body or {}, headers=headers)
        except httpx.HTTPError as e:
            raise GatewayError(f"gateway unreachable: {e.__class__.__name__}: {e}") from e
    try:
        body = resp.json()
    except ValueError:
        body = {"status_code": resp.status_code, "text": resp.text[:500]}
    if resp.status_code >= 400:
        detail = body.get("message") or body.get("detail") or body if isinstance(body, dict) else body
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
    if not query:
        return text_result("query is required", is_error=True)
    try:
        body = await gateway_call(
            "GET",
            "/v1/search",
            params={
                "q": query,
                "sources": args.get("sources", "all"),
                "num_results": args.get("num_results", 10),
                "language": args.get("language") or "",
                "country": args.get("country") or "",
                "time_range": args.get("time_range", "all"),
                "offset": args.get("offset", 0),
                "cache": args.get("cache", True),
                "domains": ",".join(args.get("domains") or []),
                "exclude_domains": ",".join(args.get("exclude_domains") or []),
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
    if not url:
        return text_result("url is required", is_error=True)
    body = {"url": url, "format": args.get("format", "markdown")}
    if args.get("max_chars"):
        body["max_chars"] = args["max_chars"]
    try:
        result = await gateway_call("POST", "/v1/extract", json_body=body)
    except GatewayError as e:
        return text_result(str(e), is_error=True)
    content = result.get("content")
    if content is None:
        return text_result(json.dumps(result, indent=2, ensure_ascii=False))
    return text_result(content)


async def handle_ingest(ctx: ServerRequestContext, params: types.CallToolRequestParams) -> types.CallToolResult:
    if not SEARCHOS_ADMIN_KEY:
        return text_result(
            "ingest requires SEARCHOS_ADMIN_KEY env var (the gateway admin API key)", is_error=True
        )
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


def build_http_app(server: Server):
    """Starlette app exposing Streamable HTTP (/mcp) + legacy HTTP+SSE (/sse, /messages)."""
    sse = SseServerTransport("/messages/")

    async def handle_sse(request):
        async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
            await server.run(streams[0], streams[1], server.create_initialization_options())
        from starlette.responses import Response

        return Response()

    return server.streamable_http_app(
        stateless_http=STATELESS,
        custom_starlette_routes=[
            Route("/sse", endpoint=handle_sse, methods=["GET"]),
            Mount("/messages/", app=sse.handle_post_message),
        ],
    )


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

    app = build_http_app(server)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
