from __future__ import annotations

from collections import OrderedDict

from app.config import get_settings
from app.schemas import SearchResult


def reciprocal_rank_fusion(
    provider_results: dict[str, list[SearchResult]],
    *,
    k: int | None = None,
) -> list[SearchResult]:
    """Merge ranked lists from multiple providers using Reciprocal Rank Fusion."""
    if k is None:
        k = get_settings().rrf_k

    fused: dict[str, tuple[float, SearchResult]] = {}
    for provider, results in provider_results.items():
        for rank, result in enumerate(results, start=1):
            url = result.url
            if url in fused:
                existing_score, existing = fused[url]
                fused[url] = (existing_score + 1.0 / (k + rank), existing)
            else:
                fused[url] = (1.0 / (k + rank), result)

    ordered = OrderedDict(
        sorted(fused.items(), key=lambda item: item[1][0], reverse=True)
    )
    merged: list[SearchResult] = []
    for rank, (url, (score, result)) in enumerate(ordered.items(), start=1):
        merged.append(
            SearchResult(
                title=result.title,
                url=url,
                snippet=result.snippet,
                source=result.source,
                score=round(score, 6),
                rank=rank,
                published=result.published,
                domain=result.domain,
                favicon=result.favicon,
                raw=result.raw,
            )
        )
    return merged
