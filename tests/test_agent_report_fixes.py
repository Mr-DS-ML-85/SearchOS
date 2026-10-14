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


class TestModelLoadRace:
    def test_score_during_warmup_waits_for_the_model(self, monkeypatch):
        """A search mid-warm-up used to get no scores and fall back to the heuristic."""
        import threading
        import time
        import types

        class SlowCrossEncoder:
            def __init__(self, name):
                time.sleep(0.3)

            def predict(self, payload, **kw):
                return [7.5 for _ in payload]

        monkeypatch.setitem(sys.modules, "sentence_transformers", types.SimpleNamespace(CrossEncoder=SlowCrossEncoder))
        from app.search.rerank import CrossEncoderReranker

        ce = CrossEncoderReranker("fake")
        warm = threading.Thread(target=ce.warm)
        warm.start()
        time.sleep(0.05)  # warm-up is now mid-load
        assert ce.score("q", [("title", "snippet")]) == [7.5]
        warm.join()


class TestImageTopicality:
    """Real cases from the index: CLIP matched genre ("diagram"), not topic."""

    def _row(self, alt, page, url="https://x.org/i.png"):
        import types

        return types.SimpleNamespace(alt_text=alt, source_url=page, url=url)

    def test_off_topic_diagram_is_rejected(self):
        from app.api.images import _mentions, _topical_terms

        terms = _topical_terms("event loop diagram asyncio")
        assert terms == {"event", "loop", "asyncio"}
        sql = self._row(
            "Diagram showing interaction between hash index and buckets.", "https://learn.microsoft.com/sql"
        )
        assert not _mentions(terms, sql)

    def test_topic_in_page_url_vouches_for_an_unlabelled_photo(self):
        from app.api.images import _mentions, _topical_terms

        yacht = self._row(None, "https://en.wikipedia.org/wiki/Sailing_yacht")
        assert _mentions(_topical_terms("a sailing boat on the sea"), yacht)

    def test_query_of_only_generic_words_is_not_filtered(self):
        from app.api.images import _mentions, _topical_terms

        assert _mentions(_topical_terms("diagram"), self._row(None, None))


class TestFederatedAsksForIndexOnly:
    async def test_peer_is_never_asked_to_search_the_web(self, monkeypatch):
        """sources=all made each peer run its own ~5s web cascade, at the 5s timeout."""
        import httpx
        from app.search.providers.federated import FederatedProvider

        asked: list[str] = []

        class Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"results": [{"url": f"https://p.com/{asked[-1]}", "title": "t", "snippet": "s"}]}

        class Client:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *e):
                return False

            async def get(self, url, params=None, headers=None):
                asked.append(params["sources"])
                return Resp()

        monkeypatch.setattr(httpx, "AsyncClient", Client)
        out = await FederatedProvider([("a", "http://a"), ("b", "http://b")]).search(ProviderQuery(q="x"))
        assert sorted(asked) == ["local", "local", "vector", "vector"]
        assert out.error is None and {r.url for r in out.results} == {"https://p.com/local", "https://p.com/vector"}


class TestCascadeTierLatency:
    async def test_failed_tier_reports_its_real_latency(self):
        """A tier that failed inside the cascade was reported as 0ms."""
        from app.search.providers.cascade import CascadeProvider

        class Slow:
            name = "searxng"

            async def search(self, query):
                await asyncio.sleep(0.05)
                return ProviderResult(provider=self.name, query=query.q, error="searxng: timeout")

        class Ok:
            name = "google"

            async def search(self, query):
                r = ProviderResult(provider=self.name, query=query.q)
                r.results = [SearchResult(title="t", url="https://g.com", snippet="", source="google", score=1, rank=1)]
                return r

        out = await CascadeProvider([Slow(), Ok()]).search(ProviderQuery(q="x"))
        assert out.raw["tier_latency_ms"]["searxng"] >= 40
        assert "google" in out.raw["tier_latency_ms"]


class TestHtmlEntitiesDecoded:
    async def test_web_titles_and_snippets_are_unescaped(self, cache, monkeypatch):
        """'Parameters &amp; Best Practices' reached callers and the index verbatim."""
        from app.search import orchestrator as orch_mod

        async def no_persist(*a, **k):
            return {}

        monkeypatch.setattr(orch_mod, "persist_web_results", no_persist)

        class Web:
            name = "web"

            async def search(self, query):
                r = ProviderResult(provider="web", query=query.q)
                r.results = [
                    SearchResult(
                        title="Tuning: 10 Parameters &amp; Tips",
                        url="https://w.com/a",
                        snippet="a &lt;b&gt; &#39;c&#39;",
                        source="searxng",
                        score=1,
                        rank=1,
                    )
                ]
                return r

        orch = SearchOrchestrator(providers={"web": Web()}, cache=cache)
        orch.settings.rerank_enabled = False
        orch.semantic_cache.enabled = False
        resp = await orch.search(SearchRequest(q="tuning", cache=False))
        assert resp.results[0].title == "Tuning: 10 Parameters & Tips"
        assert resp.results[0].snippet == "a <b> 'c'"


class TestFederationModes:
    def _client(self, monkeypatch, asked, results=True):
        import httpx

        class Resp:
            def raise_for_status(self):
                pass

            def json(self):
                src = asked[-1]["sources"]
                return {"results": [{"url": f"https://p.com/{src}", "title": "t", "snippet": "s"}] if results else []}

        class Client:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *e):
                return False

            async def get(self, url, params=None, headers=None):
                asked.append({"sources": params["sources"], "hop": headers.get("X-SearchOS-Federated-Hop")})
                return Resp()

        monkeypatch.setattr(httpx, "AsyncClient", Client)

    async def test_web_mode_asks_peers_to_search_the_web_and_marks_the_hop(self, monkeypatch):
        """Pooling web quota across self-hosters: peers run the web search for us."""
        from app.search.providers.federated import FederatedProvider

        asked: list = []
        self._client(monkeypatch, asked)
        p = FederatedProvider([("a", "http://a")], mode="web", name="federated_web")
        out = await p.search(ProviderQuery(q="x"))
        assert asked == [{"sources": "web", "hop": "1"}]
        assert out.results[0].source == "federated_web:a"

    async def test_hop_request_never_federates_again(self, monkeypatch):
        """Two rate-limited peers would otherwise forward one query back and forth."""
        from app.search.providers.federated import FederatedProvider

        asked: list = []
        self._client(monkeypatch, asked)
        out = await FederatedProvider([("a", "http://a")], mode="web").search(ProviderQuery(q="x", federated_hop=True))
        assert asked == [] and "hop" in out.error

    async def test_cascade_skips_federated_web_on_a_hop(self):
        from app.search.providers.cascade import CascadeProvider

        class Tier:
            def __init__(self, name):
                self.name, self.called = name, False

            async def search(self, query):
                self.called = True
                return ProviderResult(provider=self.name, query=query.q, error="down")

        own, peers = Tier("searxng"), Tier("federated_web")
        await CascadeProvider([own, peers]).search(ProviderQuery(q="x", federated_hop=True))
        assert own.called and not peers.called

    def test_orchestrator_excludes_federation_on_a_hop(self, cache):
        class P:
            def __init__(self, name):
                self.name = name

        orch = SearchOrchestrator(providers={n: P(n) for n in ("local", "federated", "federated_web")}, cache=cache)
        assert {p.name for p in orch.select_providers("all", federated_hop=True)} == {"local"}
        assert orch.select_providers("federated", federated_hop=True) == []

    async def test_empty_answer_from_healthy_peer_is_not_degraded(self, monkeypatch):
        from app.search.providers.federated import FederatedProvider

        asked: list = []
        self._client(monkeypatch, asked, results=False)
        out = await FederatedProvider([("a", "http://a")]).search(ProviderQuery(q="x"))
        assert out.error is None and out.raw["empty"] is True

    def test_peer_sources_all_uses_the_web_timeout(self, monkeypatch):
        from app.config import get_settings
        from app.search.providers.federated import build_federated, build_federated_web

        s = get_settings()
        monkeypatch.setattr(s, "federated_endpoints", "a=http://a")
        monkeypatch.setattr(s, "federated_peer_sources", "all")
        fed = build_federated(s)
        assert fed.mode == "all" and fed.timeout == s.federated_web_timeout_seconds
        web = build_federated_web(s)
        assert web.name == "federated_web" and web.mode == "web"


class TestSearxngDomains:
    async def test_site_operator_reaches_searxng_and_results_are_filtered(self, monkeypatch):
        """site:arxiv.org searched the whole web: pinned it returned other domains."""
        import httpx
        from app.search.providers.searxng import SearXNGProvider

        sent: list[dict] = []

        class Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "results": [
                        {"url": "https://www.geeksforgeeks.org/x", "title": "g", "content": "c"},
                        {"url": "https://arxiv.org/abs/1706.03762", "title": "a", "content": "c"},
                        {"url": "https://export.arxiv.org/abs/2", "title": "b", "content": "c"},
                    ]
                }

        class Client:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *e):
                return False

            async def get(self, url, params=None):
                sent.append(params)
                return Resp()

        monkeypatch.setattr(httpx, "AsyncClient", Client)
        p = SearXNGProvider(None, "http://searxng", 5.0)
        q = ProviderQuery(q="transformer attention", domains=["arxiv.org"], exclude_domains=["export.arxiv.org"])
        out = await p.search(q)
        assert sent[0]["q"] == "transformer attention site:arxiv.org -site:export.arxiv.org"
        assert [r.url for r in out.results] == ["https://arxiv.org/abs/1706.03762"]
        assert out.results[0].rank == 1


class TestDatabaseUnreachable:
    def test_dns_failure_on_ops_endpoint_is_a_clean_503(self, monkeypatch):
        """A postgres DNS failure turned every /metrics scrape into a 500 + traceback."""
        import socket

        from app import db
        from app.config import get_settings
        from app.main import create_app
        from fastapi.testclient import TestClient

        s = get_settings()
        monkeypatch.setattr(s, "auth_enabled", True)
        monkeypatch.setattr(s, "rate_limit_health_endpoints", True)

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *e):
                return False

            async def scalar(self, *a, **k):
                raise socket.gaierror(-2, "Name or service not known")

        monkeypatch.setattr(db, "get_session_factory", lambda: Session)
        client = TestClient(create_app(), raise_server_exceptions=False)  # no lifespan: no real DB needed
        resp = client.get("/metrics", headers={"Authorization": "Bearer sk_test"})
        assert resp.status_code == 503
        assert resp.json()["code"] == "database_unavailable"
        assert resp.headers["retry-after"] == "5"
