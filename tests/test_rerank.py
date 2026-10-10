from __future__ import annotations

from app.schemas import SearchResult
from app.search.rerank import Reranker


def mk(
    title: str,
    score: float = 1.0,
    published: str | None = None,
    source: str = "local",
    domain: str = "example.com",
    url: str | None = None,
) -> SearchResult:
    return SearchResult(
        title=title,
        url=url or f"https://{domain}/{abs(hash(title))}",
        snippet="",
        source=source,
        score=score,
        rank=1,
        published=published,
        domain=domain,
    )


def test_rerank_boosts_title_matches():
    r1 = mk("Introduction to Machine Learning")
    r2 = mk("Something else entirely")
    reranked = Reranker(enabled=True, top_k=10).rerank("machine learning", [r2, r1])
    assert reranked[0].title == "Introduction to Machine Learning"


def test_rerank_disabled_passthrough():
    results = [mk("A", score=2.0), mk("B", score=1.0)]
    reranked = Reranker(enabled=False).rerank("x", results)
    assert reranked == results


def test_rerank_respects_top_k():
    results = [mk(f"Result {i}", score=float(i)) for i in range(20)]
    reranked = Reranker(enabled=True, top_k=5).rerank("query", results)
    assert len(reranked) == 5


def test_rerank_domain_weights():
    r1 = mk("A", score=1.0, domain="wikipedia.org")
    r2 = mk("B", score=1.0, domain="random-blog.com")
    weights = {"wikipedia.org": 1.5, "random-blog.com": 1.0}
    reranked = Reranker(enabled=True, top_k=10, domain_weights=weights).rerank("query", [r2, r1])
    assert reranked[0].url == r1.url
