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


def _as_bool(value: Any) -> bool | None:
    """Coerce an MCP argument to bool, or None to leave the server default.

    Models send JSON booleans, but some send "true"/"1"/"yes" as strings; an
    unparsed "false" string is truthy in Python and would silently enable the
    thing the caller asked to disable.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "on"):
        return True
    if text in ("false", "0", "no", "off"):
        return False
    return None


def _search_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "search query (required)"},
            "topic": {
                "type": "string",
                "maxLength": 40,
                "description": (
                    "Search one content category instead of the general web. Use the "
                    "`topics` tool to list valid ids. Omit for an ordinary search."
                ),
            },
            "sources": {
                "type": "string",
                # Must track the gateway's SearchSource enum. This list was stale
                # at four entries, so an agent asking for sources="federated"
                # got a validation error from the gateway instead of a result.
                "enum": [
                    "all",
                    "local",
                    "vector",
                    "web",
                    "searxng",
                    "google",
                    "tavily",
                    "exa",
                    "jina",
                    "federated",
                    "federated_web",
                ],
                "description": (
                    "which providers to query. 'web' is the whole fallback cascade "
                    "(SearXNG -> Google -> Tavily -> Exa -> Jina); the tier names pin one "
                    "upstream and error rather than silently falling back ('google' is "
                    "Google via the Proxify VM browser, ~12s, only when PROXIFY_URL is set); "
                    "'federated' queries the configured SearchOS peers' indexes; "
                    "'federated_web' asks those peers to run the web search (the last "
                    "cascade tier, used when this instance's own web tiers are rate-limited)."
                ),
            },
            "cursor": {
                "type": "string",
                "description": (
                    "next_cursor from a previous response to fetch the following page. "
                    "Bound to that response's query and filters; do not combine with offset."
                ),
            },
            "num_results": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            "language": {
                "type": "string",
                "description": (
                    "ISO 639-1 code, e.g. 'en' or 'fr'. Indexed results are limited to "
                    "documents detected as that language; web tiers receive it as a locale."
                ),
            },
            "country": {
                "type": "string",
                "description": (
                    "ISO 3166 code, e.g. 'ca'. Only combined with `language` into a web "
                    "locale (fr + ca -> fr-CA); indexed documents carry no country, so it "
                    "has no effect without `language` or on local/vector results."
                ),
            },
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
            "colbert": {
                "type": "boolean",
                "description": (
                    "Force late-interaction (ColBERT) reranking on or off for this "
                    "query, overriding the server default. Present so an A/B of the "
                    "reranker is possible from an agent; results differ only in "
                    "ordering and score."
                ),
            },
        },
        "required": ["query"],
    }


def _images_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "text query — text-to-image search by what the image depicts",
            },
            "image_url": {
                "type": "string",
                "description": "reference image URL — find images like this one",
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            "source": {
                "type": "string",
                "enum": ["local", "google"],
                "default": "local",
                "description": (
                    "'local' = CLIP over crawled images (text or image_url); 'google' = "
                    "Google Images via Proxify (text query only, ~12s; url is Google's "
                    "thumbnail, source_page the page it appears on)"
                ),
            },
        },
        # Exactly one of query / image_url is required. Not expressed as a
        # top-level anyOf because Anthropic's tool API rejects one; enforced in
        # handle_search_images instead.
        "additionalProperties": False,
    }


def _suggest_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "q": {"type": "string", "description": "prefix to complete"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 25, "default": 8},
        },
        "required": ["q"],
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
                "locally. Returns JSON results with title, url, snippet, source and score. "
                'The query accepts operators: site:, -site:, filetype:, "exact phrase", -term. '
                "Page with next_cursor while has_more is true."
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
        types.Tool(
            name="cache_flush",
            description=(
                "Drop cached search/extract results. Use after an ingest when you want "
                "the new document visible immediately — otherwise a cached response for "
                "the same query is served until it expires and the crawl looks like it "
                "failed. Requires the admin API key."
            ),
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        ),
        types.Tool(
            name="search_images",
            description=(
                "Find images by what they depict. CLIP embeds images and text into one "
                "space, so text-to-image and image-to-image are the same operation: pass "
                "exactly one of `query` (text) or `image_url` (reference image). Images are "
                "indexed as pages are crawled; icons and undersized assets are skipped. "
                "Text queries drop matches below `min_score`; `below_min_score` counts them, "
                "so an empty list with a non-zero count means nothing indexed depicts the query."
            ),
            input_schema=_images_schema(),
        ),
        types.Tool(
            name="suggest",
            description=(
                "Prefix completions from indexed titles, for search-as-you-type. "
                "Cheaper than a full search when the query is still being typed."
            ),
            input_schema=_suggest_schema(),
        ),
        types.Tool(
            name="topics",
            description=(
                "The topic catalogue: search categories (news, videos, science, ...) this "
                "instance can serve. Each entry reports `available` and the providers that "
                "back it, so a caller can skip a category this instance cannot fill instead "
                "of rendering an empty tab. Pass an entry's `id` as `topic` to `search`."
            ),
            input_schema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="popular",
            description=(
                "Most-run queries, from the live Redis query counter. Corpus-wide, not scoped to the calling key."
            ),
            input_schema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}},
            },
        ),
        types.Tool(
            name="recent",
            description=(
                "Documents the crawler indexed or re-crawled in the last `hours`. A "
                "'what changed' feed that search cannot express. Corpus-wide."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "hours": {"type": "integer", "minimum": 1, "maximum": 2160, "default": 24},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                },
            },
        ),
    ]


# Shown first so the page position and confidence read before the diagnostics.
_SEARCH_HEADLINE = (
    "query",
    "total",
    "returned",
    "has_more",
    "next_cursor",
    "offset",
    "took_ms",
    "top_score",
    "low_confidence",
    "min_score",
    "cache_hit",
    "cache_type",
)


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
    cursor = args.get("cursor") or None
    if cursor and offset:
        return text_result(
            "pass either cursor or offset, not both (a cursor already encodes its offset)", is_error=True
        )
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
                # Orthogonal to `sources`: the provider to query vs the content
                # category it queries. Omitted when absent so an untopiced search
                # is byte-identical to one sent before topics existed.
                "topic": args.get("topic") or None,
                "num_results": num_results,
                "language": args.get("language"),
                "country": args.get("country"),
                "time_range": args.get("time_range", "all"),
                # A cursor carries its own offset; sending both made the gateway
                # silently ignore the offset, so report the conflict instead.
                "offset": offset,
                "cursor": cursor,
                "cache": args.get("cache", True),
                # Only sent when the caller sets it, so the server default stands
                # otherwise. Accepts true/false as well as on/1 strings.
                "colbert": _as_bool(args.get("colbert")),
                "domains": domains or None,
                "exclude_domains": exclude_domains or None,
            },
        )
    except GatewayError as e:
        return text_result(str(e), is_error=True)
    # Pass the gateway's response through instead of whitelisting fields. The
    # whitelist had to be edited for every new gateway field and kept falling
    # behind: next_cursor, cache_type and the semantic-cache diagnostics were
    # each dropped in turn, and min_score and per-result favicon/raw still were.
    # An MCP caller now sees exactly what an API caller sees.
    out: dict[str, Any] = {key: body[key] for key in _SEARCH_HEADLINE if key in body}
    out.update({key: value for key, value in body.items() if key != "results"})
    out.setdefault("query", query)
    # On a cursor page the offset lives inside the cursor; echoing the
    # argument default (0) misreported where the page starts.
    out["offset"] = None if cursor else offset
    out["results"] = body.get("results") or []
    return text_result(
        json.dumps(
            out,
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


async def handle_search_images(ctx: ServerRequestContext, params: types.CallToolRequestParams) -> types.CallToolResult:
    args = params.arguments or {}
    query = args.get("query")
    image_url = args.get("image_url")
    if query and image_url:
        # The gateway used image_url and silently ignored the text.
        return text_result("pass either 'query' or 'image_url', not both", is_error=True)
    if not query and not image_url:
        # The gateway rejects this too, but an agent benefits from being told
        # which argument is missing rather than seeing a bare 422.
        return text_result(
            "provide either 'query' (text-to-image) or 'image_url' (image-to-image)",
            is_error=True,
        )
    try:
        body = await gateway_call(
            "GET",
            "/v1/images/search",
            # The gateway's parameter is `q`; the MCP tool exposes `query` to
            # match the search tool's vocabulary.
            params={
                "q": query,
                "image_url": image_url,
                "limit": args.get("limit", 10),
                "source": args.get("source"),
            },
        )
    except GatewayError as e:
        return text_result(str(e), is_error=True)
    if not body.get("image_search_available", True):
        return text_result(
            "Image search is unavailable: the `images` extra (Pillow) is not "
            "installed on the gateway. This is a degraded mode, not an empty index.",
            is_error=True,
        )
    images = [
        {
            "score": i.get("score"),
            "url": i.get("url"),
            "width": i.get("width"),
            "height": i.get("height"),
            "alt_text": i.get("alt_text"),
            "source_page": i.get("source_url"),
        }
        for i in body.get("images", [])
    ]
    return text_result(
        json.dumps(
            {
                "query": body.get("query"),
                "source": body.get("source", "local"),
                "took_ms": body.get("took_ms"),
                "min_score": body.get("min_score"),
                "below_min_score": body.get("below_min_score", 0),
                "images": images,
            },
            indent=2,
            ensure_ascii=False,
        )
    )


async def handle_suggest(ctx: ServerRequestContext, params: types.CallToolRequestParams) -> types.CallToolResult:
    args = params.arguments or {}
    prefix = args.get("q")
    if not prefix or not str(prefix).strip():
        return text_result("q is required and must not be blank", is_error=True)
    try:
        # /v1/suggest returns a bare JSON array, not an object.
        body = await gateway_call("GET", "/v1/suggest", params={"q": prefix, "limit": args.get("limit", 8)})
    except GatewayError as e:
        return text_result(str(e), is_error=True)
    items = body if isinstance(body, list) else body.get("suggestions", [])
    return text_result(
        json.dumps(
            [{"text": i.get("text"), "count": i.get("count")} for i in items],
            indent=2,
            ensure_ascii=False,
        )
    )


async def handle_topics(ctx: ServerRequestContext, params: types.CallToolRequestParams) -> types.CallToolResult:
    try:
        body = await gateway_call("GET", "/v1/topics")
    except GatewayError as e:
        return text_result(str(e), is_error=True)
    return text_result(json.dumps(body, indent=2, ensure_ascii=False))


async def handle_popular(ctx: ServerRequestContext, params: types.CallToolRequestParams) -> types.CallToolResult:
    args = params.arguments or {}
    try:
        body = await gateway_call("GET", "/v1/popular", params={"limit": args.get("limit", 10)})
    except GatewayError as e:
        return text_result(str(e), is_error=True)
    return text_result(json.dumps(body.get("queries", []), indent=2, ensure_ascii=False))


async def handle_recent(ctx: ServerRequestContext, params: types.CallToolRequestParams) -> types.CallToolResult:
    args = params.arguments or {}
    try:
        body = await gateway_call(
            "GET",
            "/v1/recent",
            params={"hours": args.get("hours", 24), "limit": args.get("limit", 20)},
        )
    except GatewayError as e:
        return text_result(str(e), is_error=True)
    return text_result(json.dumps(body.get("documents", []), indent=2, ensure_ascii=False))


async def handle_cache_flush(ctx: ServerRequestContext, params: types.CallToolRequestParams) -> types.CallToolResult:
    """Drop cached search/extract results.

    Needed after an ingest when you want to see the new document immediately:
    ingest saves into the index, but a cached response for the same query keeps
    being served until it expires. Without this an agent that just crawled a
    page and then searched for it gets the pre-crawl answer and concludes the
    crawl failed.
    """
    if not SEARCHOS_ADMIN_KEY:
        return text_result(
            "cache_flush requires SEARCHOS_ADMIN_KEY env var (the gateway admin API key)",
            is_error=True,
        )
    try:
        body = await gateway_call("POST", "/v1/admin/cache/flush", json_body={}, key=SEARCHOS_ADMIN_KEY)
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
        "search_images": handle_search_images,
        "suggest": handle_suggest,
        "topics": handle_topics,
        "popular": handle_popular,
        "recent": handle_recent,
        "cache_flush": handle_cache_flush,
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
