from __future__ import annotations

import re
import time
from datetime import UTC, datetime

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import get_settings
from app.logging import get_logger
from app.metrics import get_metrics
from app.schemas import SearchResult
from app.search.base import ProviderQuery, ProviderResult, SearchProvider, domain_allowed

SNIPPET_META_FLAG = "searxng_snippet"

logger = get_logger("search.providers.searxng")


class SearXNGProvider(SearchProvider):
    # "searxng", not "web": the cascade owns the name "web", and a tier needs a
    # distinct identity so sources=searxng can address it on its own.
    name = "searxng"

    def __init__(self, session_factory: async_sessionmaker, url: str, timeout: float = 12.0) -> None:
        self.settings = get_settings()
        self.session_factory = session_factory
        self.url = url.rstrip("/")
        self.timeout = timeout

    async def search(self, query: ProviderQuery) -> ProviderResult:
        start = time.monotonic()
        result = ProviderResult(provider=self.name, query=query.q)
        if query.skip_when_local_hit:
            try:
                local = await self._local_hits(query.q, limit=max(query.num_results, 10))
                if len(local) >= self._skip_threshold() and self._is_fresh(local):
                    result.raw = {"served_from_local": True, "local_hits": len(local)}
                    result.latency_ms = (time.monotonic() - start) * 1000
                    return result
            except Exception:
                pass
        # The orchestrator strips `site:` out of the query text into
        # query.domains. This provider used to send only the stripped text and
        # never filter, so `site:arxiv.org ...` searched the whole web: pinned
        # (sources=searxng) it returned unrelated domains, and through the
        # cascade the domain filter then kept only whichever arxiv result
        # happened to appear (often one). Send the restriction upstream, where
        # the engines can search inside the domain, and re-apply it below.
        upstream_q = query.q
        if query.domains:
            upstream_q += " " + " OR ".join(f"site:{d}" for d in query.domains)
        for d in query.exclude_domains or []:
            upstream_q += f" -site:{d}"
        params = {
            "q": upstream_q,
            "format": "json",
            "safesearch": 0,
            "categories": "general",
        }
        # SearXNG takes a locale ("fr-CA"), which is the only place `country`
        # can mean anything: indexed documents carry no country signal. It was
        # previously accepted by the API and read by no provider at all.
        if query.language:
            lang = query.language.strip()
            if query.country and "-" not in lang:
                lang = f"{lang}-{query.country.strip().upper()}"
            params["language"] = lang
        if query.time_range and query.time_range != "all":
            params["time_range"] = query.time_range
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                resp = await client.get(f"{self.url}/search", params=params)
                resp.raise_for_status()
                payload = resp.json()
        except Exception as e:
            get_metrics().provider_errors.labels(provider=self.name, kind="http").inc()
            result.error = f"searxng error: {e.__class__.__name__}: {e}"
            result.latency_ms = (time.monotonic() - start) * 1000
            return result

        # Not every engine honours `site:`; filter before the page slice so a
        # dropped row does not cost a slot.
        raw_results = [
            item
            for item in payload.get("results") or []
            if item.get("url") and domain_allowed(item["url"], query.domains, query.exclude_domains)
        ]
        for rank, item in enumerate(raw_results[: query.num_results], start=1):
            url = item.get("url") or ""
            if not url:
                continue
            title = item.get("title") or url
            snippet = (item.get("content") or "").strip()[:300]
            published = None
            pub = item.get("publishedDate") or item.get("published_date")
            if pub:
                try:
                    published = datetime.fromisoformat(str(pub).replace("Z", "+00:00")).isoformat()
                except ValueError:
                    published = None
            result.results.append(
                SearchResult(
                    title=title,
                    url=url,
                    snippet=snippet,
                    source=self.name,
                    score=float(item.get("score") or 1.0) / max(rank, 1),
                    rank=rank,
                    published=published,
                    domain=self._domain(url),
                    raw={"engine": item.get("engine"), "score": item.get("score")},
                )
            )

        # Persistence happens once in the orchestrator for every web provider
        # (app/search/web_persist.py), not here.

        result.latency_ms = (time.monotonic() - start) * 1000
        get_metrics().search_duration.labels(provider=self.name).observe(result.latency_ms / 1000)
        return result

    async def _local_hits(self, q: str, limit: int) -> list:
        from app.search.local_index import LocalIndexProvider

        return await LocalIndexProvider(self.session_factory)._fts_search(
            ProviderQuery(q=q, num_results=limit), or_fallback=False
        )

    def _skip_threshold(self) -> int:
        return self.settings.searxng_local_min_hits

    def _is_fresh(self, local: list) -> bool:
        """True when a *majority* of local hits are within the TTL.

        The previous "any single fresh row" check let one fresh document
        suppress SearXNG even when every top result was stale.
        """
        if not local:
            return False
        ttl = self.settings.searxng_local_ttl_seconds
        now = time.time()
        fresh = 0
        for row in local:
            fetched_at = row.get("fetched_at")
            if isinstance(fetched_at, datetime):
                if now - fetched_at.timestamp() < ttl:
                    fresh += 1
        return fresh * 2 > len(local)

    @staticmethod
    def _parse_dt(value: object) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(UTC).replace(tzinfo=None)
            return parsed
        except ValueError:
            return None

    @staticmethod
    def _domain(url: str) -> str:
        m = re.match(r"https?://([^/]+)", url)
        return m.group(1) if m else ""
