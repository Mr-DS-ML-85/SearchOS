from __future__ import annotations

import pytest

from app.cache import cache_key
from app.schemas import SearchRequest, SearchResult
from app.search.base import ProviderQuery, ProviderResult
from app.search.orchestrator import SearchOrchestrator
from app.search.rerank import Reranker


class FakeProvider:
    def __init__(self, name: str, results: list[SearchResult], error: str | None = None):
        self.name = name
        self._results = results
        self._error = error

    async def search(self, query: ProviderQuery) -> ProviderResult:
        return ProviderResult(
            provider=self.name,
            query=query.q,
            results=[self._results[i % len(self._results)] for i in range(min(len(self._results), query.num_results))] if self._results else [],
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


async def test_orchestrator_raises_when_all_providers_fail(cache):
    p1 = FakeProvider("local", [], error="down")
    orch = SearchOrchestrator(providers={"local": p1}, cache=cache)

    from app.errors import SearchNoResultsError

    with pytest.raises(SearchNoResultsError):
        await orch.search(SearchRequest(q="test", cache=False))


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

    assert len(page1.results) == 10
    assert len(page2.results) == 10
    assert page1.results[0].url != page2.results[0].url
    assert page1.results[0].rank == 1
    assert page2.results[0].rank == 11
    assert page1.results[9].url != page2.results[0].url


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
