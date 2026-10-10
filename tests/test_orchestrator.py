from __future__ import annotations

import pytest
from app.cache import cache_key
from app.schemas import SearchRequest, SearchResult
from app.search.base import ProviderQuery, ProviderResult
from app.search.orchestrator import SearchOrchestrator
from app.search.rerank import Reranker


class FakeProvider:
    """Provider double that behaves like a real one.

    It honours ``num_results`` and asserts that the orchestrator sends
    ``offset=0`` — providers must never paginate, because that previously
    double-counted the offset and made results 11-20 unreachable.
    """

    def __init__(self, name: str, results: list[SearchResult], error: str | None = None):
        self.name = name
        self._results = results
        self._error = error
        self.seen_offsets: list[int] = []

    async def search(self, query: ProviderQuery) -> ProviderResult:
        self.seen_offsets.append(query.offset)
        rows = self._results[: query.num_results] if self._results else []
        return ProviderResult(
            provider=self.name,
            query=query.q,
            results=[r.model_copy() for r in rows],
            error=self._error,
        )


def mk(title: str, url: str, source: str, score: float = 1.0, rank: int = 1) -> SearchResult:
    return SearchResult(title=title, url=url, snippet="snip", source=source, score=score, rank=rank)


async def test_orchestrator_merges_providers(cache):
    p1 = FakeProvider(
        "local",
        [mk("A", "https://a.com/1", "local", rank=1), mk("B", "https://a.com/2", "local", rank=2)],
    )
    p2 = FakeProvider(
        "vector",
        [mk("B", "https://a.com/2", "vector", rank=1), mk("C", "https://a.com/3", "vector", rank=2)],
    )
    orch = SearchOrchestrator(providers={"local": p1, "vector": p2}, cache=cache)
    orch.settings.rerank_enabled = False

    resp = await orch.search(SearchRequest(q="test", num_results=10, cache=False))
    assert len(resp.results) == 3
    assert resp.providers == ["local", "vector"]
    assert resp.degraded == []
    assert resp.cache_hit is False


async def test_orchestrator_caches_results(cache):
    p1 = FakeProvider("local", [mk("A", "https://a.com/1", "local", rank=1)])
    orch = SearchOrchestrator(providers={"local": p1}, cache=cache)
    orch.settings.rerank_enabled = False

    first = await orch.search(SearchRequest(q="unique-query", cache=True))
    second = await orch.search(SearchRequest(q="unique-query", cache=True))
    assert first.cache_hit is False
    assert second.cache_hit is True


async def test_orchestrator_degrades_when_provider_fails(cache):
    p1 = FakeProvider("local", [], error="local index error")
    p2 = FakeProvider("vector", [mk("A", "https://a.com/1", "vector", rank=1)])
    orch = SearchOrchestrator(providers={"local": p1, "vector": p2}, cache=cache)
    orch.settings.rerank_enabled = False

    resp = await orch.search(SearchRequest(q="test", cache=False))
    assert resp.degraded == ["local"]
    assert len(resp.results) == 1


async def test_orchestrator_raises_provider_error_when_all_fail(cache):
    """All providers down is an upstream outage (502), not "no matches" (404)."""
    p1 = FakeProvider("local", [], error="down")
    orch = SearchOrchestrator(providers={"local": p1}, cache=cache)

    from app.errors import ProviderError

    with pytest.raises(ProviderError):
        await orch.search(SearchRequest(q="test", cache=False))


async def test_orchestrator_returns_empty_page_when_no_matches(cache):
    """A healthy provider with zero matches is a valid empty 200, not an error."""
    p1 = FakeProvider("local", [])
    orch = SearchOrchestrator(providers={"local": p1}, cache=cache)

    resp = await orch.search(SearchRequest(q="test", cache=False))
    assert resp.results == []
    assert resp.total == 0
    assert resp.degraded == []


async def test_orchestrator_raises_when_no_provider_configured(cache):
    """sources=web with no web provider must explain itself, not silently 404."""
    orch = SearchOrchestrator(providers={"local": FakeProvider("local", [])}, cache=cache)

    from app.errors import ProviderError

    with pytest.raises(ProviderError, match="No search provider is configured"):
        await orch.search(SearchRequest(q="test", sources="web", cache=False))


def test_cache_key_is_stable_and_bounded():
    k1 = cache_key("search", "query", "all", 10, None, "all")
    k2 = cache_key("search", "query", "all", 10, None, "all")
    assert k1 == k2
    long_key = cache_key("search", "x" * 500)
    assert len(long_key) <= 400


async def test_orchestrator_paginates_with_offset(cache):
    p1 = FakeProvider(
        "local",
        [mk(f"R{i}", f"https://a.com/{i}", "local", rank=i) for i in range(1, 31)],
    )
    orch = SearchOrchestrator(providers={"local": p1}, cache=cache)
    orch.settings.rerank_enabled = False

    page1 = await orch.search(SearchRequest(q="test", num_results=10, offset=0, cache=False))
    page2 = await orch.search(SearchRequest(q="test", num_results=10, offset=10, cache=False))
    page3 = await orch.search(SearchRequest(q="test", num_results=10, offset=20, cache=False))
    page4 = await orch.search(SearchRequest(q="test", num_results=10, offset=30, cache=False))

    # No gaps: page 2 starts exactly where page 1 ends. Previously the offset
    # was applied both in SQL and in the orchestrator, so page 2 skipped 11-20.
    assert len(page1.results) == 10
    assert len(page2.results) == 10
    assert len(page3.results) == 10
    assert page1.results[-1].url == "https://a.com/10"
    assert page2.results[0].url == "https://a.com/11"
    assert page3.results[0].url == "https://a.com/21"

    # Past the end is an empty 200, not a 404.
    assert page4.results == []
    assert page4.total == 30

    assert page1.results[0].rank == 1
    assert page2.results[0].rank == 11
    assert page3.results[0].rank == 21

    # total is the fused match count, stable across pages
    assert page1.total == page2.total == page3.total == 30

    # Providers never receive a non-zero offset.
    assert set(p1.seen_offsets) == {0}


async def test_cache_key_distinguishes_pages_and_filters(cache):
    k0 = cache_key("search", "q", "all", 10, None, "all")
    k10 = cache_key("search", "q", "all", 20, None, "all")
    assert k0 != k10


async def test_orchestrator_num_results_exceeds_rerank_top_k(cache):
    p1 = FakeProvider(
        "local",
        [mk(f"R{i}", f"https://a.com/{i}", "local", rank=i) for i in range(1, 31)],
    )
    orch = SearchOrchestrator(providers={"local": p1}, cache=cache)
    orch.reranker = Reranker(enabled=True, top_k=10)

    resp = await orch.search(SearchRequest(q="test", num_results=25, cache=False))
    assert len(resp.results) == 25


async def test_extract_cache_key_distinguishes_max_chars():
    import hashlib

    from app.cache import cache_key

    url = "https://example.com/page"
    h = hashlib.sha256(url.encode()).hexdigest()
    k_short = cache_key("extract", h, "markdown", 100)
    k_full = cache_key("extract", h, "markdown", "full")
    assert k_short != k_full
