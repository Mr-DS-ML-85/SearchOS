from __future__ import annotations

import time
from datetime import UTC, datetime

from app.config import get_settings
from app.logging import get_logger
from app.metrics import get_metrics
from app.schemas import SearchResult
from app.search.base import ProviderQuery, ProviderResult, SearchProvider, domain_of

logger = get_logger("search.providers.exa")

_EXA_ENDPOINT = "https://api.exa.ai/search"


class ExaProvider(SearchProvider):
    """Exa neural/similarity search, used as a fallback tier.

    Exa is the one tier here that is genuinely semantic rather than a metasearch
    scrape, so it is worth keeping even when SearXNG is healthy for queries that
    SearXNG handles badly.
    """

    name = "exa"

    def __init__(self, api_key: str, timeout: float = 12.0) -> None:
        self.settings = get_settings()
        self.api_key = api_key
        self.timeout = timeout

    async def search(self, query: ProviderQuery) -> ProviderResult:
        import httpx

        start = time.monotonic()
        result = ProviderResult(provider=self.name, query=query.q)

        if not self.api_key:
            result.error = "exa: no API key configured"
            return result

        body: dict = {
            "query": query.q,
            "numResults": max(query.num_results, 10),
            "contents": {"text": {"maxCharacters": 1200}},
        }
        if query.time_range and query.time_range != "all":
            body["startPublishedDate"] = self._start_date(query.time_range)
        if query.domains:
            body["includeDomains"] = query.domains[:10]
        if query.exclude_domains:
            body["excludeDomains"] = query.exclude_domains[:10]

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    _EXA_ENDPOINT,
                    json=body,
                    headers={"x-api-key": self.api_key, "Content-Type": "application/json"},
                )
                resp.raise_for_status()
                payload = resp.json()
        except Exception as e:
            get_metrics().provider_errors.labels(provider=self.name, kind="http").inc()
            result.error = f"exa error: {e.__class__.__name__}: {e}"
            result.latency_ms = (time.monotonic() - start) * 1000
            return result

        for rank, item in enumerate((payload.get("results") or [])[: query.num_results], start=1):
            url = item.get("url") or ""
            if not url:
                continue
            snippet = (item.get("text") or item.get("snippet") or "").strip()
            result.results.append(
                SearchResult(
                    title=item.get("title") or url,
                    url=url,
                    snippet=snippet[:1200],
                    source=self.name,
                    rank=rank,
                    domain=domain_of(url),
                    score=float(item.get("score") or 0.0),
                    published=self._published(item.get("publishedDate")),
                )
            )
        result.latency_ms = (time.monotonic() - start) * 1000
        return result

    @staticmethod
    def _start_date(time_range: str) -> str | None:
        from datetime import timedelta

        days = {"day": 1, "week": 7, "month": 30, "year": 365}.get(time_range)
        if not days:
            return None
        return (datetime.now(UTC) - timedelta(days=days)).isoformat()

    @staticmethod
    def _published(value: object) -> str | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).isoformat()
