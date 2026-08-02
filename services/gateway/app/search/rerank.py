from __future__ import annotations

from dataclasses import dataclass, field

from app.schemas import SearchResult


@dataclass
class Reranker:
    enabled: bool = True
    top_k: int = 20
    domain_weights: dict[str, float] = field(default_factory=dict)
    engine_weights: dict[str, float] = field(default_factory=dict)

    def rerank(self, query: str, results: list[SearchResult]) -> list[SearchResult]:
        if not self.enabled or not results:
            return results

        q_terms = set(query.lower().split())
        for r in results:
            boost = 1.0
            title = (r.title or "").lower()
            snippet = (r.snippet or "").lower()
            overlap = sum(1 for t in q_terms if t in title)
            if overlap >= 2:
                boost *= 1.5
            elif overlap == 1:
                boost *= 1.2
            if r.published:
                boost *= 1.05
            domain = (r.domain or "").lower()
            boost *= self.domain_weights.get(domain, 1.0)
            engine = (r.source or "").lower()
            boost *= self.engine_weights.get(engine, 1.0)
            r.score = round(r.score * boost, 6)

        ranked = sorted(results, key=lambda x: x.score, reverse=True)
        for i, r in enumerate(ranked, start=1):
            r.rank = i
        return ranked[: self.top_k]
