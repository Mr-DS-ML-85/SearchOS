from __future__ import annotations

import asyncio
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


class CrossEncoderReranker:
    """Semantic reranking with a cross-encoder over (query, document) pairs.

    The heuristic reranker multiplies a fused score by boosts. Every candidate
    then keeps the reciprocal-rank-fusion magnitude, so good and bad matches
    clustered within a hair of each other (0.016 vs 0.017) and a nonsense query
    returned the same confident-looking rows as a real one.

    A cross-encoder reads the query and the document together and emits a
    calibrated relevance score, which is what makes a threshold meaningful.
    """

    def __init__(self, model_name: str, *, batch_size: int = 32) -> None:
        self.model_name = model_name
        self.batch_size = batch_size
        self._model = None
        self._loaded = False

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _ensure_model(self):
        if self._loaded:
            return self._model
        self._loaded = True
        try:
            from sentence_transformers import CrossEncoder

            self._model = CrossEncoder(self.model_name)
        except Exception:
            self._model = None
        return self._model

    def score(self, query: str, pairs: list[tuple[str, str]]) -> list[float]:
        """Return one relevance score per (title, snippet) pair."""
        model = self._ensure_model()
        if model is None:
            return []
        payload = [(query, f"{title}\n\n{snippet}") for title, snippet in pairs]
        try:
            raw = model.predict(payload, batch_size=self.batch_size, show_progress_bar=False)
        except Exception:
            return []
        # Returned raw, as the cross-encoder's relevance logit. Squashing it
        # through a sigmoid looks friendlier but saturates: measured on this
        # corpus, relevant queries scored 4.94-9.34 and nonsense 1.16, which
        # sigmoid collapses to 0.995-1.0 and 0.76 — a gap far too narrow to put
        # a threshold in. The logit scale is what actually separates them.
        return [float(x) for x in raw]

    def warm(self) -> bool:
        """Load the model up front so the first query does not pay for it."""
        return self._ensure_model() is not None


_cross_encoder: CrossEncoderReranker | None = None


class Reranker:
    """Reorder fused candidates, preferring a cross-encoder when one is available.

    Final score is the cross-encoder relevance when available, otherwise the
    fused score scaled by the heuristic boosts.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        top_k: int = 20,
        domain_weights: dict[str, float] | None = None,
        engine_weights: dict[str, float] | None = None,
        cross_encoder: CrossEncoderReranker | None = None,
        candidate_pool: int = 30,
    ) -> None:
        self.enabled = enabled
        self.top_k = top_k
        self.domain_weights = domain_weights or {}
        self.engine_weights = engine_weights or {}
        self.cross_encoder = cross_encoder
        self.candidate_pool = max(candidate_pool, top_k)

    def _boost(self, query: str, r: SearchResult) -> float:
        terms = query_terms(query)
        title_terms = query_terms(r.title or "")
        overlap = len(terms & title_terms)

        if overlap >= 2:
            title_boost = 1.5
        elif overlap == 1:
            title_boost = 1.2
        else:
            title_boost = 1.0

        published_boost = 1.05 if r.published else 1.0
        domain_weight = self.domain_weights.get((r.domain or "").lower(), 1.0)
        engine_weight = self.engine_weights.get((r.source or "").lower(), 1.0)
        return title_boost * published_boost * domain_weight * engine_weight

    async def rerank_async(self, query: str, results: list[SearchResult]) -> list[SearchResult]:
        if not self.enabled or not results:
            return results

        if self.cross_encoder is not None:
            # Only the head of the list can survive into the page, so scoring
            # every fused candidate is wasted inference — an all-sources query
            # can fuse 100+, and each one is a forward pass.
            pool = sorted(results, key=lambda x: x.score, reverse=True)[: self.candidate_pool]
            tail = [r for r in results if r not in pool]
            scores = await asyncio.to_thread(
                self.cross_encoder.score,
                query,
                [(r.title or "", r.snippet or "") for r in pool],
            )
            if scores and len(scores) == len(pool):
                for r, score in zip(pool, scores, strict=True):
                    r.score = round(score, 6)
                return sorted(pool, key=lambda x: x.score, reverse=True)[: self.top_k] + tail

        return self.rerank(query, results)

    def rerank(self, query: str, results: list[SearchResult]) -> list[SearchResult]:
        if not self.enabled or not results:
            return results

        for r in results:
            r.score = round(r.score * self._boost(query, r), 6)

        ranked = sorted(results, key=lambda x: x.score, reverse=True)
        return ranked[: self.top_k]


def get_cross_encoder() -> CrossEncoderReranker:
    """Return a process-wide cross-encoder singleton.

    The orchestrator is rebuilt per request, so a per-instance reranker reloaded
    the model from disk on every search — that alone turned a 126ms local query
    into 2.4s, and a cold all-sources query into 26s.
    """
    global _cross_encoder
    if _cross_encoder is None:
        settings = get_settings()
        _cross_encoder = CrossEncoderReranker(settings.rerank_model, batch_size=settings.rerank_batch_size)
    return _cross_encoder


def build_reranker(settings=None) -> Reranker:
    cfg = settings or get_settings()
    return Reranker(enabled=cfg.rerank_enabled, top_k=cfg.rerank_top_k, cross_encoder=get_cross_encoder())
