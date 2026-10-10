"""End-to-end MCP protocol tests: all three transports against a live gateway.

Skipped unless ``TEST_MCP_GATEWAY_URL`` + ``TEST_MCP_API_KEY`` are set. These
drive the real SDK client rather than mocks, so a protocol regression (missing
initialize handshake, broken SSE framing, wrong content type) cannot pass.

    export TEST_MCP_GATEWAY_URL=http://127.0.0.1:8000
    export TEST_MCP_API_KEY=sk_...
    pytest tests/test_mcp_e2e.py -q

The HTTP transports need the MCP server running as well:

    SEARCHOS_URL=... SEARCHOS_API_KEY=... python services/mcp/searchos_mcp.py --transport http
    export TEST_MCP_SERVER_URL=http://127.0.0.1:8099
    export TEST_MCP_HTTP_TOKEN=...   # only if the server enforces one
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MCP_SCRIPT = REPO / "services" / "mcp" / "searchos_mcp.py"
PYTHON = sys.executable

GATEWAY = os.environ.get("TEST_MCP_GATEWAY_URL")
API_KEY = os.environ.get("TEST_MCP_API_KEY", "")
SERVER = os.environ.get("TEST_MCP_SERVER_URL")
HTTP_TOKEN = os.environ.get("TEST_MCP_HTTP_TOKEN")

requires_gateway = pytest.mark.skipif(
    not (GATEWAY and API_KEY), reason="TEST_MCP_GATEWAY_URL + TEST_MCP_API_KEY required"
)
requires_server = pytest.mark.skipif(
    not SERVER, reason="TEST_MCP_SERVER_URL required (run searchos_mcp.py --transport http)"
)
requires_token = pytest.mark.skipif(not HTTP_TOKEN, reason="TEST_MCP_HTTP_TOKEN not set; server may be unauthenticated")

EXPECTED_TOOLS = {"search", "extract", "ingest", "stats", "health"}


def _stdio_params():
    from mcp import StdioServerParameters

    env = dict(os.environ)
    env["SEARCHOS_URL"] = GATEWAY or ""
    env["SEARCHOS_API_KEY"] = API_KEY
    return StdioServerParameters(
        command=PYTHON,
        args=[str(MCP_SCRIPT), "--transport", "stdio"],
        env=env,
    )


def _result_text(result) -> dict:
    content = result.content
    assert content, "tool returned no content blocks"
    text = content[0].text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_raw": text}


class TestMcpModule:
    def test_script_exists_and_compiles(self):
        import py_compile

        assert MCP_SCRIPT.is_file()
        py_compile.compile(str(MCP_SCRIPT), doraise=True)

    def test_imports_cleanly_with_sdk_installed(self):
        """CI never installed services/mcp/requirements.txt, so a bad import
        (`import mcp_types`, an alias of mcp.types) would have gone unnoticed."""
        out = subprocess.run(
            [PYTHON, "-c", f"import sys; sys.path.insert(0, {str(MCP_SCRIPT.parent)!r}); import searchos_mcp"],
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert out.returncode == 0, out.stderr

    def test_server_builds_and_exposes_tools(self):
        out = subprocess.run(
            [
                PYTHON,
                "-c",
                (
                    f"import sys; sys.path.insert(0, {str(MCP_SCRIPT.parent)!r}); "
                    "import searchos_mcp as m; "
                    "print(sorted(t.name for t in m._tools())); "
                    "m.build_server()"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert out.returncode == 0, out.stderr
        assert all(name in out.stdout for name in EXPECTED_TOOLS)


@requires_gateway
class TestStdioTransport:
    """stdio: the transport opencode, Claude Desktop and Cursor use locally."""

    async def test_lists_tools(self):
        from mcp import Client

        async with Client(_stdio_params()) as client:
            tools = await client.list_tools()
            assert {t.name for t in tools.tools} == EXPECTED_TOOLS

    async def test_reports_server_identity(self):
        from mcp import Client

        async with Client(_stdio_params()) as client:
            info = client.server_info
            assert info is not None
            assert info.name == "searchos"

    async def test_health_tool(self):
        from mcp import Client

        async with Client(_stdio_params()) as client:
            result = await client.call_tool("health", {})
            assert not result.is_error
            assert _result_text(result)["status"] in ("ok", "degraded")

    async def test_search_tool(self):
        from mcp import Client

        async with Client(_stdio_params()) as client:
            result = await client.call_tool("search", {"query": "test", "num_results": 3})
            assert not result.is_error
            payload = _result_text(result)
            assert isinstance(payload["results"], list)
            assert payload["results"] or payload["total"] == 0

    async def test_search_pagination_is_gapless(self):
        """The MCP client must be able to walk pages without overlap."""
        from mcp import Client

        async with Client(_stdio_params()) as client:
            seen: list[str] = []
            for offset in (0, 5):
                result = await client.call_tool("search", {"query": "test", "num_results": 5, "offset": offset})
                assert not result.is_error
                seen.extend(r["url"] for r in _result_text(result)["results"])
            assert len(seen) == len(set(seen)), f"duplicate results across pages: {seen}"

    async def test_search_with_domain_filter(self):
        """domains must reach the gateway as a real filter, not be dropped."""
        from mcp import Client

        async with Client(_stdio_params()) as client:
            result = await client.call_tool("search", {"query": "test", "num_results": 5, "domains": ["wikipedia.org"]})
            assert not result.is_error
            for row in _result_text(result)["results"]:
                assert "wikipedia.org" in (row.get("url") or "")

    async def test_stats_tool(self):
        from mcp import Client

        async with Client(_stdio_params()) as client:
            result = await client.call_tool("stats", {})
            assert not result.is_error
            assert "documents" in _result_text(result)

    async def test_extract_tool(self):
        from mcp import Client

        async with Client(_stdio_params()) as client:
            result = await client.call_tool("extract", {"url": "https://example.com/", "max_chars": 200})
            # example.com is reachable but may render empty; either a payload or
            # a clean tool error is acceptable, a protocol failure is not.
            assert isinstance(result.is_error, bool)

    async def test_ingest_without_admin_key_errors_cleanly(self):
        from mcp import Client

        params = _stdio_params()
        params.env = {**params.env, "SEARCHOS_ADMIN_KEY": ""}
        async with Client(params) as client:
            result = await client.call_tool("ingest", {"url": "https://example.com/"})
            assert result.is_error
            assert "SEARCHOS_ADMIN_KEY" in result.content[0].text

    async def test_unknown_tool_is_an_error(self):
        from mcp import Client

        async with Client(_stdio_params()) as client:
            result = await client.call_tool("definitely_not_a_tool", {})
            assert result.is_error

    async def test_missing_required_argument_is_an_error(self):
        from mcp import Client

        async with Client(_stdio_params()) as client:
            result = await client.call_tool("search", {})
            assert result.is_error
            assert "query is required" in result.content[0].text


@requires_server
class TestStreamableHttpTransport:
    """Streamable HTTP: the modern remote transport (``POST /mcp``).

    Uses the low-level ``ClientSession`` + ``streamable_http_client`` pair so a
    bearer header can be supplied; the high-level ``Client(url)`` helper does not
    expose one, which is why an authenticated server looks unreachable to it.
    """

    def _streams(self):
        from mcp.client.streamable_http import streamable_http_client
        from mcp.shared._httpx_utils import create_mcp_http_client

        headers = {"Authorization": f"Bearer {HTTP_TOKEN}"} if HTTP_TOKEN else None
        http_client = create_mcp_http_client(headers=headers)
        return streamable_http_client(f"{SERVER.rstrip('/')}/mcp", http_client=http_client)

    async def test_lists_tools(self):
        from mcp import ClientSession

        async with self._streams() as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert {t.name for t in tools.tools} == EXPECTED_TOOLS

    async def test_health_tool(self):
        from mcp import ClientSession

        async with self._streams() as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("health", {})
            assert not result.is_error
            assert _result_text(result)["status"] in ("ok", "degraded")

    async def test_search_tool(self):
        from mcp import ClientSession

        async with self._streams() as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("search", {"query": "test", "num_results": 3})
            assert not result.is_error
            assert isinstance(_result_text(result)["results"], list)


@requires_server
class TestLegacySseTransport:
    async def test_sse_endpoint_streams(self):
        import httpx

        headers = {"Authorization": f"Bearer {HTTP_TOKEN}"} if HTTP_TOKEN else {}
        async with httpx.AsyncClient(timeout=20) as client:
            async with client.stream("GET", f"{SERVER.rstrip('/')}/sse", headers=headers) as resp:
                assert resp.status_code == 200
                assert resp.headers.get("content-type", "").startswith("text/event-stream")

    @requires_token
    async def test_sse_rejects_wrong_token(self):
        import httpx

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(f"{SERVER.rstrip('/')}/sse", headers={"Authorization": "Bearer wrong-token"})
        assert resp.status_code == 401

    @requires_token
    async def test_sse_rejects_missing_token(self):
        import httpx

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(f"{SERVER.rstrip('/')}/sse")
        assert resp.status_code == 401


@requires_server
class TestHttpTransportAuth:
    """The HTTP transports used to have no auth at all, exposing admin `ingest`."""

    @requires_token
    async def test_mcp_rejects_missing_token(self):
        import httpx

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(f"{SERVER.rstrip('/')}/mcp", json={})
        assert resp.status_code == 401

    @requires_token
    async def test_mcp_rejects_wrong_token(self):
        import httpx

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{SERVER.rstrip('/')}/mcp",
                json={},
                headers={"Authorization": "Bearer wrong-token"},
            )
        assert resp.status_code == 401

    @requires_token
    async def test_mcp_accepts_correct_token(self):
        import httpx

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{SERVER.rstrip('/')}/mcp",
                json={},
                headers={"Authorization": f"Bearer {HTTP_TOKEN}"},
            )
        assert resp.status_code != 401
