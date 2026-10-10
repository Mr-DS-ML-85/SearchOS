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


def parse_limit_response(response) -> tuple[str, str, list, float | None]:
    """Read a Tavily 429 in either documented shape.

    Keyless 429s use ``{"error": {code, message, window, retry_after_seconds,
    next_actions}}`` and carry the authoritative wait in the *body*. Keyed 429s
    use ``{"detail": {...}}`` plus an HTTP ``Retry-After`` header. Reading only
    the header silently misses the keyless value, which is the mode most
    deployments actually run in.
    """
    code = window = ""
    next_actions: list = []
    retry_after: float | None = None
    try:
        body = response.json()
    except Exception:
        body = {}
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        code = err.get("code") or ""
        window = err.get("window") or ""
        next_actions = err.get("next_actions") or []
        try:
            retry_after = float(err.get("retry_after_seconds"))
        except (TypeError, ValueError):
            retry_after = None
    if retry_after is None:
        header = response.headers.get("retry-after")
        if header:
            try:
                retry_after = float(header)
            except ValueError:
                retry_after = None
    return code, window, next_actions, retry_after


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
                # Keyless access is a documented, supported mode — free, with an
                # identical response schema, covering /search and /extract. It is
                # fair-use capped per IP, so a 429 here is capacity, not a broken
                # integration, and saying so is what stops an operator auditing
                # their own configuration for a fault that isn't there.
                #
                # The two envelopes differ and both are handled:
                #   keyless: {"error": {code, message, window,
                #                         retry_after_seconds, next_actions}}
                #   keyed:   {"detail": {"error": "..."}} plus a Retry-After header
                code, window, next_actions, retry_after = parse_limit_response(e.response)

                parts = ["HTTP 429 rate limited"]
                if code:
                    parts.append(f"({code})")
                if window:
                    parts.append(f"window={window}")
                if not self.api_key:
                    parts.append("keyless tier — set TAVILY_API_KEY for a dedicated quota (free, 1000 credits/month)")
                if next_actions:
                    rendered = []
                    for action in next_actions[:2]:
                        if isinstance(action, dict):
                            # The documented shape is {type, resource,
                            # instruction}; the instruction is the part an
                            # operator can act on.
                            rendered.append(str(action.get("instruction") or action.get("type") or ""))
                        else:
                            rendered.append(str(action))
                    parts.append("; " + " ".join(x for x in rendered if x))
                if retry_after is not None:
                    parts.append(f"; retry after {int(retry_after)}s")
                result.error = " ".join(parts)
                result.retry_after = retry_after
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
