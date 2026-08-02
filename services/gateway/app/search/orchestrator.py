from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from app.cache import Cache, cache_key, get_cache
from app.config import get_settings
from app.metrics import get_metrics
from app.schemas import SearchRequest, SearchResponse, SearchResult
from app.search.base import ProviderQuery, ProviderResult, SearchProvider
from app.search.fusion import reciprocal_rank_fusion
from app.search.rerank import Reranker


@dataclass
class SearchOrchestrator:
    providers: dict[str, SearchProvider] = field(default_factory=dict)
    cache: Cache | None = None
    reranker: Reranker | None = None
    settings = None

    def __post_init__(self) -> None:
        if self.settings is None:
            self.settings = get_settings()
        if self.cache is None:
            self.cache = get_cache()
        if self.reranker is None:
            self.reranker = Reranker(enabled=self.settings.rerank_enabled, top_k=self.settings.rerank_top_k)

    def select_providers(self, sources: str) -> list[SearchProvider]:
        if sources == "all":
            return list(self.providers.values())
        provider = self.providers.get(sources)
        return [provider] if provider else []

    async def search(self, req: SearchRequest) -> SearchResponse:
        start = time.monotonic()
        metrics = get_metrics()
        cache = self.cache
        cache_key_str = cache_key(
            "search",
            req.q.lower().strip(),
            req.sources,
            req.num_results,
            req.offset,
            req.language,
            req.country,
            req.time_range,
            "-".join(req.domains or []),
            "-".join(req.exclude_domains or []),
        )

        if req.cache:
            cached = await cache.get(cache_key_str)
            if cached is not None:
                metrics.cache_hits.inc()
                cached["cache_hit"] = True
                return SearchResponse(**cached)

        metrics.cache_misses.inc()
        providers = self.select_providers(req.sources)
        query = ProviderQuery(
            q=req.q,
            language=req.language,
            country=req.country,
            time_range=req.time_range,
            domains=req.domains,
            exclude_domains=req.exclude_domains,
            num_results=max(req.num_results * 2, 20),
            offset=req.offset,
            skip_when_local_hit=(req.sources == "all" and req.time_range == "all"),
        )

        tasks = [provider.search(query) for provider in providers]
        provider_results: list[ProviderResult] = await asyncio.gather(*tasks, return_exceptions=True)

        by_provider: dict[str, list[SearchResult]] = {}
        degraded: list[str] = []
        for provider, outcome in zip(providers, provider_results):
            if isinstance(outcome, BaseException):
                degraded.append(provider.name)
                metrics.provider_errors.labels(provider=provider.name, kind="exception").inc()
                continue
            if outcome.error:
                degraded.append(provider.name)
            else:
                metrics.upstream_latency.labels(provider=provider.name).observe(outcome.latency_ms / 1000)
            by_provider[provider.name] = outcome.results

        if not by_provider or not any(by_provider.values()):
            from app.errors import SearchNoResultsError

            raise SearchNoResultsError("No search providers returned results.")

        fused = reciprocal_rank_fusion(by_provider)
        # Never let the reranker cap results below what the caller asked for.
        self.reranker.top_k = max(self.settings.rerank_top_k, req.offset + req.num_results)
        ranked = self.reranker.rerank(req.q, fused)
        total = len(ranked)

        results = ranked[req.offset : req.offset + req.num_results]

        for i, r in enumerate(results, start=req.offset + 1):
            r.rank = i

        response = SearchResponse(
            query=req.q,
            results=results,
            total=total,
            took_ms=int((time.monotonic() - start) * 1000),
            cache_hit=False,
            degraded=degraded,
            providers=sorted(by_provider.keys()),
        )

        if req.cache and not degraded:
            await cache.set(cache_key_str, response.model_dump(), ttl_seconds=self.settings.search_cache_ttl_seconds)

        try:
            from app.workers.snippet import enqueue_snippets

            await enqueue_snippets([r.url for r in results[:5]])
        except Exception:
            pass

        return response
