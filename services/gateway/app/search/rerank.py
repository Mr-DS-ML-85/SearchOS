from __future__ import annotations

import re

from app.config import get_settings
from app.schemas import SearchResult

# Token pattern shared with the FTS provider so query terms and title terms are
# compared the same way. The previous `query.lower().split()` test was a
# *substring* check, so "art deep" boosted "P-artial derivative" and the query
# "gpu, cuda?" produced terms {"gpu,", "cuda?"} that never matched anything.
TERM_RE = re.compile(r"[0-9A-Za-z_]+|[^\W\d_]+", re.UNICODE)


def query_terms(query: str) -> set[str]:
    return {t for t in TERM_RE.findall(query.lower()) if len(t) > 1}


class Reranker:
    """Heuristic reordering on top of fused candidates.

    Final score = fused_rrf * title_boost * freshness_boost * domain_weight * engine_weight
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        top_k: int = 20,
        domain_weights: dict[str, float] | None = None,
        engine_weights: dict[str, float] | None = None,
    ) -> None:
        self.enabled = enabled
        self.top_k = top_k
        self.domain_weights = domain_weights or {}
        self.engine_weights = engine_weights or {}

    def rerank(self, query: str, results: list[SearchResult]) -> list[SearchResult]:
        if not self.enabled or not results:
            return results

        terms = query_terms(query)
        for r in results:
            title_terms = query_terms(r.title or "")
            overlap = len(terms & title_terms)

            if overlap >= 2:
                title_boost = 1.5
            elif overlap == 1:
                title_boost = 1.2
            else:
                title_boost = 1.0

            # Presence of a date only, not recency: ranking by age would make
            # scores non-reproducible for identical documents.
            published_boost = 1.05 if r.published else 1.0
            domain_weight = self.domain_weights.get((r.domain or "").lower(), 1.0)
            engine_weight = self.engine_weights.get((r.source or "").lower(), 1.0)

            r.score = round(
                r.score * title_boost * published_boost * domain_weight * engine_weight,
                6,
            )

        ranked = sorted(results, key=lambda x: x.score, reverse=True)
        return ranked[: self.top_k]


def build_reranker(settings=None) -> Reranker:
    cfg = settings or get_settings()
    return Reranker(enabled=cfg.rerank_enabled, top_k=cfg.rerank_top_k)
