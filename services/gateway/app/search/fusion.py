from __future__ import annotations

from collections import OrderedDict

from app.config import get_settings
from app.schemas import SearchResult


def reciprocal_rank_fusion(
    provider_results: dict[str, list[SearchResult]],
    *,
    k: int | None = None,
) -> list[SearchResult]:
    """Merge ranked lists from multiple providers using Reciprocal Rank Fusion.

    The same page may appear from several providers (e.g. a live SearXNG hit for
    a URL that is already indexed locally). Such duplicates are collapsed to a
    single result keyed by normalized URL; when sources conflict we keep the
    indexed (local/vector) version because it carries the full crawled content,
    while the RRF score still accumulates from every list it appeared in.
    """
    from app.workers.ingest import normalize_url

    if k is None:
        k = get_settings().rrf_k

    def _src_priority(source: str) -> int:
        return 2 if source in ("local", "vector") else 1

    fused: dict[str, tuple[float, SearchResult]] = {}
    for results in provider_results.values():
        for rank, result in enumerate(results, start=1):
            key = normalize_url(result.url)
            increment = 1.0 / (k + rank)
            if key in fused:
                score, kept = fused[key]
                if _src_priority(result.source) > _src_priority(kept.source):
                    kept = result
                fused[key] = (score + increment, kept)
            else:
                fused[key] = (increment, result)

    ordered = OrderedDict(sorted(fused.items(), key=lambda item: item[1][0], reverse=True))
    merged: list[SearchResult] = []
    for rank, (_key, (score, result)) in enumerate(ordered.items(), start=1):
        merged.append(
            SearchResult(
                title=result.title,
                url=result.url,
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
