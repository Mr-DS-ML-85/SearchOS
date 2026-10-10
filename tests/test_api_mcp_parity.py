"""Every gateway feature an agent should have is reachable through MCP.

Derived from the gateway's own OpenAPI schema, so a new route or parameter
fails here until the MCP server exposes it or it is listed as deliberately
excluded. The search tool once lost next_cursor, cache_type and min_score to
a hand-kept field whitelist; this is the guard against that class of drift.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "mcp"))

TOOL_ROUTES = {
    "search": "GET /v1/search",
    "search_images": "GET /v1/images/search",
    "extract": "POST /v1/extract",
    "suggest": "GET /v1/suggest",
    "popular": "GET /v1/popular",
    "recent": "GET /v1/recent",
    "ingest": "POST /v1/admin/ingest",
    "cache_flush": "POST /v1/admin/cache/flush",
    "stats": "GET /stats",
    "health": "GET /health",
}

#: Routes deliberately not exposed to agents, and why.
NOT_FOR_AGENTS = {
    "GET /": "landing JSON",
    "GET /health/live": "orchestrator probe; `health` covers an agent",
    "GET /health/ready": "orchestrator probe; `health` covers an agent",
    "GET /metrics": "Prometheus scrape endpoint",
    "GET /v1/admin/keys": "key management is a privilege an agent must not self-grant",
    "POST /v1/admin/keys": "key management is a privilege an agent must not self-grant",
    "DELETE /v1/admin/keys/{key_id}": "key management is a privilege an agent must not self-grant",
}

#: MCP argument name -> gateway parameter name.
RENAMED = {"search": {"query": "q"}, "search_images": {"query": "q"}}
#: Gateway parameters an MCP caller does not need: auth comes from the MCP
#: server's configured key, and `freshness` is an alias of `time_range`.
NOT_ARGUMENTS = {"authorization", "x-api-key", "freshness"}


@pytest.fixture(scope="module")
def openapi():
    import os

    os.environ.setdefault("AUTH_ENABLED", "false")
    from app.main import create_app

    spec = create_app().openapi()
    routes: dict[str, set[str]] = {}
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            params = {p["name"] for p in op.get("parameters", [])}
            ref = op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {}).get("$ref")
            if ref:
                params |= set(spec["components"]["schemas"][ref.rsplit("/", 1)[-1]].get("properties", {}))
            routes[f"{method.upper()} {path}"] = params
    return routes


@pytest.fixture(scope="module")
def tools():
    import searchos_mcp

    return {t.name: t for t in searchos_mcp._tools()}


def test_every_route_is_a_tool_or_deliberately_excluded(openapi, tools):
    assert set(TOOL_ROUTES) == set(tools), "tool list and TOOL_ROUTES disagree"
    unaccounted = set(openapi) - set(TOOL_ROUTES.values()) - set(NOT_FOR_AGENTS)
    assert not unaccounted, f"gateway routes with no MCP tool: {sorted(unaccounted)}"


@pytest.mark.parametrize("tool", sorted(TOOL_ROUTES))
def test_every_parameter_is_accepted_by_the_tool(openapi, tools, tool):
    accepted = {RENAMED.get(tool, {}).get(k, k) for k in tools[tool].input_schema.get("properties", {})}
    missing = openapi[TOOL_ROUTES[tool]] - accepted - NOT_ARGUMENTS
    assert not missing, f"{tool} cannot pass {sorted(missing)}"


def test_search_passes_every_response_field_through():
    import searchos_mcp as m
    from app.schemas import SearchResponse, SearchResult

    body = {f: f"<{f}>" for f in SearchResponse.model_fields}
    body["results"] = [{f: f"<{f}>" for f in SearchResult.model_fields if f != "colbert_score"}]

    class Resp:
        status_code = 200

        def json(self):
            return body

    class Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, *a, **kw):
            return Resp()

    original = m.httpx.AsyncClient
    m.httpx.AsyncClient = Client
    try:

        class P:
            name = "search"
            arguments = {"query": "x"}

        out = json.loads(asyncio.run(m.handle_search(None, P())).content[0].text)
    finally:
        m.httpx.AsyncClient = original
    assert set(SearchResponse.model_fields) <= set(out)
    assert out["results"] == body["results"]
