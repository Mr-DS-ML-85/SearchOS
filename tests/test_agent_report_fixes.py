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


class TestTotalIsTheMatchCount:
    async def test_reranker_does_not_cap_total(self, cache):
        """25 matches used to report total=20: the reranker cut at RERANK_TOP_K."""
        orch = SearchOrchestrator(providers={"local": ListProvider(25)}, cache=cache)
        orch.settings.rerank_enabled = True  # heuristic path; no cross-encoder in tests
        orch.reranker.cross_encoder = None
        orch.reranker.enabled = True
        orch.semantic_cache.enabled = False
        resp = await orch.search(SearchRequest(q="count", num_results=10, cache=False))
        assert resp.total == 25 and resp.returned == 10 and resp.has_more
        last = await orch.search(SearchRequest(q="count", num_results=10, offset=20, cache=False))
        assert [r.rank for r in last.results] == [21, 22, 23, 24, 25] and not last.has_more


class TestSettingsIsolation:
    def test_a_mutates_global_settings(self):
        from app.config import get_settings

        get_settings().rerank_enabled = False

    def test_b_sees_the_original_value(self):
        from app.config import Settings, get_settings

        assert get_settings().rerank_enabled == Settings().rerank_enabled


class TestSemanticReplay:
    async def test_has_more_follows_stored_total_not_snapshot(self, cache):
        """A 3-result snapshot of 10 matches replayed as has_more=false, no cursor."""
        stored = {
            "query": "orig",
            "results": [
                SearchResult(
                    title=f"T{i}", url=f"https://e.com/{i}", snippet="", source="local", score=1, rank=i
                ).model_dump()
                for i in (1, 2, 3)
            ],
            "total": 10,
            "returned": 3,
            "has_more": True,
            "took_ms": 1,
            "cache_hit": False,
            "degraded": [],
            "providers": ["local"],
        }

        class FakeSemantic:
            enabled = True
            threshold = 0.8

            async def lookup(self, query, scope, embedding=None, *, min_results=0):
                return dict(stored), "orig", 0.98, "matched"

        orch = _orch(cache)
        orch.semantic_cache = FakeSemantic()
        resp = await orch.search(SearchRequest(q="paraphrase", num_results=3, cache=True))
        assert resp.cache_type == "semantic"
        assert resp.has_more is True and resp.next_cursor


class TestMissReason:
    async def test_nothing_in_scope_is_empty_not_below_threshold(self, monkeypatch):
        from app.search import semantic_cache as sc

        entry = '{"q": "other", "k": "k", "e": [1.0, 0.0], "t": 9e18, "s": "federated|x", "off": 0, "n": 10}'

        class FakeCache:
            async def lrange(self, *a):
                return [entry]

        monkeypatch.setattr(sc, "get_cache", lambda: FakeCache())
        cache = sc.SemanticCache()
        cache.enabled = True
        out = await cache.lookup("q", "all|y", embedding=[1.0, 0.0])
        assert out[3] == "empty" and out[1] is None
