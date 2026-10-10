from __future__ import annotations

import asyncio
import time

from app.config import get_settings
from app.logging import get_logger
from app.metrics import get_metrics
from app.search.base import ProviderQuery, ProviderResult, SearchProvider

logger = get_logger("search.providers.cascade")


def _bare(host: str) -> str:
    """Lowercase a hostname and drop a leading www. so filters match subdomains."""
    h = (host or "").strip().lower().rstrip(".")
    return h[4:] if h.startswith("www.") else h


class CascadeProvider(SearchProvider):
    """Try web providers in order; the first one that answers wins.

    SearXNG is the primary. Tavily, Exa and Jina follow in the configured order,
    and each is skipped unless it has the credentials it needs. When the whole
    chain is exhausted the cascade opens a cooldown: for
    ``search_cascade_cooldown_seconds`` it stops attempting live providers and
    returns immediately, so a request does not pay the timeout of every dead
    tier on the way to the same failure.

    The provider keeps the ``web`` name so ``sources=web`` routes through the
    whole chain and the orchestrator's degraded reporting still works.
    """

    name = "web"

    def __init__(self, tiers: list[SearchProvider], *, cooldown_seconds: float = 30.0) -> None:
        self.tiers = [t for t in tiers if t is not None]
        self.cooldown_seconds = max(cooldown_seconds, 0.0)
        self._cooldown_until = 0.0
        self._lock = asyncio.Lock()

    @property
    def in_cooldown(self) -> bool:
        return time.monotonic() < self._cooldown_until

    def _open_cooldown(self, retry_after: float | None = None) -> None:
        """Pause the chain.

        ``retry_after`` is what an upstream asked for via Retry-After on a 429.
        A rate limit is not an outage: the service is healthy and will answer
        again shortly, so pausing for its own stated window beats the generic
        cooldown, which would mute a working tier for longer than necessary —
        and shorter than necessary would hammer a tier that just said "wait".
        """
        seconds = self.cooldown_seconds
        reason = "all web providers failed"
        if retry_after:
            seconds = max(seconds, float(retry_after))
            reason = "web providers rate limited"
        self._cooldown_until = time.monotonic() + seconds
        logger.warning("%s; cooling down for %ds before retrying the chain", reason, seconds)

    @staticmethod
    def _enforce_domains(query: ProviderQuery, results: list) -> list:
        """Apply the caller's domain constraints to whatever a tier returned.

        Not every upstream honours them: Jina's search endpoint has no domain
        parameter at all, and Exa's MCP server returned undsci.berkeley.edu
        alongside en.wikipedia.org despite includeDomains. Trusting each tier
        meant `domains=wikipedia.org` silently returned dictionaries and other
        unrelated sites, which the e2e suite caught.
        """
        if not query.domains and not query.exclude_domains:
            return results
        included = [_bare(d) for d in (query.domains or []) if d]
        excluded = [_bare(d) for d in (query.exclude_domains or []) if d]

        def host_of(url: str) -> str:
            return _bare(url.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0])

        kept = []
        for r in results:
            host = host_of(getattr(r, "url", "") or "")
            if excluded and any(host == e or host.endswith("." + e) for e in excluded):
                continue
            if included and not any(host == i or host.endswith("." + i) for i in included):
                continue
            kept.append(r)
        return kept

    async def search(self, query: ProviderQuery) -> ProviderResult:
        start = time.monotonic()

        # The lock guards only the cooldown read. It used to wrap the whole tier
        # loop, so every live-web search in the process ran one at a time: a
        # second query waited for the first one's full chain (12s once Google
        # is a tier) before it could even start.
        async with self._lock:
            in_cooldown = self.in_cooldown
        if in_cooldown:
            result = ProviderResult(provider=self.name, query=query.q)
            result.error = "web cascade in cooldown after upstream failure"
            result.raw = {
                "cooldown": True,
                "retry_after_seconds": round(self._cooldown_until - time.monotonic(), 1),
                "tiers": [t.name for t in self.tiers],
            }
            result.latency_ms = (time.monotonic() - start) * 1000
            get_metrics().provider_errors.labels(provider=self.name, kind="cooldown").inc()
            return result

        combined = ProviderResult(provider=self.name, query=query.q)
        attempted: list[str] = []
        failures: list[str] = []
        # Retry-After asked for by any tier in this attempt, so the cooldown
        # can honour the upstream's own window rather than a fixed guess.
        retry_waits: list[float] = []

        for tier in self.tiers:
            attempted.append(tier.name)
            try:
                outcome = await tier.search(query)
            except Exception as exc:  # a tier must never break the chain
                failures.append(f"{tier.name}: {exc.__class__.__name__}")
                get_metrics().provider_errors.labels(provider=tier.name, kind="exception").inc()
                continue

            if outcome.error:
                failures.append(outcome.error)
                get_metrics().provider_errors.labels(provider=tier.name, kind="http").inc()
                continue

            results = self._enforce_domains(query, outcome.results)
            if outcome.retry_after:
                retry_waits.append(outcome.retry_after)
            if not results:
                # Either nothing matched, or the domain filter removed
                # everything. Treat as a miss and keep descending.
                failures.append(f"{tier.name}: no results after domain filter")
                continue

            combined.results = results
            # Clear any cooldown from an earlier outage. Without this the
            # flag survives until its original expiry even though the chain
            # is healthy again, so a recovered service stays muted.
            self._cooldown_until = 0.0
            combined.raw = {
                "served_by": tier.name,
                "attempted": attempted,
                "fallback_used": tier.name != self.tiers[0].name,
                "degraded_tiers": failures,
            }
            if outcome.raw and isinstance(outcome.raw, dict):
                for key, value in outcome.raw.items():
                    combined.raw.setdefault(key, value)
            combined.latency_ms = (time.monotonic() - start) * 1000
            logger.info(
                "web served by %s after %d tier(s): %s",
                tier.name,
                len(attempted),
                query.q[:60],
            )
            return combined

        combined.error = "web cascade exhausted: " + " | ".join(failures[:4])
        combined.raw = {"attempted": attempted, "degraded_tiers": failures}
        combined.latency_ms = (time.monotonic() - start) * 1000
        # Respect the longest Retry-After any tier asked for.
        self._open_cooldown(max(retry_waits) if retry_waits else None)
        return combined


def build_cascade(settings=None) -> CascadeProvider | None:
    """Assemble the chain from configured credentials, or None if only SearXNG.

    Returns None when there is nothing to cascade to, so a stock install keeps
    the plain SearXNG provider and no behaviour changes.
    """
    cfg = settings or get_settings()
    from app.db import get_session_factory
    from app.search.providers.exa_mcp import ExaMCPProvider
    from app.search.providers.jina import JinaProvider
    from app.search.providers.searxng import SearXNGProvider
    from app.search.providers.tavily import TavilyProvider

    tiers: list[SearchProvider] = []
    if cfg.searxng_url:
        tiers.append(SearXNGProvider(get_session_factory(), cfg.searxng_url, cfg.searxng_timeout_seconds))
    # Tavily is included even with no key: its keyless mode needs no account.
    if cfg.tavily_api_key or cfg.tavily_keyless:
        tiers.append(TavilyProvider(cfg.tavily_api_key, cfg.tavily_timeout_seconds, keyless=cfg.tavily_keyless))
    # Exa's REST API returns 402 x402 without payment. The hosted MCP server at
    # mcp.exa.ai answers anonymously under a rate limit, so MCP is the default
    # route and REST is only used when a funded key is configured.
    if cfg.exa_api_key and cfg.exa_use_mcp:
        tiers.append(ExaMCPProvider(cfg.exa_timeout_seconds, advanced=cfg.exa_advanced, api_key=cfg.exa_api_key))
    elif cfg.exa_api_key:
        from app.search.providers.exa import ExaProvider

        tiers.append(ExaProvider(cfg.exa_api_key, cfg.exa_timeout_seconds))
    elif cfg.exa_mcp_enabled:
        tiers.append(ExaMCPProvider(cfg.exa_timeout_seconds, advanced=cfg.exa_advanced))
    # s.jina.ai returns 401 AuthenticationRequired without a key; only
    # r.jina.ai (the reader) is open. Do not add an unkeyed search tier.
    if cfg.jina_api_key:
        tiers.append(JinaProvider(cfg.jina_api_key, cfg.jina_timeout_seconds))
    if cfg.proxify_url:
        from app.search.providers.google import GoogleProvider, build_client

        google = GoogleProvider(build_client())
        # ~12s per query (VM browser + link resolution), so by default it is
        # the fallback after SearXNG rather than the first thing every query pays.
        if cfg.google_cascade_position == "first":
            tiers.insert(0, google)
        else:
            tiers.insert(1 if cfg.searxng_url else 0, google)

    if len(tiers) <= 1:
        return None
    return CascadeProvider(tiers, cooldown_seconds=cfg.search_cascade_cooldown_seconds)
