"""Regressions for the 2026-10-11 MCP agent report and the bugs found behind it."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from app.errors import ValidationError
from app.schemas import SearchRequest, SearchResult
from app.search.base import ProviderQuery, ProviderResult
from app.search.orchestrator import SearchOrchestrator, decode_cursor, encode_cursor

MCP_DIR = Path(__file__).resolve().parents[1] / "services" / "mcp"
sys.path.insert(0, str(MCP_DIR))


class ListProvider:
    name = "local"

    def __init__(self, n: int):
        self.rows = [
            SearchResult(title=f"T{i}", url=f"https://e.com/{i}", snippet="s", source="local", score=100 - i, rank=i)
            for i in range(1, n + 1)
        ]

    async def search(self, query: ProviderQuery) -> ProviderResult:
        return ProviderResult(provider=self.name, query=query.q, results=[r.model_copy() for r in self.rows])


def _orch(cache, n=6) -> SearchOrchestrator:
    orch = SearchOrchestrator(providers={"local": ListProvider(n)}, cache=cache)
    orch.settings.rerank_enabled = False
    orch.semantic_cache.enabled = False
    return orch


class TestCursorPaging:
    async def test_next_cursor_fetches_the_following_page(self, cache):
        orch = _orch(cache)
        first = await orch.search(SearchRequest(q="paging", num_results=2, cache=False))
        assert first.has_more and first.next_cursor
        second = await orch.search(SearchRequest(q="paging", num_results=2, cursor=first.next_cursor, cache=False))
        assert [r.rank for r in second.results] == [3, 4]

    async def test_cursor_from_another_query_is_rejected(self, cache):
        """The fingerprint was written into every cursor and never checked."""
        orch = _orch(cache)
        first = await orch.search(SearchRequest(q="paging", num_results=2, cache=False))
        with pytest.raises(ValidationError):
            await orch.search(SearchRequest(q="other query", num_results=2, cursor=first.next_cursor, cache=False))

    async def test_cursor_is_bound_to_filters(self, cache):
        orch = _orch(cache)
        first = await orch.search(SearchRequest(q="paging", num_results=2, cache=False))
        with pytest.raises(ValidationError):
            await orch.search(
                SearchRequest(q="paging", num_results=2, cursor=first.next_cursor, language="fr", cache=False)
            )

    def test_decode_without_fingerprint_still_reads_offset(self):
        assert decode_cursor(encode_cursor(7, "abc")) == 7
        assert decode_cursor(encode_cursor(7, "abc"), "xyz") is None


class TestPageCacheIdentity:
    async def test_same_window_different_offset_does_not_collide(self, cache):
        """offset=0&n=4 and offset=2&n=2 share window=4 and used to share a key."""
        orch = _orch(cache)
        await orch.search(SearchRequest(q="collide", num_results=4, cache=True))
        page = await orch.search(SearchRequest(q="collide", num_results=2, offset=2, cache=True))
        assert [r.rank for r in page.results] == [3, 4]


class TestFreshnessAlias:
    def test_freshness_sets_time_range(self):
        assert SearchRequest(q="x", freshness="week").time_range == "week"

    def test_explicit_time_range_wins(self):
        assert SearchRequest(q="x", freshness="week", time_range="day").time_range == "day"


class TestLanguageFilter:
    def test_iso_codes_map_to_stored_names(self):
        from app.search.langdetect import language_name

        assert language_name("fr") == "french"
        assert language_name("fr-CA") == "french"
        assert language_name("french") == "french"
        assert language_name("xx") is None

    def test_local_filter_uses_stored_name(self):
        from app.search.local_index import LocalIndexProvider

        sql, params = LocalIndexProvider(None).filter_sql(ProviderQuery(q="x", language="fr"))
        assert "d.lang = :req_lang" in sql and params["req_lang"] == "french"

    def test_unknown_language_matches_nothing_rather_than_everything(self):
        from app.search.local_index import LocalIndexProvider

        sql, _ = LocalIndexProvider(None).filter_sql(ProviderQuery(q="x", language="xx"))
        assert "AND false" in sql


class TestVectorSql:
    def test_ranked_by_similarity_after_distinct_on(self):
        """LIMIT inside DISTINCT ON (d.id) kept the first N documents by UUID."""
        from app.search.vector_index import build_sql

        sql = " ".join(str(build_sql()).split())
        assert sql.index("ORDER BY d.id") < sql.index(") best ORDER BY similarity DESC LIMIT :limit")

    def test_filters_are_applied(self):
        from app.search.vector_index import build_sql

        assert "AND d.lang = :req_lang" in str(build_sql(" AND d.lang = :req_lang"))


class TestImageUrls:
    def test_html_entities_are_unescaped(self):
        from app.search.image_index import extract_image_urls

        html = '<img src="https://x.org/photo.jpg?a=1&amp;b=2" width="400" height="300">'
        assert extract_image_urls(html, "https://x.org/", 5)[0][0] == "https://x.org/photo.jpg?a=1&b=2"


@pytest.fixture
def mcp():
    import importlib

    import searchos_mcp

    return importlib.reload(searchos_mcp)


class _Resp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _Client:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None, headers=None):
        self.calls.append(params)
        return _Resp(self.payload)


class _Params:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class TestMcpSearch:
    def test_next_cursor_reaches_the_agent(self, mcp, monkeypatch):
        import json

        client = _Client({"query": "x", "results": [], "has_more": True, "next_cursor": "abc"})
        monkeypatch.setattr(mcp.httpx, "AsyncClient", lambda **kw: client)
        result = asyncio.run(mcp.handle_search(None, _Params("search", {"query": "x"})))
        assert json.loads(result.content[0].text)["next_cursor"] == "abc"

    # Both stub a *successful* gateway, so an error can only come from the
    # handler's own validation rather than from an unreachable gateway.
    def test_cursor_and_offset_together_is_an_error(self, mcp, monkeypatch):
        client = _Client({"query": "x", "results": []})
        monkeypatch.setattr(mcp.httpx, "AsyncClient", lambda **kw: client)
        result = asyncio.run(mcp.handle_search(None, _Params("search", {"query": "x", "cursor": "abc", "offset": 5})))
        assert result.is_error and not client.calls

    def test_images_reject_both_inputs(self, mcp, monkeypatch):
        client = _Client({"query": "boat", "images": [], "image_search_available": True})
        monkeypatch.setattr(mcp.httpx, "AsyncClient", lambda **kw: client)
        args = {"query": "boat", "image_url": "https://x.org/a.jpg"}
        result = asyncio.run(mcp.handle_search_images(None, _Params("search_images", args)))
        assert result.is_error and not client.calls
