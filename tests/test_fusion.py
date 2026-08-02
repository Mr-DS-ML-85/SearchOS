from __future__ import annotations

import pytest

from app.search.fusion import reciprocal_rank_fusion
from app.schemas import SearchResult


def mk(title: str, url: str, source: str, score: float = 1.0, rank: int = 1) -> SearchResult:
    return SearchResult(
        title=title,
        url=url,
        snippet="",
        source=source,
        score=score,
        rank=rank,
    )


def test_rrf_prefers_high_rank_across_providers():
    a = [mk("A1", "https://a.com/1", "p1", rank=1), mk("A2", "https://a.com/2", "p1", rank=2)]
    b = [mk("B1", "https://a.com/2", "p2", rank=1), mk("B2", "https://b.com/2", "p2", rank=2)]

    merged = reciprocal_rank_fusion({"p1": a, "p2": b}, k=60)
    urls = [r.url for r in merged]
    assert urls[0] == "https://a.com/2"


def test_rrf_deduplicates_by_url():
    a = [mk("X", "https://x.com/1", "p1", rank=1)]
    b = [mk("X", "https://x.com/1", "p2", rank=3)]

    merged = reciprocal_rank_fusion({"p1": a, "p2": b}, k=60)
    assert len(merged) == 1
    assert merged[0].url == "https://x.com/1"


def test_rrf_ranks_single_provider_consistently():
    a = [mk("A", "https://a.com/1", "p1", rank=1), mk("B", "https://a.com/2", "p1", rank=2)]
    merged = reciprocal_rank_fusion({"p1": a}, k=60)
    assert [r.url for r in merged] == ["https://a.com/1", "https://a.com/2"]


def test_rrf_empty_input():
    assert reciprocal_rank_fusion({}) == []
