"""MCP server tests: tool contracts, gateway delegation, auth, transports.

Covers the three transports (stdio, legacy HTTP+SSE, Streamable HTTP) plus the
parameter handling bugs that made ``domains`` and ``exclude_domains`` silently
no-ops end-to-end.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

MCP_DIR = Path(__file__).resolve().parents[1] / "services" / "mcp"
sys.path.insert(0, str(MCP_DIR))


@pytest.fixture(autouse=True)
def _reload_mcp():
    """Import the MCP module with test-friendly environment."""
    import importlib

    import searchos_mcp

    importlib.reload(searchos_mcp)
    return searchos_mcp


class FakeParams:
    def __init__(self, name: str, arguments: dict | None = None):
        self.name = name
        self.arguments = arguments or {}


class FakeCtx:
    pass


class FakeResponse:
    def __init__(self, status_code: int = 200, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class RecordingClient:
    """Captures requests instead of making them."""

    def __init__(self, response: FakeResponse | Exception):
        self.response = response
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None, headers=None):
        self.calls.append({"method": "GET", "url": url, "params": params, "headers": headers})
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    async def post(self, url, json=None, headers=None):
        self.calls.append({"method": "POST", "url": url, "json": json, "headers": headers})
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def install_client(mcp, monkeypatch, response):
    client = RecordingClient(response)
    monkeypatch.setattr(mcp.httpx, "AsyncClient", lambda **kw: client)
    return client


def result_text(result) -> dict:
    assert result.content, "tool returned no content"
    raw = result.content[0].text
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw}


class TestToolCatalogue:
    def test_exposes_the_documented_tools(self, _reload_mcp):
        names = {t.name for t in _reload_mcp._tools()}
        assert names == {
            "search",
            "extract",
            "ingest",
            "stats",
            "health",
            "search_images",
            "topics",
            "suggest",
            "popular",
            "recent",
            "cache_flush",
        }

    def test_admin_tools_require_the_admin_key(self, _reload_mcp):
        """ingest and cache_flush act on the index; neither may run without one.

        cache_flush is easy to overlook as a privileged call — it discards every
        cached response, so an unauthenticated agent could force re-queries
        against the whole corpus and make the gateway re-run expensive stages.
        """
        import asyncio

        module = _reload_mcp
        original = module.SEARCHOS_ADMIN_KEY
        try:
            module.SEARCHOS_ADMIN_KEY = ""
            for name, handler in (
                ("ingest", module.handle_ingest),
                ("cache_flush", module.handle_cache_flush),
            ):
                result = asyncio.run(handler(FakeCtx(), FakeParams(name, {})))
                assert result.is_error, f"{name} ran without an admin key"
                assert "ADMIN_KEY" in result.content[0].text
        finally:
            module.SEARCHOS_ADMIN_KEY = original

    def test_search_sources_enum_matches_the_gateway(self, _reload_mcp):
        """The MCP enum must track the gateway, not its own history.

        It listed four sources while the gateway accepted ten, so an agent asking
        for sources="federated" got a validation error from the gateway rather
        than results. Importing SearchSource here is what keeps them from
        drifting apart again.
        """
        from typing import get_args

        from app.schemas import SearchSource

        expected = set(get_args(SearchSource))
        search = next(t for t in _reload_mcp._tools() if t.name == "search")
        assert set(search.input_schema["properties"]["sources"]["enum"]) == expected

    def test_every_tool_has_a_schema_and_description(self, _reload_mcp):
        for tool in _reload_mcp._tools():
            assert tool.description, f"{tool.name} has no description"
            assert tool.input_schema["type"] == "object"

    def test_search_schema_declares_domains_as_array(self, _reload_mcp):
        search = next(t for t in _reload_mcp._tools() if t.name == "search")
        props = search.input_schema["properties"]
        assert props["domains"]["type"] == "array"
        assert props["exclude_domains"]["type"] == "array"

    def test_ingest_requires_no_schema_args_beyond_url(self, _reload_mcp):
        ingest = next(t for t in _reload_mcp._tools() if t.name == "ingest")
        assert "url" in ingest.input_schema["properties"]
        assert "kind" in ingest.input_schema["properties"]

    def test_unknown_tool_is_an_error_not_a_crash(self, _reload_mcp):
        import asyncio

        result = asyncio.run(_reload_mcp._call_tool(FakeCtx(), FakeParams("nope")))
        assert result.is_error
        assert "unknown tool" in result_text(result)["_raw"]


class TestConfig:
    def test_default_gateway_url_is_port_8000_not_8010(self, _reload_mcp):
        """docs used SEARCHOS_API_URL and defaulted to the renderer's port."""
        assert _reload_mcp.SEARCHOS_URL.endswith(":8000")
        assert ":8010" not in _reload_mcp.SEARCHOS_URL

    def test_reads_searchos_url_env_var(self, _reload_mcp, monkeypatch):
        monkeypatch.setenv("SEARCHOS_URL", "http://gateway.internal:9999")
        import importlib

        importlib.reload(_reload_mcp)
        assert _reload_mcp.SEARCHOS_URL == "http://gateway.internal:9999"

    def test_timeout_is_configurable(self, _reload_mcp, monkeypatch):
        monkeypatch.setenv("GATEWAY_TIMEOUT", "12.5")
        import importlib

        importlib.reload(_reload_mcp)
        assert _reload_mcp.GATEWAY_TIMEOUT == 12.5


class TestSearchTool:
    def test_missing_query_is_an_error(self, _reload_mcp):
        import asyncio

        result = asyncio.run(_reload_mcp.handle_search(FakeCtx(), FakeParams("search", {})))
        assert result.is_error

    def test_domains_sent_as_repeated_params(self, _reload_mcp, monkeypatch):
        """A comma-joined string parsed as one list element matched nothing."""
        import asyncio

        client = install_client(
            _reload_mcp,
            monkeypatch,
            FakeResponse(200, {"query": "x", "results": [], "total": 0}),
        )
        asyncio.run(
            _reload_mcp.handle_search(
                FakeCtx(),
                FakeParams("search", {"query": "x", "domains": ["a.com", "b.com"]}),
            )
        )
        assert client.calls[0]["params"]["domains"] == ["a.com", "b.com"]

    def test_empty_domains_are_not_sent(self, _reload_mcp, monkeypatch):
        import asyncio

        client = install_client(_reload_mcp, monkeypatch, FakeResponse(200, {"query": "x", "results": []}))
        asyncio.run(_reload_mcp.handle_search(FakeCtx(), FakeParams("search", {"query": "x", "domains": []})))
        assert "domains" not in client.calls[0]["params"]

    def test_blank_optional_params_are_dropped(self, _reload_mcp, monkeypatch):
        """`language=""` used to be forwarded as an empty query value."""
        import asyncio

        client = install_client(_reload_mcp, monkeypatch, FakeResponse(200, {"query": "x", "results": []}))
        asyncio.run(
            _reload_mcp.handle_search(FakeCtx(), FakeParams("search", {"query": "x", "language": "", "country": ""}))
        )
        params = client.calls[0]["params"]
        assert "language" not in params
        assert "country" not in params

    def test_domain_and_published_are_forwarded_in_results(self, _reload_mcp, monkeypatch):
        import asyncio

        install_client(
            _reload_mcp,
            monkeypatch,
            FakeResponse(
                200,
                {
                    "query": "x",
                    "results": [
                        {
                            "rank": 1,
                            "source": "local",
                            "title": "T",
                            "url": "https://a.com/x",
                            "domain": "a.com",
                            "snippet": "s",
                            "score": 0.5,
                            "published": "2026-01-01",
                        }
                    ],
                },
            ),
        )
        result = asyncio.run(_reload_mcp.handle_search(FakeCtx(), FakeParams("search", {"query": "x"})))
        payload = result_text(result)
        assert payload["results"][0]["domain"] == "a.com"
        assert payload["results"][0]["published"] == "2026-01-01"

    def test_gateway_4xx_becomes_tool_error_with_message(self, _reload_mcp, monkeypatch):
        import asyncio

        install_client(
            _reload_mcp,
            monkeypatch,
            FakeResponse(401, {"code": "unauthorized", "message": "Invalid API key."}),
        )
        result = asyncio.run(_reload_mcp.handle_search(FakeCtx(), FakeParams("search", {"query": "x"})))
        assert result.is_error
        assert "Invalid API key" in result_text(result)["_raw"]

    def test_legacy_nested_detail_error_is_unwrapped(self, _reload_mcp, monkeypatch):
        """Older gateways returned {"detail": {...}}; both shapes must read well."""
        import asyncio

        install_client(
            _reload_mcp,
            monkeypatch,
            FakeResponse(429, {"detail": {"code": "rate_limited", "message": "Slow down."}}),
        )
        result = asyncio.run(_reload_mcp.handle_search(FakeCtx(), FakeParams("search", {"query": "x"})))
        assert result.is_error
        assert "Slow down" in result_text(result)["_raw"]

    def test_unreachable_gateway_is_a_tool_error_not_a_crash(self, _reload_mcp, monkeypatch):
        import asyncio

        import httpx

        install_client(_reload_mcp, monkeypatch, httpx.ConnectError("refused"))
        result = asyncio.run(_reload_mcp.handle_search(FakeCtx(), FakeParams("search", {"query": "x"})))
        assert result.is_error
        assert "unreachable" in result_text(result)["_raw"]

    def test_non_json_error_body_is_surfaced(self, _reload_mcp, monkeypatch):
        import asyncio

        install_client(_reload_mcp, monkeypatch, FakeResponse(502, None, text="<html>bad gateway</html>"))
        result = asyncio.run(_reload_mcp.handle_search(FakeCtx(), FakeParams("search", {"query": "x"})))
        assert result.is_error
        assert "502" in result_text(result)["_raw"]


class TestExtractTool:
    def test_missing_url_is_an_error(self, _reload_mcp):
        import asyncio

        result = asyncio.run(_reload_mcp.handle_extract(FakeCtx(), FakeParams("extract", {})))
        assert result.is_error

    def test_posts_to_v1_extract(self, _reload_mcp, monkeypatch):
        import asyncio

        client = install_client(
            _reload_mcp, monkeypatch, FakeResponse(200, {"content": "body", "url": "https://a.com"})
        )
        asyncio.run(_reload_mcp.handle_extract(FakeCtx(), FakeParams("extract", {"url": "https://a.com"})))
        assert client.calls[0]["method"] == "POST"
        assert client.calls[0]["url"].endswith("/v1/extract")


class TestIngestTool:
    def test_requires_admin_key(self, _reload_mcp, monkeypatch):
        import asyncio

        monkeypatch.setattr(_reload_mcp, "SEARCHOS_ADMIN_KEY", "")
        result = asyncio.run(_reload_mcp.handle_ingest(FakeCtx(), FakeParams("ingest", {"url": "https://a.com"})))
        assert result.is_error
        assert "SEARCHOS_ADMIN_KEY" in result_text(result)["_raw"]

    def test_uses_admin_key_not_user_key(self, _reload_mcp, monkeypatch):
        import asyncio

        monkeypatch.setattr(_reload_mcp, "SEARCHOS_API_KEY", "sk_user")
        monkeypatch.setattr(_reload_mcp, "SEARCHOS_ADMIN_KEY", "sk_admin")
        client = install_client(_reload_mcp, monkeypatch, FakeResponse(200, {"job_id": "1"}))
        asyncio.run(_reload_mcp.handle_ingest(FakeCtx(), FakeParams("ingest", {"url": "https://a.com"})))
        assert client.calls[0]["headers"]["Authorization"] == "Bearer sk_admin"


class TestStatsAndHealthTools:
    def test_stats_calls_stats_endpoint(self, _reload_mcp, monkeypatch):
        import asyncio

        client = install_client(_reload_mcp, monkeypatch, FakeResponse(200, {"documents": 3}))
        result = asyncio.run(_reload_mcp.handle_stats(FakeCtx(), FakeParams("stats", {})))
        assert client.calls[0]["url"].endswith("/stats")
        assert result_text(result)["documents"] == 3

    def test_health_calls_health_endpoint(self, _reload_mcp, monkeypatch):
        import asyncio

        client = install_client(_reload_mcp, monkeypatch, FakeResponse(200, {"status": "ok"}))
        asyncio.run(_reload_mcp.handle_health(FakeCtx(), FakeParams("health", {})))
        assert client.calls[0]["url"].endswith("/health")


class TestHttpTransportAuth:
    def test_loopback_without_token_is_allowed(self, _reload_mcp, monkeypatch):
        monkeypatch.setattr(_reload_mcp, "MCP_HTTP_TOKEN", "")
        assert _reload_mcp.require_http_token_or_loopback("127.0.0.1") is None
        assert _reload_mcp.require_http_token_or_loopback("localhost") is None

    def test_public_bind_without_token_is_refused(self, _reload_mcp, monkeypatch):
        monkeypatch.setattr(_reload_mcp, "MCP_HTTP_TOKEN", "")
        message = _reload_mcp.require_http_token_or_loopback("0.0.0.0")
        assert message is not None
        assert "MCP_HTTP_TOKEN" in message

    def test_public_bind_with_token_is_allowed(self, _reload_mcp, monkeypatch):
        monkeypatch.setattr(_reload_mcp, "MCP_HTTP_TOKEN", "s3cret")
        assert _reload_mcp.require_http_token_or_loopback("0.0.0.0") is None

    def test_middleware_rejects_missing_token(self, _reload_mcp):
        import asyncio

        from starlette.responses import JSONResponse

        monkey_target = _reload_mcp
        monkey_target.MCP_HTTP_TOKEN = "s3cret"

        captured: dict = {}

        async def inner(scope, receive, send):
            captured["reached"] = True

        mw = _reload_mcp.BearerAuthMiddleware(inner, "s3cret")

        async def receive():
            return {"type": "http.request"}

        sent: list = []

        async def send(message):
            sent.append(message)

        asyncio.run(
            mw(
                {"type": "http", "headers": [(b"authorization", b"Bearer wrong")]},
                receive,
                send,
            )
        )
        assert "reached" not in captured
        assert any(m.get("status") == 401 for m in sent)

    def test_middleware_accepts_correct_token(self, _reload_mcp):
        import asyncio

        captured: dict = {}

        async def inner(scope, receive, send):
            captured["reached"] = True

        mw = _reload_mcp.BearerAuthMiddleware(inner, "s3cret")

        async def receive():
            return {"type": "http.request"}

        async def send(message):
            return None

        asyncio.run(
            mw(
                {"type": "http", "headers": [(b"authorization", b"Bearer s3cret")]},
                receive,
                send,
            )
        )
        assert captured.get("reached") is True


class TestServerConstruction:
    def test_build_server_succeeds(self, _reload_mcp):
        server = _reload_mcp.build_server()
        assert server is not None

    def test_http_app_builds(self, _reload_mcp, monkeypatch):
        monkeypatch.setattr(_reload_mcp, "MCP_HTTP_TOKEN", "")
        app = _reload_mcp.build_http_app(_reload_mcp.build_server())
        assert app is not None

    def test_http_app_wraps_with_auth_when_token_set(self, _reload_mcp, monkeypatch):
        monkeypatch.setattr(_reload_mcp, "MCP_HTTP_TOKEN", "abc")
        app = _reload_mcp.build_http_app(_reload_mcp.build_server())
        assert isinstance(app, _reload_mcp.BearerAuthMiddleware)


class TestDocsAndExamples:
    def test_examples_reference_the_right_env_var(self, _reload_mcp):
        import importlib

        importlib.reload(_reload_mcp)
        examples_dir = MCP_DIR / "examples"
        for path in examples_dir.glob("*.json"):
            content = path.read_text()
            assert "SEARCHOS_API_URL" not in content, f"{path.name} still uses the wrong env var"
            if "SEARCHOS_URL" in content:
                assert ":8000" in content or "localhost" in content

    def test_mcp_docs_reference_the_right_env_var(self, _reload_mcp):
        """The env var table must name SEARCHOS_URL, not the never-read alias."""
        docs = Path(__file__).resolve().parents[1] / "docs" / "mcp.md"
        content = docs.read_text()
        # Only the explanatory prose may mention the old name.
        table_rows = [line for line in content.splitlines() if line.strip().startswith("|")]
        assert not any("SEARCHOS_API_URL" in line for line in table_rows)
        assert any("`SEARCHOS_URL`" in line for line in table_rows)

    def test_mcp_docs_do_not_default_to_the_renderer_port(self, _reload_mcp):
        docs = Path(__file__).resolve().parents[1] / "docs" / "mcp.md"
        content = docs.read_text()
        assert "127.0.0.1:8010" not in content


# ---------------------------------------------------------------------------
# Live-transport tests. Skipped unless TEST_MCP_GATEWAY_URL is set, which should
# point at a running gateway (see docs/development.md).
# ---------------------------------------------------------------------------

GATEWAY = os.environ.get("TEST_MCP_GATEWAY_URL")
LIVE = pytest.mark.skipif(not GATEWAY, reason="TEST_MCP_GATEWAY_URL not set; skipping live MCP tests")


class TestAgainstLiveGateway:
    @LIVE
    def test_search_tool_over_http(self, _reload_mcp, monkeypatch):
        import asyncio

        monkeypatch.setattr(_reload_mcp, "SEARCHOS_URL", GATEWAY)
        monkeypatch.setattr(_reload_mcp, "SEARCHOS_API_KEY", os.environ.get("TEST_MCP_API_KEY", ""))
        client = install_client(_reload_mcp, monkeypatch, FakeResponse(200, {"query": "x", "results": []}))
        result = asyncio.run(
            _reload_mcp.handle_search(_reload_mcp and FakeCtx(), FakeParams("search", {"query": "postgres"}))
        )
        assert result.is_error is False
        assert client.calls[0]["url"] == f"{GATEWAY}/v1/search"

    @LIVE
    def test_health_tool_reports_status(self, _reload_mcp, monkeypatch):
        import asyncio

        monkeypatch.setattr(_reload_mcp, "SEARCHOS_URL", GATEWAY)
        install_client(_reload_mcp, monkeypatch, FakeResponse(200, {"status": "ok", "services": {}}))
        result = asyncio.run(_reload_mcp.handle_health(FakeCtx(), FakeParams("health", {})))
        assert result.is_error is False
        assert result_text(result)["status"] == "ok"


class TestHttpTokenEnvVarNaming:
    """`MCP_HTTP_TOKEN` is the only unprefixed setting in the process.

    Everything else is SEARCHOS_-prefixed, so setting SEARCHOS_MCP_HTTP_TOKEN
    out of habit silently yielded an empty token — and an HTTP transport
    serving the admin `ingest` tool to anything that could reach the port.
    """

    def test_prefixed_alias_is_accepted(self, monkeypatch):
        import importlib

        import services.mcp.searchos_mcp as mod

        monkeypatch.delenv("MCP_HTTP_TOKEN", raising=False)
        monkeypatch.setenv("SEARCHOS_MCP_HTTP_TOKEN", "aliased-secret")
        reloaded = importlib.reload(mod)
        try:
            assert reloaded.MCP_HTTP_TOKEN == "aliased-secret"
        finally:
            monkeypatch.undo()
            importlib.reload(mod)

    def test_canonical_name_wins_when_both_set(self, monkeypatch):
        import importlib

        import services.mcp.searchos_mcp as mod

        monkeypatch.setenv("MCP_HTTP_TOKEN", "canonical")
        monkeypatch.setenv("SEARCHOS_MCP_HTTP_TOKEN", "aliased")
        reloaded = importlib.reload(mod)
        try:
            assert reloaded.MCP_HTTP_TOKEN == "canonical"
        finally:
            monkeypatch.undo()
            importlib.reload(mod)

    def test_public_binding_still_refuses_without_a_token(self):
        """The alias must not weaken the loopback-or-token guard."""
        from services.mcp.searchos_mcp import require_http_token_or_loopback

        assert require_http_token_or_loopback("0.0.0.0") is not None
        assert require_http_token_or_loopback("127.0.0.1") is None
