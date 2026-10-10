from __future__ import annotations

import time

from app.config import get_settings
from app.logging import get_logger
from app.metrics import get_metrics
from app.schemas import SearchResult
from app.search.base import (
    ProviderQuery,
    ProviderResult,
    SearchProvider,
    domain_allowed,
    domain_of,
)

logger = get_logger("search.providers.jina")

_JINA_ENDPOINT = "https://s.jina.ai"


class JinaProvider(SearchProvider):
    """Jina Reader search, the last tier in the cascade.

    Works without an API key at a lower rate limit, which makes it a useful
    last resort for a fresh install that has no other key configured.
    """

    name = "jina"

    def __init__(self, api_key: str = "", timeout: float = 20.0) -> None:
        self.settings = get_settings()
        self.api_key = api_key
        self.timeout = timeout

    async def search(self, query: ProviderQuery) -> ProviderResult:
        import httpx

        start = time.monotonic()
        result = ProviderResult(provider=self.name, query=query.q)

        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(f"{_JINA_ENDPOINT}/{query.q}", headers=headers)
                resp.raise_for_status()
                payload = resp.json()
        except Exception as e:
            get_metrics().provider_errors.labels(provider=self.name, kind="http").inc()
            result.error = f"jina error: {e.__class__.__name__}: {e}"
            result.latency_ms = (time.monotonic() - start) * 1000
            return result

        for rank, item in enumerate((payload.get("data") or [])[: query.num_results], start=1):
            url = item.get("url") or ""
            if not url:
                continue
            if not domain_allowed(url, query.domains, query.exclude_domains):
                continue
            snippet = (item.get("description") or item.get("content") or "").strip()
            result.results.append(
                SearchResult(
                    title=item.get("title") or url,
                    url=url,
                    snippet=snippet[:1200],
                    source=self.name,
                    rank=rank,
                    domain=domain_of(url),
                    # Jina returns no score. Ordering is its relevance rank, so
                    # encode it as a bounded value rather than inventing one.
                    score=round(1.0 - (rank - 1) * 0.01, 4),
                )
            )
        result.latency_ms = (time.monotonic() - start) * 1000
        return result

        def bare(value: str) -> str:
            v = (value or "").strip().lower().rstrip(".")
            return v[4:] if v.startswith("www.") else v

        host = bare(url.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0])
        excluded = [bare(d) for d in (query.exclude_domains or []) if d]
        if excluded and any(host == d or host.endswith("." + d) for d in excluded):
            return False
        included = [bare(d) for d in (query.domains or []) if d]
        if included and not any(host == d or host.endswith("." + d) for d in included):
            return False
        return True
