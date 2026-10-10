from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from app.cache import Cache, cache_key, get_cache
from app.config import Settings, get_settings
from app.metrics import get_metrics
from app.schemas import SearchRequest, SearchResponse, SearchResult
from app.search.base import ProviderQuery, ProviderResult, SearchProvider
from app.search.fusion import reciprocal_rank_fusion
from app.search.operators import parse as parse_query
from app.search.rerank import Reranker, get_cross_encoder


def encode_cursor(offset: int, fingerprint: str) -> str:
    """Opaque page cursor, bound to the query it was produced for.

    Binding matters: reusing a cursor from one query against another would skip
    an arbitrary number of rows and silently return the wrong page. The
    fingerprint is compared on decode and a mismatch is rejected.
    """
    import base64
    import json

    payload = json.dumps({"o": offset, "f": fingerprint}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def decode_cursor(cursor: str) -> int | None:
    import base64
    import json

    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded))
        return int(data["o"])
    except Exception:
        return None


def _operators_summary(parsed) -> dict:
    return {
        "text": parsed.text,
        "include_domains": parsed.include_domains,
        "exclude_domains": parsed.exclude_domains,
        "filetypes": parsed.filetypes,
        "phrases": parsed.phrases,
        "negated": parsed.negated,
    }


def _filter_by_filetype(results: list, filetypes: list[str]) -> list:
    """Keep results whose URL ends in one of the requested extensions.

    Extensionless URLs are kept: an indexed document has no extension in its
    URL, so dropping those would empty the local index for any filetype query.
    A filetype query against the local index therefore behaves as a soft filter.
    """
    wanted = {f.lstrip(".").lower() for f in filetypes}
    kept = []
    for r in results:
        path = urlsplit(r.url or "").path
        ext = path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
        if ext and ext not in wanted:
            continue
        kept.append(r)
    return kept


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

        # Operators are stripped before anything reaches the index, so
        # `filetype:pdf` does not match documents literally containing "filetype".
        parsed = parse_query(req.q)
        query_text = parsed.text or req.q
        domains = list(parsed.include_domains) + list(req.domains or [])
        exclude_domains = list(parsed.exclude_domains) + list(req.exclude_domains or [])
        if parsed.negated:
            query_text = f"{query_text} {' '.join('-' + t for t in parsed.negated)}".strip()

        cursor_fingerprint = hashlib.sha256(
            json.dumps([query_text, req.sources, sorted(domains), sorted(exclude_domains)], default=str).encode()
        ).hexdigest()[:16]

        offset = min(req.offset, self.settings.search_max_offset)
        if req.cursor:
            decoded = decode_cursor(req.cursor)
            if decoded is None:
                from app.errors import ValidationError

                raise ValidationError("cursor is not valid; pass next_cursor from a previous response")
            offset = min(decoded, self.settings.search_max_offset)
        window = offset + req.num_results
        cache_key_str = cache_key(
            "search",
            query_text.lower().strip(),
            req.sources,
            window,
            req.language,
            req.country,
            req.time_range,
            req.freshness,
            tuple(sorted(domains)),
            tuple(sorted(exclude_domains)),
            tuple(parsed.phrases),
        )

        if req.cache:
            cached = await cache.get(cache_key_str)
            if cached is not None:
                metrics.cache_hits.inc()
                # Count it anyway: a cache hit is still a query an agent ran,
                # and skipping it undercounts popular queries exactly where
                # repeat traffic is highest.
                try:
                    await cache.record_query(req.q, results=len(cached.get("results") or []))
                except Exception:
                    pass
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
            q=query_text,
            language=req.language,
            country=req.country,
            time_range=req.time_range,
            domains=domains or None,
            exclude_domains=exclude_domains or None,
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
        ranked = await self.reranker.rerank_async(query_text, fused)

        # Drop rows the reranker scored as irrelevant. Only meaningful once a
        # cross-encoder produced the scores: the heuristic keeps every fused
        # score near 0.016, so cutting there would empty every result set.
        min_score = self.settings.rerank_min_score
        if self.reranker.cross_encoder is not None and self.reranker.cross_encoder.loaded:
            ranked = [r for r in ranked if r.score >= min_score]
            total = len(ranked)

        low_confidence = not ranked or ranked[0].score < self.settings.rerank_low_confidence_score

        if parsed.filetypes:
            ranked = _filter_by_filetype(ranked, parsed.filetypes)
            total = len(ranked)

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
            next_cursor=(encode_cursor(offset + len(results), cursor_fingerprint) if has_more and results else None),
            operators_applied=_operators_summary(parsed) if parsed.has_operators else None,
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
            await cache.record_query(req.q, results=len(results))
        except Exception:
            pass

        try:
            from app.workers.snippet import enqueue_snippets

            # Was a hardcoded top 5, so an all-sources query returned 40 results
            # of which 35 carried only a search-engine snippet. The rest stay
            # queue-backed so this stays off the request path.
            await enqueue_snippets([r.url for r in results[: self.settings.search_enrich_top_n]])
        except Exception:
            pass

        return response
