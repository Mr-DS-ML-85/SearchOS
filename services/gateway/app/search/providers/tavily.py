from __future__ import annotations

import time
from datetime import datetime

import httpx

from app.config import get_settings
from app.logging import get_logger
from app.metrics import get_metrics
from app.schemas import SearchResult
from app.search.base import ProviderQuery, ProviderResult, SearchProvider, domain_of

logger = get_logger("search.providers.tavily")

_TAVILY_ENDPOINT = "https://api.tavily.com/search"

# Tavily maps its own freshness vocabulary; SearchOS uses the SearXNG spelling.
_TIME_RANGE = {"day": "day", "week": "week", "month": "month", "year": "year"}


class TavilyProvider(SearchProvider):
    """Tavily search API, used as a fallback tier behind SearXNG.

    Returns an optional generated answer, which is kept on `raw` rather than
    folded into results: the reranker scores documents, and an answer is not a
    document.
    """

    name = "tavily"

    def __init__(self, api_key: str, timeout: float = 12.0, *, keyless: bool = True) -> None:
        self.settings = get_settings()
        self.api_key = api_key
        self.timeout = timeout
        self.keyless = keyless

    async def search(self, query: ProviderQuery) -> ProviderResult:

        start = time.monotonic()
        result = ProviderResult(provider=self.name, query=query.q)

        # Tavily serves /search without an account via a keyless header. This is
        # the only genuinely credential-free commercial fallback, so it stays in
        # the chain even with no key configured. Verified live: keyless returns
        # the same schema as a keyed call.
        if not self.api_key and not self.keyless:
            result.error = "tavily: no API key configured and keyless access disabled"
            return result

        body: dict = {
            "query": query.q,
            "max_results": max(query.num_results, 10),
            # basic, not advanced: the cross-encoder reranks downstream, so
            # Tavily's deeper (and ~4x slower) pass buys nothing here.
            "search_depth": self.settings.tavily_search_depth,
            "include_answer": False,
            "include_raw_content": False,
        }
        if query.time_range in _TIME_RANGE:
            body["time_range"] = _TIME_RANGE[query.time_range]
        if query.domains:
            body["include_domains"] = query.domains[:10]
        if query.exclude_domains:
            body["exclude_domains"] = query.exclude_domains[:10]

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            # A key takes precedence over keyless when both are sent, so send
            # only the Authorization header in that case.
            headers["Authorization"] = f"Bearer {self.api_key}"
        else:
            headers["X-Tavily-Access-Mode"] = "keyless"

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(_TAVILY_ENDPOINT, json=body, headers=headers)
                resp.raise_for_status()
                payload = resp.json()
        except httpx.HTTPStatusError as e:
            get_metrics().provider_errors.labels(provider=self.name, kind="http").inc()
            status = e.response.status_code
            if status == 429:
                # Keyless access is shared and rate-limited per IP. Saying "429"
                # sends the operator looking at their own configuration, when the
                # cause is upstream capacity.
                result.error = "tavily: HTTP 429 rate limited" + (
                    " (keyless tier — set TAVILY_API_KEY for a dedicated quota)" if not self.api_key else ""
                )
            else:
                result.error = f"tavily: HTTP {status}"
            result.latency_ms = (time.monotonic() - start) * 1000
            return result
        except Exception as e:
            get_metrics().provider_errors.labels(provider=self.name, kind="http").inc()
            result.error = f"tavily error: {e.__class__.__name__}: {e}"
            result.latency_ms = (time.monotonic() - start) * 1000
            return result

        items = payload.get("results") or []
        for rank, item in enumerate(items[: query.num_results], start=1):
            url = item.get("url") or ""
            if not url:
                continue
            snippet = (item.get("content") or "").strip()
            result.results.append(
                SearchResult(
                    title=item.get("title") or url,
                    url=url,
                    snippet=snippet[:1200],
                    source=self.name,
                    rank=rank,
                    domain=domain_of(url),
                    score=float(item.get("score") or 0.0),
                    published=self._published(item.get("published_date")),
                )
            )
        result.raw = {"answer": payload.get("answer")}
        result.latency_ms = (time.monotonic() - start) * 1000
        return result

    @staticmethod
    def _published(value: object) -> str | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).isoformat()
        except ValueError:
            return None
