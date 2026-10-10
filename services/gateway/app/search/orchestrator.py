from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from app.cache import Cache, cache_key, get_cache
from app.config import Settings, get_settings
from app.metrics import get_metrics
from app.schemas import SearchRequest, SearchResponse, SearchResult
from app.search.base import ProviderQuery, ProviderResult, SearchProvider
from app.search.fusion import reciprocal_rank_fusion
from app.search.rerank import Reranker, get_cross_encoder


@dataclass
class SearchOrchestrator:
    providers: dict[str, SearchProvider] = field(default_factory=dict)
    cache: Cache | None = None
    reranker: Reranker | None = None
    settings: Settings | None = None

    def __post_init__(self) -> None:
        if self.settings is None:
            self.settings = get_settings()
        if self.cache is None:
            self.cache = get_cache()
        if self.reranker is None:
            self.reranker = Reranker(
                enabled=self.settings.rerank_enabled,
                top_k=self.settings.rerank_top_k,
                candidate_pool=self.settings.rerank_candidate_pool,
                cross_encoder=(get_cross_encoder() if self.settings.rerank_model else None),
            )

    def select_providers(self, sources: str) -> list[SearchProvider]:
        if sources == "all":
            return list(self.providers.values())
        provider = self.providers.get(sources)
        return [provider] if provider else []

    async def search(self, req: SearchRequest) -> SearchResponse:
        start = time.monotonic()
        metrics = get_metrics()
        cache = self.cache
        # One deep page is fetched; pagination is applied exactly once, below.
        # offset is clamped so a client cannot force Postgres to walk an
        # arbitrarily large OFFSET on every request.
        offset = min(req.offset, self.settings.search_max_offset)
        window = offset + req.num_results
        cache_key_str = cache_key(
            "search",
            req.q.lower().strip(),
            req.sources,
            window,
            req.language,
            req.country,
            req.time_range,
            req.freshness,
            tuple(sorted(req.domains or ())),
            tuple(sorted(req.exclude_domains or ())),
        )

        if req.cache:
            cached = await cache.get(cache_key_str)
            if cached is not None:
                metrics.cache_hits.inc()
                cached["cache_hit"] = True
                cached["query"] = req.q
                # A cache hit must not report the latency of the original fetch.
                cached["took_ms"] = int((time.monotonic() - start) * 1000)
                return SearchResponse(**cached)

            metrics.cache_misses.inc()
        providers = self.select_providers(req.sources)
        if not providers:
            from app.errors import ProviderError

            raise ProviderError(
                f"No search provider is configured for sources={req.sources!r}. "
                "Enable the local index or set SEARXNG_URL."
            )
        query = ProviderQuery(
            q=req.q,
            language=req.language,
            country=req.country,
            time_range=req.time_range,
            domains=req.domains,
            exclude_domains=req.exclude_domains,
            # Candidates for the whole requested window, capped. Providers must
            # NOT receive req.offset — the orchestrator owns pagination.
            num_results=min(max(window, 20) * 2, self.settings.search_max_candidates),
            offset=0,
            skip_when_local_hit=(req.sources == "all" and req.time_range == "all"),
        )

        tasks = [provider.search(query) for provider in providers]
        provider_results: list[ProviderResult] = await asyncio.gather(*tasks, return_exceptions=True)

        by_provider: dict[str, list[SearchResult]] = {}
        degraded: list[str] = []
        for provider, outcome in zip(providers, provider_results, strict=True):
            if isinstance(outcome, BaseException):
                degraded.append(provider.name)
                metrics.provider_errors.labels(provider=provider.name, kind="exception").inc()
                continue
            if outcome.error:
                degraded.append(provider.name)
            else:
                metrics.upstream_latency.labels(provider=provider.name).observe(outcome.latency_ms / 1000)
            by_provider[provider.name] = outcome.results

        # Distinguish "index has no matches" (a valid empty 200) from "every
        # provider failed" (an upstream outage, which must not be reported as
        # 404 no_results).
        if degraded and not any(by_provider.values()):
            from app.errors import ProviderError

            raise ProviderError(f"All search providers failed: {', '.join(sorted(degraded))}")

        fused = reciprocal_rank_fusion(by_provider)
        # Total matches are known before the reranker truncates anything.
        total = len(fused)

        # Never let the reranker cap results below what the caller asked for.
        self.reranker.top_k = max(self.settings.rerank_top_k, window)
        ranked = await self.reranker.rerank_async(req.q, fused)

        # Drop rows the reranker scored as irrelevant. Only meaningful once a
        # cross-encoder produced the scores: the heuristic keeps every fused
        # score near 0.016, so cutting there would empty every result set.
        min_score = self.settings.rerank_min_score
        if self.reranker.cross_encoder is not None and self.reranker.cross_encoder.loaded:
            ranked = [r for r in ranked if r.score >= min_score]
            total = len(ranked)

        low_confidence = not ranked or ranked[0].score < self.settings.rerank_low_confidence_score

        # The one and only pagination slice.
        results = ranked[offset : offset + req.num_results]
        for i, r in enumerate(results, start=offset + 1):
            r.rank = i

        # Paging past the end returned `{"total": 13, "results": []}`, which left
        # a client unable to tell "no matches" from "you paged too far".
        # `total` stays the true match count; `has_more` and `returned` make the
        # page position explicit.
        has_more = offset + len(results) < total

        response = SearchResponse(
            query=req.q,
            results=results,
            total=total,
            returned=len(results),
            has_more=has_more,
            took_ms=int((time.monotonic() - start) * 1000),
            cache_hit=False,
            degraded=degraded,
            providers=sorted(by_provider.keys()),
            top_score=results[0].score if results else None,
            low_confidence=low_confidence,
            min_score=min_score if ranked else None,
        )

        # Cache even when a provider is degraded: the remaining providers' output
        # is still useful, and refusing to cache here made a single permanently
        # broken provider keep the cache cold for every query forever.
        if req.cache:
            # An empty or low-confidence answer is the one most likely to be
            # wrong a moment later — the index may still be draining, or
            # SearXNG may have recovered. Caching it for the full TTL means a
            # fixed miss stays fixed for minutes.
            weak = not results or low_confidence
            ttl = self.settings.search_negative_cache_ttl_seconds if weak else self.settings.search_cache_ttl_seconds
            await cache.set(cache_key_str, response.model_dump(), ttl_seconds=ttl)

        try:
            from app.workers.snippet import enqueue_snippets

            # Was a hardcoded top 5, so an all-sources query returned 40 results
            # of which 35 carried only a search-engine snippet. The rest stay
            # queue-backed so this stays off the request path.
            await enqueue_snippets([r.url for r in results[: self.settings.search_enrich_top_n]])
        except Exception:
            pass

        return response
